# TIRA — Technology Intelligence & Research Agent

TIRA discovers, evaluates, verifies, explains, and publishes technology intelligence. The application implements **V0** manual analysis and **V1** bounded automated source discovery. Claim-level evidence verification (V2) and gated autonomous publishing (V3) are not enabled.

## V0 features

- Submit article text or a public article URL for analysis.
- Normalize content, classify it, calculate a deterministic weighted score, and create an original Telegram-ready draft.
- Use Google's Gemini API (Gemini 3.5 Flash by default) or an OpenAI-compatible LLM adapter when configured; without an LLM key, use a clearly identified conservative local fallback.
- Persist articles, analysis, draft revisions, review decisions, Telegram delivery state, and audit events.
- Preview drafts privately, explicitly approve them, and publish only through an authorized API action.
- Enforce safe URL fetching, input limits, rate limits, and manual publishing by default.
- Collect from configured RSS/Atom feeds, arXiv, and GitHub Releases; isolate source failures, deduplicate results, and keep collection-run metrics.
- Keep publishing manual and require an administrator for source configuration and collection runs.

## Requirements and setup

Python 3.12+ is the recommended runtime. The code is kept compatible with Python 3.11 for local development and CI environments that have not yet moved to 3.12.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Set a long random `ADMIN_API_TOKEN` before exposing the API. Configure external service credentials through environment variables or your deployment platform's secret store, never in source control. The default SQLite database is suitable for development; configure `DATABASE_URL` to a PostgreSQL SQLAlchemy URL for deployment.

Apply database migrations before starting the API, including when upgrading an existing local database:

```bash
alembic upgrade head
```

Development mode creates missing tables automatically for a new database; migrations are still required to upgrade an existing database.

Start the API:

```bash
uvicorn app.main:app --app-dir backend --reload
```

Interactive API documentation is available at `/docs`; health status is available at `/api/v1/health`.

## API

### Analyze text or a URL

`POST /api/v1/articles/analyze`

Provide exactly one of `text` or `url`. For text submissions, `title` is optional and defaults to the first non-empty line.

```json
{
  "title": "A research lab announces a new model",
  "text": "The lab's announcement and technical details...",
  "category_hint": "AI",
  "language": "en"
}
```

The response contains the article and draft IDs, category, dimension scores, importance score, recommendation, quality status, and workflow status. Model-generated analysis is schema-validated; when no model key is configured the response clearly identifies the deterministic fallback.

### Retrieve article and draft

- `GET /api/v1/articles/{article_id}`
- `GET /api/v1/articles/{article_id}/draft`

### Preview, approve, and publish

All reviewer/admin operations require `Authorization: Bearer $ADMIN_API_TOKEN`.

- `POST /api/v1/drafts/{draft_id}/preview` sends the draft to `TELEGRAM_REVIEW_CHAT_ID`.
- `POST /api/v1/drafts/{draft_id}/approve` records an explicit approval.
- `POST /api/v1/drafts/{draft_id}/publish` sends an approved draft to `TELEGRAM_CHANNEL_ID`.

Approval is required even if a draft has a high score. Publishing is always manual in V0; there is no auto-publish mode.

## V1 source discovery

Source administration and collection endpoints require the admin bearer token:

- `POST /api/v1/sources` registers a source; `source_type` is `RSS`, `ARXIV`, or `GITHUB_RELEASES`.
- `GET /api/v1/sources` lists registered sources and their health.
- `PATCH /api/v1/sources/{source_id}` enables or disables a source.
- `POST /api/v1/collection/run` runs collection now; pass `?trigger_type=SCHEDULED` for an external scheduler invocation.
- `GET /api/v1/collection/runs/latest` and `GET /api/v1/collection/runs/{run_id}` inspect run results and source-level failures.

Example RSS source:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/sources \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Example AI research feed",
    "source_type": "RSS",
    "base_url": "https://example.org/research/feed.xml",
    "categories": ["AI"],
    "interval_minutes": 60,
    "max_items_per_run": 10
  }'

curl -X POST http://127.0.0.1:8000/api/v1/collection/run \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"
```

Connect an external scheduler (for example, a hosted cron service) to `POST /api/v1/collection/run?trigger_type=SCHEDULED` using the bearer token stored as a scheduler secret. Do not expose the token in a public URL. Scheduled runs honor each source's polling interval. The worker caps sources, requests, item volume, model analyses, and execution time. Configure `MAX_COLLECTION_SOURCES`, `MAX_SOURCE_REQUESTS_PER_RUN`, `MAX_COLLECTION_ITEMS_PER_RUN`, `MAX_LLM_ITEMS_PER_RUN`, `MAX_LLM_ITEMS_PER_DAY`, `MAX_COLLECTION_DURATION_SECONDS`, and `MAX_ARTICLE_AGE_DAYS` to tune these limits. Five consecutive source failures disable that source; re-enable it with the source PATCH endpoint after investigation.

## Configuration

See [.env.example](./.env.example). Important values:

- `DATABASE_URL`: SQLAlchemy URL; defaults to a local SQLite database.
- `ADMIN_API_TOKEN`: required for privileged API actions.
- `LLM_PROVIDER`: `google_gemini` (default) or `openai_compatible`.
- `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`: optional hosted model configuration. Gemini defaults to the Google Generative Language API and `gemini-3.5-flash`; confirm that model ID is enabled for your Google AI Studio/API project.
- `LLM_API_KEY`: set this to your Google AI Studio API key when `LLM_PROVIDER=google_gemini`.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_REVIEW_CHAT_ID`: needed for Telegram delivery.
- `MAX_ARTICLE_CHARS`, `MAX_FETCH_BYTES`: bounds on user and remote content.
- `MAX_REQUESTS_PER_MINUTE`: per-client in-memory API limit.
- `MAX_COLLECTION_*`, `MAX_SOURCE_REQUESTS_PER_RUN`, `MAX_LLM_ITEMS_PER_*`: collection and provider budget limits.
- `PUBLISH_MODE`: V0 accepts only `manual`.

## Development and checks

```bash
pytest
ruff check backend
```

The tests use mocked external services and do not require live LLM, URL, or Telegram credentials.

## Safety and version boundaries

- URL fetching accepts only HTTP(S), rejects localhost/private/link-local/metadata addresses, revalidates redirects, and enforces time and response-size limits.
- External article text is treated as untrusted evidence, never as instructions.
- A score is not a probability or a substitute for verification. The local analyzer is deliberately conservative.
- V1 fetches only explicitly registered sources using bounded requests. It does not claim to verify facts, and publishing remains manual; claim-level evidence verification (V2) and autonomous publishing (V3) are not implemented.
# TIRA-
