# Autonomous SWE Agent — Master Plan & Source of Truth

> **Status:** ACTIVE — this is the single source of truth · **Version:** 2.0 · **Date:** 2026-07-02 · **Owner:** Nadeem
>
> **This document supersedes `plan.md`.** It folds the entire original design
> into one place, updates every section to reflect what is actually built and
> what a 2026-07-02 deep-scan audit changed, and records the security hardening
> now in the code. If you read only one file to understand this project, read
> this one. `plan.md` is retained only as the historical v0.1 draft.
>
> **Conventions:** `[DECISION]` = needs a human call before it's final;
> `[ASSUMPTION]` = current default, override if wrong; `MUST`/`SHOULD`/`MAY` =
> requirement strength. Build state is marked **(built)**, **(next)**, or
> **(not started)** throughout.

---

## 0. Document control

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-06-28 | Initial full design draft (`plan.md`). |
| 2.0 | 2026-07-02 | **Merged into this single source of truth.** Absorbed all of plan.md; updated statuses to real build state; integrated the deep-scan audit — security hole H1, robustness bugs, correctness/hygiene fixes (all applied, §17); rewrote the security model (§9) to document the hardening now in code. |

---

## 1. What we are building (one paragraph)

A production-grade service that watches one or more GitHub repositories, and
when an actionable issue appears, autonomously: clones the repo, builds a
semantic + structural understanding of the codebase, drives an LLM through a
Reason→Act→Observe loop that reads code, edits files, and runs tests inside a
hardened sandbox, and — if it produces a verified fix — commits the change to a
branch and opens a pull request that references the issue. The entire path from
"issue opened" to "PR opened" runs without a human in the loop, under hard caps
on time, tokens, and money, with full tracing of every step.

## 2. Goals and non-goals

**Goals**
- **G1. End-to-end autonomy:** webhook → PR, no human step.
- **G2. Safety first:** untrusted repo code and untrusted issue text must never
  compromise the host, leak secrets, or reach the network from the execution
  sandbox. *(This goal drove the audit's top finding — see §9 / §17 H1.)*
- **G3. Correctness gating:** the agent only opens a PR when its change exists
  and the repo's tests (or a defined check) pass; otherwise it reports why and
  stops.
- **G4. Cost/time bounded:** every run has hard ceilings (steps, tokens, USD,
  wall-clock) and is observable in real time.
- **G5. Model-pluggable:** the reasoning core is not wedded to one vendor
  (Anthropic **or** Google), selectable by config.
- **G6. Idempotent & re-runnable:** re-processing the same issue updates the
  same branch/PR rather than duplicating work.

**Non-goals (for this document)**
- **N1.** The web frontend's internal design (components, state, styling). We
  define only the **API contract** it consumes (§3, §7.5).
- **N2.** Monorepo-scale indexing optimizations beyond what §6.4 specifies.
- **N3.** Fine-tuning or training any model. We use hosted LLMs + a local
  embedding model only.
- **N4.** Supporting languages beyond the agreed initial set (`[DECISION D1]`,
  §16 — default Python-first).

## 3. System architecture (the six layers)

```
                                   ┌─────────────────────────────────────────────────────────┐
   GitHub  ──webhook──▶  (1) API   │  FastAPI receiver: verify HMAC, parse event, enqueue job │
   (issues, PRs)        Gateway    └───────────────┬─────────────────────────────────────────┘
                                                   │ enqueue (Celery → Redis broker)
                                                   ▼
                                   ┌─────────────────────────────────────────────────────────┐
                        (2) Queue  │  Redis broker + Celery workers (the orchestrator runs    │
                        & Workers  │  here; one job = one issue run)                          │
                                   └───────────────┬─────────────────────────────────────────┘
            ┌──────────────────────────────────────┼───────────────────────────────────────────┐
            ▼                                       ▼                                            ▼
 ┌────────────────────┐   ┌──────────────────────────────────┐   ┌────────────────────────────────────┐
 │ (3) Codebase       │   │ (4) Reasoning core (ReAct loop)  │   │ (5) Sandbox (Docker)               │
 │  understanding     │   │  • LLM provider (Claude/Gemini)  │   │  • ephemeral container per run     │
 │  • tree-sitter AST │◀──│  • Budget controller             │──▶│  • no network, ro host, cpu/mem cap│
 │  • embeddings      │   │  • tools: retrieve/read/edit/test│   │  • runs tests / untrusted code     │
 │  • ChromaDB vectors│   └──────────────────────────────────┘   └────────────────────────────────────┘
 └─────────┬──────────┘                    │
           │ vectors                       │ trace, status, usage
           ▼                               ▼
 ┌────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ (6) State & Observability:  PostgreSQL (runs, steps, PRs)  ·  Redis (broker/cache/rate-limit)        │
 │                              Object store (workspaces, logs, diffs)  ·  Prometheus/Grafana/OTel      │
 └────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                   │ git push + REST
                                                   ▼
                                                GitHub  (branch + Pull Request)
```

Each layer is independently testable and deployable. Layers 1–2 are stateless
request handlers + a queue; the heavy lifting (3,4,5) happens inside a worker
process; layer 6 is shared infrastructure.

**Current build state of the six layers:** Layer 3 (`agent/retrieval.py`),
Layer 4 (`agent/loop.py` + `agent/providers/`), the GitHub half of Layer 1
(`agent/github.py`), and Layer 5 (`agent/sandbox.py`) are **built**. The
orchestrator that runs inside Layer 2, the FastAPI half of Layer 1
(`app/main.py`), and all of Layer 6 are **not started**. See §15 for the file
map and §18 for the build order.

## 4. The life of an issue (end-to-end data flow)

The canonical sequence. Bracketed numbers cite the component (§6) and store (§7).

1. **Issue opened on GitHub.** GitHub POSTs an `issues` webhook to
   `POST /webhooks/github` [API §6.1].
2. **Verify + parse.** The API verifies the `X-Hub-Signature-256` HMAC against
   the configured secret, parses the payload (`parse_webhook_event`), and
   decides if it is actionable (opened/reopened/labeled, not a PR, not a bot
   author). Non-actionable → `204`, no work. Actionable → it writes a `runs`
   row (status `queued`) to **PostgreSQL** [§7.1], enqueues a Celery task
   carrying the `run_id` onto the **Redis broker** [§7.2], and returns `202
   Accepted`. *The API does no heavy work and holds no repo code.*
3. **A worker picks up the job.** A Celery worker [§6.2] claims the task. The
   worker is the **orchestrator** — it runs the whole pipeline for this one
   issue and updates the `runs` row to `running`. It posts an optional "🤖 on
   it" comment back on the issue.
4. **Fetch the code.** The orchestrator clones the target repo into a
   **per-run workspace**: `${WORKSPACE_ROOT}/${run_id}/repo` [§7.4]. Auth uses
   the installation/PAT token; the token is scrubbed from `.git/config`
   immediately after clone (never persisted). The default branch is read from
   GitHub; a deterministic working branch `agent/issue-<n>` is created off it.
   *This directory is the single working copy for the run.*
5. **Understand the code.** The orchestrator runs the **retrieval/indexer**
   [§6.4]: tree-sitter parses files into AST-aware chunks, a **local embedding
   model** (sentence-transformers, in-process — no API call) embeds them, and
   vectors + metadata are written to a **ChromaDB collection keyed by repo**
   [§7.3], persisted on a volume so re-runs are incremental. An embedding cache
   keyed by `(content hash, embedder name)` avoids re-embedding unchanged
   chunks.
6. **Reason — where the LLM runs.** The orchestrator builds the task string
   from the issue (`Issue.to_task()`) and starts the **ReAct loop** [§6.5],
   which calls the **LLM provider** [§6.6]. **This API call originates from the
   worker/host process, NOT from inside the sandbox.** The sandbox has no API
   key and no network; the model itself never executes anywhere untrusted.
7. **Act — tool execution & the trust boundary.** When the model requests a tool:
   - `retrieve_context` → queries ChromaDB [§7.3] (host-side, read-only, safe).
   - `read_file` / `edit_file` / `list_dir` → operate on the workspace,
     path-confined to it (host-side; reading/writing files is not code
     execution).
   - `run_tests` (any "execute code") → **dispatched into the Docker sandbox**
     [§6.7, §10]: a container with the workspace mounted, **no network,
     read-only host FS, `.git` masked, non-root, resource/PID/time limits**,
     captures stdout/stderr/exit code. This is the only place untrusted repo
     code runs.
8. **Observe + loop.** Each tool result (clipped) is fed back to the model.
   Every step (model text, tool calls, args, results, token usage, cost) is
   appended to the **structured trace** and (once the orchestrator exists)
   persisted incrementally to **PostgreSQL** `run_steps` [§7.1]. The **Budget
   controller** [§6.5] checks step/token/USD caps before every model call; a
   wall-clock cap is enforced by the worker. Exhausting any cap stops the run
   cleanly with a status.
9. **Produce the result.** When the model declares completion (or a cap trips):
   - **No changes** in the working tree → status `no_changes`; comment the
     reason on the issue; **no PR**.
   - **Changes** → `git add -A && commit` (with hooks/fsmonitor disabled and
     `--no-verify` — §9), push the branch (`--force-with-lease`, token inline
     only), then open (or adopt) a **pull request** via REST [§6.1], body
     referencing `Closes #<n>` + a trace summary.
10. **Finalize.** Write the terminal status, PR number/URL, and usage totals to
    the `runs` row, emit metrics [§11], comment the PR link on the issue, and
    tear down the workspace + sandbox.

**One-line mental model:** *code is fetched into a per-run host workspace;
understanding and file edits happen host-side against that workspace; the LLM is
called from the worker (never the sandbox); only test/code execution is pushed
into a network-isolated Docker container with `.git` masked; durable state →
Postgres + object store, vectors → ChromaDB, queueing/caching → Redis.*

## 5. Why this shape (key architectural decisions)

- **The model runs on the host, the code runs in the sandbox.** Inverting this
  would either deny the model network (it needs the LLM API) or grant the
  sandbox network (defeating isolation). `[DECISION D2 — as designed]`
- **One job = one issue = one worker task.** Simplest correct concurrency unit;
  horizontal scale = more workers. No shared mutable state between runs except
  read-mostly infra.
- **Postgres for truth, Redis for transit, ChromaDB for vectors, object store
  for blobs.** Each store does one job (§7).
- **Idempotency via deterministic branch names + adoptive PR creation.**
  Re-running issue #42 updates `agent/issue-42` and its existing PR.
- **Provider abstraction in front of the LLM.** The loop depends on an
  interface, not a vendor SDK (§6.6).
- **The workspace is untrusted after the sandbox touches it.** The clone, the
  model's edits, *and any file the repo's own tests wrote* all live in one
  directory the orchestrator later runs `git` against. That makes host-side git
  a trust boundary, hardened in §9 — a lesson from the audit (§17 H1).

## 6. Component specifications

### 6.1 GitHub integration (`agent/github.py`) — **built**
- **Runs:** in the API (parsing/verification) and in the worker
  (clone/branch/commit/push, PR creation).
- **REST client (`GitHubClient`):** `get_issue`, `get_default_branch`,
  `comment_on_issue`, `find_pull_request`, `create_pull_request`. Pure stdlib
  (`urllib`) — no dependency. Bounded exponential backoff + jitter on
  429/5xx/network; 403 retried only when rate-limited (honors
  `Retry-After`/`X-RateLimit-Reset`); idempotent PR creation (422 → adopt
  existing). `GitHubError` carries the HTTP `status`. Tokens redacted from all
  errors/logs. `transport`/`sleep`/`now` are injectable so the client tests
  fully offline with a fake transport (no network, no mock library).
- **Git helpers:** clone (token scrubbed from config after clone), branch
  (`agent/issue-<n>`), commit (returns bool so empty PRs are skipped), push
  (`--force-with-lease`, token inline never persisted), local identity config.
  **All git calls are hardened** — see §9 (`_GIT_HARDENING`, `--no-verify`).
- **Webhook parsing:** `parse_webhook_event` with infinite-loop guards (skip
  PRs, bot authors, non-actionable actions).
- **Auth:** `[DECISION D3]` GitHub App (installation tokens — recommended for
  prod) **vs** PAT (simplest for dev). Default: App for prod, PAT for local.
- **Self-test:** `python -m agent.github` (offline, exit 0).

### 6.2 API gateway / webhook receiver (`app/main.py`, FastAPI) — **not started**
- **Runs:** stateless web pods behind a load balancer; only `/webhooks/*` needs
  to be public.
- **Endpoints:**
  - `POST /webhooks/github` — verify HMAC, parse, enqueue, `202`. Returns
    within ~1s; never blocks on agent work.
  - `GET /healthz`, `GET /readyz` — liveness/readiness.
  - `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/steps` — read API (§7.5).
  - `POST /runs` — manual trigger (`{repo, issue_number}`) for testing.
  - `GET /metrics` — Prometheus exposition.
- **Security:** constant-time signature compare; reject unsigned/oversized
  bodies; per-source rate limiting.

### 6.3 Queue & workers (Celery + Redis) — **not started (orchestrator = M1, next)**
- **Broker:** Redis (§7.2). **Result backend:** Postgres `[ASSUMPTION]` for
  durability.
- **Worker = orchestrator:** one Celery task `run_issue(run_id)` executes the §4
  pipeline. Concurrency = N replicas × M slots, bounded by sandbox capacity.
  Task-level `time_limit` (hard) + `soft_time_limit` (graceful) enforce
  wall-clock.
- **Retries:** infrastructure failures (clone blip, DB hiccup) retry with
  backoff; **agent-logic failures do not auto-retry** (re-running an LLM run
  blindly burns money) — they record a terminal status. `[DECISION D4 — no]`

### 6.4 Codebase understanding / retrieval (`agent/retrieval.py`) — **built**
- **Runs:** in the worker, in-process (CPU; GPU optional for embeddings).
- **Pipeline:** tree-sitter chunking (functions/methods/classes; decorated defs
  unwrapped; methods indexed individually + a class-header chunk to dodge
  embedding truncation; imports) → batched sentence-transformers embeddings →
  upsert into ChromaDB with **delete-by-file first** (no stale dupes) →
  `build_call_graph` for structural neighbors. `assemble_context(query, k,
  token_budget)` packs the most relevant chunks under a token budget for the
  loop.
- **Token counting:** provider-neutral local estimate (~3.5 chars/token) for
  packing; exact counts (budget-critical) come from the active provider's API.
  Never `tiktoken` (wrong for both vendors).
- **Store location:** ChromaDB collection per repo, persisted on a volume;
  incremental across runs. **Opened lazily** and **env-overridable**
  (`CHROMA_DIR`, `EMBEDDING_CACHE_DIR`) — see §17 M2. Chunk IDs and the
  delete-by-file filter are keyed on **absolute** file paths (§17 L1).
- **Self-test:** `python -m agent.retrieval` (needs the ML deps; run in the
  Linux/WSL venv).

### 6.5 Reasoning core — ReAct loop + Budget (`agent/loop.py`) — **built**
- **Runs:** in the worker (host side).
- **Loop:** `while not done and within budget: provider.complete(...) → if tool
  calls: dispatch (Act) → feed results (Observe); else finish`.
- **Tools (the model's only way to touch the repo):** `retrieve_context`,
  `read_file`, `edit_file` (exact-string replace / create), `run_tests`,
  `list_dir`. All file paths confined to the workspace (`_safe_path` blocks
  `../` traversal); `run_tests` routed to the sandbox when one is supplied
  (§6.7), otherwise run host-side against the workspace's own venv.
- **Budget controller:** hard caps `max_steps`, `max_total_tokens`, `max_usd`,
  read from `.env` (`MAX_STEPS`/`MAX_TOTAL_TOKENS`/`MAX_USD`); checked before
  every model call, usage accrued after. Worker adds a wall-clock cap. CLI flags
  override env only when explicitly passed (§17 L2).
- **Fail-soft tools:** an *expected* tool failure is raised as `ToolError` and
  returned to the model as `is_error` (it can recover); any *unexpected* tool
  bug is caught, logged, and also returned as an error. A tool never crashes the
  loop — only budgets, completion, refusal, or a provider error end a run.
- **File I/O is LF-normalized** (`_read_text`/`_write_text` via bytes, not
  `read_text`) so the model's `\n`-based edits match on any platform.
- **Output:** a `RunResult` (status, final text, full step trace, budget
  totals). Statuses: `completed | max_tokens | refused | max_steps |
  token_budget | usd_budget | provider_error | index_error | error`.
  *(Persisted to Postgres once the orchestrator exists.)*
- **System prompt:** minimal correct change, read-before-edit, run tests after
  editing, no unrequested refactors, autonomous (no asking mid-run).
- **Self-test:** `python -m agent.loop` (offline; exercises tools + guards).

### 6.6 LLM provider abstraction + the models (`agent/providers/`) — **built**
- **Interface (`base.py`):** `LLMProvider` Protocol with `complete`,
  message-construction helpers (`user_message`, `assistant_turn`,
  `tool_result_message`), `count_tokens`, `cost_usd`. Neutral value objects:
  `ToolSpec`, `ToolCall`, `Usage`, `LLMResponse`, `ProviderError`. **The loop
  imports only these — never a vendor SDK.**
- **Selection:** `get_provider()` driven by `LLM_PROVIDER` (`anthropic` default
  | `gemini`); `LLM_MODEL`, `LLM_EFFORT` configurable.

**Models used in the pipeline:**

| Role | Default model | ID / package | Where it runs | Notes |
|---|---|---|---|---|
| **Reasoning (default)** | **Claude Opus 4.8** | `claude-opus-4-8` (Anthropic SDK) | Worker → Anthropic API | 1M context, 128K max output, **$5 / 1M in · $25 / 1M out**. Uses **adaptive thinking** (`thinking:{type:"adaptive"}`) + **effort** (`output_config.effort`, default `high`; `xhigh` for hard work). `budget_tokens`/`temperature`/`top_p`/`top_k` are **removed on 4.8** — not sent. Handles `stop_reason == "refusal"`. The adapter degrades gracefully (drops effort/thinking and retries) if a model rejects them. |
| **Reasoning (alternative)** | **Gemini 2.5 Pro** | `gemini-2.5-pro` (`google-genai`) | Worker → Google API | `LLM_PROVIDER=gemini`. Manual function-calling (automatic disabled so the loop keeps control). Verify model id is current for your account. |
| **Embeddings (RAG)** | **all-MiniLM-L6-v2** | sentence-transformers, local | Worker, in-process | **No API cost, no network.** ~256-token chunk limit drives §6.4 chunking. `[ASSUMPTION A1]` — upgrade to a larger local embedder for quality. |
| **Token counting** | provider-native | Anthropic `count_tokens` / Gemini equivalent | Worker → API (when exact) | Local heuristic for packing; provider counts only for budget-critical gates. |

- **Cost-control levers:** **prompt caching** of the stable prefix (system
  prompt + tool specs + retrieved context) — cache reads ≈ 0.1× input, writes
  ≈ 1.25× — so repeated agentic turns in one run are much cheaper. Keep the
  cached prefix byte-stable (no timestamps/UUIDs in the system prompt); put
  volatile content last. `[DECISION D5]` whether to also adopt **Task Budgets**
  (beta).
- **Worked cost example (Opus 4.8):** a 15-step run averaging ~12K
  cached-prefix + 3K fresh input and ~1.5K output per step ≈ **~$0.87/issue**.
  The `max_usd` cap (default $5) bounds the worst case. *Re-baseline on real
  traffic.*

### 6.7 Sandbox (`agent/sandbox.py`, Docker) — **built**
- **Runs:** started once per run by the worker (`docker run -d ... sleep
  infinity`), commands dispatched with `docker exec`, destroyed at run end
  (`[DECISION D6 — reuse within a run]`).
- **Isolation (all MUST — enforced in `_run_args`):** `--network none`; host FS
  read-only; only the run's workspace mounted read-write; **`.git` masked with a
  read-only tmpfs** (§9 / §17 H1); non-root user; `--cap-drop ALL` +
  `--security-opt no-new-privileges`; `--pids-limit`, `--memory`, `--cpus`; a
  writable `/tmp` tmpfs; `HOME=/tmp`; **no secrets/env passed in**. Wall-clock
  enforced in two layers: an in-container coreutils `timeout --signal=KILL`
  (probed at start) that kills just the offending process, plus an outer
  subprocess-timeout backstop that kills the whole container.
- **Resilience:** if the outer backstop had to kill the container, the next
  `exec()`/`run_tests()` transparently starts a fresh one — a single runaway
  test no longer strands the rest of the run (§17 M1).
- **Interface:** `run_tests(target)` returns exactly the loop's host shape
  (`"exit code: N\n<output>"`), so the loop targets the sandbox with no changes.
  Path confinement (`_container_path`) mirrors the loop's `_safe_path`.
- **No new dependency:** shells out to the `docker` CLI (like `github.py` →
  `git`). Needs `docker` on PATH + a daemon for live use.
- **User/mount gotcha handled:** default user = host `uid:gid` on POSIX (so the
  bind-mounted workspace stays writable), `65534:65534` on Windows/macOS Docker
  Desktop. Override with `SANDBOX_USER`.
- **Runtime hardening:** `[DECISION D7]` plain Docker + the above hardening for
  the first release; gVisor/Kata is the documented upgrade path for truly
  hostile repos.
- **Image:** `docker/sandbox.Dockerfile` (python:3.12-slim-bookworm + pytest
  pre-baked, since the container has no network — `[DECISION D9]`). Pin by
  digest in prod.
- **Self-test:** `python -m agent.sandbox` (degrades gracefully with no daemon;
  asserts isolation live when a daemon is up).

## 7. Data & storage architecture

### 7.1 PostgreSQL — system of record — **not started**
- **`runs`** — one row per issue run. `id (uuid pk)`, `repo`, `issue_number`,
  `issue_title`, `provider`, `model`, `status` (`queued|running|completed|
  no_changes|refused|budget_exhausted|provider_error|sandbox_error|error`),
  `branch`, `pr_number`, `pr_url`, `steps_used`, `input_tokens`,
  `output_tokens`, `cost_usd`, `started_at`, `finished_at`, `error_detail`.
  Unique index on `(repo, issue_number)` for idempotency.
- **`run_steps`** — one row per ReAct step. `id`, `run_id (fk)`, `n`,
  `stop_reason`, `text` (clipped), `input_tokens`, `output_tokens`, `tools
  (jsonb: name/args/result/is_error)`. Written incrementally so a crashed run is
  still partly observable.
- **`webhook_events`** — dedupe + audit. `delivery_id (unique)`, `event_type`,
  `received_at`, `action_taken`. Guards against GitHub redeliveries.
- **`repos`** *(optional)* — per-repo config: default-branch cache, index
  version, last-indexed commit.

Postgres holds **no large blobs** — those live in the object store (§7.4).

### 7.2 Redis — transit & ephemeral — **not started**
- Celery **broker** (and optionally result backend).
- **Cache:** default-branch lookups, GitHub rate-limit state, **idempotency
  locks** on `(repo, issue_number)` so two near-simultaneous webhooks don't
  double-run.
- **Rate limiting** for the webhook endpoint.
- Treated as **non-durable** — losing Redis loses in-flight queue state
  (re-enqueueable from Postgres `queued` rows), never the source of truth.

### 7.3 Vector store — ChromaDB — **built (client), volume deploy pending**
- **One collection per repo**, persisted on a volume. Stores chunk embeddings +
  metadata (file path, symbol name, start/end lines, content hash, repo).
- Written by the indexer (§6.4); read by `retrieve_context`. Incremental:
  delete-by-file before re-index; embedding cache keyed by `(content hash,
  embedder)`.
- Location is env-overridable (`CHROMA_DIR`) and opened lazily (§17 M2).
- `[DECISION D8]` ChromaDB (simple, file-backed) vs pgvector/Qdrant if we
  outgrow it. Default ChromaDB.

### 7.4 Object / blob store + workspace filesystem — **not started**
- **Per-run workspace:** `${WORKSPACE_ROOT}/${run_id}/repo` on a scratch volume
  (ephemeral; deleted after the run). Where the fetched code physically goes.
- **Artifacts** (final diff/patch, full logs, sandbox output): uploaded to an
  **object store** (S3/MinIO `[ASSUMPTION]`) under `runs/${run_id}/...`,
  referenced by key from the `runs` row. Retention window (§13) then GC'd.
- The ChromaDB persistence volume is separate from the ephemeral workspace.

### 7.5 API contract the frontend consumes — **not started**
Read-only JSON over §6.2 endpoints:
- `GET /runs?status=&repo=&limit=&cursor=` → paginated run summaries.
- `GET /runs/{id}` → full run incl. status, budget totals, PR link.
- `GET /runs/{id}/steps` → ordered step trace for live/replay.
- `POST /runs {repo, issue_number}` → manual trigger.
Stable field names = the run/step columns in §7.1. The frontend has **no direct
DB access**.

## 8. Configuration & secrets (env vars)

| Var | Purpose | Default |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` \| `gemini` | `anthropic` |
| `LLM_MODEL` | model id | `claude-opus-4-8` |
| `LLM_EFFORT` | `low\|medium\|high\|xhigh\|max` | `high` |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | model auth | — |
| `GITHUB_TOKEN` *(dev)* / GitHub App creds *(prod)* | repo auth | — |
| `GITHUB_WEBHOOK_SECRET` | HMAC verification | — |
| `GITHUB_API_URL` | enterprise override | `api.github.com` |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | commit identity | agent defaults |
| `DATABASE_URL` | Postgres DSN | — |
| `REDIS_URL` | broker/cache | — |
| `WORKSPACE_ROOT` | scratch dir for clones | `/var/agent/workspaces` |
| `CHROMA_DIR` / `EMBEDDING_CACHE_DIR` | vector store + embed cache location | under `agent/` |
| `OBJECT_STORE_URL` / creds | artifacts | — |
| `MAX_STEPS` / `MAX_TOTAL_TOKENS` / `MAX_USD` / `MAX_WALLCLOCK_S` | budgets | 30 / 500k / 5.0 / 1800 |
| `SANDBOX_IMAGE`, `SANDBOX_CPUS`, `SANDBOX_MEMORY`, `SANDBOX_PIDS_LIMIT`, `SANDBOX_TIMEOUT_S`, `SANDBOX_TMPFS_SIZE`, `SANDBOX_USER`, `DOCKER_BIN` | sandbox limits | pinned / 1 / 2g / 256 / 300 / 64m / auto / `docker` |

**Secret handling rules:** secrets come only from the environment / a secrets
manager (never committed, never in the system prompt, never passed into the
sandbox); tokens are redacted from every log and error; the sandbox container
receives **no** secrets and **no** network.

## 9. Security model (threat-driven)

This section is the **most safety-critical** part of the project (Goal G2) and
was substantially hardened by the 2026-07-02 audit. Threats and mitigations:

- **Malicious repo code (the biggest threat).** Runs only inside the sandbox:
  no network, read-only host FS, resource/time caps, non-root, dropped caps. It
  cannot reach the LLM key (host-side), the GitHub token (host-side), Postgres,
  or the internet.
- **Sandbox → host escape via `.git` (found & fixed — §17 H1).** The workspace
  is mounted read-write so tests can run, but it contains `.git`. Untrusted test
  code could plant `.git/hooks/*` or set `core.fsmonitor=<cmd>` in
  `.git/config`; that code does nothing in the sandbox but **would execute on
  the host** the next time the orchestrator runs `git add/commit/push` — with
  the GitHub token in the environment. **Mitigated in two layers:**
  1. *Sandbox:* `.git` is shadowed by a read-only tmpfs (`--tmpfs
     /work/.git:ro`), so untrusted code can neither read history nor plant
     anything.
  2. *Host-side git:* every git invocation runs with hooks and fsmonitor
     disabled — `git -c core.hooksPath=/dev/null -c core.fsmonitor= …`
     (`_GIT_HARDENING`) — and commits use `--no-verify`. So even a workspace
     that somehow arrived dirty cannot run code on the host.
- **Prompt injection via issue text or repo files.** The model can only act
  through the constrained, path-confined tool surface; the destructive ops
  (push, PR) are performed by the orchestrator *after* the loop, not by the
  model. The model cannot exfiltrate secrets it never sees.
- **Token leakage.** Tokens never persisted to git config (clone scrubs the
  remote; push passes auth inline), redacted from logs/errors, short-lived
  (GitHub App installation tokens preferred).
- **Webhook spoofing.** HMAC verification with constant-time compare; reject
  unsigned/oversized payloads; `webhook_events` dedupe.
- **Runaway cost/loops.** Budget controller + wall-clock + idempotency lock +
  bot-author webhook guard (so the agent never reacts to its own activity).
- **Supply chain.** Sandbox and worker images pinned by digest; dependencies
  pinned.

`[DECISION D9]` Dependency install in a no-network sandbox: (a) pre-bake common
deps into the image, (b) a vetted offline mirror, (c) a brief audited
network-allowed install phase before lockdown. Default `[ASSUMPTION]` (a)+(b).

## 10. Sandbox execution detail (expanded)

Per run: one long-lived container is started —
```
docker run -d --rm --name <n> --network none --read-only \
  -v <workspace>:/work:rw --workdir /work \
  --tmpfs /tmp:rw,exec,size=<S> [--tmpfs /work/.git:ro] \
  --user <uid:gid> --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit <P> --memory <M> --cpus <C> --env HOME=/tmp \
  <image> sleep infinity
```
— then each test run is dispatched with `docker exec [timeout --signal=KILL Ns]
sh -lc "<cmd>"`, and the container is `docker rm -f`'d at the end. Stdout/stderr
are captured and truncated to the tool-result cap; the exit code is returned.
The worker needs access to a Docker daemon — `[DECISION D10]`
Docker-out-of-Docker (host socket — simplest, weaker boundary) vs rootless/
remote builder vs Kubernetes-native per-run Job. Default `[ASSUMPTION]` rootless
Docker / dedicated daemon; never the privileged host socket in prod.

## 11. Observability — **not started**
- **Metrics (Prometheus):** runs started/succeeded/failed by status; run
  duration histogram; steps per run; tokens & USD per run; sandbox executions +
  failures; GitHub API calls + rate-limit hits; queue depth & worker saturation.
- **Tracing (OpenTelemetry):** one span per run, child spans per step and per
  tool call (incl. sandbox exec and LLM latency). Correlate by `run_id`.
- **Logs:** structured JSON, `run_id`-tagged, token-redacted; full per-run log
  archived to the object store.
- **Dashboards (Grafana):** throughput, success rate, p50/p95 latency,
  cost/issue, failure breakdown, queue health.

## 12. Cost & time controls (consolidated)
- Per-run hard caps: `MAX_STEPS`, `MAX_TOTAL_TOKENS`, `MAX_USD`,
  `MAX_WALLCLOCK_S` (Celery hard/soft time limits).
- Prompt caching of the stable prefix (§6.6).
- Effort tuned per workload (`high` default; `medium` for cheap repos, `xhigh`
  for hard ones).
- Optional Task Budgets (D5).
- Cost surfaced per run in Postgres + Grafana.

## 13. Failure modes, recovery, retention
- **Clone/network failure:** retry with backoff; after N, terminal `error`.
- **Indexing failure:** terminal `index_error`; never reason on an un-indexed
  repo.
- **Provider error / refusal:** `provider_error` / `refused`; no PR; reason
  recorded.
- **Sandbox failure/timeout:** returned to the model as a tool error (it can
  adapt; the container auto-restarts after a kill — §6.7); repeated → run ends
  with `sandbox_error`.
- **No changes produced:** `no_changes`; comment + stop.
- **Worker crash mid-run:** the `runs` row stays `running`; a reaper marks runs
  stale after `MAX_WALLCLOCK_S` and either re-queues (infra cause) or fails
  them. Partial `run_steps` remain for debugging.
- **Idempotency:** deterministic branch + adoptive PR + `(repo, issue_number)`
  lock ⇒ re-delivery/re-run is safe.
- **Retention:** workspaces deleted at run end; artifacts/logs kept
  `[DECISION D11]` (default 30 days) then GC'd; Postgres rows kept indefinitely.

## 14. Deployment topology — **not started**
- **Containers/images:** `api` (FastAPI), `worker` (Celery + orchestrator +
  retrieval + loop), `sandbox` (execution image, pulled by workers). Pinned by
  digest, built in CI.
- **Stateful services:** PostgreSQL, Redis, object store (S3/MinIO), ChromaDB
  persistence volume, Prometheus/Grafana.
- **Kubernetes:** `api` Deployment + Service + Ingress (only `/webhooks/*`
  public); `worker` Deployment (HPA on queue depth); the sandbox runs as a child
  of the worker (D10) or as per-run K8s Jobs; Postgres/Redis as managed services
  or StatefulSets; secrets via K8s Secrets / External Secrets.
- **Helm:** one chart, values per environment (dev/staging/prod).
- **CI/CD (GitHub Actions):** lint + the offline self-tests of each module →
  build & scan images → push by digest → Helm deploy to staging → smoke test →
  promote to prod.

## 15. Repository / code layout (current)
```
auto-swe-agent/
├── agent/                  # the reasoning + integration library
│   ├── github.py           # REST client + git helpers (hardened) + webhooks  [§6.1]  (built)
│   ├── retrieval.py        # tree-sitter + embeddings + ChromaDB (lazy)        [§6.4]  (built)
│   ├── loop.py             # ReAct loop + Budget + workspace-confined tools    [§6.5]  (built)
│   ├── sandbox.py          # hardened Docker isolation (.git masked)           [§6.7]  (built)
│   └── providers/          # LLMProvider interface + anthropic + gemini        [§6.6]  (built)
├── app/                    # FastAPI gateway / webhook receiver                [§6.2]  (empty — M3)
│   └── main.py
├── workers/                # Celery app + the run_issue orchestrator           [§6.3]  (empty — M1, NEXT)
│   └── tasks.py
├── db/                     # SQLAlchemy models + Alembic migrations            [§7.1]  (not started)
├── docker/
│   └── sandbox.Dockerfile  # sandbox execution image (pytest pre-baked)                (built)
├── eval/                   # benchmark harness (SWE-bench-style)                       (later)
├── monitoring/             # Prometheus rules + Grafana dashboards             [§11]   (later)
├── k8s/ , helm/            # manifests + chart                                 [§14]   (later)
├── docs/                   # retrieval.md, loop.md, github.md, sandbox.md,
│                           #  llm-provider-abstraction.md (ADR-001)
├── plan2.md                # THIS document — the source of truth
├── plan.md                 # historical v0.1 draft (superseded)
├── Claude.md               # AI-assistant working context (rewritten 2026-07-02)
├── .gitattributes          # LF normalization for source
├── requirements.txt
└── ReadMe.md
```

## 16. Open decisions (please rule on these)

| ID | Decision | Options | Current default |
|---|---|---|---|
| **D1** | Initial language support | Python-only / Python+JS / language-agnostic | Python-first |
| **D2** | Model host-side, code sandbox-side | as designed / alternative | **as designed** |
| **D3** | GitHub auth | GitHub App / PAT | App (prod), PAT (dev) |
| **D4** | Auto-retry agent-logic failures? | yes / no | **no** (infra only) |
| **D5** | Adopt Task Budgets (beta)? | yes / no | start no, revisit |
| **D6** | Sandbox per-call vs per-run reuse | fresh / reuse | reuse within a run |
| **D7** | Sandbox runtime hardening | Docker / gVisor / Kata | Docker + hardening first |
| **D8** | Vector store | ChromaDB / pgvector / Qdrant | ChromaDB |
| **D9** | Deps in a no-network sandbox | pre-bake / mirror / audited install | pre-bake + mirror |
| **D10** | How workers get Docker | host socket / rootless / K8s Jobs | rootless or K8s Jobs |
| **D11** | Artifact/log retention | 7 / 30 / 90 days | 30 days |
| **D12** | Embedding model | MiniLM / larger local | MiniLM first |
| **D13** | Concurrency cap | by worker replicas / global semaphore | HPA + global cap |

## 17. Deep-scan audit — findings & fixes (2026-07-02, all applied)

A senior-engineer review of every built module. **Severity:** 1 high
(security), 2 medium (robustness), 4 low (correctness/hygiene), plus doc/config
cleanup. All fixes are in the working tree and the offline self-tests
(`loop`, `github`, `sandbox`) pass.

### 🔴 H1 — Sandbox → host escape via `.git` *(security, high)* — FIXED
Full description and the two-layer mitigation are in **§9**. Files:
`agent/sandbox.py` (`.git` tmpfs mask), `agent/github.py` (`_GIT_HARDENING` +
`commit --no-verify`).

### 🟠 M1 — A single test timeout stranded the rest of the run *(robustness)* — FIXED
When a command exceeded the outer wall-clock, the container was killed and
`container_id` set to `None`; every later `run_tests` then raised "sandbox is
not started", killing the run's ability to verify any fix. **Fix:** `exec()`
detects a started-then-killed sandbox (`_was_started`) and transparently starts
a fresh container; `close()` clears the flag so a deliberately closed sandbox
never auto-restarts. (`agent/sandbox.py`)

### 🟠 M2 — `retrieval.py` did real work at import time *(robustness/perf)* — FIXED
Importing the module created the ChromaDB `PersistentClient` and the cache dir
as side effects — every importer (loop, future API, tests, linters) paid the
cost and created a `.chroma` dir. **Fix:** lazy `get_collection()`, lazy cache
`mkdir`, and env-overridable `CHROMA_DIR` / `EMBEDDING_CACHE_DIR`.
(`agent/retrieval.py`)

### 🟡 L1 — Index keys mixed relative & absolute paths *(correctness)* — FIXED
Chunk IDs and the delete-by-file filter used the raw `os.walk` path (relative
when `root` was relative) while metadata used `abspath` — so re-indexing via a
different path form left **stale duplicate chunks**, and two repos indexed by
the same relative path could collide. **Fix:** normalize `fpath` with
`os.path.abspath` at the top of the index loop. (`agent/retrieval.py`)

### 🟡 L2 — CLI budget flags silently overrode `.env` *(correctness)* — FIXED
`--max-steps`/`--max-usd` defaulted to `30`/`5.0`, so they always overrode
`MAX_STEPS`/`MAX_USD` from `.env` — setting the env vars appeared to do nothing.
**Fix:** flags default to `None`; only explicit values override.
(`agent/loop.py`)

### 🟡 L3 — Sandbox startup failure crashed the CLI *(robustness)* — FIXED
`Sandbox(...).start()` ran outside the try/except, so a `SandboxError` surfaced
as an unhandled traceback. **Fix:** wrapped → clean message, exit 1.
(`agent/loop.py`)

### 🟡 L4 — `assemble_context` could `KeyError` on sparse metadata *(robustness)* — FIXED
It indexed `c['metadata']['file']` directly; a chunk with missing metadata
raised `KeyError` and surfaced as a spurious tool error. **Fix:** `.get(...)`
with `'?'` fallbacks. (`agent/retrieval.py`)

### 🧹 Hygiene — FIXED
- **`Claude.md` described the wrong project** (invoice OCR / PaddleOCR /
  reconciliation — that's the *MoM Recon* project). Rewritten to document this
  agent, its layout/status, and the security invariants a contributor must not
  break.
- **Removed** stray tracked empty file `retrieval_output.txt`.
- **Added `.gitattributes`** enforcing LF for source (git was warning on every
  touch; the agent's own I/O is LF and everything runs on Linux).
- **Cleaned confused/incorrect inline comments** in `loop.py` — including two
  that wrongly said the agent uses **OpenAI** embeddings (it's local
  sentence-transformers, no API/network).
- **`docs/sandbox.md`** — added the `.git`-masking row to the guarantees table.

### Deliberately NOT changed (call these; don't guess)
- **`ReActAgent.max_output_tokens` defaults to 8192.** Fine now; raise if real
  runs show `stop_reason == "max_tokens"` truncation.
- **`provider.count_tokens` is implemented but unused by `Budget`.** The budget
  accrues from each response's returned `usage` (correct and free);
  `count_tokens` costs an extra round-trip — keep it for a future pre-flight
  gate, not per-step.
- **Gemini `assistant_turn` echoes `resp.raw`, `None` on a refusal.** Safe today
  (the loop returns before appending on refusal) but fragile; guard when
  hardening providers.
- **`.venv` is a Unix-layout venv on a Windows host** (has `bin/python`, no
  `Scripts/`). Retrieval/loop tests that need ML deps must run in WSL/Linux —
  a dev-env note, not a code bug.

## 18. Build order (milestones)

1. **M1 — Orchestrator (`workers/tasks.py`) — NEXT.** Glue the built pieces:
   issue → clone (token-scrubbed) → `create_branch` → `index_repo` →
   `ReActAgent.run` → `submit_changes`. Run against a **trusted** repo first;
   write `runs`/`run_steps` to Postgres as it goes (pulled earlier than the
   original plan so every run is inspectable). *The H1/`_GIT_HARDENING`
   prerequisites for running the git steps safely are now in place.*
2. **M2 — Sandbox integration end-to-end.** Wire the `--sandbox` path through
   the orchestrator; confirm the `.git` mask holds live; pre-bake a target
   repo's deps into the image (D9). Required before pointing at any untrusted
   repo.
3. **M3 — API + webhooks (`app/main.py`).** HMAC-verified `/webhooks/github` →
   enqueue; manual `POST /runs`; read endpoints (§7.5). `app/main.py` is
   currently empty.
4. **M4 — Persistence & idempotency hardening.** Alembic migrations,
   `webhook_events` dedupe, `(repo, issue_number)` lock, stale-run reaper.
5. **M5 — Observability.** Prometheus metrics + OTel spans + Grafana dashboards.
6. **M6 — Deployment.** Dockerfiles, Helm chart, K8s manifests, GitHub Actions
   CI/CD (run each module's offline self-test in CI).
7. **M7 — Eval harness.** Measured success rate on a benchmark set; tune
   effort/budgets/prompt.

## 19. How to run what exists today

```bash
# 1. Install deps (Linux/WSL venv — ML deps don't build cleanly on Windows).
pip install -r requirements.txt

# 2. Offline self-tests — no API key, network, or daemon needed:
python -m agent.loop        # ReAct tools + guards
python -m agent.github      # REST client + webhook parsing + git helpers
python -m agent.sandbox     # sandbox isolation (skips live checks if no daemon)
python -m agent.retrieval   # index + retrieve a repo (needs ML deps; run in WSL)

# 3. Pick a provider, set its key in .env:
#    ANTHROPIC_API_KEY=...                         # default (claude-opus-4-8)
#    GEMINI_API_KEY=...  with LLM_PROVIDER=gemini   # alternative (gemini-2.5-pro)

# 4. Run the agent on a real task against a repo (indexes it first):
python -m agent.loop "Fix the off-by-one in paginate()" \
    --workspace /path/to/repo --auto-index

# 5. (Optional) run untrusted tests inside the hardened sandbox — needs Docker:
docker build -f docker/sandbox.Dockerfile -t auto-swe-sandbox:latest .
SANDBOX_IMAGE=auto-swe-sandbox:latest \
  python -m agent.loop "Fix ..." --workspace /path/to/repo --auto-index --sandbox
```

Budgets: `MAX_STEPS`/`MAX_TOTAL_TOKENS`/`MAX_USD`/`MAX_WALLCLOCK_S` in `.env`,
overridable per-run via `--max-steps` / `--max-usd`. Model + effort:
`LLM_PROVIDER` / `LLM_MODEL` / `LLM_EFFORT`.

## 20. Glossary
- **Run:** one autonomous attempt to resolve one issue.
- **Workspace:** the per-run host directory holding the cloned repo.
- **Orchestrator:** the worker code that executes a run end-to-end
  (`workers/tasks.py`, M1).
- **ReAct loop:** the Reason→Act→Observe model-driving loop (`agent/loop.py`).
- **Sandbox:** the network-isolated Docker container where untrusted code/tests
  run, with `.git` masked (`agent/sandbox.py`).
- **Provider:** an adapter implementing `LLMProvider` for a specific vendor.
- **Budget:** the hard caps (steps/tokens/USD/wall-clock) bounding a run.

---

*End of the source of truth. Every `[DECISION]` in §16 is open for your call;
any section can be revised on request (bump the version in §0).*
