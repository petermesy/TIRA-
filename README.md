# TIRA — Technology Intelligence & Research Agent

TIRA discovers, evaluates, verifies, explains, and publishes technology intelligence. It implements **V0** manual analysis, **V1** bounded source discovery, **V2** claim-level evidence verification, and the controlled **V3** review/publishing workflow. Manual approval is the default; automatic publishing is disabled unless explicitly enabled and all configured gates pass.

## V0 features

- Submit article text or a public article URL for analysis.
- Normalize content, classify it, calculate a deterministic weighted score, and create an original Telegram-ready draft.
- Use Google's Gemini API (Gemini 3.5 Flash by default) or an OpenAI-compatible LLM adapter when configured; without an LLM key, use a clearly identified conservative local fallback.
- Persist articles, analysis, draft revisions, review decisions, Telegram delivery state, and audit events.
- Preview drafts privately, explicitly approve them, and publish only through an authorized API action.
- Enforce safe URL fetching, input limits, rate limits, and manual publishing by default.
- Collect from configured RSS/Atom feeds, arXiv, and GitHub Releases; isolate source failures, deduplicate results, and keep collection-run metrics.
- Keep publishing manual and require an administrator for source configuration and collection runs.
- Extract factual claims, search for web-grounded primary and independent evidence, preserve evidence and reviewer-set credibility, and classify claims with transparent deterministic rules.

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

## V2 claim verification

Verification endpoints require the admin bearer token:

- `POST /api/v1/articles/{article_id}/verify` extracts material claims with the configured LLM, finds evidence using a configured SearXNG instance (`SEARXNG_BASE_URL`), Brave Search (`BRAVE_SEARCH_API_KEY`), Tavily (`TAVILY_API_KEY`), or Gemini Google Search grounding, safely retrieves cited public HTTPS pages, and persists claim-level supporting, contradicting, or contextual evidence. Search precedence is Brave, Tavily, SearXNG, then Gemini grounding.
- `GET /api/v1/articles/{article_id}/verification` returns the latest verification, including claim status, cited evidence excerpts/URLs, score factors, unresolved issues, and CVE identifiers.
- `GET /api/v1/sources/credibility` lists reviewer-maintained domain tiers.
- `PUT /api/v1/sources/credibility/{domain}` sets a domain tier (`UNASSESSED`, `LOW`, `MEDIUM`, `HIGH`, or `AUTHORITATIVE`) with an auditable rationale.

Claim statuses are `CONFIRMED`, `SUPPORTED`, `UNCONFIRMED`, `CONFLICTING`, and `FALSE`; missing evidence is never treated as proof of falsity. `MISLEADING` is reserved for claims with sufficient evidence to justify that determination and is not inferred solely from missing search results. Confirmation requires an authoritative primary source and reliable independent corroboration. Duplicate URLs, identical content, or sources sharing an independence group do not count as separate corroboration. `nvd.nist.gov`, `cve.org`, and `cisa.gov` are treated as authoritative defaults; admins can set tiers for other domains.

Scores use the V2 policy version stored with each run. Verification score factors are primary evidence (30%), independent corroboration (25%), source quality (20%), technical evidence (15%), and recency/context (10%). Importance uses the existing dimension weights by default; optional `IMPORTANCE_WEIGHTS` JSON can override weights per category while preserving the sum-to-one requirement. Verification work is bounded by `MAX_VERIFICATION_CLAIMS`, `MAX_EVIDENCE_PER_CLAIM`, `MAX_VERIFICATION_REQUESTS`, and `MAX_VERIFICATION_DURATION_SECONDS`. Without a supported Gemini configuration/search result, claims stay unconfirmed and the run reports a warning.

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/articles/ARTICLE_ID/verify \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"

curl http://127.0.0.1:8000/api/v1/articles/ARTICLE_ID/verification \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"
```

Verification is an evidence triage aid, not a guarantee of truth. Reviewers remain responsible for interpreting sources and approving every post. V2 does not enable automatic publishing.

## V3 controlled review and publishing

The application supports `PUBLISH_MODE=manual` (default), `semi_automatic` (prioritize important items in the review queue; still requires approval), and `auto_with_gates`. Automatic publishing additionally requires `AUTO_PUBLISH_ENABLED=true`, explicit `AUTO_PUBLISH_CATEGORIES`, high importance and verification thresholds, confirmed status for every material claim, reliable primary evidence, passed draft checks, an available daily post allowance, and no active emergency pause. Missing verification is never auto-publish eligible. Cybersecurity is not eligible unless explicitly allowlisted by the operator.

V3 administration and review endpoints require the admin bearer token:

- `GET /api/v1/operations/status`, `/review-queue`, and `/daily-summary` expose queue/operational state and daily activity.
- `POST /api/v1/operations/pause` and `/resume` accept `{"scope":"publishing|collection|all","reason":"..."}` and persist the emergency pause.
- `POST /api/v1/operations/auto-publish` is the externally schedulable entry point for eligible items. It requires the admin bearer token and both `PUBLISH_MODE=auto_with_gates` and `AUTO_PUBLISH_ENABLED=true`; configure a trusted scheduler to call it only after enabling the gates.
- `POST /api/v1/drafts/{draft_id}/preview` sends a review card with direct evidence links and opaque approve/reject buttons to the private review chat.
- `POST /api/v1/drafts/{draft_id}/approve`, `/reject`, `/edit`, and `/publish` record reviewer actions. Sending to the public channel is idempotent and subject to the daily limit and emergency pause.
- `POST /api/v1/drafts/published/{post_id}/correction` creates a correction draft linked to the original post; correction publication still requires approval.

Telegram commands and review callbacks require `TELEGRAM_WEBHOOK_SECRET` and a non-empty `TELEGRAM_REVIEWER_IDS` allowlist. Configure Telegram's webhook URL as `https://YOUR_HOST/api/v1/telegram/webhook` and set the matching secret token in Telegram. Allowlisted admins can use `/help`, `/status`, `/queue`, `/pause`, `/resume`, `/run`, `/search <topic or question>`, `/settings`, `/analyze <https-url>`, `/verify <article-id>`, `/draft <draft-id>`, `/edit <draft-id> <text>`, `/approve <draft-id>`, `/reject <draft-id>`, `/publish <draft-id>`, `/sources`, `/addsource <name>|RSS|<https-feed-url>`, and `/source <source-id> enable|disable` in the review chat or a private bot conversation. `/search` uses SearXNG to find a source article for your topic, prepares a source-linked LLM draft, and sends it for your approval. When approval comes from Telegram, TIRA attempts to publish immediately; publication still enforces quality, pause, and daily-limit gates. `/settings` reports current non-secret settings; change deployment configuration through `.env` and restart the app. Do not expose the review bot in an untrusted group.

Scheduled discovery is opt-in. Set `COLLECTION_SCHEDULER_ENABLED=true` to poll registered sources using each source's configured interval. To also search the indexed public web without registering every site, configure a SearXNG instance and set `WEB_DISCOVERY_ENABLED=true`. TIRA searches bounded topics covering science facts and explainers, AI, space, and software development, analyzes a small number of HTTPS candidates, and sends an LLM-prepared, source-linked post preview to the Telegram review chat. Science drafts use an accessible explainer style; the LLM is instructed to keep claims, numbers, estimates, and caveats grounded in the retrieved article. This searches the search engine's available index; it does not crawl the entire internet. Use `/run` to request an immediate scan. Scheduled web search is limited by `WEB_DISCOVERY_INTERVAL_SECONDS`, `MAX_WEB_DISCOVERY_ITEMS_PER_RUN`, and the existing LLM daily budget. Each Telegram review card has Approve and publish / Reject actions. The scheduler requires Telegram bot, channel, review-chat, webhook-secret, and reviewer-ID configuration. It respects the collection pause and configured source/LLM budgets.

Automatic publishing is opt-in and remains off in the sample configuration. Only enable it after evaluating V2 results on a representative, human-labeled dataset. `AUTO_PUBLISH_CATEGORIES=[]` disables all auto-publishing categories, `MAX_POSTS_PER_DAY` sets the daily delivery cap, and `MIN_AUTO_IMPORTANCE_SCORE` / `MIN_AUTO_VERIFICATION_SCORE` set the required thresholds.

## Configuration

See [.env.example](./.env.example). Important values:

- `DATABASE_URL`: SQLAlchemy URL; defaults to a local SQLite database.
- `ADMIN_API_TOKEN`: required for privileged API actions.
- `LLM_PROVIDER`: `google_gemini` (default), `groq`, or `openai_compatible`.
- `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`: hosted model configuration. Gemini defaults to the Google Generative Language API and `gemini-3.5-flash`; Groq uses its OpenAI-compatible API and a model ID available in your Groq Console. When `LLM_PROVIDER=groq` and `LLM_BASE_URL` is still the Gemini default, TIRA automatically uses `https://api.groq.com/openai/v1`.
- Set `LLM_API_KEY` to the key for the selected provider. Do not reuse a Gemini key for Groq or commit keys to source control.
- `BRAVE_SEARCH_API_KEY`: separate Brave Search API key for V2 web evidence discovery with Groq or another configured LLM. Brave Search provides candidate URLs; TIRA safely fetches pages and requires exact quotes from retrieved text before persisting evidence.
- `TAVILY_API_KEY`: optional alternate web-search key; used when Brave is not configured. Search providers only provide candidate URLs; TIRA safely fetches pages and requires exact quotes from retrieved text before persisting evidence.
- `SEARXNG_BASE_URL`: optional HTTPS URL for a SearXNG instance, used if Brave and Tavily are not configured. No search API key is required. Public instances listed at [searx.space](https://searx.space/) may rate-limit requests or disable JSON output; a self-hosted instance gives more control but requires hosting.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_REVIEW_CHAT_ID`: needed for Telegram delivery.
- `MAX_ARTICLE_CHARS`, `MAX_FETCH_BYTES`: bounds on user and remote content.
- `MAX_REQUESTS_PER_MINUTE`: per-client in-memory API limit.
- `MAX_COLLECTION_*`, `MAX_SOURCE_REQUESTS_PER_RUN`, `MAX_LLM_ITEMS_PER_*`: collection and provider budget limits.
- `COLLECTION_SCHEDULER_ENABLED`, `COLLECTION_SCHEDULER_INTERVAL_SECONDS`: opt-in background discovery for registered sources; new drafts await Telegram approval.
- `WEB_DISCOVERY_ENABLED`, `WEB_DISCOVERY_INTERVAL_SECONDS`, `MAX_WEB_DISCOVERY_ITEMS_PER_RUN`: opt-in, budget-limited topic searches through SearXNG; candidate posts are prepared by the LLM and sent for Telegram approval.
- `MAX_VERIFICATION_*`, `MAX_EVIDENCE_PER_CLAIM`: bounded claim-verification workload.
- `IMPORTANCE_WEIGHTS`: optional JSON mapping categories to dimension weights; each category's weights must sum to 1.
- `PUBLISH_MODE`: `manual` (default), `semi_automatic`, or `auto_with_gates`.
- `AUTO_PUBLISH_ENABLED`, `AUTO_PUBLISH_CATEGORIES`, `MIN_AUTO_IMPORTANCE_SCORE`, `MIN_AUTO_VERIFICATION_SCORE`, `MAX_POSTS_PER_DAY`: explicit V3 gates; automatic publishing remains off by default.
- `TELEGRAM_RETRY_BASE_SECONDS`, `TELEGRAM_RETRY_MAX_SECONDS`, `TELEGRAM_MAX_ATTEMPTS`: bounded exponential retry policy for retryable delivery failures; permanent errors are blocked for operator review.
- `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_REVIEWER_IDS`: optional signed webhook and allowlist for Telegram review actions and commands.

## Development and checks

```bash
pytest
ruff check backend
```

The tests use mocked external services and do not require live LLM, URL, or Telegram credentials.

## Safety and version boundaries

- URL fetching accepts only HTTP(S), rejects localhost/private/link-local/metadata addresses, revalidates redirects, and enforces time and response-size limits.
- External article text is treated as untrusted evidence, never as instructions.
- A score is not a probability or a substitute for human review. The local analyzer is deliberately conservative.
- V1 fetches only explicitly registered sources using bounded requests. V2 uses grounded search citations, safe page fetching, verbatim quote checks, persisted evidence, and deterministic status/score policy; it cannot guarantee correctness or independent sourcing.
- Publishing remains manual. Autonomous publishing (V3) is not implemented.
# TIRA-
