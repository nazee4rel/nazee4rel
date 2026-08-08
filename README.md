# X Account Intelligence Agent

An agentic system that monitors and analyses a single X/Twitter account: it collects
performance data on a schedule, turns it into insights and recommendations, verifies
whether its own advice worked, and reports.

**Current status: Phase 2 complete** — foundation, database and authentication.
The stack runs, migrations apply, and you can sign in. X integration begins in Phase 3.

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

Verify configuration without starting the server:

```bash
docker compose exec backend python -m app.cli check-config
```

## What Phase 2 contains

```
backend/
  app/
    core/       config, security (Argon2id, AES-256-GCM, UUIDv7), logging, errors, rate limiting
    db/         async SQLAlchemy engine, session management, declarative base
    models/     users, sessions, x_accounts, oauth_tokens, account_capabilities, audit, system logs
    schemas/    Pydantic request/response types
    services/   auth and audit services
    api/v1/     health, auth, accounts routers
    cli.py      create-owner, check-config
  alembic/      migrations (0001 creates all seven tables)
  tests/        57 tests — auth flows, crypto primitives, migration parity
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
| 3 | X OAuth 2.0 + PKCE, API client, cost ledger, capability probe | Next |
| 4 | Collectors, Celery schedule, snapshot pipeline, cost governor | |
| 5 | Analytics engine: engagement, baselines, topics, timing, attribution | |
| 6 | Agent loop, Claude structured outputs, recommendations, verification | |
| 7 | Full dashboard | |
| 8 | Alerts and scheduled reports | |
| 9 | Test hardening, security review, deployment | |

Phase 4 is the one to reach quickly: until collectors run, the impression dataset is not
accumulating, and that data cannot be recovered retroactively.
