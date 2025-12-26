# Log Detector

Central log ingestion + alerting backend, plus a lightweight agent that tails Docker logs and posts matched events.

## Components

- `backend/`: FastAPI service with Postgres storage, Alembic migrations, and SES email alerts.
- `agent/`: Script (or container) that follows Docker logs, matches patterns, and posts to the backend.

## Backend setup

1. Copy `.env.example` to `.env` and fill in SES + alert settings.
2. Start services:

```bash
docker compose up --build
```

The backend auto-runs Alembic migrations on startup.

### Key env vars

- `DATABASE_URL`: Postgres connection string.
- `ALERT_EMAIL_ENABLED`: Toggle alerting.
- `ALERT_EMAIL_TO`: Comma-separated recipients.
- `BASE_URL`: Public URL used in email links.
- `DEDUPE_WINDOW_SECONDS`: Suppress duplicate alerts within this window.

### API

- `POST /api/logs`: Ingest a log record.
- `GET /api/logs`: List log records.
- `GET /api/logs/{id}`: Get a record + email history.
- `GET /api/logs/{id}/emails`: Email history for a record.
- `GET /api/emails`: Paginated email history.
- `GET /api/recipients`: List email recipients.
- `POST /api/recipients`: Add recipients (comma-separated emails).
- `DELETE /api/recipients/{id}`: Deactivate a recipient.

## Agent setup

The agent can run on any host with Docker access.

### Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r agent/requirements.txt
cp agent/.env.example agent/.env
export $(cat agent/.env | xargs)
python agent/monitor.py
```

### Docker run

```bash
docker build -t log-detector-agent ./agent

docker run --rm \
  --env-file agent/.env \
  -v /var/run/docker.sock:/var/run/docker.sock \
  log-detector-agent
```

### Agent env vars

- `BACKEND_URL`: Backend ingest endpoint (default `http://localhost:8000/api/logs`).
- `MONITOR_CONTAINERS`: Comma-separated list of containers to follow.
- `LOG_MATCH_PATTERNS`: Comma-separated regex patterns; defaults to `ERROR_PATTERN` if empty.
- `TRACEBACK_PATTERN`: Regex to detect traceback blocks.
- `ALERT_COALESCE_SECONDS`: Window to combine consecutive error logs into one alert (set `0` to disable).
- `ALERT_COALESCE_MAX_SECONDS`: Max time to hold a coalesced batch before flushing.
- `ALERT_COALESCE_MAX_ENTRIES`: Max entries per coalesced batch before flushing.
- `DOCKER_CMD`: Docker command to run (e.g. `sudo docker` when the host requires sudo).

## Notes

- Duplicate suppression only affects alerting; records are still stored with `is_duplicate=true`.
- Email history is stored per log record in the `email_notifications` table.
- If active recipients exist in the database, they override `ALERT_EMAIL_TO`.
