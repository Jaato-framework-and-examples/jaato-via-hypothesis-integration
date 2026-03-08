# Jaato + Via Integration

Self-hosted [Hypothesis](https://hypothes.is/) annotation platform with an AI annotation agent powered by [Jaato](https://github.com/Jaato-framework-and-examples/jaato).

Annotate any web page, and the agent replies to your annotations using an LLM — with access to the source repository behind the page.

## Architecture

Seven Docker containers, orchestrated via Compose:

| Service         | Port | Description |
|-----------------|------|-------------|
| **postgres**    | 5432 | Shared database (separate `h` and `via` databases) |
| **elasticsearch** | 9200 | Full-text search for annotations |
| **rabbit**      | 5672 | Celery broker for async tasks |
| **h**           | 5000 | [Hypothesis](https://github.com/hypothesis/h) backend (API, WebSocket, worker) |
| **client**      | 3001 | [Hypothesis client](https://github.com/hypothesis/client) (boot.js + static assets) |
| **via**         | 9083 | [Via](https://github.com/hypothesis/via) proxy/annotation UI |
| **viahtml**     | 9085 | [ViaHTML](https://github.com/hypothesis/viahtml) web archive proxy (pywb) |

The **annotation agent** runs on the host (outside Docker) and bridges Hypothesis annotations with a Jaato server.

```
Browser :9083 → Via → ViaHTML :9085 (proxied page + injected client)
                                ↓
                          h :5000 (stores annotations)
                                ↓
                      Annotation Agent (polls h API)
                                ↓
                        Jaato server (LLM processing)
```

All upstream source code is cloned from GitHub during `docker build` — nothing is embedded in this repo.

## Prerequisites

- Docker and Docker Compose
- LAN-accessible host (services bind to the host's LAN IP, not localhost)

## Quick Start

```bash
git clone https://github.com/Jaato-framework-and-examples/jaato-via-integration.git
cd jaato-via-integration
./start.sh
```

`start.sh` detects your LAN IP, generates `.env` from the template, and starts all containers.

## First-Time Setup

After the first `./start.sh`, initialize the Hypothesis database:

```bash
# Create schema and search index
docker compose run --rm h hypothesis init-db
docker compose run --rm h hypothesis search reindex

# Create your user (replace YOUR_USERNAME)
docker compose run --rm h hypothesis user add --username YOUR_USERNAME --email you@example.com --password YOUR_PASSWORD
```

The `hypothesis user add` command sets `authority=localhost`, which must be corrected. Get your host IP from `.env` (`grep AUTHORITY .env`) and run:

```bash
docker compose exec postgres psql -U postgres -d h -c "
  UPDATE \"user\" SET authority='YOUR_HOST_IP', activation_date=NOW() WHERE username='YOUR_USERNAME';
  UPDATE \"group\" SET authority='YOUR_HOST_IP' WHERE pubid='__world__';
"
```

Create the OAuth client for the annotation sidebar:

```bash
docker compose exec postgres psql -U postgres -d h -c "
  INSERT INTO authclient (name, authority, grant_type, response_type, redirect_uri, trusted)
  VALUES ('Hypothesis Client', 'YOUR_HOST_IP', 'authorization_code', 'code',
          'http://YOUR_HOST_IP:5000/app/oauth/authorize', true)
  RETURNING id;
"
```

Copy the returned UUID into `CLIENT_OAUTH_ID` in `.env.template`, then re-run `./start.sh`.

## Annotation Agent

The agent polls the Hypothesis API for new annotations and replies using a Jaato-connected LLM. It discovers the source repository behind annotated pages via `<link rel="vcs-git">` tags.

### Setup

```bash
pip install --extra-index-url https://test.pypi.org/simple/ -r requirements-agent.txt
```

Edit `.env.template` to set `ANNOTATION_AGENT_USER` to your Hypothesis username, then re-run `./start.sh` to generate an API token.

### Running

Ensure a Jaato server is listening on `/tmp/jaato.sock`, then:

```bash
python annotation_agent.py
```

Use `--verbose` for debug output, or configure `JAATO_TRACE_LOG` in `.env.template` for file-based logging.

## Configuration

All configuration lives in `.env.template`. The `start.sh` script substitutes `__HOST_IP__` with your detected LAN IP to produce `.env`.

Key settings to review:

| Variable | Purpose |
|----------|---------|
| `ANNOTATION_AGENT_USER` | Hypothesis username for the agent |
| `JAATO_PROVIDER` / `MODEL_NAME` | LLM provider and model |
| `CLIENT_OAUTH_ID` | UUID from the authclient table (see first-time setup) |
| `SECRET_KEY` and `*_SECRET` vars | Change these for any non-local deployment |

## Pinning Upstream Versions

Each component is pinned via build args in its Dockerfile:

| Dockerfile | Build arg | Default |
|------------|-----------|---------|
| `Dockerfile.via` | `VIA_REF` | `main` |
| `Dockerfile.client` | `CLIENT_TAG` | `v1.1746.0` |
| `Dockerfile.viahtml` | `VIAHTML_REF` | `main` |

The `h` service uses a pre-built image pinned in `docker-compose.yml` (`hypothesis/hypothesis:20260216-g4352007`).

To override, pass build args:

```bash
docker compose build --build-arg VIA_REF=some-commit via
```
