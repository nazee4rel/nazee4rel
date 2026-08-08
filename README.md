# X Account Intelligence Agent

An agentic system that monitors and analyses a single X/Twitter account: it collects
performance data on a schedule, turns it into insights and recommendations, verifies
whether its own advice worked, and reports.

**Current status: Phase 4 complete** — scheduled collection is running. Follower counts
are snapshotted hourly, posts are discovered and their metrics captured on a decaying
cadence, and every post gets a mandatory capture before its 30-day metrics window closes.
The dataset is now accumulating. Turning it into analytics is Phase 5.

Read [`docs/phase-1-architecture.md`](docs/phase-1-architecture.md) first. It documents the
X API constraints that shape everything else, and three of them are load-bearing:

- **Impressions are perishable.** X only returns `non_public_metrics` for posts under 30
  days old. Collected snapshots are the permanent record; a missed window is unrecoverable
  and there is no historical backfill.
- **Reads cost money per resource.** X is pay-per-use since Feb 2026, with "Owned Reads"
  (your own app reading your own data) at $0.001/resource since April 2026. A cost governor
  is a core component.
- **Follower attribution is inference, not measurement.** X exposes no follower-event
  stream, so "which post gained me followers" is modelled and labelled as such.

## Quick start

Requires Docker and Docker Compose.

```bash
git clone <this-repo> && cd nazee4rel
make setup     # creates .env and generates real secrets
make up        # builds, applies migrations, starts everything
```

Then open **http://localhost:3000** and create the owner account. Registration closes
once that account exists — this is a single-tenant deployment.

| Service | URL |
|---|---|
| Dashboard | http://localhost:3000 |
| API docs | http://localhost:8000/docs |
| Health | http://localhost:8000/api/v1/health/ready |

```bash
make logs      # tail backend logs
make test      # run the backend test suite
make lint      # ruff + mypy
make down      # stop
make reset     # destroy all data and rebuild
```

## Configuration

`make setup` copies `.env.example` to `.env` and generates `SECRET_KEY`,
`TOKEN_ENCRYPTION_KEY` and a database password. Every setting is documented inline in
[`.env.example`](.env.example). Two are worth knowing about now:

- **`TOKEN_ENCRYPTION_KEY`** encrypts X OAuth tokens at rest with AES-256-GCM. Losing it
  means reconnecting your X account. Back it up somewhere real before production.
- **`X_ENABLE_WRITE_ACTIONS`** defaults to `false`. While false, the app never requests
  the `tweet.write` scope, so the agent cannot publish regardless of any other failure.
  Turning it on enables *drafting* posts for your per-draft approval — it does not enable
  autonomous posting. Nothing is ever published without you approving that specific draft.

### Connecting your X account (Phase 3)

You need an app in the [X developer portal](https://developer.x.com) with **OAuth 2.0
enabled as a Confidential Client**, and its callback URL set to exactly:

```
http://localhost:8000/api/v1/x/oauth/callback
```

Put the client ID and secret in `.env` as `X_CLIENT_ID` / `X_CLIENT_SECRET`, restart, then
click **Connect X account** on the Overview page. The capability probe runs automatically
on connection and tells you what your access level actually supports.

Since Feb 2026 X bills pay-per-use. Reading your own data qualifies for **Owned Reads** at
$0.001/resource, so the probe costs well under a cent and ordinary collection runs a few
dollars a month. Month-to-date spend is shown on the Overview page, and the governor
refuses calls that would breach `X_MONTHLY_BUDGET_USD` rather than letting a bill surprise
you.

Verify configuration without starting the server:

```bash
docker compose exec backend python -m app.cli check-config
```

## What the codebase contains

```
backend/
  app/
    core/       config, security (Argon2id, AES-256-GCM, UUIDv7), logging, errors, rate limiting
    db/         async SQLAlchemy engine, session management, declarative base
    models/     users, sessions, x_accounts, oauth_tokens, account_capabilities, audit, system logs
    schemas/    Pydantic request/response types
    services/   auth and audit services
    api/v1/     health, auth, accounts, x_oauth routers
    integrations/x/  endpoint registry, OAuth+PKCE, API client, rate limiting, capability probe
    collectors/ snapshot schedule, payload parsing, the collectors themselves
    worker/     Celery app and the beat schedule
    cli.py      create-owner, check-config
  alembic/      0001 identity · 0002 OAuth state + usage ledger · 0003 posts + snapshots
  tests/        178 tests — auth, crypto, migration parity, OAuth/PKCE, client, cost,
                probe, snapshot scheduling, parsing, collectors
frontend/
  src/app/      login + the six dashboard sections, App Router, Server Components
  src/lib/      server-side API client (never imported client-side)
  src/components/  ProvenanceBadge, CapabilityMatrix, PhaseNotice
```

### Security posture already in place

- OAuth 2.0 + PKCE is the only path to X. **No X password is ever requested, seen or
  stored** — there is no code path that could.
- Dashboard passwords are Argon2id. Session tokens are stored as SHA-256 digests, so a
  database leak yields no usable session, and sessions are server-side so they can be
  revoked instantly.
- X tokens are AES-256-GCM encrypted at rest and never appear in any response schema.
  The browser talks only to Next.js, which calls the backend server-side.
- Structured logs pass through a scrubber that redacts credential-shaped keys, because
  the usual way tokens leak is an exception rendered with its arguments.
- Login is timing-equalised against an unknown email, so responses cannot be used to
  enumerate accounts.
- Append-only audit log, security headers, per-IP rate limiting, RBAC scaffolding.

### Two design primitives that exist before the features that use them

**`Provenance`** (`app/models/enums.py`, `components/ProvenanceBadge.tsx`) — every
metric-bearing row will declare whether it was `MEASURED`, `DERIVED`, `INFERRED`,
`USER_ENTERED`, `IMPORTED` or `UNAVAILABLE`, and the UI renders that distinction. This is
how "don't invent data" becomes a schema constraint rather than a matter of discipline.
`UNAVAILABLE` never plots as zero — the chart breaks the line.

**The capability matrix** (`account_capabilities`) — the system never hardcodes what the
X API can do. It probes, records the result, and drives both collector and UI from that
table. An endpoint that starts returning 403 next quarter degrades a panel to "unavailable
since <date>" instead of silently producing zeros. Note that `UNKNOWN` ("not yet checked")
is deliberately distinct from `UNAVAILABLE` ("confirmed absent").

### How collection works (Phase 4)

Two containers do the work: `worker` executes jobs, `beat` schedules them. Exactly one
beat instance — two would double every job.

```bash
make worker-logs   # watch collection happen
```

Snapshot frequency decays with post age, because engagement velocity is front-loaded and
every read costs money:

| Post age | Frequency | Why |
|---|---|---|
| 0–24h | hourly | The curve is steepest here; this is where breakouts show |
| 1–7d | every 6h | Long-tail accumulation |
| 7–29d | daily | Slow drift |
| **day 29** | **mandatory final freeze** | **Last chance before impressions vanish** |

That works out to ~71 snapshots per post — about **$10.65/month at 5 posts/day**, and the
Overview page projects it from your actual posting rate.

**The freeze is the part that matters.** As the budget tightens the governor stretches the
cadence (hourly → 2-hourly → daily) and finally stops ordinary snapshots altogether — but
the pre-cliff freeze still runs even at zero budget. Losing resolution is recoverable;
losing impressions is not. A dedicated hourly sweep gives every post a second, independent
chance at its freeze in case the ordinary cycle is degraded or failing.

Posts already past 30 days when you connect are recorded with their impressions marked
**unavailable, not zero** — they were never obtainable, which is a different fact from a
collection failure, and the dashboard says so.

### Three things worth knowing about the X integration

**Endpoints are a registry, not strings.** `app/integrations/x/endpoints.py` declares every
callable endpoint with its scopes, cost class and capability. The client refuses anything
undeclared, so "never invent an API endpoint" fails locally and immediately rather than as
a puzzling 404.

**Refresh tokens are single-use.** X rotates them on every exchange and invalidates the
old one, so two workers refreshing concurrently would destroy the credential and force a
reconnect. Refresh is serialised behind a Redis lock with a re-read after acquisition, and
the replacement is committed before anything else can fail.

**The probe reads payloads, not just status codes.** A 200 does not mean impressions are
available — X returns 200 while omitting `non_public_metrics` for posts past 30 days. So
when an account has no post recent enough to decide, the probe records `UNKNOWN` rather
than guessing either way, and an inconclusive run never overwrites a previously settled
answer.

## Development without Docker

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                      # runs on in-memory SQLite, no containers needed
ruff check . && mypy app
```

```bash
cd frontend
npm install
npm run typecheck && npm run build
```

The backend test suite runs against SQLite because Phase 2 uses no Postgres-specific SQL.
`tests/test_migration_parity.py` replays the migration against a recorder and diffs the
result against the ORM metadata, so model/migration drift fails the suite without needing
a live database.

## Roadmap

| Phase | Scope | State |
|---|---|---|
| 1 | Architecture, X API capability analysis, data model, agent design | Done |
| 2 | Project structure, backend, database, authentication | Done |
| 3 | X OAuth 2.0 + PKCE, API client, cost ledger, capability probe | Done |
| 4 | Collectors, Celery schedule, snapshot pipeline, cost governor | Done |
| 5 | Analytics engine: engagement, baselines, topics, timing, attribution | Next |
| 6 | Agent loop, Claude structured outputs, recommendations, verification | |
| 7 | Full dashboard | |
| 8 | Alerts and scheduled reports | |
| 9 | Test hardening, security review, deployment | |

Collection is live as of Phase 4, so the impression dataset is accumulating from now on.
Everything from here builds on that history rather than racing it.
