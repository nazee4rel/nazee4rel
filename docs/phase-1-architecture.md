# Phase 1 — Architecture, X API Capability Analysis, Data Model & Agent Design

**Project:** Agentic X/Twitter account intelligence system
**Status:** Design only. No implementation code in this phase.
**Date:** August 2026

---

## 0. Executive summary — read this first

Three findings from the API research change the shape of this project. They are not
details; they should drive the whole design.

**1. Impressions are perishable. Your database is the system of record, not a cache.**
`non_public_metrics` and `organic_metrics` — the fields containing `impression_count`,
`url_link_clicks`, and `user_profile_clicks` — are only returned for posts created in
the **last 30 days**. After that they are gone from the API permanently. This is a hard
API limitation, not a lag.

The consequence: a missed collection window is unrecoverable data loss. Everything about
the scheduler, the retry logic, and the alerting is designed around not missing the
30-day cliff. It also means there is **no historical backfill** — impressions for posts
older than 30 days at the moment we go live can never be obtained. Day 1 of running this
system is day 1 of the impression dataset.

**2. Reads now cost money per resource, so the collector must be cost-aware.**
X moved to pay-per-use as the default billing model in February 2026 and retired the
free tier for new developers. Reads are $0.005/resource generally, but **"Owned Reads" —
requests by your own app for your own posts, followers, likes and bookmarks — are
$0.001/resource** as of April 20, 2026. This project is almost entirely Owned Reads,
which makes it economically viable, but a naive "poll everything hourly" collector would
still cost real money every month. A **cost governor** is a first-class component, not an
afterthought. This also gives a precise meaning to your safeguard "no spending without
authorization" — it applies to API spend, not just ad spend.

**3. Follower attribution is an inference, not a measurement.**
The API gives you a follower *count* on the user object. It does **not** give you a
follower *event stream* — there is no "who followed you, when, and from which post"
endpoint. So your question *"which posts brought the most followers?"* cannot be answered
directly. It can be answered *statistically* by modelling follower deltas against post
timing, and that is worth building — but it must be labelled as a model output with a
confidence interval, never presented as a fact. This is the single biggest place where a
system like this is tempted to quietly invent data, so provenance labelling is baked into
the schema and carried all the way to the UI.

---

## 1. X API capability analysis

### 1.1 Method and confidence caveat

`docs.x.com` and `devcommunity.x.com` are blocked by this environment's network egress
policy, so I could not fetch the primary documentation pages directly. The findings below
come from search-index summaries of the official docs and developer-forum announcements,
plus corroborating third-party sources. **Every item marked "verify" must be re-confirmed
against the live docs and against your own developer dashboard before we write the
integration in Phase 3.** The capability probe in §1.5 exists precisely so the system
never trusts this table at runtime.

### 1.2 What is available and reliable

| Capability | Endpoint / field | Notes |
|---|---|---|
| Own profile + follower count | `GET /2/users/me`, `public_metrics.followers_count` | Point-in-time only. No history — we snapshot it ourselves. |
| Own posts | `GET /2/users/:id/tweets` | Up to 100 per page. 900 req/15min user-context (verify). |
| Public engagement | `public_metrics` — `like_count`, `reply_count`, `retweet_count`, `quote_count`, `bookmark_count`, `impression_count` | Available for any post. |
| Private performance | `non_public_metrics` — `impression_count`, `url_link_clicks`, `user_profile_clicks` | **Own posts only, OAuth 2.0 user context, last 30 days only.** |
| Organic breakdown | `organic_metrics` | Same 30-day + own-post + user-context restrictions. |
| Promoted breakdown | `promoted_metrics` | Only populated for promoted posts. Will be null for organic-only accounts. |
| Post content & entities | `text`, `entities`, `attachments`, `referenced_tweets`, `created_at`, `conversation_id` | Basis for topic classification and format detection. |

### 1.3 What is restricted, uncertain, or must be probed

| Capability | Status | Handling |
|---|---|---|
| Follower **list** (`GET /2/users/:id/followers`) | **Uncertain.** Was removed from Basic/Pro tiers; the April 2026 Owned Reads announcement lists `/followers` as qualifying for own-data pricing. Conflicting signals. | Runtime capability probe. If unavailable, audience analysis degrades to aggregate-only and the UI says so. Never assume. |
| Rate limits per endpoint | Vary by endpoint and billing mode | Read `x-rate-limit-remaining` / `x-rate-limit-reset` headers at runtime rather than hardcoding. |
| Legacy tier vs pay-per-use | Depends on your account | Config flag + probe; changes cost model entirely. |

### 1.4 What is genuinely NOT available — and what we do instead

This is the honest list. Nothing here will be faked.

| You asked for | Reality | Our approach |
|---|---|---|
| **Audience demographics** (age, gender, location, interests) | Not available in API v2. X removed audience analytics in 2020. Estimated demographics exist only in **Ads Manager / Ads API**, which requires a separate ad account and separate API access approval. | Marked **UNAVAILABLE** in the dashboard with an explanation. Optional Phase 8+ integration if you have Ads API access. We will not scrape it, and we will not estimate it from follower names or bios. |
| **Follower attribution** ("which post got me followers") | No follower-event stream exists. | Statistical attribution model (§4.4), labelled `INFERRED` with confidence intervals. |
| **Follower growth history** before go-live | No historical endpoint. | Starts accumulating from day 1. Dashboard shows an explicit "collection started" marker. |
| **X monetization / creator payouts** | **No public API.** Ads Revenue Share, Subscriptions and Tips have no developer endpoints. Payouts run through Stripe. | Three legitimate paths: (a) manual entry UI, (b) CSV import of payout statements, (c) optional read-only **Stripe API** integration against *your own* Stripe account, which is where X actually deposits creator payouts. All revenue rows are tagged with their source and provenance. |
| **Sponsorships / affiliate / brand deals** | Not X data at all | First-class manual entry + campaign model. This is user-entered data by definition and is labelled as such. |
| **Impressions for posts >30 days old** | Permanently unavailable from API | Served from our own snapshot history. If we weren't running then, the UI shows "not collected" — not zero, and not an estimate. |
| **Revenue per 1,000 impressions (RPM)** | Requires both revenue and impressions | Computable **only** where we hold both. Derived value, labelled as such, suppressed where inputs are missing. |

### 1.5 The capability probe

Because several of the above are uncertain and X changes access rules frequently, the
system does not hardcode what it can do. On account connection and weekly thereafter, a
probe runs one minimal request per capability, records the outcome in a
`account_capabilities` table, and drives both the collector and the UI from that table.

An endpoint that starts returning 403 next quarter degrades a dashboard panel to
"unavailable — access changed on <date>" and raises an alert. It does not silently produce
zeros. This is the difference between a system that ages gracefully and one that quietly
starts lying.

---

## 2. Provenance model — the core design primitive

Your brief says twice, in different words, *don't invent data and clearly distinguish
actual data from estimates.* The way to guarantee that structurally, rather than by
discipline, is to make provenance a non-nullable column on every metric-bearing row and
to render it in the UI.

Every value in the system carries one of:

| Provenance | Meaning | UI treatment |
|---|---|---|
| `MEASURED` | Returned directly by the X API | Plain value |
| `DERIVED` | Deterministic arithmetic on measured values (e.g. engagement rate) | Plain value, formula on hover |
| `INFERRED` | Statistical model output (e.g. follower attribution) | Shown with confidence interval + "modelled" badge |
| `USER_ENTERED` | You typed it (revenue, sponsorships) | "Entered by you" badge |
| `IMPORTED` | From CSV or Stripe | Source + import timestamp |
| `UNAVAILABLE` | Cannot be obtained | Explanation, never `0` |

Rules enforced in code:
- `UNAVAILABLE` and `NULL` are distinct from `0`. Charts break the line; they do not plot zero.
- Any aggregate mixing provenances inherits the weakest one.
- The LLM is given provenance in its input and is instructed — and evaluated — on
  preserving it in its output claims.

---

## 3. Technology stack

Recommendation, with reasoning where the choice is live.

| Layer | Choice | Why |
|---|---|---|
| Backend | **Python 3.12 + FastAPI** | You offered Python or Node. Python wins here because the workload is statistical — time-series resampling, baseline modelling, regression-based attribution — where `pandas`/`numpy`/`statsmodels` are the difference between fifty lines and five hundred. Async FastAPI handles the API-client side fine. |
| Frontend | **Next.js 15 (App Router), React, TypeScript, Tailwind** | As specified. Server Components for dashboard reads keeps tokens server-side. |
| Charts | **Recharts** | Sufficient for this; avoids a heavy viz dependency. |
| Database | **PostgreSQL 16** | As specified. |
| Migrations | **Alembic** | Every schema change versioned from the start. |
| Jobs | **Celery + Redis**, `celery-beat` for schedules | As specified. Redis doubles as rate-limit token bucket and cache. |
| LLM | **Claude API** (`claude-opus-5` for weekly/monthly analysis, `claude-sonnet-5` for daily) with **structured outputs / tool-schema-constrained JSON** | Tiered by cadence to control cost. Structured output is mandatory — free-text analysis is unparseable and unverifiable. |
| Auth (you → app) | Session cookies, Argon2id password hashing, optional TOTP | Separate from X OAuth. |
| Auth (app → X) | **OAuth 2.0 Authorization Code + PKCE**, scopes `tweet.read users.read offline.access` (+ `like.read bookmark.read follows.read` as probes permit) | `offline.access` is required for a refresh token; without it tokens expire in ~2h and unattended collection is impossible. |
| Secrets | Env vars; token encryption at rest via **AES-256-GCM** with a key from env/KMS | Tokens never reach the browser. |
| Deploy | Docker Compose (dev + single-host prod) | As specified. |
| Observability | `structlog` JSON logs, Prometheus metrics, Sentry | Cost and rate-limit counters are first-class metrics. |

**A deliberate non-choice:** no Kafka, no TimescaleDB, no vector database, no microservices.
This is a single-account (or small multi-tenant) system whose largest table will hold
maybe low millions of snapshot rows after years. Plain Postgres with correct indexes and
monthly partitioning on the snapshot tables handles it comfortably. Adding distributed
infrastructure here would be cost and operational burden with no benefit.

---

## 4. System architecture

### 4.1 Components

```
┌─────────────────────────────────────────────────────────────────┐
│  Next.js dashboard (TS/Tailwind)                                │
│  Overview · Content · Audience · Revenue · AI Insights · Agent  │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTPS, session cookie
┌────────────────────────▼────────────────────────────────────────┐
│  FastAPI                                                        │
│  REST · OAuth callback · approval endpoints · SSE for live agent│
└──┬──────────────┬──────────────┬──────────────┬─────────────────┘
   │              │              │              │
┌──▼───────┐ ┌───▼────────┐ ┌───▼─────────┐ ┌──▼──────────────┐
│ Collector│ │ Analytics  │ │ Agent core  │ │ Notifier        │
│ (X API)  │ │ engine     │ │ (LLM)       │ │ email/push/slack│
└──┬───────┘ └───┬────────┘ └───┬─────────┘ └──┬──────────────┘
   │             │              │              │
   │  ┌──────────▼──────────────▼──────────────▼────┐
   │  │  PostgreSQL  (system of record)             │
   │  └─────────────────────────────────────────────┘
   │  ┌─────────────────────────────────────────────┐
   └──┤  Redis: Celery broker · rate-limit buckets  │
      │         · cost ledger counters              │
      └─────────────────────────────────────────────┘
```

### 4.2 The X API client layer

Every call goes through one client that owns, in order:

1. **Capability check** — is this endpoint known-available for this account?
2. **Cost pre-authorization** — estimated resource count against the monthly budget. Over
   budget ⇒ the job is deferred and an alert raised, never silently dropped.
3. **Rate limiting** — Redis token bucket per endpoint per account, primed from
   `x-rate-limit-*` response headers rather than hardcoded numbers.
4. **Request** with automatic OAuth refresh on 401.
5. **Retry** — exponential backoff with jitter on 429/5xx; respects `Retry-After`.
6. **Ledger write** — every call records endpoint, resources returned, estimated cost,
   latency, outcome. This is what makes cost observable rather than a monthly surprise.

### 4.3 Collection schedule — designed around the 30-day cliff

Snapshot frequency decays with post age, because engagement velocity is front-loaded and
because every snapshot costs money:

| Post age | Frequency | Snapshots | Rationale |
|---|---|---|---|
| 0–24h | Hourly | 24 | Velocity curve; this window determines whether a post is a breakout |
| 1–7d | Every 6h | 24 | Long-tail accumulation |
| 7–29d | Daily | 22 | Slow drift |
| **Day 29** | **Mandatory final freeze** | 1 | **Last chance before impressions vanish forever** |

Account-level follower count: **hourly**, permanently. It is one resource per call
(~$0.72/month) and hourly resolution is what makes the attribution model in §4.4 possible
at all. Under-sampling here would be a false economy.

**Worked cost estimate** (pay-per-use, Owned Reads at $0.001/resource):

```
posts in the rolling 30-day window × ~71 snapshots each × $0.001
  5 posts/day  → ~150 posts →  ~10,650 reads → ~$10.65/month
  2 posts/day  →  ~60 posts →   ~4,260 reads →  ~$4.26/month
 10 posts/day  → ~300 posts →  ~21,300 reads → ~$21.30/month
+ hourly follower snapshots                    →  ~$0.72/month
```

Note that billing is per **resource returned**, not per request, so batching 100 posts
per page saves rate limit but not money. The lever that actually controls cost is snapshot
frequency — which is exactly what the governor exposes as a tunable, with a hard monthly
ceiling and a degradation ladder (drop to daily → drop to freeze-only → halt + alert).

### 4.4 Analytics engine

Deterministic, testable, no LLM involved. The LLM interprets these outputs; it never
computes them.

- **Engagement rate.** Multiple denominators, because the "right" one is contested:
  per-impression (preferred where impressions exist), per-follower (fallback), and
  per-reach-proxy. Always labelled with which denominator was used.
- **Baseline & anomaly detection.** Rolling median with MAD-based bands over follower
  delta and engagement, plus day-of-week seasonality. Robust statistics, not mean/σ —
  one viral post would poison a mean-based baseline for weeks. Anomalies are what drive
  the "unusual growth" alerts.
- **Post performance percentile.** Each post scored against *this account's* trailing
  distribution, not global benchmarks. Breakout = sustained >p95.
- **Follower attribution (INFERRED).** The interesting one. Hourly follower deltas form a
  signal; each post contributes a decaying response curve from its publication time.
  Regress the observed delta series on those overlapping curves (non-negative ridge
  regression) to apportion excess-over-baseline growth across posts. Output is a point
  estimate **plus a confidence interval**, and the interval widens sharply when posts
  overlap in time or when the account's baseline is noisy. When the model can't separate
  contributions, it says so rather than picking a winner.
- **Topic classification.** LLM-assigned labels from a controlled, user-editable taxonomy
  (not free-form, or it drifts and cross-period comparison breaks). Cached per post;
  reclassified only when the taxonomy changes.
- **Format detection.** Deterministic: thread vs single, has-media, has-link, has-poll,
  length bucket, question vs statement. Cheap and highly predictive.
- **Posting-time analysis.** Performance by (weekday × hour) in your local timezone,
  with sample-size gating — no recommending 3am Tuesday off two posts.
- **Revenue analytics.** Revenue per post/campaign/source, monthly totals, growth, and
  RPM where and only where impressions exist.

### 4.5 Agent architecture

Not a chatbot. A durable state machine with persisted runs, executing your loop:

```
OBSERVE   → collectors write snapshots; agent reads what changed since last run
COLLECT   → fill gaps, retry failures, run the capability probe if stale
ANALYZE   → deterministic analytics engine (§4.4) produces a structured findings packet
REASON    → Claude, given the packet + provenance, produces structured hypotheses
RECOMMEND → concrete, ranked, falsifiable actions with expected effect
ACT       → tiered autonomy (below)
VERIFY    → each recommendation is a testable prediction; a later run checks whether it
            held and records the outcome
REPORT    → daily / weekly / monthly artifacts + alerts
```

**The VERIFY stage is what makes this agentic rather than generative.** Every
recommendation is stored with a prediction and a check date. Later runs grade it
(`CONFIRMED` / `REFUTED` / `INCONCLUSIVE`). Those grades are fed back into subsequent
prompts, so the agent is accountable to its own track record and you can see, on the
Agent Activity page, whether its advice has actually been any good. Without this, an
"AI insights" panel is just confident-sounding text.

**Tiered autonomy.** Every action type has a level, and the level is enforced in code, not
by prompting:

| Tier | Meaning | Examples |
|---|---|---|
| **T0 — Automatic** | No approval. Read-only or internal. | Collect, analyze, classify, generate insights, send reports and alerts |
| **T1 — Approval required** | Queued; expires in 24h if unapproved | Posting, replying, scheduling a post |
| **T2 — Explicit confirmation + re-auth** | Typed confirmation | Anything spending money; changing collection budget upward |
| **T3 — Forbidden** | Not implemented at all | Deleting posts, changing account settings, following/unfollowing, DMs, anything irreversible |

T3 is enforced by the OAuth scopes we request: without `tweet.write` or `follows.write`
in the token, those actions are impossible even if every other layer is compromised.
**Default posture for Phase 6 is read-only** — T1 write capability is opt-in and ships
disabled.

**Prompt injection defence.** This matters more than usual here, because the agent
ingests text that strangers wrote — replies and quote-posts on your content are attacker-
controlled input. Defences:
- All third-party text is wrapped in explicit untrusted-content delimiters with standing
  instructions that content inside is data to analyze, never instructions to follow.
- The LLM has **no tool access** in the reasoning stage. It returns structured JSON that
  a deterministic executor validates against a schema and an allowlist. An injected
  "post this tweet" cannot become an action, because reasoning and acting are separate
  processes and the executor only accepts enumerated action types.
- Any T1+ action originating from a run that ingested third-party text is flagged for
  human review regardless of tier.
- Injection attempts are logged and alerted, not silently dropped — they're a signal.

---

## 5. Database design

PostgreSQL. `snake_case`, UUID v7 primary keys (time-sortable), `TIMESTAMPTZ` everywhere,
soft deletes on user data. Full DDL and Alembic migrations land in Phase 2; this is the
model and the reasoning.

### Identity & access
- **`users`** — app users. `email`, `password_hash` (Argon2id), `totp_secret_enc`, `role`
  (`OWNER`/`ADMIN`/`VIEWER`), `timezone`. *Never stores an X password — the OAuth flow
  means we never see one.*
- **`x_accounts`** — connected X accounts. `x_user_id` (immutable, the real key — handles
  change), `username`, `connected_at`, `is_active`.
- **`oauth_tokens`** — `access_token_enc`, `refresh_token_enc` (AES-256-GCM), `scopes[]`,
  `expires_at`, `last_refreshed_at`. Encrypted at rest, never serialized to any API
  response. Separate table from `x_accounts` so it can carry tighter access control.
- **`account_capabilities`** — the probe results from §1.5. `capability`, `status`,
  `last_checked_at`, `last_error`. Drives collector and UI.

### Content & metrics
- **`posts`** — `x_post_id`, `text`, `posted_at`, `post_type` (original/reply/quote/repost),
  `conversation_id`, `has_media`, `has_link`, `is_thread`, `thread_position`, `lang`,
  `raw_payload` (JSONB, for reprocessing without re-fetching — which matters when reads
  cost money).
- **`post_metric_snapshots`** — **append-only time series.** `post_id`, `captured_at`,
  `post_age_hours`, all public/non-public/organic metric fields, `provenance`,
  `is_final_freeze`. *Never updated in place.* Partitioned monthly by `captured_at`.
  Unique on `(post_id, captured_at)`. This table is the irreplaceable asset — it is the
  only place 31-day-old impressions will ever exist.
- **`post_metrics_current`** — materialized latest-snapshot-per-post view. Dashboards read
  this; it keeps the hot path off the partitioned history.
- **`account_metric_snapshots`** — hourly `followers_count`, `following_count`,
  `post_count`, `listed_count`. Same append-only discipline. The substrate for growth
  analysis and attribution.

### Classification
- **`topics`** — user-editable controlled taxonomy. `name`, `description`, `is_active`.
- **`post_topics`** — many-to-many with `confidence` and `classified_by` (model version),
  so a taxonomy or model change is traceable rather than silently rewriting history.

### Revenue (all `USER_ENTERED` or `IMPORTED` — never from X)
- **`revenue_sources`** — `X_ADS_SHARE`, `X_SUBSCRIPTIONS`, `TIPS`, `SPONSORSHIP`,
  `AFFILIATE`, `BRAND_DEAL`, `OTHER`.
- **`campaigns`** — `name`, `sponsor`, `start_date`, `end_date`, `contracted_amount`,
  `currency`, `status`.
- **`sponsorships`** — deal-level detail, deliverables, linked to `campaigns`.
- **`revenue_entries`** — `amount_minor` (integer minor units — never floats for money),
  `currency`, `earned_at`, `source_id`, `campaign_id?`, `post_id?` (nullable — much
  revenue isn't attributable to one post), `provenance`, `external_ref` (Stripe payout id),
  `notes`.
- **`revenue_attributions`** — explicit, auditable split of one entry across several posts
  with a stated method, rather than a hidden heuristic.

### Agent
- **`agent_runs`** — one row per loop execution. `run_type`, `trigger`, `status`,
  `started_at`, `finished_at`, `stages_completed`, `llm_tokens`, `llm_cost_minor`, `error`.
- **`agent_tasks`** — durable work items with retry state, so a crash resumes rather than
  restarts.
- **`insights`** — structured LLM findings. `category`, `title`, `body`, `severity`,
  `confidence`, `supporting_data` (JSONB — the exact figures cited, enabling verification
  that the model didn't hallucinate a number), `provenance_summary`.
- **`recommendations`** — `action_text`, `rationale`, `expected_effect`, `priority`,
  `predicted_outcome` (JSONB), `verify_after`, `status`, `verification_result`,
  `verified_at`. The accountability loop from §4.5.
- **`agent_actions`** — proposed/approved/executed actions. `action_type`, `tier`,
  `payload`, `status`, `approved_by`, `approved_at`, `executed_at`, `result`,
  `ingested_untrusted_content` (bool). Immutable audit trail.
- **`alerts`** — `alert_type`, `severity`, `title`, `body`, `triggered_by`, `data`,
  `acknowledged_at`, `delivered_channels[]`.

### Operations
- **`api_usage_ledger`** — per-call `endpoint`, `resources_returned`, `estimated_cost_minor`,
  `billing_mode`, `status_code`, `latency_ms`, `rate_limit_remaining`. Drives the governor
  and makes spend auditable to the request.
- **`audit_logs`** — every privileged action: who, what, when, from where, before/after.
- **`system_logs`** — structured errors and warnings surfaced on the Agent Activity page.
- **`reports`** — generated daily/weekly/monthly artifacts with their content snapshot, so
  a historical report renders identically forever even after models and code change.

### Indexing highlights
`posts(x_account_id, posted_at DESC)` · `post_metric_snapshots(post_id, captured_at DESC)`
+ monthly partitions · `account_metric_snapshots(x_account_id, captured_at DESC)` ·
`revenue_entries(x_account_id, earned_at DESC)` and `(campaign_id)` ·
`agent_actions(status, tier)` for the approval queue · partial index on
`recommendations(verify_after) WHERE status='PENDING_VERIFICATION'` ·
`api_usage_ledger(x_account_id, created_at DESC)` for budget windows.

---

## 6. Security

| Concern | Approach |
|---|---|
| X credentials | OAuth 2.0 + PKCE only. No password is ever seen, requested, or stored. |
| Token storage | AES-256-GCM at rest, key from env/KMS, never in any API response, never in frontend bundles or Server Component props. All X calls are server-side. |
| Least privilege | Request only read scopes by default. Write scopes are opt-in and make T3 actions technically impossible when absent. |
| App auth | Argon2id, secure/httpOnly/SameSite cookies, optional TOTP, short sessions with rotation. |
| Authorization | RBAC (`OWNER`/`ADMIN`/`VIEWER`); every query scoped by account ownership at the repository layer, not per-endpoint (which is where these bugs come from). |
| Input validation | Pydantic on every boundary; parameterized queries throughout. |
| Rate limiting | Inbound per-IP and per-user limits; outbound token buckets to X. |
| Prompt injection | §4.5 — delimiting, no tool access during reasoning, schema+allowlist validation, flagging, logging. |
| Unauthorized agent actions | Tiered autonomy enforced in the executor, approval queue with expiry, full audit trail, forbidden tier unimplemented. |
| Spend | Hard monthly ceiling on API cost, LLM token budget per run, degradation ladder, alert before halt. |
| Secrets | Env vars only; `.env.example` committed, `.env` never; pre-commit secret scanning. |
| Audit | Append-only `audit_logs` for all privileged actions. |

---

## 7. Delivery plan

Each phase leaves the repo runnable, migrated, and tested.

| Phase | Deliverable | Runnable state at the end |
|---|---|---|
| **1** | This document | — |
| **2** | Repo structure, Docker Compose, FastAPI skeleton, Postgres + Alembic, app auth, Next.js shell | `docker compose up` → login works, empty dashboard |
| **3** | X OAuth 2.0 + PKCE, API client (rate limit, retry, cost ledger), capability probe | Connect your X account; see live profile + capability matrix |
| **4** | Collectors, Celery beat schedule, snapshot pipeline, backfill-what's-possible, cost governor | Data accumulating on schedule; spend visible |
| **5** | Analytics engine: engagement, baselines, anomalies, topics, formats, timing, attribution | Real metrics on the dashboard |
| **6** | Agent loop, Claude structured outputs, insights, recommendations, verification, approval queue | Agent runs and reasons; read-only |
| **7** | Full dashboard: all six sections | Complete UI |
| **8** | Alerts + daily/weekly/monthly reports, delivery channels | Scheduled reports arriving |
| **9** | Test hardening, security review, deployment guide, runbook | Production-ready |

Phase 4 is the one to reach quickly. Until collectors run, the impression dataset isn't
accumulating — and that data cannot be recovered retroactively.

---

## 8. What I need from you before Phase 2

Only the items that actually change the build:

1. **X API access state.** Do you have a developer account and app already? Are you on
   pay-per-use, or a legacy Basic/Pro subscription? This determines which endpoints exist
   and how the cost model is configured.
2. **Monthly API budget ceiling.** Governor default — I'll assume **$25/month** unless
   told otherwise.
3. **Account size.** Approximate follower count and posts/day, to size the schedule.
   I'll assume ~5 posts/day.
4. **Autonomy posture.** Read-only + recommendations only, or should T1 (post/reply with
   your approval) be built now? Default is read-only.
5. **Tenancy.** Just you, or multi-user from the start? Default: single-tenant schema that
   is multi-tenant-ready.
6. **Monetization inputs.** Manual entry only, or also CSV import and/or read-only Stripe?
7. **Alert channels.** Email, Slack, push, or dashboard-only.

Sensible defaults exist for all seven, so none of them blocks starting Phase 2 — but 1, 4
and 5 are the ones where a wrong assumption costs rework.

---

## 9. Decisions taken (answers to §8)

| # | Decision | Consequence for the build |
|---|---|---|
| 1 | **X API access: unknown** | Design for pay-per-use (the only option for new developers since Feb 2026). The Phase 3 capability probe determines the true state on connection; billing mode is a config value, not an assumption. |
| 2 | API budget | Default ceiling **$25/month**, configurable. Governor degradation ladder as §4.3. |
| 3 | Account size | Assume ~5 posts/day for schedule sizing; tunable. |
| 4 | **Autonomy: posting with approval (T1)** | We request `tweet.write` **in addition to** the read scopes. Posting is drafted by the agent, queued, and published only on explicit human approval with 24h expiry. T2 and T3 restrictions are unchanged — no spending, no deletion, no follows, no DMs. |
| 5 | **Tenancy: single user, one X account** | Single-tenant deployment on a multi-tenant-ready schema (`user_id`/`x_account_id` scoping is present from the start so it never needs retrofitting). RBAC kept minimal. |
| 6 | **Revenue: CSV import** | CSV import with column mapping and dedupe is the primary ingest path. A minimal manual entry/edit form ships alongside it — imported rows need correction, and sponsorship/brand-deal revenue has no statement to import. |
| 7 | Alert channels | Default to email + dashboard; channel adapters are pluggable in Phase 8. |

### Security consequences of decision 4

Granting `tweet.write` is the single largest increase in blast radius in this design, so
the compensating controls are explicit:

- The write scope is **requested but disabled by default** via a config flag. Enabling it
  is a deliberate, audited action, not a deployment default.
- No post is ever published without a human approving that specific draft. Approvals do
  not batch and do not carry over between drafts.
- Approvals expire after 24h, so a stale queue cannot publish something later made
  irrelevant or wrong by events.
- Any draft produced by a run that ingested third-party text (replies, quote-posts) is
  flagged as such in the approval UI — that is the prompt-injection path, and it is
  surfaced rather than hidden.
- Every publish is written to `agent_actions` and `audit_logs` with the approving user,
  the source run, and the exact payload.
- `tweet.moderate.write`, `follows.write`, `like.write` and DM scopes are **never**
  requested, so T3 actions remain technically impossible regardless of any other failure.

---

## 10. Phase 6 as built — where the implementation refines this document

Four things were decided during implementation that this document did not settle, and one
was tightened.

**The untrusted-content flag covers all unauthored text, not only third-party text.**
§9 flags drafts produced from runs that ingested "replies or quote-posts". As built, the
flag is set whenever *any* free text this system did not author enters the prompt — which
today means the account's own post text. The vector is not really "whose account posted
it": a transcribed screenshot, a pasted reply, or a compromised account all arrive as your
own post text. Replies and quote-posts, when Phase 8 ingests them, then need no change
here. The flag never blocks a T0 action (a note with no outward effect is not a risk); it
marks the run and every action from it, and the approval screen shows it.

**Publishing cannot be reached by a single model output.** `PUBLISH_POST` is absent from
the set of actions the structured-output schema allows the model to propose. Publishing is
reachable only by a human promoting a draft. This is narrower than §9's "drafted by the
agent, queued, published on approval" and costs nothing, since the draft still comes from
the agent.

**Insights carry verified figures, not quoted ones.** §2's provenance model says values
declare where they came from. The agent adds a second check: an insight must cite its
figures by key *and* value, the pair is compared against the run's evidence bundle, and a
mismatch discards the whole item rather than trimming the bad citation. The stored value is
then read from the evidence, never from the model's reply. Rejections are counted per run —
a rising count is a real signal about the model or the prompt.

**Writes get their own cost class.** X documents read pricing in detail and post creation
far less clearly. Rather than assert a rate the ledger could not support, `CostClass.WRITE`
bills at `X_COST_WRITE_MICROS`, which defaults to zero and is set from your invoice. The
ledger records the call either way.

**Verification refuses to grade advice you dismissed.** §4.5 describes recommendations as
falsifiable predictions graded later. As built, a dismissed recommendation grades
INCONCLUSIVE, never REFUTED — scoring unfollowed advice would corrupt the feedback loop in
whichever direction the account happened to move. Grading also runs on its own schedule,
separate from the daily cycle, so accountability for past advice does not depend on the
reasoning step being available today.

---

## 11. Phase 7 as built — the dashboard

The interesting decisions in this phase are all about the same thing: charts want dense
arrays, and this data is not dense.

**Null is a value the chart renders, not a value it fills in.** `components/charts.tsx`
takes `(number | null)[]`. A null breaks the line, shades a hatched band over the missing
span, and draws an empty slot rather than a zero-height bar. Every charting library would
have wanted the holes filled first, which is why there is no charting library here — a
flat line across a collection outage is a worse lie than a visible gap, because it looks
like data.

**The series endpoint walks the calendar, not the rows.** `/analytics/{id}/series` iterates
every date in the window and reports `observed: false` for dates with no snapshot. The day
*after* a gap also reports a null change: attributing two days of growth to one day would
manufacture a spike. §4.4's "gaps render as gaps" is enforced here rather than left to the
client.

**The headline row is one query.** `/analytics/{id}/summary` exists because six independent
requests can disagree with each other when the collector writes between two of them, and a
dashboard whose tiles contradict each other is worse than a slow one.

**Two functions written in Phase 5 finally have callers.** `compute_seasonality` and the
weekday profile were built with the analytics engine and left unused until there was a
surface for them. They now back the Audience page's weekday rhythm, split into separate
follower and engagement profiles rather than one combined figure.

**Topic performance is marked INFERRED, formats are DERIVED.** Both are group comparisons
that look identical on screen, so the provenance badge is doing real work: formats are
mechanical facts about a post, topics are model output with classification error, and the
share of posts still unclassified is stated next to the comparison.

---

## 12. Phase 8 as built — alerts and reports

§4.5 and §9 called for alerts on growth, engagement, revenue and suspicious activity, with
email and dashboard channels. As built, four things are sharper than that description.

**Alert fatigue is treated as the primary failure mode.** Dedupe keys, per-rule cooldowns
and a severity floor are in the schema, not in the detectors, and the dedupe key is a unique
constraint so the guarantee does not depend on the service remembering to check. Most of
the detector code is refusal logic.

**Stateful and point-in-time rules are distinguished.** Operational conditions
(collection stopped, budget exhausted, access degraded, freeze at risk) end, and close
themselves when the detector stops seeing them. Statistical observations do not end and
never resolve. Conflating the two produces either a banner that sticks or a problem that
can be clicked away while it is still happening — so `acknowledge` deliberately does not
resolve.

**"Suspicious activity" is scoped to what the data supports.** X exposes no follower-event
stream, so `UNUSUAL_CHURN` reports the timing of an unusual net loss and states in its body
that a bot purge, a bad post and a compromised account are indistinguishable from here.
Claiming to detect compromise would have been the dishonest option.

**No model is involved.** §4.5 places alerting after the agent in the loop, which invited
using the agent to write them. Alerts run on their own 30-minute schedule with no LLM call,
so they keep working when the Anthropic key is missing, the budget is exhausted or the
model is refusing — which are precisely the conditions under which you most want to hear
from the system.

**Reply and quote-post ingestion is deferred, not forgotten.** Earlier notes anticipated it
landing here as the first genuinely attacker-controlled text. It is a collection feature
rather than an alerting one: it costs money per resource, needs a mention-timeline endpoint
this registry does not yet declare, and none of the ten rules needs it. The prompt fencing
built in Phase 6 is already in place for when it arrives.

---

## Sources

- [X API pricing update: Owned Reads $0.001, effective April 20 2026 — X Developers](https://devcommunity.x.com/t/x-api-pricing-update-owned-reads-now-0-001-other-changes-effective-april-20-2026/263025)
- [X API pay-per-usage pricing and credits — docs.x.com](https://docs.x.com/x-api/getting-started/pricing)
- [Metrics — docs.x.com](https://docs.x.com/x-api/fundamentals/metrics)
- [X API Rate Limits — docs.x.com](https://docs.x.com/x-api/fundamentals/rate-limits)
- [OAuth 2.0 Authorization Code Flow with PKCE — docs.x.com](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code)
- [Non-public metrics for tweets older than 30 days — X Developers](https://devcommunity.x.com/t/non-public-metrics-for-tweets-older-than-30-days/173971)
- [API v2 Followers List endpoint access issue with Basic plan — X Developers](https://devcommunity.x.com/t/api-v2-followers-list-endpoint-access-issue-with-basic-plan/233381)
- [X (Twitter) API pricing in 2026: all tiers — Postproxy](https://postproxy.dev/blog/x-api-pricing-2026/)
- [X (Twitter) API in 2026: pricing, rate limits — SocialCrawl](https://www.socialcrawl.dev/blog/x-twitter-api-2026)
- [Twitter (X) API demographics — Phyllo](https://www.getphyllo.com/post/twitter-api-demographics-understanding-your-audience)
- [Creator Revenue Sharing — X Help](https://help.x.com/en/using-x/creator-revenue-sharing)
- [X Creator Monetization 2026: revenue share + payout rules](https://www.auditsocials.com/blog/x-creator-monetization-standards-2026)
