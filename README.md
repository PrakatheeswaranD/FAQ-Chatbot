# KIT FAQ Chatbot

Production-ready fixed-knowledge chatbot for **Kalaignar Karunanidhi Institute
of Technology (KIT), Coimbatore**. It uses few-shot prompting without
fine-tuning. The model selects FAQ records; it never writes the visible answer.
Every informational response is copied from published, stored FAQ text.

## Strict FAQ-only architecture

1. Retrieval selects at most eight published candidate FAQs.
2. Four role-separated few-shot demonstrations teach the model to select
   candidate numbers or refuse.
3. JSON mode requests `{ "answerable": true, "faq_ids": [1] }`.
4. The server rejects malformed JSON, extra fields, duplicate IDs, invalid
   types and out-of-range IDs.
5. Selected English or reviewed Tamil answers are copied from SQLite.
6. Invalid, unknown and off-topic questions receive one fixed fallback.

Prompt injection or model-generated prose cannot enter an answer. A model can
choose an irrelevant existing FAQ, which is an accuracy risk measured by the
evaluation suite, but it cannot introduce outside information.

## Production features

### Persistent state and migrations

`storage.py` initializes a WAL-mode SQLite database containing:

- governed FAQ records and version snapshots;
- unanswered questions and invalid selector outputs;
- durable answer IDs and idempotent feedback events;
- administrator audit events and schema migrations.

The database location is configured with `DATABASE_PATH`. Use a persistent
volume in production. `python scripts/backup.py` creates a consistent online
backup.

### Authenticated admin dashboard

Open `/admin/login`. Administrators can:

- create and preview FAQs;
- edit English and reviewed Tamil answers;
- move records through draft, published and archived states;
- assign categories, effective dates and verification dates;
- inspect version history and roll back;
- validate/import and export complete JSON catalogues;
- review unanswered questions, feedback and audit events.

Production requires `ADMIN_PASSWORD_HASH`. Generate one with:

```powershell
python scripts/create_admin_hash.py
```

### Reliability and security

- Groq timeout, retries, bounded output and circuit breaker.
- Redis-capable shared rate limits for chat, feedback, admin and global traffic.
- Optional trusted-proxy handling with an exact hop count.
- Same-origin checks, CSRF-protected admin forms and secure session cookies.
- CSP, anti-framing, HSTS, permissions policy and no-store API responses.
- Structured JSON request logs with request IDs and latency.
- `/health` liveness and `/ready` database/configuration readiness.
- Bearer-protected `/metrics` output for response, latency, model-error and
  fallback alerts.

### Testing and delivery

- Unit, adversarial, production-route, persistence and admin tests.
- A 31-case live Groq evaluation covering exact, paraphrased, unknown,
  off-topic and hallucination-trap questions.
- `tests/load_test.py` for a bounded deployment load probe.
- GitHub Actions compilation, unit tests, dependency audit and image build.
- Non-root Docker image and Compose stack with Redis and persistent volumes.

## Local setup

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python scripts/create_admin_hash.py
```

Put the generated password hash and Groq API key into `.env`.

Start development mode:

```powershell
python app.py
```

Start the production WSGI server locally:

```powershell
$env:APP_ENV="production"
python serve.py
```

The default development address is `http://127.0.0.1:5000`; `serve.py`
defaults to port `8000`.

## Docker production deployment

Set strong values in `.env`:

- `GROQ_API_KEY`
- `SECRET_KEY` with at least 32 random bytes
- `ADMIN_PASSWORD_HASH`
- `METRICS_TOKEN`

Then run:

```powershell
docker compose up --build -d
docker compose ps
```

The Compose deployment provides one Waitress web service, one Redis service,
a persistent SQLite volume and a persistent Redis volume. Terminate TLS at a
trusted reverse proxy and set `TRUST_PROXY_COUNT` to the exact proxy count.

Back up the SQLite volume regularly. For horizontal web scaling, move FAQ and
event storage to a network database before starting multiple web containers;
SQLite is intentionally the supported single-web-service production profile.

## Configuration

See `.env.example` for every setting. Important groups:

- model: `GROQ_API_KEY`, `GROQ_MODEL`;
- runtime: `APP_ENV`, `HOST`, `PORT`, `SECRET_KEY`, `DATABASE_PATH`;
- administration: `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`;
- rate limits: `RATELIMIT_STORAGE_URI` and per-route limits;
- proxy trust: `TRUST_PROXY_COUNT`;
- resilience: failure threshold, cooldown and answer retention.

The application refuses production startup without a Groq key, session secret
and administrator password hash.

## FAQ governance

`data/faq.json` is the bootstrap seed used only when the database has no FAQ
records. After initialization, the database is authoritative.

Each governed FAQ has:

- stable ID;
- English question and answer;
- optional reviewed Tamil answer;
- category;
- draft, published or archived status;
- effective date;
- last-verified date;
- version number and immutable history.

Only published records whose effective date has arrived enter retrieval.
Generated translation is used solely to improve retrieval; it is never shown
as an answer. Tamil output is used only when a reviewed Tamil variant is stored.

## Verification commands

```powershell
python -m compileall -q app.py fact_check.py language.py prompt.py retrieval.py report.py storage.py serve.py tests
python -m unittest discover tests -v
python tests/evaluate.py
python report.py
python scripts/backup.py
```

To probe a running service without bypassing rate limits:

```powershell
python tests/load_test.py --url http://127.0.0.1:8000 --requests 10 --concurrency 2
```

## Operational checklist

- Terminate HTTPS at the reverse proxy and verify HSTS.
- Use Redis for rate limiting.
- Mount persistent storage for the SQLite database.
- Schedule encrypted off-host backups and test restoration.
- Rotate Groq and administrator credentials.
- Monitor readiness, 5xx responses, model circuit events and fallback rate.
- Scrape `/metrics` with `Authorization: Bearer <METRICS_TOKEN>` and configure
  alerts in the monitoring system used by the deployment.
- Review unanswered questions and negative feedback.
- Verify or archive time-sensitive FAQs periodically.
- Run unit, live-model, dependency-audit and container-build checks for releases.
