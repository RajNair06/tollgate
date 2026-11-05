# Toll Gate

A policy gateway between AI agents and PayPal. Agents authenticate with API
keys and *ask permission* to move money; the gateway decides (allow, deny,
require human approval, deduplicate), enforces spending caps atomically,
audits everything, and holds the PayPal credentials so agents never touch
money directly.

## Architecture

```
AI agent ──(X-API-Key)──▶ FastAPI
                            │  auth: SHA-256 lookup in agent_keys (per request, no cache)
                            │  rate limit: Redis fixed windows (per-agent + global)
                            ▼
                        policy engine ──▶ PostgreSQL (decisions, caps, audit, outbox)
                            │
                        approvals (human-in-the-loop) ──▶ PayPal refunds
```

Two subsystems, one repo:

- **Policy engine** (PostgreSQL + Redis): agent identity, per-agent policies,
  atomic spending caps, approval workflow, append-only audit log.
- **PayPal client** (`gateway/paypal_client.py`): OAuth2 token acquisition and
  caching (in-memory or Redis-backed), Orders v2 (create/get/capture),
  captures + full/partial refunds, disputes, idempotency via
  `PayPal-Request-Id`, `Decimal` money as strings, typed error hierarchy with
  `debug_id` logging.

## Data model (8 tables, `gateway/models.py`)

| Table        | Role |
|--------------|------|
| `agents`     | Who may call the gateway (`name` UNIQUE, `is_active` kill switch) |
| `agent_keys` | API-key SHA-256 hashes, 1:N per agent → zero-downtime key rotation; soft revocation (`revoked_at`) keeps rows for forensics |
| `policies`   | One per agent: `allowed_actions` allowlist, `per_txn_cap`, `rolling_24h_cap`, `approval_threshold`, approval TTL. CHECK: threshold ≤ per-txn cap |
| `actions`    | Every authorization request + verdict (`allowed` / `denied` / `pending_approval` / `deduplicated`) with reason code. Partial unique index `(agent_id, idempotency_key) WHERE outcome <> 'deduplicated'` makes double-spending physically impossible |
| `approvals`  | Human-in-the-loop state machine: `pending → approved → executed`, plus `rejected` / `expired` / `failed`; CHECKs enforce timeline consistency |
| `audit_log`  | Append-only record of **every** decision, including auth failures (nullable `agent_id`). FK with no cascade: audit rows block agent deletion |
| `events`     | Immutable domain-event log (JSONB payloads) |
| `outbox`     | Same events with `status` for a publish relay — written in the decision's transaction, so notifications can't be lost to a crash |

Money is `NUMERIC(12,2)` everywhere; statuses are `TEXT + CHECK` (widening a
CHECK migrates cleanly, unlike `ALTER TYPE`). Schema evolves through Alembic
(`alembic/versions/`, 3 migrations).

## How decisions stay correct under concurrency

`authorize()` (`gateway/policy_engine.py`) takes `SELECT ... FOR UPDATE` on the
agent row first, so concurrent requests from one agent serialize while
different agents never block each other. The rolling 24h cap sums committed
`allowed` actions plus `pending_approval` actions whose approval is still live
— pending approvals *reserve* headroom; rejecting or expiring them releases
it. Every decision writes action + audit + event + outbox rows in **one
transaction**: all land or none do. Proven by test: 50 barrier-aligned threads
× $10 against a $100 cap → exactly 10 approvals, total exactly $100.00.

## API

### Agent-facing (auth: `X-API-Key` or `Authorization: Bearer`)

| Endpoint | Purpose |
|---|---|
| `POST /v1/agent-actions` | The decision endpoint. Body: `action_type`, `amount` (decimal **string** — JSON numbers rejected), `currency`, `idempotency_key`, optional `reference_id`. All four outcomes return 200; 4xx means the *request* failed (401/403 auth, 422 validation, 429 rate limit) |
| `GET /v1/actions/{id}` | Action detail (own agent only; foreign ids → 404) |
| `GET /v1/approvals/{id}` | Approval + action detail |
| `POST /v1/approvals/{id}/decision` | `approved` / `rejected` (illegal transitions → 409) |
| `POST /v1/approvals/{id}/execute` | Record execution result: `executed` / `failed` |
| `POST /v1/admin/expire-approvals` | Run the expiry sweep now (pending approvals past `expires_at` → `expired`, releasing cap reservations) |
| `GET /health` | Liveness + `paypal_mode` (never calls PayPal) |

Strict validation (`gateway/schemas.py`): `extra="forbid"` + `strict=True` —
unknown fields, floats-for-money, and malformed values are 422s, mirroring
the DB CHECKs at the edge.

### Admin (auth: `X-Admin-Key` = `ADMIN_API_KEY` env var; unset → 503)

| Endpoint | Purpose |
|---|---|
| `POST /v1/admin/agents` | Provision agent + policy + first key. **Plaintext key returned once, never retrievable again** |
| `POST /v1/admin/agents/{id}/keys` | Issue an additional key (rotation: both keys live until the old one is revoked) |
| `DELETE /v1/admin/keys/{key_id}` | Soft-revoke (idempotent). Takes effect on the running server immediately |
| `GET /v1/admin/agents[/{id}]` | Ops views: policy + key prefixes (never hashes) |

The admin credential is deliberately an env-var bootstrap secret, **not** an
agent key — agents cannot provision or approve as admins. Because auth does a
fresh DB read per request, keys issued or revoked through these endpoints are
live on the next request with no restart (verified with a 50-cycle
create→use-immediately hammer). Mutating handlers **commit before returning**
so a client acting instantly on a response can never outrun the commit.

## Requirements

- Python ≥ 3.12, [uv](https://docs.astral.sh/uv/)
- Docker (PostgreSQL 16 + Redis 7) — ports 5433/6380 by default, since native
  services often own 5432/6379 on dev machines
- PayPal sandbox credentials (developer.paypal.com) for payment execution

## Setup

```bash
# infrastructure
docker run -d --name tollgate-pg -e POSTGRES_USER=tollgate -e POSTGRES_PASSWORD=tollgate \
  -e POSTGRES_DB=tollgate -p 5433:5432 postgres:16-alpine
docker run -d --name tollgate-redis -p 6380:6379 redis:7-alpine

# app
uv sync
cp .env.example .env    # fill PayPal creds; generate ADMIN_API_KEY:  tga_$(openssl rand -hex 24)
uv run alembic upgrade head
uv run uvicorn main:app --port 8000
```

## Usage

Provision an agent (via API, or the CLI equivalent
`uv run python scripts/create_agent.py <name> [per_txn] [rolling_24h] [threshold] [ttl_min] [actions...]`):

```bash
curl -s -X POST localhost:8000/v1/admin/agents \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"refund-bot","allowed_actions":["refund"],
       "per_txn_cap":"25.00","rolling_24h_cap":"100.00","approval_threshold":"20.00"}'
# → {"agent_id":1,"api_key":"tg_...","key_id":1,"key_prefix":"tg_..."}   (key shown once)
```

Request a decision:

```bash
curl -s -X POST localhost:8000/v1/agent-actions \
  -H "X-API-Key: tg_..." -H 'Content-Type: application/json' \
  -d '{"action_type":"refund","amount":"22.00","idempotency_key":"req-0001"}'
# → {"outcome":"pending_approval","reason_code":"APPROVAL_REQUIRED","approval_id":1,...}
# replaying the same idempotency_key → {"outcome":"deduplicated","original_outcome":...}
```

Decide and execute the approval:

```bash
curl -s -X POST localhost:8000/v1/approvals/1/decision \
  -H "X-API-Key: tg_..." -H 'Content-Type: application/json' \
  -d '{"decision":"approved","decided_by":"ops-alice"}'
curl -s -X POST localhost:8000/v1/approvals/1/execute \
  -H "X-API-Key: tg_..." -H 'Content-Type: application/json' \
  -d '{"executed":true}'
```

PayPal end-to-end sandbox flow (create order → approve in browser → capture →
partial refund → idempotent replay; refuses to run outside sandbox):

```bash
uv run python scripts/toll_flow.py
```

## Tests

```bash
uv run pytest        # 54 tests, ~1.5 min — requires the Docker containers running
```

Runs against a real PostgreSQL test database (`tollgate_test`, created and
Alembic-migrated automatically by `tests/conftest.py`, truncated between
tests) because the correctness claims under test — `SELECT FOR UPDATE`
serialization, partial unique indexes, CHECK constraints — are PostgreSQL
behavior. Coverage: policy outcomes and caps, dedup, approval lifecycle and
expiry, 50-thread concurrency (allowed path and pending-reservation path),
API auth/validation/rate-limiting, admin provisioning and key rotation. The
PayPal client is tested offline via `httpx.MockTransport` (token refresh,
error mapping).

## Performance

Audit query (one agent's last-24h decisions, grouped) over 200k rows:

| | Plan | Buffers | Time |
|---|---|---|---|
| Before | Parallel Seq Scan (97k rows discarded per worker) | 1,935 | 113.8 ms |
| After | Index Only Scan, `Heap Fetches: 0` | 57 | 9.0 ms (warm) |

Fix: covering index `(agent_id, created_at DESC) INCLUDE (decision,
reason_code, amount)` (migration `fa956bf87c5a`). Cost scales with the
agent's window activity, not total table growth. Full EXPLAIN ANALYZE
transcripts: `git log` → `docs/explain_analyze_audit.md` (removed from the
tree after being summarized here).

## Known simplifications (fix before production)

- **Operator identity**: approval `/decision` and `/execute` accept agent API
  keys — an agent could approve its own refund. Needs a separate operator
  role (same pattern as `ADMIN_API_KEY`).
- **PayPal execution glue**: `/execute` records the outcome; wiring the
  actual `PayPalClient.refund_capture()` call into the approval flow is TODO.
- **Outbox relay**: rows are written transactionally but no publisher process
  drains them yet.
- **Expiry sweep**: on-demand via endpoint; needs a scheduler (cron/APScheduler).
- Rate limiting fails **open** if Redis is down (protection, not correctness —
  DB locks and CHECKs still enforce the money rules); token cache falls back
  to per-call fetch.

## Project layout

```
config.py               env-driven settings (PayPal, DB, Redis, rate limits, admin key)
main.py                 FastAPI app, lifespan (PayPal client w/ Redis token cache)
gateway/
  paypal_client.py      PayPal REST client (pluggable TokenCache)
  models.py             the 8 tables + vocabularies + transition map
  db.py                 engine, session_scope (transaction contract)
  api_keys.py           key generation + SHA-256 hashing
  auth.py               request pipeline: get_db → auth → rate limit
  policy_engine.py      authorize(): lock, dedup, caps, threshold, outcomes
  approvals.py          state machine, expiry sweep
  audit.py              append-only writer (insert is the only operation)
  redis_client.py       token cache + fixed-window rate limits
  schemas.py            strict Pydantic request/response models
  routes.py             agent-facing endpoints
  admin.py              admin provisioning endpoints
alembic/                migrations (schema → perf index → agent_keys)
scripts/
  create_agent.py       CLI provisioning (prints key once)
  toll_flow.py          live PayPal sandbox end-to-end demo
tests/                  54 tests (real Postgres + MockTransport)
```

## License

MIT — see [LICENSE](LICENSE).
