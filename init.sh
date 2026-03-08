#!/bin/bash
set -e

HOST_IP=${1:?Usage: init.sh <HOST_IP>}
cd "$(dirname "$0")"

# Guard: only run if DB is not yet initialized
DB_CHECK=$(docker compose exec -T postgres psql -U postgres -d h -tAc \
  "SELECT to_regclass('public.\"user\"')")
if [ -n "$(echo "$DB_CHECK" | tr -d '[:space:]')" ]; then
    echo "Database already initialized. Skipping."
    exit 0
fi

# 1. Initialize schema
echo "Initializing Hypothesis database..."
docker compose exec -T postgres psql -U postgres -d h -c \
  'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'
docker compose exec -T h sh -c "cd /var/lib/hypothesis && python -c \"
from h.db import Base
from sqlalchemy import create_engine
import os, h.models
engine = create_engine(os.environ['DATABASE_URL'])
Base.metadata.create_all(engine)
\""
docker compose exec -T h sh -c \
  "cd /var/lib/hypothesis && alembic -c conf/alembic.ini stamp head"

# 2. Create seed data (default organization + __world__ group)
docker compose exec -T h sh -c "cd /var/lib/hypothesis && python -c \"
from sqlalchemy import create_engine
from h.db import Session, _maybe_create_default_organization, _maybe_create_world_group
import os
engine = create_engine(os.environ['DATABASE_URL'])
default_org = _maybe_create_default_organization(engine, os.environ['AUTHORITY'])
_maybe_create_world_group(engine, os.environ['AUTHORITY'], default_org)
\""

# 3. Prompt for user credentials
if [ ! -t 0 ]; then
    echo "ERROR: First-time setup requires interactive input. Run from a terminal."
    exit 1
fi

echo ""
echo "Create your Hypothesis user:"
read -rp "  Username: " H_USERNAME
read -rp "  Email: " H_EMAIL
read -rsp "  Password: " H_PASSWORD; echo
read -rsp "  Confirm password: " H_PASSWORD2; echo

if [ "$H_PASSWORD" != "$H_PASSWORD2" ]; then
    echo "ERROR: Passwords do not match."
    exit 1
fi

# 4. Create user with correct authority
docker compose run --rm h hypothesis user add \
  --username "$H_USERNAME" --email "$H_EMAIL" --password "$H_PASSWORD" \
  --authority "$HOST_IP"

# 5. Activate user
docker compose exec -T postgres psql -U postgres -d h -c "
  UPDATE \"user\" SET activation_date=NOW()
    WHERE username='${H_USERNAME}' AND authority='${HOST_IP}';
"

# 6. Create OAuth client and capture UUID
OAUTH_ID=$(docker compose exec -T postgres psql -U postgres -d h -qtAc "
  INSERT INTO authclient (name, authority, grant_type, response_type, redirect_uri, trusted)
  VALUES ('Hypothesis Client', '${HOST_IP}', 'authorization_code', 'code',
          'http://${HOST_IP}:5000/app/oauth/authorize', true)
  RETURNING id;
")
OAUTH_ID=$(echo "$OAUTH_ID" | tr -d '[:space:]')

# 7. Update .env.template with OAuth ID and agent username
sed -i "s|^CLIENT_OAUTH_ID=.*|CLIENT_OAUTH_ID=${OAUTH_ID}|" .env.template
sed -i "s|^ANNOTATION_AGENT_USER=.*|ANNOTATION_AGENT_USER=${H_USERNAME}|" .env.template

echo ""
echo "Database initialized successfully."
echo "  User: ${H_USERNAME}"
echo "  OAuth Client ID: ${OAUTH_ID}"
