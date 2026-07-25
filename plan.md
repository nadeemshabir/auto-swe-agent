# Autonomous SWE Agent — Master Plan (Source of Truth)

> ⚠️ **SUPERSEDED (2026-07-02).** This is the historical v0.1 draft. The active
> single source of truth is now **`plan2.md`**, which absorbs everything below,
> updates it to the real build state, and folds in the deep-scan audit. Read
> `plan2.md` instead. This file is kept only for history.

> **Status:** DRAFT for review · **Version:** 0.1 · **Date:** 2026-06-28 · **Owner:** Nadeem
> **Scope of this document:** the complete backend system — issue intake → codebase understanding → reasoning → sandboxed execution → pull request — plus data, models, security, observability, and deployment. **The web frontend is explicitly out of scope here** (a thin read-only dashboard over the API documented in §3; its internals are a separate document).
>
> **How to review this document:** this is meant to be read top-to-bottom and marked up. Every design decision is stated explicitly so it can be challenged. Open decisions that need your call are collected in **§16 Open Questions** and tagged `[DECISION]` inline. Suggested-but-not-final choices are tagged `[ASSUMPTION]`. When you request a change, reference the section number; I'll bump the version and update the changelog in §0.

---

## 0. Document control

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-06-28 | Initial full draft for review. |

**Conventions:** `[DECISION]` = needs your approval before build; `[ASSUMPTION]` = my default, override if wrong; `MUST`/`SHOULD`/`MAY` = requirement strength.

---

## 1. What we are building (one paragraph)

A production-grade service that watches one or more GitHub repositories, and when an actionable issue appears, autonomously: clones the repo, builds a semantic + structural understanding of the codebase, drives an LLM through a Reason→Act→Observe loop that reads code, edits files, and runs tests inside a hardened sandbox, and — if it produces a verified fix — commits the change to a branch and opens a pull request that references the issue. The entire path from "issue opened" to "PR opened" runs without a human in the loop, under hard caps on time, tokens, and money, with full tracing of every step.

## 2. Goals and non-goals

**Goals**
- G1. End-to-end autonomy: webhook → PR, no human step.
- G2. Safety first: untrusted repo code and untrusted issue text must never compromise the host, leak secrets, or reach the network from the execution sandbox.
- G3. Correctness gating: the agent only opens a PR when its change exists and the repo's tests (or a defined check) pass; otherwise it reports why and stops.
- G4. Cost/Time bounded: every run has hard ceilings (steps, tokens, USD, wall-clock) and is observable in real time.
- G5. Model-pluggable: the reasoning core is not wedded to one vendor (Anthropic **or** Google), selectable by config.
- G6. Idempotent & re-runnable: re-processing the same issue updates the same branch/PR rather than duplicating work.

**Non-goals (for this document)**
- N1. The web frontend's internal design (components, state, styling). We define only the **API contract** it consumes (§3, §7.5).
- N2. Multi-repo *monorepo-scale* indexing optimizations beyond what §6.4 specifies.
- N3. Fine-tuning or training any model. We use hosted LLMs + a local embedding model only.
- N4. Supporting languages beyond the agreed initial set (`[DECISION D1]` in §16 — default Python-first).

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

Each layer is independently testable and deployable. Layers 1–2 are stateless request handlers + a queue; the heavy lifting (3,4,5) happens inside a worker process; layer 6 is shared infrastructure.

## 4. The life of an issue (end-to-end data flow — *where code goes, where processing happens*)

This is the canonical sequence. Numbers in brackets cite the component (§6) and store (§7) involved.

1. **Issue opened on GitHub.** GitHub POSTs an `issues` webhook to our public endpoint `POST /webhooks/github` [API §6.1].
2. **Verify + parse.** The API verifies the `X-Hub-Signature-256` HMAC against the configured secret, parses the payload (`parse_webhook_event`), and decides if it is actionable (opened/reopened/labeled, not a PR, not a bot author). Non-actionable → `204`, no work. Actionable → it writes a `runs` row (status `queued`) to **PostgreSQL** [§7.1], enqueues a Celery task carrying the `run_id` onto the **Redis broker** [§7.2], and returns `202 Accepted`. *The API does no heavy work and holds no repo code.*
3. **A worker picks up the job.** A Celery worker [§6.2] claims the task. The worker is the **orchestrator** — it runs the whole pipeline for this one issue and updates the `runs` row to `running`. It posts an optional "🤖 on it" comment back on the issue.
4. **Fetch the code — where the code goes.** The orchestrator clones the target repo into a **per-run workspace directory** on a scratch volume: `${WORKSPACE_ROOT}/${run_id}/repo` [§7.4]. Auth uses the installation/PAT token; the token is scrubbed from `.git/config` immediately after clone (never persisted). The default branch is read from GitHub; a deterministic working branch `agent/issue-<n>` is created off it. *This directory is the single working copy for the run; nothing else mutates it.*
5. **Understand the code — where processing happens.** The orchestrator runs the **retrieval/indexer** [§6.4] over the workspace: tree-sitter parses files into AST-aware chunks, a **local embedding model** (sentence-transformers, runs in-process on the worker — no API call) embeds them, and vectors + metadata are written to a **ChromaDB collection keyed by repo** [§7.3], persisted on a volume so re-runs of the same repo are incremental. Embedding cache keyed by `(content hash, embedder name)` avoids re-embedding unchanged chunks.
6. **Reason — where the LLM runs.** The orchestrator constructs the task string from the issue (`Issue.to_task()`) and starts the **ReAct loop** [§6.5]. The loop calls the **LLM provider** [§6.6] — **this API call originates from the worker/host process, NOT from inside the sandbox**. The model sees the system prompt, the running message history, and the tool specs. The sandbox has no API key and no network; the model itself never executes anywhere untrusted.
7. **Act — tool execution & the trust boundary.** When the model requests a tool:
   - `retrieve_context` → queries ChromaDB [§7.3] (host-side, read-only, safe).
   - `read_file` / `edit_file` / `list_dir` → operate on the workspace directory, path-confined to it (host-side; reading/writing files is not code execution).
   - `run_tests` (and any "execute code") → **dispatched into the Docker sandbox** [§6.7, §10]: a fresh container mounts the workspace, runs `pytest` (or the repo's check) with **no network, read-only host FS, a writable workspace mount, and CPU/memory/PID/time limits**, captures stdout/stderr/exit code, and is destroyed. This is the only place untrusted repo code runs.
8. **Observe + loop.** Each tool result (clipped) is fed back to the model. Every step (model text, tool calls, args, results, token usage, cost) is appended to the **structured trace** and persisted incrementally to **PostgreSQL** `run_steps` [§7.1]. The **Budget controller** [§6.5] checks step/token/USD caps before every model call; a wall-clock cap is enforced by the worker. Exhausting any cap stops the run cleanly with a status.
9. **Produce the result.** When the model declares completion (or a budget/wall-clock cap trips):
   - If the working tree has **no changes** → status `no_changes`; the orchestrator comments the failure reason on the issue; **no PR**.
   - If there are changes → `git add -A && commit` in the workspace, push the branch with the token supplied inline (`--force-with-lease`), then open (or adopt, if it already exists) a **pull request** via the GitHub REST API [§6.1], body referencing `Closes #<n>` and a short trace summary.
10. **Finalize.** The orchestrator writes the terminal status, PR number/URL, and final usage totals to the `runs` row, emits metrics [§11], comments the PR link on the issue, and tears down the workspace + sandbox. The job ends.

**One-line mental model of placement:** *code is fetched into a per-run host workspace; understanding and file edits happen host-side against that workspace; the LLM is called from the worker (never the sandbox); only test/code execution is pushed into a network-isolated Docker container; all durable state goes to Postgres + object store, vectors to ChromaDB, queueing/caching to Redis.*

## 5. Why this shape (key architectural decisions)

- **The model runs on the host, the code runs in the sandbox.** Inverting this (running the agent loop inside the sandbox) would either deny the model network (it needs the LLM API) or grant the sandbox network (defeating isolation). Keeping the loop host-side and pushing *only execution* into the sandbox gives us isolation without crippling the model. `[DECISION D2]`
- **One job = one issue = one worker task.** Simplest correct concurrency unit; horizontal scale = more workers. No shared mutable state between runs except read-mostly infra (Postgres, ChromaDB per-repo, Redis).
- **Postgres for truth, Redis for transit, ChromaDB for vectors, object store for blobs.** Each store does one job (§7). We do not stuff large blobs in Postgres or treat Redis as durable.
- **Idempotency via deterministic branch names + adoptive PR creation.** Re-running issue #42 updates `agent/issue-42` and its existing PR rather than spawning duplicates.
- **Provider abstraction in front of the LLM.** The loop depends on an interface, not a vendor SDK, so Claude/Gemini swap by env var (§6.6).

## 6. Component specifications

For each: responsibility, where it runs, inputs/outputs, key interfaces.

### 6.1 GitHub integration (`agent/github.py`)
- **Runs:** inside the API (parsing/verification) and inside the worker (clone/branch/commit/push, PR creation).
- **REST client:** `get_issue`, `get_default_branch`, `comment_on_issue`, `find_pull_request`, `create_pull_request`. Stdlib HTTP; bounded retry/backoff on 429/5xx/network; 403 retried only when rate-limited (honors `Retry-After`/`X-RateLimit-Reset`); idempotent PR creation (422 → adopt existing). Tokens redacted from all errors/logs.
- **Git helpers:** clone (token scrubbed from config), branch (`agent/issue-<n>`), commit (returns bool so empty PRs are skipped), push (`--force-with-lease`, token inline never persisted), identity config for clean CI.
- **Webhook parsing:** `parse_webhook_event` with infinite-loop guards (skip PRs, bot authors, non-actionable actions).
- **Auth:** `[DECISION D3]` GitHub App (installation tokens, short-lived, per-repo, recommended for prod) **vs** a classic/fine-grained PAT (simplest for a demo). Default `[ASSUMPTION]` GitHub App for the real deployment, PAT for local dev.

### 6.2 API gateway / webhook receiver (`app/main.py`, FastAPI)
- **Runs:** stateless web pods behind a load balancer; publicly reachable (only the `/webhooks/*` path needs to be).
- **Endpoints:**
  - `POST /webhooks/github` — verify HMAC, parse, enqueue, `202`. **Returns within ~1s**; never blocks on agent work.
  - `GET /healthz`, `GET /readyz` — liveness/readiness.
  - `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/steps` — read API the frontend consumes (§7.5).
  - `POST /runs` — manual trigger (`{repo, issue_number}`) for testing without a webhook.
  - `GET /metrics` — Prometheus exposition (or a sidecar).
- **Security:** constant-time signature compare; reject unsigned/oversized bodies; per-source rate limiting.

### 6.3 Queue & workers (Celery + Redis)
- **Broker:** Redis (§7.2). **Result backend:** Postgres or Redis `[ASSUMPTION]` Postgres for durability of run outcomes.
- **Worker = orchestrator:** one Celery task `run_issue(run_id)` executes the §4 pipeline. Concurrency = N worker replicas × M task slots, bounded by sandbox capacity. Task-level `time_limit` (hard) + `soft_time_limit` (graceful) enforce wall-clock.
- **Retries:** infrastructure failures (clone network blip, DB hiccup) retry with backoff; **agent logic failures do not auto-retry** (re-running an LLM run blindly burns money) — they record a terminal status. `[DECISION D4]`

### 6.4 Codebase understanding / retrieval (`agent/retrieval.py`)
- **Runs:** in the worker, in-process (CPU; GPU optional for embeddings).
- **Pipeline:** tree-sitter chunking (functions/methods/classes, decorated defs unwrapped, methods indexed individually + a class-header chunk to dodge embedding truncation) → batched embeddings via sentence-transformers → upsert into ChromaDB with delete-by-file first (no stale dupes) → optional call-graph for structural neighbors. `assemble_context(query, k, token_budget)` packs the most relevant chunks under a token budget for the loop.
- **Token counting:** provider-neutral local estimate for packing; exact counts (when needed for budget-critical checks) come from the active provider's API.
- **Index location:** ChromaDB collection per repo, persisted on a volume keyed by repo slug (§7.3); incremental across runs.

### 6.5 Reasoning core — ReAct loop + Budget (`agent/loop.py`)
- **Runs:** in the worker (host side).
- **Loop:** `while not done and within budget: provider.complete(...) → if tool calls: dispatch (Act) → feed results (Observe); else finish`.
- **Tools (the model's only way to touch the repo):** `retrieve_context`, `read_file`, `edit_file` (exact-string replace / create), `run_tests`, `list_dir`. All file paths confined to the workspace; `run_tests` routed to the sandbox (§6.7).
- **Budget controller:** hard caps `max_steps`, `max_total_tokens`, `max_usd`; checked before every model call; usage accrued after each. Worker adds a wall-clock cap.
- **Fail-soft tools:** a tool error is returned to the model as `is_error` (it can recover), never crashes the run. Buggy tools can't kill the loop.
- **Output:** a `RunResult` (status, final text, full step trace, budget totals) persisted to Postgres.
- **System prompt:** instructs minimal correct change, read-before-edit, run tests after editing, no unrequested refactors, autonomous operation (no asking the user mid-run).

### 6.6 LLM provider abstraction + **the models** (`agent/providers/`)
- **Interface (`base.py`):** `LLMProvider` with `complete`, message-construction helpers (`user_message`, `assistant_turn`, `tool_result_message`), `count_tokens`, `cost_usd`. Neutral value objects: `ToolSpec`, `ToolCall`, `Usage`, `LLMResponse`. The loop only ever uses these.
- **Selection:** `LLM_PROVIDER` env var (`anthropic` default | `gemini`); `LLM_MODEL`, `LLM_EFFORT` configurable.

**Models used in the pipeline (the full list):**

| Role | Default model | ID / package | Where it runs | Notes |
|---|---|---|---|---|
| **Reasoning (default)** | **Claude Opus 4.8** | `claude-opus-4-8` (Anthropic SDK) | Worker → Anthropic API | 1M context, 128K max output, **$5 / 1M input · $25 / 1M output**. Use **adaptive thinking** (`thinking:{type:"adaptive"}`) and **effort** (`output_config.effort`, default `high`; `xhigh` for hard agentic work). `budget_tokens`, `temperature`, `top_p`, `top_k` are **removed on 4.8** — do not send them. Handle `stop_reason == "refusal"`. |
| **Reasoning (alternative)** | **Gemini 2.5 Pro** | `gemini-2.5-pro` (`google-genai`) | Worker → Google API | Selected via `LLM_PROVIDER=gemini`. Manual function-calling shape implemented in `gemini_provider.py`. Pricing per Google's schedule (re-baseline separately). |
| **Embeddings (RAG)** | **all-MiniLM-L6-v2** (sentence-transformers) | local model | Worker, in-process | Local, **no API cost**, no network. Produces the vectors stored in ChromaDB. 256-token chunk limit drives the chunking strategy in §6.4. `[ASSUMPTION A1]` — could upgrade to a larger local embedder for quality. |
| **Token counting** | provider-native | Anthropic `count_tokens` / Gemini equivalent | Worker → API (when exact) | Local heuristic for packing; exact provider counts only for budget-critical gates. Never `tiktoken` (wrong for both vendors). |

- **Cost-control levers (built into the provider):** **prompt caching** of the stable prefix (system prompt + tool specs + retrieved context) — cache reads ≈ 0.1× input price, writes ≈ 1.25× — so the repeated agentic turns of one run are dramatically cheaper. Keep the cached prefix byte-stable (no timestamps/UUIDs in the system prompt); put volatile content last. `[DECISION D5]` whether to also adopt **Task Budgets** (beta) to let the model self-moderate token spend across the loop.
- **Worked cost example (Opus 4.8):** a 15-step run averaging ~12K cached-prefix + 3K fresh input and ~1.5K output per step ≈ (15 × 3K × $5/1M) + (15 × 12K × $0.5/1M cached) + (15 × 1.5K × $25/1M) ≈ $0.22 + $0.09 + $0.56 ≈ **~$0.87/issue**. The `max_usd` cap (default $5) bounds the worst case. *Numbers illustrative — re-baseline on real traffic.*

### 6.7 Sandbox (`agent/sandbox.py`, Docker)
- **Runs:** spawned by the worker per `run_tests`/code-execution call (or once per run and reused `[DECISION D6]`).
- **Isolation (all MUST):** `--network none`; host FS read-only; only the run's workspace mounted read-write; non-root user; dropped Linux capabilities; `--pids-limit`, `--memory`, `--cpus`, and a hard timeout; no secrets/env passed in. Image is a minimal language runtime + test tooling, pinned by digest.
- **Interface:** mirrors the loop's tool surface (`run_tests`, optionally `read_file`/`edit_file` if we move file ops in too) so the loop can target it without code changes — the loop was written for this swap.
- **Output:** `{exit_code, stdout, stderr}` clipped to a size cap before returning to the model.
- `[DECISION D7]` runtime: plain Docker (simplest) vs gVisor/Kata (stronger isolation for truly untrusted repos). Default `[ASSUMPTION]` Docker with the hardening above for the first release; document the upgrade path.

## 7. Data & storage architecture (where everything lives)

### 7.1 PostgreSQL — system of record (durable truth)
Tables (initial schema; column lists abbreviated):

- **`runs`** — one row per issue run. `id (uuid pk)`, `repo`, `issue_number`, `issue_title`, `provider`, `model`, `status` (`queued|running|completed|no_changes|refused|budget_exhausted|provider_error|sandbox_error|error`), `branch`, `pr_number`, `pr_url`, `steps_used`, `input_tokens`, `output_tokens`, `cost_usd`, `started_at`, `finished_at`, `error_detail`. Unique index on `(repo, issue_number)` for idempotency lookups.
- **`run_steps`** — one row per ReAct step. `id`, `run_id (fk)`, `n`, `stop_reason`, `text` (clipped), `input_tokens`, `output_tokens`, `tools (jsonb: name/args/result/is_error)`. Written incrementally so a crashed run is still partly observable.
- **`webhook_events`** — dedupe + audit of inbound deliveries. `delivery_id (unique)`, `event_type`, `received_at`, `action_taken`. Guards against GitHub redeliveries.
- **`repos`** *(optional)* — per-repo config: default branch cache, index version, last-indexed commit.

Postgres holds **no large blobs** — diffs, full logs, and workspaces live in the object store (§7.4), referenced by key.

### 7.2 Redis — transit & ephemeral
- **Celery broker** (job queue) and optionally result backend.
- **Cache:** default-branch lookups, GitHub rate-limit state, idempotency locks (a short-lived lock on `(repo, issue_number)` so two near-simultaneous webhooks don't double-run).
- **Rate limiting** for the webhook endpoint.
- Treated as **non-durable**: losing Redis loses in-flight queue state (jobs re-enqueueable from Postgres `queued` rows), never the source of truth.

### 7.3 Vector store — ChromaDB
- **One collection per repo**, persisted on a volume keyed by repo slug. Stores chunk embeddings + metadata (file path, symbol name, start/end lines, content hash).
- Written by the indexer (§6.4); read by `retrieve_context`. Incremental: delete-by-file before re-index; embedding cache keyed by `(content hash, embedder)`.
- `[DECISION D8]` ChromaDB (simple, file-backed, fine for the project scale) vs a managed vector DB (pgvector/Qdrant) if we outgrow it. Default ChromaDB.

### 7.4 Object / blob store + workspace filesystem
- **Per-run workspace:** `${WORKSPACE_ROOT}/${run_id}/repo` on a scratch volume (ephemeral; deleted after the run). This is **where the fetched code physically goes** and where edits happen.
- **Artifacts** (final diff/patch, full run logs, sandbox output): uploaded to an **object store** (S3/MinIO `[ASSUMPTION]`) under `runs/${run_id}/...`, referenced by key from the `runs` row. Kept for a retention window (§13) then GC'd.
- ChromaDB persistence volume is separate from the per-run ephemeral workspace (it must survive across runs).

### 7.5 API contract the frontend consumes (so the frontend can be built independently)
Read-only JSON over the endpoints in §6.2:
- `GET /runs?status=&repo=&limit=&cursor=` → paginated run summaries.
- `GET /runs/{id}` → full run incl. status, budget totals, PR link.
- `GET /runs/{id}/steps` → ordered step trace for the live/replay view.
- `POST /runs {repo, issue_number}` → manual trigger.
Stable field names = the run/step columns in §7.1. The frontend is a thin viewer over this; it has **no direct DB access**.

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
| `DATABASE_URL` | Postgres DSN | — |
| `REDIS_URL` | broker/cache | — |
| `WORKSPACE_ROOT` | scratch dir for clones | `/var/agent/workspaces` |
| `OBJECT_STORE_URL` / creds | artifacts | — |
| `MAX_STEPS` / `MAX_TOTAL_TOKENS` / `MAX_USD` / `MAX_WALLCLOCK_S` | budgets | 30 / 500k / 5.0 / 1800 |
| `SANDBOX_IMAGE`, `SANDBOX_CPUS`, `SANDBOX_MEMORY`, `SANDBOX_TIMEOUT_S` | sandbox limits | pinned / 1 / 2g / 300 |

**Secret handling rules:** secrets come only from the environment / a secrets manager (never committed, never in the system prompt, never passed into the sandbox); tokens are redacted from every log and error; the sandbox container receives **no** secrets or network.

## 9. Security model (threat-driven)

Threats and mitigations:
- **Malicious repo code** (the biggest one): runs only inside the sandbox — no network, read-only host, resource/time caps, non-root, dropped caps. It cannot reach the LLM key (host-side), the GitHub token (host-side), Postgres, or the internet.
- **Prompt injection via issue text or repo files:** the model can only act through the constrained tool surface; tools are path-confined and the destructive ones (push, PR) are performed by the orchestrator *after* the loop, not by the model directly. The model cannot exfiltrate secrets it never sees.
- **Token leakage:** tokens never persisted to git config, redacted from logs/errors, short-lived (GitHub App installation tokens preferred).
- **Webhook spoofing:** HMAC verification with constant-time compare; reject unsigned/oversized payloads.
- **Runaway cost/loops:** Budget controller + wall-clock + idempotency lock + bot-author webhook guard (so the agent never reacts to its own activity).
- **Supply chain:** sandbox and worker images pinned by digest; dependencies pinned.

`[DECISION D9]` Do we allow the agent to install dependencies inside the sandbox (needed to run some test suites) given the sandbox has no network? Options: (a) pre-bake common deps into the image, (b) a vetted offline package mirror, (c) a brief, audited network-allowed install phase before locking down. Default `[ASSUMPTION]` (a)+(b).

## 10. Sandbox execution detail (expanded)

Per execution: `docker run --rm --network none --read-only --user 65534:65534 --cap-drop ALL --pids-limit 256 --memory ${SANDBOX_MEMORY} --cpus ${SANDBOX_CPUS} -v ${workspace}:/work:rw -w /work ${SANDBOX_IMAGE} <cmd>` with a wall-clock kill. Stdout/stderr captured, truncated to the tool-result cap, exit code returned. The container is destroyed after each call (or pooled per-run if D6 says reuse). The worker that launches it needs access to a Docker daemon — `[DECISION D10]` Docker-out-of-Docker (mount the host socket — simplest, weaker boundary) vs a rootless/remote builder vs Kubernetes-native (e.g. a per-run Job). Default `[ASSUMPTION]` rootless Docker / dedicated daemon, never the privileged host socket in prod.

## 11. Observability

- **Metrics (Prometheus):** runs started/succeeded/failed by status; run duration histogram; steps per run; tokens & USD per run; sandbox executions + failures; GitHub API calls + rate-limit hits; queue depth & worker saturation. Derived from the structured trace the loop already emits.
- **Tracing (OpenTelemetry):** one span per run, child spans per step and per tool call (incl. sandbox exec and LLM call latency). Correlate by `run_id`.
- **Logs:** structured JSON, `run_id`-tagged, token-redacted, shipped to the log store; full per-run log archived to the object store.
- **Dashboards (Grafana):** throughput, success rate, p50/p95 run latency, cost/issue, failure breakdown, queue health.

## 12. Cost & time controls (consolidated)
- Per-run hard caps: `MAX_STEPS`, `MAX_TOTAL_TOKENS`, `MAX_USD`, `MAX_WALLCLOCK_S` (Celery hard/soft time limits).
- Prompt caching of the stable prefix (§6.6) to cut repeated-turn cost.
- Effort tuned per workload (`high` default; drop to `medium` for cheap repos, raise to `xhigh` for hard ones).
- Optional Task Budgets (D5) for self-moderation.
- Cost surfaced per run in Postgres + Grafana so regressions are visible.

## 13. Failure modes, recovery, retention
- **Clone/network failure:** retry with backoff; after N, terminal `error` with detail.
- **Indexing failure:** terminal `error`; never proceed to reason on an un-indexed repo.
- **Provider error / refusal:** captured as `provider_error` / `refused`; no PR; reason recorded.
- **Sandbox failure/timeout:** returned to the model as a tool error (it can adapt); repeated → run ends with `sandbox_error`.
- **No changes produced:** `no_changes`; comment + stop.
- **Worker crash mid-run:** the `runs` row stays `running`; a reaper marks runs stale after `MAX_WALLCLOCK_S` and either re-queues (infra cause) or fails them. Partial `run_steps` remain for debugging.
- **Idempotency:** deterministic branch + adoptive PR + `(repo, issue_number)` lock ⇒ re-delivery/re-run is safe.
- **Retention:** workspaces deleted at run end; artifacts/logs kept `[DECISION D11]` (default 30 days) then GC'd; Postgres rows kept indefinitely (or archived).

## 14. Deployment topology (where each piece sits)
- **Containers/images:** `api` (FastAPI), `worker` (Celery + orchestrator + retrieval + loop), `sandbox` (the execution image, pulled by workers). All pinned by digest, built in CI.
- **Stateful services:** PostgreSQL, Redis, object store (S3/MinIO), ChromaDB persistence volume, Prometheus/Grafana.
- **Kubernetes:** `api` Deployment + Service + Ingress (only `/webhooks/*` public); `worker` Deployment (HPA on queue depth); the sandbox runs as a child of the worker (D10) or as per-run K8s Jobs; Postgres/Redis as managed services or StatefulSets; secrets via K8s Secrets/External Secrets.
- **Helm:** one chart, values per environment (dev/staging/prod) — budgets, replica counts, image digests, resource limits.
- **CI/CD (GitHub Actions):** lint + unit tests (incl. the offline self-tests of each module) → build & scan images → push by digest → Helm deploy to staging → smoke test → promote to prod.

## 15. Repository / code layout (target)
```
auto-swe-agent/
├── agent/                  # the reasoning + integration library
│   ├── github.py           # REST client + git helpers + webhook parsing   [§6.1]  (built)
│   ├── retrieval.py        # tree-sitter + embeddings + ChromaDB           [§6.4]  (built)
│   ├── loop.py             # ReAct loop + Budget + tools                   [§6.5]  (built)
│   ├── sandbox.py          # Docker isolation                             [§6.7]  (to build)
│   └── providers/          # LLMProvider interface + anthropic + gemini   [§6.6]  (built)
├── app/                    # FastAPI gateway / webhook receiver           [§6.2]  (to build)
│   └── main.py
├── workers/                # Celery app + the run_issue orchestrator      [§6.3]  (to build)
│   └── tasks.py
├── db/                     # SQLAlchemy models + migrations (Alembic)     [§7.1]  (to build)
├── eval/                   # benchmark harness (e.g. SWE-bench-style)             (later)
├── monitoring/             # Prometheus rules + Grafana dashboards        [§11]   (later)
├── k8s/ , helm/            # manifests + chart                            [§14]   (later)
├── docs/                   # per-component deep-dives (retrieval, loop, github, …)
├── plan.md                 # THIS document — source of truth
├── requirements.txt
└── Claude.md / ReadMe.md
```

## 16. Open questions / decisions pending (please rule on these)

| ID | Decision | Options | My default |
|---|---|---|---|
| **D1** | Initial language support for target repos | Python-only first / Python+JS / language-agnostic via tree-sitter grammars | Python-first |
| **D2** | Model host-side, code sandbox-side | as designed / alternative | **as designed** |
| **D3** | GitHub auth | GitHub App (installation tokens) / PAT | App for prod, PAT for dev |
| **D4** | Auto-retry agent-logic failures? | yes / no | **no** (only infra retries) |
| **D5** | Adopt Task Budgets (beta) for self-moderation? | yes / no | start no, revisit |
| **D6** | Sandbox per-call vs per-run reuse | fresh each call / reuse within a run | reuse within a run |
| **D7** | Sandbox runtime hardening | Docker / gVisor / Kata | Docker + hardening first |
| **D8** | Vector store | ChromaDB / pgvector / Qdrant | ChromaDB |
| **D9** | Dependency install in a no-network sandbox | pre-bake / offline mirror / audited install phase | pre-bake + mirror |
| **D10** | How workers get Docker | host socket / rootless / K8s Jobs | rootless or K8s Jobs (not host socket in prod) |
| **D11** | Artifact/log retention window | 7 / 30 / 90 days | 30 days |
| **D12** | Embedding model | MiniLM (fast) / larger local model (quality) | MiniLM first |
| **D13** | Concurrency cap (runs in flight) | by worker replicas / global semaphore | HPA on queue depth + global cap |

## 17. Build order (proposed milestones)

1. **M1 — Orchestrator (glue what exists).** `workers/tasks.py`: issue → clone → index → loop → commit/push → PR, against a **trusted** repo, with the existing `github.py` + `loop.py` + `retrieval.py`. Run executes end-to-end on a real issue, file edits happen host-side, tests run via the loop's current (non-sandboxed) `run_tests`. Postgres `runs`/`run_steps` written. *This proves the pipeline.*
2. **M2 — Sandbox (`agent/sandbox.py`).** Move `run_tests`/code-exec into a hardened Docker container per §10. Required before pointing the agent at any untrusted repo.
3. **M3 — API + webhooks (`app/main.py`).** HMAC-verified `/webhooks/github` → enqueue; manual `POST /runs`; read endpoints (§7.5).
4. **M4 — Persistence & idempotency hardening.** Alembic migrations, dedupe table, idempotency lock, reaper for stale runs.
5. **M5 — Observability.** Prometheus metrics + OTel spans + Grafana dashboards.
6. **M6 — Deployment.** Dockerfiles, Helm chart, K8s manifests, GitHub Actions CI/CD.
7. **M7 — Eval harness.** Measured success rate on a benchmark set; tune effort/budgets/prompt.

## 18. Glossary
- **Run:** one autonomous attempt to resolve one issue.
- **Workspace:** the per-run host directory holding the cloned repo.
- **Orchestrator:** the worker code that executes a run end-to-end.
- **ReAct loop:** the Reason→Act→Observe model-driving loop.
- **Sandbox:** the network-isolated Docker container where untrusted code/tests run.
- **Provider:** an adapter implementing `LLMProvider` for a specific vendor.
- **Budget:** the hard caps (steps/tokens/USD/wall-clock) bounding a run.

---

*End of plan. Mark it up — every `[DECISION]` in §16 and every `[ASSUMPTION]` inline is open for your call, and any section can be revised on request (I'll bump the version in §0).*
