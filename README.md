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
git clone https://github.com/Jaato-framework-and-examples/jaato-via-hypothesis-integration.git
cd jaato-via-hypothesis-integration
./start.sh
```

`start.sh` detects your LAN IP, generates `.env` from the template, and starts all containers.

## First-Time Setup

Handled automatically. On the first run, `start.sh` detects an uninitialized database and calls `init.sh`, which:

1. Creates the DB schema and search index
2. Prompts you for a username, email, and password
3. Creates the user with the correct authority
4. Generates the OAuth client and updates `.env.template`

Just run `./start.sh` and follow the prompts. You can also re-run `./init.sh <HOST_IP>` independently if needed.

## Usage

Open Via in your browser to annotate any web page with the Hypothesis client injected:

```
http://<HOST_IP>:9083/<URL_TO_ANNOTATE>
```

For example, to annotate `https://example.com`:

```
http://192.168.50.212:9083/https://example.com
```

You can also visit `http://<HOST_IP>:9083` directly — the Via front page lets you paste a URL to annotate.

Log in with the credentials you created during first-time setup. Select text on the proxied page to create annotations.

## Annotation Agent

The agent listens for new annotations in real time via the Hypothesis WebSocket API and replies using a Jaato-connected LLM.

### Source Repository Discovery

For the agent to read and propose edits to the source code behind annotated pages, those pages must include a `<link rel="vcs-git">` tag in their HTML `<head>`:

```html
<link rel="vcs-git" href="https://github.com/your-org/your-repo">
```

When the agent processes an annotation, it fetches the annotated page and looks for this tag to locate the backing repository. If the tag is missing, the agent can still reply to annotations but won't have access to the source code — it will only see the page content quoted in the annotation.

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
