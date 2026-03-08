#!/bin/bash
set -e

cd "$(dirname "$0")"

HOST_IP=$(hostname -I | awk '{print $1}')
echo "Detected host IP: $HOST_IP"

sed "s/__HOST_IP__/$HOST_IP/g" .env.template > .env
echo "Generated .env with IP $HOST_IP"

docker compose up -d "$@"

echo "Waiting for postgres..."
docker compose exec -T postgres pg_isready -U postgres -d h -q --timeout=30

# ---------------------------------------------------------------------------
# First-run detection: initialize DB if schema doesn't exist yet
# ---------------------------------------------------------------------------
DB_CHECK=$(docker compose exec -T postgres psql -U postgres -d h -tAc \
  "SELECT to_regclass('public.\"user\"')")
if [ -z "$(echo "$DB_CHECK" | tr -d '[:space:]')" ]; then
    echo "First run detected — running database initialization..."
    ./init.sh "$HOST_IP"
    # Regenerate .env since init.sh updated .env.template
    sed "s/__HOST_IP__/$HOST_IP/g" .env.template > .env
    docker compose up -d
    echo "Waiting for postgres..."
    docker compose exec -T postgres pg_isready -U postgres -d h -q --timeout=30
fi

# ---------------------------------------------------------------------------
# Generate a developer API token for the annotation agent
# ---------------------------------------------------------------------------
AGENT_USER=$(grep '^ANNOTATION_AGENT_USER=' .env | cut -d= -f2)

USER_ID=$(docker compose exec -T postgres psql -U postgres -d h -tAc \
  "SELECT id FROM \"user\" WHERE username = '${AGENT_USER}' AND authority = '${HOST_IP}'")

if [ -z "$USER_ID" ]; then
    echo "WARNING: user '${AGENT_USER}' not found — skipping API token generation."
    echo "  Create the user first, then re-run ./start.sh"
else
    # Remove previous agent developer tokens (authclient_id IS NULL = developer token)
    docker compose exec -T postgres psql -U postgres -d h -c \
      "DELETE FROM token WHERE user_id = ${USER_ID} AND authclient_id IS NULL;" > /dev/null

    TOKEN=$(openssl rand -base64 32 | tr -d '/+=')
    docker compose exec -T postgres psql -U postgres -d h -c \
      "INSERT INTO token (value, user_id) VALUES ('${TOKEN}', ${USER_ID});" > /dev/null

    sed -i "s|__H_API_TOKEN__|${TOKEN}|" .env
    echo "Generated API token for ${AGENT_USER}"
fi

# ---------------------------------------------------------------------------
# Annotation agent: install dependencies and launch
# ---------------------------------------------------------------------------
if [ -z "$VIRTUAL_ENV" ]; then
    echo ""
    read -rp "No Python virtual environment active. Create one in ./venv? [Y/n] " CREATE_VENV
    if [ "$CREATE_VENV" != "n" ] && [ "$CREATE_VENV" != "N" ]; then
        python3 -m venv venv
        source venv/bin/activate
        echo "Created and activated virtualenv in ./venv"
    fi
fi

pip install --extra-index-url https://test.pypi.org/simple/ -r requirements-agent.txt

echo ""
echo "Annotate any page by visiting:"
echo "  http://${HOST_IP}:9083/<URL_TO_ANNOTATE>"
echo "Or open http://${HOST_IP}:9083 and paste a URL."
echo ""
echo "Starting annotation agent..."
python annotation_agent.py
