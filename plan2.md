# Autonomous SWE Agent — Master Plan & Source of Truth

> **Status:** ACTIVE — this is the single source of truth · **Version:** 3.3 · **Date:** 2026-07-30 · **Owner:** Nadeem
>
> **This document supersedes `plan.md` (v0.1) and absorbs `newplan.md` (multi-agent
> design + build order).** v2.0 described a single-loop agent where the backend was
> "not started". Since then the orchestrator, API, persistence layer, Docker Compose
> stack, and the three-agent split (Planner → Coder → Reviewer) have all been built.
> v3.0 re-baselines every section against the code that is actually in the tree as of
> 2026-07-30, and folds in a second deep-scan audit (§22) that found 16 integration
> defects — most of them in the seams between the agents, not inside them.
>
> **Read this if you read nothing else.** `newplan.md` remains useful as the
> narrative rationale for the multi-agent split and the cloud phasing; where the two
> disagree, **this document wins**.
>
> **Conventions:** `[DECISION]` = needs a human call before it's final;
> `[ASSUMPTION]` = current default, override if wrong; `MUST`/`SHOULD`/`MAY` =
> requirement strength.
>
> **Build state vocabulary — v3.0 introduces a third state, and it matters:**
> - **(built)** — implemented, exercised, believed correct.
> - **(built — defective)** — the code exists and runs, but a defect in §22 means it
>   does not deliver the feature it was written for. *This is the most dangerous
>   state in the project right now, because it looks finished from the outside.*
> - **(not started)** — no implementation.

---

## 0. Document control

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-06-28 | Initial full design draft (`plan.md`). |
| 2.0 | 2026-07-02 | Merged into a single source of truth. Absorbed plan.md; integrated deep-scan audit 1 — security hole H1, robustness bugs, hygiene (all applied, §17); rewrote the security model (§9). |
| 2.1 | 2026-07-27 | Appended §21 cloud deployment roadmap (Phases 0–5). |
| **3.0** | **2026-07-30** | **Full re-baseline.** Backend/API/persistence/Compose marked built. Multi-agent architecture (Planner/Coder/Reviewer) documented as first-class (§6.8–6.10, §4 rewritten). Absorbed `newplan.md`. **Added deep-scan audit 2 (§22) — 16 findings, all OPEN.** Added remediation work order (§23) and testing strategy (§24). Model/pricing table now delegates to code (§6.6) instead of duplicating figures that went stale. New decisions D14–D20. |
| **3.1** | **2026-07-30** | **M6 landed — all 4 Critical findings closed (F1, F2, F3, F4) plus F11.** Pipeline reordered so the Planner runs after clone+index with retrieval enabled; Reviewer now gets a staged diff and real test output; one Budget threaded through all three agents; a good fix survives a failed review round. Fixed a latent `None` subscript crash on the `max_tokens` result path. **First real integration tests added** (`tests/test_orchestrator_critical.py`, 5 tests) — §24 Layer 2 opened. 11 findings remain open (5 High, 6 Medium). |
| **3.3** | **2026-07-30** | **M9, M11 and most of M10 landed — F8, F12, F13, F14, F15, F16 closed; F9 half-closed.** `POST /runs` now requires a shared secret (fail-closed), honours a repo allowlist, and is rate limited. Guardrail paths normalised and deviations surfaced in the trace and PR. Reviewer failures are now visibly `review_status='unavailable'` instead of silently "approved". Red/Green extraction rewritten. Coder re-runs receive their own prior diff. Vector index dropped on cleanup. `requirements.txt` fully pinned. **29 tests.** **Only D19 (persistent index) and the decision-gated items remain — see §25.** |
| **3.2** | **2026-07-30** | **M7 landed — F5, F6, F7, F10 closed.** One run id now spans API, Celery, and the `runs` row, so the read API is reachable. Tool calls actually persist. `runs` is **append-only** with a partial unique index on active runs (migration `c4d1e88a5f27`), replacing the PK-mutating re-run path; the index also settles the concurrency race at the database, so **no Redis lock is needed** (D16/D17 resolved). Stale-run reaper added. Test harness extracted to `tests/conftest.py`; `tests/test_orchestrator_trace.py` added — **12 tests total**. **7 findings remain: 2 High (F8, F9), 5 Medium (F12–F16).** |

---

## 1. What we are building (one paragraph)

A production-grade service that watches one or more GitHub repositories, and when an
actionable issue appears, autonomously: clones the repo, builds a semantic +
structural understanding of the codebase, then drives **three cooperating LLM agents**
— a **Planner** that produces a structured repair plan, a **Coder** that executes it
through a Reason→Act→Observe loop with sandboxed test execution, and a **Reviewer**
that audits the resulting diff with fresh context — and, if the change is verified,
commits it to a branch and opens a pull request that references the issue and carries
the full evidence trail (understanding, root cause, Red→Green test output, reviewer
verdict). The entire path from "issue opened" to "PR opened" runs without a human in
the loop, under hard caps on time, tokens, and money, with every step persisted.

## 2. Goals and non-goals

**Goals**
- **G1. End-to-end autonomy:** webhook → PR, no human step.
- **G2. Safety first:** untrusted repo code and untrusted issue text must never
  compromise the host, leak secrets, or reach the network from the execution sandbox.
- **G3. Correctness gating:** the agent only opens a PR when its change exists and
  the repo's tests (or a defined check) pass; otherwise it reports why and stops.
- **G4. Cost/time bounded:** every run has hard ceilings (steps, tokens, USD,
  wall-clock) and is observable in real time. **Every LLM call in the system counts
  against those ceilings** — including Planner and Reviewer calls (§22 F4).
- **G5. Model-pluggable:** the reasoning core is not wedded to one vendor, selectable
  by config, **per agent role** (a strong model for planning/review, a cheaper one for
  the coding loop).
- **G6. Idempotent & re-runnable:** re-processing the same issue updates the same
  branch/PR rather than duplicating work.
- **G7. *(new in 3.0)* Trustworthy without a checkout.** A stranger reading the PR on
  GitHub must be able to judge it without pulling the branch: what the agent
  understood, why it thinks this is the root cause, the failing-then-passing test
  output, and what the reviewer flagged. **This is the goal the multi-agent split
  exists to serve** — and the one §22 F1/F2/F3 currently defeat.
- **G8. *(new in 3.0)* No silent degradation.** When a stage fails or is skipped, the
  system must say so — in the run record and in the PR body — rather than emitting
  output indistinguishable from the healthy path (§22 F15).

**Non-goals**
- **N1. A web frontend.** *Upgraded from "out of scope" to "deliberately not built."*
  GitHub **is** the interface: issue comment → PR diff → PR description → review
  status all render natively. The only UI-shaped surface we want is the read API
  (§7.5) for `curl`/debugging. This matches how Devin, Sweep, and Jules are actually
  used. Do not build a frontend.
- **N2.** Monorepo-scale indexing optimizations beyond what §6.4 specifies.
- **N3.** Fine-tuning or training any model. Hosted LLMs + a local embedding model only.
- **N4.** Languages beyond the initial set (`[DECISION D1]` — Python-first).
- **N5. *(new)* Kubernetes as a milestone.** K8s/Helm is a *learning project* attached
  to this system (§21 Phase 4), never a blocker for shipping.

## 3. System architecture (the six layers)

```
                                   ┌─────────────────────────────────────────────────────────┐
   GitHub  ──webhook──▶  (1) API   │  FastAPI receiver: verify HMAC, dedupe, enqueue job      │
   (issues, PRs)        Gateway    └───────────────┬─────────────────────────────────────────┘
                                                   │ enqueue (Celery → Redis broker)
                                                   ▼
                                   ┌─────────────────────────────────────────────────────────┐
                        (2) Queue  │  Redis broker + Celery workers. The orchestrator runs    │
                        & Workers  │  here; one job = one issue run = all three agents        │
                                   └───────────────┬─────────────────────────────────────────┘
            ┌──────────────────────────────────────┼───────────────────────────────────────────┐
            ▼                                      ▼                                           ▼
 ┌────────────────────┐   ┌──────────────────────────────────┐   ┌────────────────────────────────────┐
 │ (3) Codebase       │   │ (4) Reasoning core — 3 agents    │   │ (5) Sandbox (Docker)               │
 │  understanding     │   │   Planner  → structured plan     │   │  • ephemeral container per run     │
 │  • tree-sitter AST │◀──│   Coder    → ReAct loop + Budget │──▶│  • no network, ro host, cpu/mem cap│
 │  • embeddings      │   │   Reviewer → verdict on the diff │   │  • runs tests / untrusted code     │
 │  • ChromaDB vectors│   │   (provider-agnostic, per-role)  │   └────────────────────────────────────┘
 └─────────┬──────────┘   └──────────────────────────────────┘
           │ vectors                       │ trace, status, usage
           ▼                               ▼
 ┌────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ (6) State & Observability:  PostgreSQL (runs, run_steps, webhook_events)  ·  Redis (broker)          │
 │                              Object store (artifacts — deferred)  ·  Prometheus/Grafana (deferred)   │
 └────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                   │ git push + REST
                                                   ▼
                                                GitHub  (branch + Pull Request + issue comments)
```

**Current build state of the six layers (2026-07-30):**

| Layer | State | Evidence |
|---|---|---|
| (1) API gateway | **built** | `app/main.py` — HMAC verify, dedupe, enqueue, read API, health probes |
| (2) Queue & workers | **built** | `workers/tasks.py` — full orchestrator; `workers/__init__.py` — Celery app |
| (3) Codebase understanding | **built** *(index lifecycle defective — F9)* | `agent/retrieval.py` |
| (4) Reasoning core | **built** ✅ | `agent/planner.py`, `agent/loop.py`, `agent/reviewer.py`, `agent/schemas.py`. Handoffs repaired in v3.1 (F1–F4, F11); quality items F12–F15 remain |
| (5) Sandbox | **built** | `agent/sandbox.py` — cloud story still open (D10) |
| (6) State & observability | **built** ✅ | `db/` + 4 Alembic migrations + read API. Run identity unified and traces populated in v3.2 (F5, F6, F7, F10) |

> **The headline:** every box on this diagram exists. v2.0's problem was missing
> components; v3.0's was that the arrows between them leaked. v3.1 sealed the four
> critical leaks (F1–F4, F11) and v3.2 the identity/trace leaks (F5–F7, F10). **Seven
> findings remain open (§22)** — F8 (no auth) is the only one that blocks going live.

## 4. The life of an issue (end-to-end data flow)

> ✅ **As of v3.1 this matches the code.** `workers/tasks.py::run_issue` implements
> exactly this order, and `tests/test_orchestrator_critical.py` asserts the parts that
> were previously wrong. Bracketed numbers cite the component (§6) and store (§7).

1. **Issue opened on GitHub.** GitHub POSTs an `issues` webhook to
   `POST /webhooks/github` [API §6.2].
2. **Verify + dedupe + enqueue.** The API verifies the `X-Hub-Signature-256` HMAC
   (constant-time), rejects oversized bodies, checks `webhook_events.delivery_id`
   for a redelivery, parses the payload (`parse_webhook_event`), and decides if it is
   actionable (opened/reopened/labeled, not a PR, not a bot author). Non-actionable →
   `204`. Actionable → **mint the `run_id` here** [§22 F5], record the
   `webhook_events` row, enqueue the Celery task with that id as its `task_id`, and
   return `202`. *The API does no heavy work and holds no repo code.*
3. **A worker picks up the job.** A Celery worker [§6.3] claims the task and becomes
   the **orchestrator** for this one issue. It writes/updates the `runs` row to
   `running`.
4. **Fetch the code.** Clone into a per-run workspace `${WORKSPACE_ROOT}/${run_id}/repo`
   [§7.4]. Token scrubbed from `.git/config` immediately after clone. Default branch
   read from GitHub; deterministic working branch `agent/issue-<n>` created off it.
5. **Understand the code.** Run the indexer [§6.4]: tree-sitter AST chunking → local
   sentence-transformers embeddings → ChromaDB upsert, keyed by repo. *(Index lifecycle
   and cleanup: F9 / D19.)*
6. **PLANNER — structured analysis** [§6.8]. **This must happen after step 5, not
   before.** The Planner retrieves top-k relevant chunks for the issue text and emits
   a `PlannerOutput`: `understanding`, `root_cause_hypothesis`, `files_to_touch`,
   `plan_steps`, `test_strategy`, `risk_notes`. Persisted to `runs.planner_output` and
   as a `run_steps` row tagged `agent_name='planner'`. **Planner token usage accrues
   to the run Budget** (F4).
7. **Pre-work issue comment.** Post the Planner's `understanding` + `root_cause_hypothesis`
   + `plan_steps` back on the issue ("🤖 working on this, here's what I think"). This is
   the first visible signal to a human and must carry real analysis, which is only
   possible because step 6 now runs after indexing.
8. **CODER — Reason/Act/Observe** [§6.5]. The ReAct loop receives the plan as
   structured context. When `test_strategy` is present it follows a **Red→Green**
   workflow: write the test first, prove it fails, fix the code, prove it passes, and
   emit both outputs under `### Red (before fix)` / `### Green (after fix)` headings.
   Tools: `retrieve_context`, `read_file`, `edit_file`, `run_tests`, `list_dir` — all
   path-confined; `run_tests` dispatched into the sandbox [§6.7, §10]. Editing a file
   outside `files_to_touch` is recorded as a **deviation in the step trace**, not just
   a log line (F12).
9. **Observe + loop.** Each tool result (clipped) is fed back. Every step — model
   text, tool calls, args, results, token usage — is appended to the structured trace
   and **persisted incrementally** to `run_steps` [§7.1]. The Budget controller [§6.5]
   checks step/token/USD caps before every model call. Exhausting a cap stops the run
   cleanly with a status.
10. **REVIEWER — fresh-context audit** [§6.9]. Capture the **full** diff — staged, so
    newly created files are included (`git add -A && git diff --cached`, F2) — plus the
    real test output from the trace (not the Coder's prose summary, F11). The Reviewer
    sees the issue, the plan, the diff, and the test results, with **no access to the
    Coder's conversation**, and returns `verdict` / `concerns` / `confidence` /
    `summary`. Persisted to `runs.reviewer_output`.
11. **Review feedback loop (max `MAX_REVIEW_ROUNDS`, default 2).** On
    `request_changes`, feed the concerns back to the Coder **along with its own prior
    diff** (F13) and re-run. **The last known-good result is retained**: if a re-run
    exhausts its budget or errors, the pipeline falls back to the previous successful
    attempt rather than discarding the work (F3). After the final round, proceed
    regardless, flagged "needs human review".
12. **Produce the result.**
    - **No changes** in the working tree → status `no_changes`; comment the reason;
      **no PR**.
    - **Changes** → `git add -A && commit` (hooks/fsmonitor disabled, `--no-verify` —
      §9), push (`--force-with-lease`, token inline only), open (or adopt) a PR via
      REST [§6.1].
13. **Assemble the PR body — deterministically, no extra LLM call.** Everything needed
    is already persisted:
    ```markdown
    Closes #<n>
    ## Issue           ← Planner.understanding
    ## Root cause      ← Planner.root_cause_hypothesis
    ## Fix             ← Coder final summary, file by file
    ## Test Evidence   ← 🔴 Red (before) / 🟢 Green (after), or plan.test_strategy
    ## Reviewer notes  ← verdict + concerns + confidence, or an explicit
                         "review unavailable" when the stage failed (G8, F15)
    ---
    run <id> · N steps · N tokens · $X.XXXX   ← must sum ALL agents (F4)
    ```
14. **Finalize.** Write terminal status, PR number/URL, usage totals, and
    `test_evidence` to the `runs` row; comment the PR link on the issue; tear down the
    sandbox, the workspace, **and the run's vector-store entries** (F9).

**One-line mental model:** *code is fetched into a per-run host workspace; understanding
and file edits happen host-side against that workspace; all three agents call the LLM
from the worker (never the sandbox); only test execution is pushed into a
network-isolated container with `.git` masked; durable state → Postgres, vectors →
ChromaDB, queueing → Redis.*

## 5. Why this shape (key architectural decisions)

- **The model runs on the host, the code runs in the sandbox.** Inverting this would
  either deny the model network (it needs the LLM API) or grant the sandbox network
  (defeating isolation). `[DECISION D2 — as designed]`
- **One job = one issue = one Celery task — including all three agents.**
  `newplan.md` §1.4 suggested making each agent its own Celery task "for free retry
  and inspection". **We deliberately did not do that**, and should not. The three
  agents share a cloned workspace, a live ChromaDB index, and a sandbox container;
  splitting them across tasks would force us to either serialize that state or pin
  tasks to a specific worker, trading a large amount of complexity for retry
  semantics we do not want anyway (D4 — agent-logic failures must not auto-retry).
  Per-agent observability is achieved instead via `run_steps.agent_name`.
  `[DECISION D14 — one task, confirmed]`
- **Three agents, not one loop, because self-review does not work.** A single model
  reviewing its own work inside the same context rubber-stamps it. The Reviewer gets a
  **fresh context and adversarial framing** specifically to break that. The corollary
  is that the Reviewer's input must be *complete* — which is exactly what F2 breaks
  today, and why that finding is Critical rather than cosmetic.
- **Planning is a single structured call, not a loop.** The Planner has no tools and
  makes one call (plus at most one reparse retry). Planning is one-shot structured
  reasoning; giving it a tool loop would double cost for no gain. The Coder is the only
  agent that needs ReAct.
- **Structured handoffs, not prose.** `agent/schemas.py` defines `PlannerOutput` and
  `ReviewerOutput` as the inter-agent contract, with every field defaulted so partial
  or malformed LLM output degrades instead of crashing. *We should go one step further
  and use the providers' native structured-output support rather than parsing JSON out
  of free text* — `[DECISION D15]`, see §22 F-note.
- **Postgres for truth, Redis for transit, ChromaDB for vectors.** Each store does one
  job (§7).
- **Idempotency via deterministic branch names + adoptive PR creation.** Re-running
  issue #42 updates `agent/issue-42` and its existing PR.
- **Provider abstraction in front of the LLM, now per-role.** The loop depends on an
  interface, not a vendor SDK (§6.6). Each agent resolves its own provider
  (`PLANNER_PROVIDER` → `LLM_PROVIDER`), so a strong model can plan and review while a
  cheap one does the mechanical coding turns.
- **The workspace is untrusted after the sandbox touches it.** The clone, the model's
  edits, *and anything the repo's own tests wrote* live in one directory the
  orchestrator later runs `git` against. Host-side git is therefore a trust boundary,
  hardened in §9 (audit 1, H1).
- **The PR body is assembled deterministically from persisted state.** No summarizing
  LLM call at the end. Everything in the PR already exists in `runs` — which makes the
  PR body free, reproducible, and testable.

## 6. Component specifications

### 6.1 GitHub integration (`agent/github.py`, 904 lines) — **built**
- **REST client (`GitHubClient`):** `get_issue`, `get_default_branch`,
  `comment_on_issue`, `find_pull_request`, `create_pull_request`. Pure stdlib
  (`urllib`) — no dependency. Bounded exponential backoff + jitter on 429/5xx/network;
  403 retried only when rate-limited (honors `Retry-After`/`X-RateLimit-Reset`);
  idempotent PR creation (422 → adopt existing). Tokens redacted from all errors/logs.
  `transport`/`sleep`/`now` injectable → fully offline tests with a fake transport.
- **Git helpers:** `clone` (token scrubbed post-clone), `create_branch`
  (`agent/issue-<n>`), `commit_all` (returns bool so empty PRs are skipped), `push`
  (`--force-with-lease`, token inline never persisted), `configure_identity`,
  `has_uncommitted_changes`, `submit_changes` (commit→push→PR in one idempotent call).
  All git calls hardened — §9 (`_GIT_HARDENING`, `--no-verify`).
- **Webhook parsing:** `parse_webhook_event` with infinite-loop guards (skip PRs, bot
  authors, non-actionable actions).
- **Auth:** `[DECISION D3]` GitHub App (installation tokens, prod) vs PAT (dev).
- **Gap:** no `check_suite` / `workflow_run` handler → **CI failure recovery is not
  started** (§18 M16).
- **Self-test:** `python -m agent.github` (offline, exit 0).

### 6.2 API gateway (`app/main.py`, FastAPI) — **built** ✅
- **Endpoints (all implemented):**
  - `POST /webhooks/github` — HMAC verify → size check → `webhook_events` dedupe →
    parse → enqueue → `202`. `204` non-actionable, `403` bad signature, `413` oversized.
  - `POST /runs` — manual trigger `{repo, issue_number, use_sandbox}`. ✅ Requires
    `AGENT_API_TOKEN` (fail-closed), honours `AGENT_REPO_ALLOWLIST`, rate limited.
    Returns 401 / 403 / 429 / 503 as appropriate (F8).
  - `GET /runs` — keyset-paginated list, filters on `status` / `repo`.
  - `GET /runs/{id}` — DB first, Celery result backend as fallback.
  - `GET /runs/{id}/steps` — ordered trace, optional `?agent=planner|coder|reviewer`.
  - `GET /healthz` (liveness), `GET /readyz` (checks Redis **and** Postgres, 503 if
    either is down).
- ✅ **F5 fixed (v3.2):** `_enqueue_run()` mints one UUID used for Celery's `task_id`,
  the task's `run_id` kwarg, and `runs.id` — so the id returned to a caller resolves at
  both `/runs/{id}` and `/runs/{id}/steps`.
- ✅ **F8 fixed (v3.3):** `_require_api_token()`, `_check_repo_allowed()`, and
  `_check_rate_limit()` guard the manual trigger; the allowlist also applies to the
  webhook. See §9.
- **Not built:** `GET /metrics` (Prometheus); rate limiting on the webhook itself.
- **Self-test:** `python -m app.main` (TestClient + in-memory SQLite).

### 6.3 Queue & workers (Celery + Redis) — **built**
- **Broker:** Redis. **Result backend:** Redis/Postgres per `.env`.
- **Worker = orchestrator:** one Celery task `run_issue(repo, issue_number, use_sandbox)`
  executes the entire §4 pipeline. `time_limit` = `MAX_WALLCLOCK_S` (hard),
  `soft_time_limit` = `MAX_WALLCLOCK_S - 60` (graceful), `acks_late=True`.
  Compose runs `--concurrency=2`.
- **Retries:** infrastructure failures may retry with backoff; **agent-logic failures
  do not auto-retry** — re-running an LLM run blindly burns money.
  `[DECISION D4 — no]`
- **Signature:** `run_issue(repo, issue_number, use_sandbox=False, run_id=None)`. The API
  passes `run_id` and the identical Celery `task_id`; `_resolve_run_id()` falls back to
  the task id, then a fresh UUID, so `run_issue.delay(repo, n)` still works (F5).

### 6.4 Codebase understanding / retrieval (`agent/retrieval.py`) — **built** *(D19 open)*
- **Pipeline:** tree-sitter chunking (functions/methods/classes; decorated defs
  unwrapped; methods indexed individually + a class-header chunk to dodge embedding
  truncation; imports) → batched sentence-transformers embeddings → ChromaDB upsert
  with **delete-by-file first** → `build_call_graph` for structural neighbours.
  `assemble_context(query, repo, k, token_budget)` packs the most relevant chunks under
  a token budget.
- **Isolation:** results *are* correctly scoped — `retrieve()` filters on a `repo`
  metadata field set to `os.path.abspath(root)`. Cross-repo leakage into results does
  **not** occur.
- 🟡 **F9 half fixed (v3.3):** `drop_repo(root)` deletes a run's chunks and the
  orchestrator calls it during cleanup, so the collection no longer grows without bound.
  **Still open:** the index is keyed on the ephemeral workspace path, so the full repo is
  re-embedded on **every single issue**. Making it persistent and incremental means
  keying on the repo slug — `[DECISION D19]`, §25.2.
- **Token counting:** provider-neutral local estimate (~3.5 chars/token) for packing;
  exact counts come from the active provider. Never `tiktoken` (wrong for both vendors).
- **Self-test:** `python -m agent.retrieval` (needs ML deps; run in the Linux/WSL venv).

### 6.5 Coder agent — ReAct loop + Budget (`agent/loop.py`, 901 lines) — **built**
- **Loop:** `while not done and within budget: provider.complete(...) → if tool calls:
  dispatch (Act) → feed results (Observe); else finish`.
- **Tools:** `retrieve_context`, `read_file`, `edit_file` (exact-string replace /
  create), `run_tests`, `list_dir`. Paths confined by `_safe_path` (blocks `../`
  traversal); `run_tests` routed to the sandbox when supplied, else host-side against
  the workspace's own `.venv`.
- **Multi-agent entry point:** `run_with_plan(task, plan)` prepends the Planner's
  structured context and, when `test_strategy` is present, injects an explicit
  **TEST-FIRST (Red→Green)** instruction block. `extract_test_evidence(final_text)`
  parses the `### Red` / `### Green` sections back out for the PR body (fragile — F14).
- **Guardrail:** `_files_to_touch` warns when the Coder edits a file outside the plan.
  Currently exact-string match and log-only (F12).
- **Budget controller:** hard caps `max_steps`, `max_total_tokens`, `max_usd` from
  `.env`; checked before every model call, usage accrued after. **Known issue:** the
  Budget instance is shared across review rounds, which double-counts on re-run and
  can strand a good fix (F3, F4).
- **Fail-soft tools:** expected failures raise `ToolError` → returned to the model as
  `is_error` (recoverable); unexpected tool bugs are caught, logged, and also returned.
  A tool never crashes the loop — only budgets, completion, refusal, or a provider
  error end a run.
- **File I/O is LF-normalized** via bytes, so `\n`-based edits match on any platform.
- **Statuses:** `completed | max_tokens | refused | max_steps | token_budget |
  usd_budget | provider_error | index_error | error`.
- **Self-test:** `python -m agent.loop` (offline).

### 6.6 LLM provider abstraction (`agent/providers/`) — **built**
- **Interface (`base.py`):** `LLMProvider` Protocol — `complete`, `user_message`,
  `assistant_turn`, `tool_result_message`, `count_tokens`, `cost_usd`. Neutral value
  objects: `ToolSpec`, `ToolCall`, `Usage`, `LLMResponse`, `ProviderError`. **Agents
  import only these — never a vendor SDK.**
- **Adapters:** `anthropic_provider.py`, `gemini_provider.py`.
- **Selection:** `get_provider(name=..., model=...)` driven by `LLM_PROVIDER` /
  `LLM_MODEL`, with per-role overrides resolved by each agent (§6.8, §6.9).
- **Embeddings:** `all-MiniLM-L6-v2` via sentence-transformers, **local, in-process,
  no API cost, no network**. Its ~256-token chunk limit drives §6.4's chunking.
  `[ASSUMPTION A1 / DECISION D12]`

> **Model IDs and pricing are deliberately NOT duplicated here.** v2.0 pinned a table
> of model names and per-Mtok prices into this document; it drifted out of sync with
> `.env` and the adapters within weeks. **The `PRICING` dict in each provider module is
> the single source of truth**, and `.env` is the source of truth for which model is
> selected. When you change a model, change it there — this document points at it.
>
> 🔶 **`[DECISION D20]` — the configured defaults need re-validation.** `.env`
> currently selects `LLM_PROVIDER=gemini` / `LLM_MODEL=gemini-3.5-flash-lite` for all
> roles, with Anthropic and per-role overrides commented out. A flash-lite-class model
> is a reasonable Coder but a weak Planner and a **weak Reviewer** — and review quality
> is the whole point of the third agent. Recommended: keep the cheap model for the
> Coder loop (which spends the most tokens) and set `PLANNER_MODEL` / `REVIEWER_MODEL`
> to the strongest model you're willing to pay for. Re-validate every model id against
> the provider's current catalogue before the first cloud deploy.

- **Cost-control levers:** prompt caching of the stable prefix (system prompt + tool
  specs + retrieved context) — keep the cached prefix byte-stable (no timestamps/UUIDs
  in the system prompt); put volatile content last. `[DECISION D5]` Task Budgets.

### 6.7 Sandbox (`agent/sandbox.py`, 436 lines, Docker) — **built**
- **Lifecycle:** started once per run (`docker run -d ... sleep infinity`), commands
  dispatched via `docker exec`, destroyed at run end. `[DECISION D6 — reuse within a run]`
- **Isolation (all MUST — enforced in `_run_args`):** `--network none`; host FS
  read-only; only the run's workspace mounted rw; **`.git` masked with a read-only
  tmpfs** (§9 / §17 H1); non-root; `--cap-drop ALL` + `--security-opt
  no-new-privileges`; `--pids-limit`, `--memory`, `--cpus`; writable `/tmp` tmpfs;
  `HOME=/tmp`; **no secrets, no env passed in**. Wall-clock enforced twice: an
  in-container `timeout --signal=KILL` (kills just the offending process) plus an outer
  subprocess-timeout backstop (kills the container).
- **Resilience:** if the outer backstop killed the container, the next `exec()`
  transparently starts a fresh one (§17 M1).
- **Interface:** `run_tests(target)` returns exactly the loop's host shape
  (`"exit code: N\n<output>"`), so the loop targets the sandbox with no changes.
- **No new dependency:** shells out to the `docker` CLI.
- **Image:** `docker/sandbox.Dockerfile` (python:3.12-slim-bookworm + pytest pre-baked,
  since the container has no network — `[DECISION D9]`). Pin by digest in prod.
- 🔶 **Open:** `[DECISION D10]` — how workers get Docker **in the cloud**. Azure
  Container Apps does not offer the host socket. This blocks sandboxed execution on
  Phase 2 and must be decided before it (§21 Phase 1).
- **Self-test:** `python -m agent.sandbox` (degrades gracefully with no daemon).

### 6.8 Planner agent (`agent/planner.py`) — **built** ✅ 🆕
- **Job:** read the issue + retrieved codebase context, emit a structured repair plan.
  **Writes no code.**
- **Shape:** a single LLM call with `tools=[]`. On unparseable output, retries once
  with a nudge; on a second failure, falls back to using the raw text as
  `understanding`. Returns `(PlannerOutput, Usage)`.
- **Provider resolution:** `PLANNER_PROVIDER`/`PLANNER_MODEL` → `LLM_PROVIDER`/`LLM_MODEL`.
- **Contract:** `understanding`, `root_cause_hypothesis`, `files_to_touch`,
  `plan_steps`, `test_strategy`, `risk_notes`. `test_strategy` is prompted to be
  concrete enough (test file, function name, input, expected behaviour) that the Coder
  can write the test without guessing — this is what powers Red→Green.
- **Budget:** accepts a `budget=` parameter and accrues every model call into the shared
  run Budget, so Planner spend counts against the run's caps (§22 F4, fixed v3.1).
- ✅ **F1 fixed (v3.1):** the orchestrator now calls the Planner *after* clone + index
  with retrieval enabled, so `files_to_touch` is grounded in the actual repository.
  `skip_retrieval` remains for tests only and is documented as such.
- **Self-test:** `python -m agent.planner` (mock provider, no API key). Passing.

### 6.9 Reviewer agent (`agent/reviewer.py`) — **built** *(F15 open)* 🆕
- **Job:** audit the Coder's diff against the issue and the plan, with **fresh context
  and adversarial framing** — deliberately *not* sharing the Coder's conversation.
- **Shape:** a single LLM call with `tools=[]`. Returns `(ReviewerOutput, Usage)`.
  `build_feedback_task(...)` renders concerns back into a Coder prompt for round 2.
- **Provider resolution:** `REVIEWER_PROVIDER`/`REVIEWER_MODEL` → `PLANNER_*` →
  `LLM_*`. Review quality matters as much as planning quality, so it inherits the
  Planner's model by default.
- **Contract:** `verdict` (`approve` | `request_changes`), `concerns[]`, `confidence`
  (`high|medium|low`), `summary`. Checks correctness, completeness, regressions,
  security, style, and **test-first compliance**.
- **Budget:** accepts a `budget=` parameter and accrues its model call into the shared
  run Budget (§22 F4, fixed v3.1).
- ✅ **F2 fixed (v3.1):** the orchestrator now stages before diffing, so files the Coder
  *created* — normally the new test — are visible. ✅ **F11 fixed:** it receives real
  `run_tests` output rather than the Coder's prose summary.
- ✅ **F15 fixed (v3.3, D18):** it still fails open — a broken reviewer must never block
  a PR — but `ReviewerOutput.review_status` (`reviewed` | `unavailable`) now records
  whether a model actually judged the diff, and the PR body says so plainly when it did
  not. An unreviewed PR is no longer indistinguishable from an approved one (G8).

### 6.10 Inter-agent contracts (`agent/schemas.py`, 351 lines) — **built** 🆕
- `PlannerOutput` / `ReviewerOutput` dataclasses: pure data, no side effects,
  JSON-serializable via `asdict()` for DB persistence and API responses.
- `_extract_json()` handles the messy reality of LLM output — markdown fences, prose
  before/after the object, nested braces, braces inside strings — via a depth-tracking
  scan. `from_dict` coerces types and clamps enums (an invalid verdict becomes
  `approve`, an invalid confidence becomes `medium`).
- **Self-test:** `python -m agent.schemas`. Passing, with good edge-case coverage.
- 🔶 `[DECISION D15]` — this whole parsing layer exists because we ask for JSON in a
  prompt. Both providers support native structured output (Anthropic forced tool use;
  Gemini `response_schema`). Adding `complete_structured(schema=...)` to `LLMProvider`
  would make malformed output *impossible* rather than merely recoverable, and delete
  most of `_extract_json` plus the retry-nudge path in §6.8.

## 7. Data & storage architecture

### 7.1 PostgreSQL — system of record (`db/models.py`) — **built** ✅
- **`runs`** — one row per issue run. `id (uuid pk)`, `repo`, `issue_number`,
  `issue_title`, `provider`, `model`, `status`, `branch`, `pr_number`, `pr_url`,
  `steps_used`, `input_tokens`, `output_tokens`, `cost_usd`, `started_at`,
  `finished_at`, `error_detail`, `final_text`, and the multi-agent columns
  **`planner_output`**, **`reviewer_output`**, **`test_evidence`** (portable JSON —
  JSONB on Postgres, JSON on SQLite).
- **`run_steps`** — one row per agent step. `id`, `run_id (fk, cascade)`, `n`,
  **`agent_name`** (`planner|coder|reviewer`), `stop_reason`, `text` (clipped),
  `input_tokens`, `output_tokens`, `tools (json)`, `created_at`. Unique on
  `(run_id, n)`. Written incrementally so a crashed run is still observable.
- **`webhook_events`** — dedupe + audit. `delivery_id (unique)`, `event_type`, `repo`,
  `issue_number`, `action_taken`, `received_at`.
- **Migrations (Alembic, 4):** `ce67fdd77d09_initial` → `a3f82c1d4e91_add_multi_agent_columns`
  → `b7e4a9f12c03_add_test_evidence_column` → `c4d1e88a5f27_runs_append_only`. Single
  head; verified to apply cleanly end to end. *(The last two are untracked in git —
  commit them.)*
- ✅ **F6, F7, F10 fixed (v3.2), D16 resolved.** `runs` is **append-only**: every attempt
  inserts a new row, so history survives and no primary key is ever mutated.
  Idempotency comes from the partial unique index `uq_runs_active_issue` over
  `(repo, issue_number) WHERE status='running'` — at most one active run per issue,
  unlimited finished ones. `run_steps.tools` now actually persists.
- **Concurrency:** the partial index is also the race arbiter. A worker that loses the
  insert race catches `IntegrityError` and returns `skipped_duplicate`. No advisory lock
  is involved, which is why §7.2 no longer needs Redis for this.
- **Stale runs:** `_reap_stale_runs()` marks `running` rows older than
  `MAX_WALLCLOCK_S + 300s` as `stale`, so a crashed worker cannot block an issue forever.
- Postgres holds **no large blobs** (§7.4).
- **Self-test:** `python -m db.models` (in-memory SQLite). Passing.

### 7.2 Redis — transit & ephemeral — **built (broker) / partial**
- Celery **broker** — built. Result backend — built.
- **Idempotency locks: no longer needed here.** v3.2 settles the race with a partial
  unique index in Postgres (§7.1). That is stronger than an advisory lock — it cannot be
  bypassed by a client that forgets to take it, and it survives a Redis flush — so Redis
  stays a pure broker and gains no correctness responsibility.
- **Not built:** default-branch cache, GitHub rate-limit state, webhook rate limiting
  (M9).

### 7.3 Vector store — ChromaDB — **built** *(D19 open)*
- Chunk embeddings + metadata (file path, symbol, start/end lines, repo). Written by
  §6.4, read by `retrieve_context`. Location env-overridable (`CHROMA_DIR`), opened
  lazily (§17 M2). Chunk IDs and the delete-by-file filter keyed on **absolute** paths
  (§17 L1).
- 🟡 **F9 / `[DECISION D19]`** — the intended design is "one collection per repo,
  persisted, incremental across runs". v3.3 shipped option **(b)**: keep the index
  per-run and delete `where={'repo': ...}` during cleanup via `retrieval.drop_repo()`.
  That closes the unbounded growth. Option **(a)** — key the index on the **repo slug**
  so it persists and is genuinely incremental, removing the full re-embed on every issue
  — is still open. It is the better steady state but needs staleness handling and a
  persistent volume in Phase 2, so it is the owner's call (§25.2).
- `[DECISION D8]` ChromaDB vs pgvector/Qdrant if we outgrow it. Default ChromaDB.

### 7.4 Object store + workspace filesystem — **partial**
- **Per-run workspace:** `${WORKSPACE_ROOT}/${run_id}/repo` — **built**, created per
  run and `rmtree`'d in the orchestrator's `finally` block.
- **Artifacts** (final diff/patch, full logs, sandbox output) → object store —
  **not started**. Today the diff exists only transiently in memory and the final text
  is clipped into Postgres. Worth adding once there is real traffic (§18 M15).

### 7.5 API contract — **built (see F5)**
- `GET /runs?status=&repo=&limit=&cursor=` → keyset-paginated summaries.
- `GET /runs/{id}` → full run incl. `planner_output`, `reviewer_output`, `test_evidence`.
- `GET /runs/{id}/steps?agent=` → ordered step trace, filterable per agent.
- `POST /runs {repo, issue_number, use_sandbox}` → manual trigger.
- Field names mirror the §7.1 columns. **No direct DB access from any client.**
- 🔴 F5 makes the two `/{id}` routes unreachable with the id callers are actually given.

## 8. Configuration & secrets (env vars)

| Var | Purpose | Default | State |
|---|---|---|---|
| `LLM_PROVIDER` | `anthropic` \| `gemini` | `gemini` *(per `.env`)* | built |
| `LLM_MODEL` | model id | see `.env` | built |
| `LLM_EFFORT` | `low\|medium\|high\|xhigh\|max` | `high` | built |
| `PLANNER_PROVIDER` / `PLANNER_MODEL` | per-role override for §6.8 | falls back to `LLM_*` | built |
| `REVIEWER_PROVIDER` / `REVIEWER_MODEL` | per-role override for §6.9 | falls back to `PLANNER_*` → `LLM_*` | built |
| `PLANNER_RETRIEVAL_K` / `PLANNER_TOKEN_BUDGET` | Planner context size | 10 / 6000 | built *(unused until F1)* |
| `MAX_REVIEW_ROUNDS` | Reviewer↔Coder feedback rounds | 2 | built |
| `COMMENT_ON_START` | post the pre-work issue comment | `true` | built |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | model auth | — | built |
| `GITHUB_TOKEN` *(dev)* / App creds *(prod)* | repo auth | — | built |
| `GITHUB_WEBHOOK_SECRET` | HMAC verification | — | built |
| `MAX_WEBHOOK_BODY_BYTES` | oversized-payload guard | 1 MB | built |
| `GITHUB_API_URL` | enterprise override | `api.github.com` | built |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | commit identity | agent defaults | built |
| `DATABASE_URL` | Postgres DSN | — | built |
| `REDIS_URL` | broker/cache | `redis://localhost:6379/0` | built |
| `WORKSPACE_ROOT` | scratch dir for clones | `/var/agent/workspaces` | built |
| `CHROMA_DIR` / `EMBEDDING_CACHE_DIR` | vector store + embed cache | under `agent/` | built |
| `MAX_STEPS` / `MAX_TOTAL_TOKENS` / `MAX_USD` / `MAX_WALLCLOCK_S` | budgets | 30 / 500k / 5.0 / 1800 | built |
| `SANDBOX_IMAGE`, `SANDBOX_CPUS`, `SANDBOX_MEMORY`, `SANDBOX_PIDS_LIMIT`, `SANDBOX_TIMEOUT_S`, `SANDBOX_TMPFS_SIZE`, `SANDBOX_USER`, `DOCKER_BIN` | sandbox limits | pinned / 1 / 2g / 256 / 300 / 64m / auto / `docker` | built |
| **`AGENT_API_TOKEN`** | shared secret for `POST /runs`; **unset = endpoint disabled (503)** | — | ✅ built |
| **`AGENT_REPO_ALLOWLIST`** | comma-separated `owner/repo` the agent may work on; empty = no limit. Enforced on `/runs` (403) and the webhook (204) | — | ✅ built |
| **`RUNS_RATE_LIMIT` / `RUNS_RATE_WINDOW_S`** | per-client, per-process cap on `POST /runs` | 10 / 60s | ✅ built |
| `OBJECT_STORE_URL` / creds | artifacts | — | not started |

**Secret handling rules:** secrets come only from the environment / a secrets manager
(never committed, never in a system prompt, never passed into the sandbox); tokens are
redacted from every log and error; the sandbox container receives **no** secrets and
**no** network.

## 9. Security model (threat-driven)

Audit 1 (§17) hardened this substantially; audit 2 (§22) found one new hole (F8).

- **Malicious repo code (the biggest threat).** Runs only inside the sandbox: no
  network, read-only host FS, resource/time caps, non-root, dropped caps. It cannot
  reach the LLM key (host-side), the GitHub token (host-side), Postgres, or the
  internet. **built**
- **Sandbox → host escape via `.git` (§17 H1).** Untrusted test code could plant
  `.git/hooks/*` or set `core.fsmonitor=<cmd>`; that code does nothing in the sandbox
  but **would execute on the host** on the next `git add/commit/push` — with the
  GitHub token in the environment. Mitigated in two layers: `.git` shadowed by a
  read-only tmpfs in the sandbox, and every host-side git call runs with
  `core.hooksPath=/dev/null -c core.fsmonitor=` plus `commit --no-verify`. **built**
- **Prompt injection via issue text or repo files.** The model can only act through the
  constrained, path-confined tool surface; the destructive operations (push, PR) are
  performed by the orchestrator *after* the loop, not by the model. The model cannot
  exfiltrate secrets it never sees. **built**
- **Token leakage.** Never persisted to git config (clone scrubs the remote; push
  passes auth inline), redacted from logs/errors, short-lived App tokens preferred. **built**
- **Webhook spoofing.** HMAC constant-time compare; unsigned rejected; **an unset
  `GITHUB_WEBHOOK_SECRET` rejects everything** (fail-closed, correct); oversized bodies
  rejected; `webhook_events` dedupe. **built**
- **Manual trigger (F8).** ✅ **built (v3.3).** `POST /runs` requires `AGENT_API_TOKEN`
  via `Authorization: Bearer` or `X-Agent-Token`, compared in constant time, and is
  **fail-closed** — an unset token disables the endpoint (503) rather than leaving it
  open, so a deployment cannot expose it by forgetting a variable.
- **Arbitrary-target guard.** ✅ **built.** `AGENT_REPO_ALLOWLIST` restricts which
  repositories may be targeted, on both `POST /runs` (403) and the webhook (204).
  Authentication alone would still let a leaked token target anything. 🔶 **Currently
  unset — set it before exposing the service (§25.1).**
- **Rate limiting.** ✅ **built** on `POST /runs` (`RUNS_RATE_LIMIT` per
  `RUNS_RATE_WINDOW_S`, per client IP). 🔶 In-process only — not distributed across API
  replicas. The webhook is still unlimited, but it is HMAC-signed, so the exposure is
  much smaller.
- **Runaway cost/loops.** ✅ Budget controller + wall-clock + bot-author webhook guard,
  with **all three agents accruing into one budget** since v3.1 (F4), so the enforced
  ceiling matches real spend.
- **Supply chain.** ✅ `requirements.txt` is fully pinned to exact versions (F16).
  🔶 Sandbox and worker images should still be pinned by digest in prod.

`[DECISION D9]` Dependency install in a no-network sandbox: (a) pre-bake common deps,
(b) a vetted offline mirror, (c) a brief audited network-allowed install phase.
Default `[ASSUMPTION]` (a)+(b).

## 10. Sandbox execution detail (expanded)

Per run, one long-lived container:
```
docker run -d --rm --name <n> --network none --read-only \
  -v <workspace>:/work:rw --workdir /work \
  --tmpfs /tmp:rw,exec,size=<S> [--tmpfs /work/.git:ro] \
  --user <uid:gid> --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit <P> --memory <M> --cpus <C> --env HOME=/tmp \
  <image> sleep infinity
```
Each test run dispatches `docker exec [timeout --signal=KILL Ns] sh -lc "<cmd>"`; the
container is `docker rm -f`'d at the end. Stdout/stderr captured and truncated to the
tool-result cap; exit code returned.

`[DECISION D10 — OPEN, now blocking]` How workers get Docker: host socket (simplest,
weakest boundary — what Compose does today via a mounted socket) vs rootless/remote
daemon vs Kubernetes-native per-run Jobs. **Azure Container Apps offers none of these**,
so this decision gates §21 Phase 2. Options: rootless DinD in the worker container;
replace Docker with a subprocess sandbox (`nsjail`/`bubblewrap`); or run unsandboxed on
cloud **against trusted repos only** and restore the sandbox at AKS (Phase 4). Whichever
you pick, record it here and update `agent/sandbox.py`.

## 11. Observability — **built** ✅

**What exists and now works end to end:**
- Durable step trace in `run_steps`, tagged per agent, written incrementally, **with
  tool calls, args, and error flags** (F6 fixed in v3.2).
- Read API for runs and traces (§7.5), filterable by agent, **reachable with the id
  callers are actually given** (F5 fixed in v3.2).
- Full run history per issue — re-runs no longer overwrite the previous attempt (F10).
- Structured `run_id`-tagged logging throughout the orchestrator.
- `GET /healthz` / `GET /readyz` (Redis + Postgres).

Between them these give you `curl /runs/{id}/steps` as a complete replay of what all
three agents did — which was the entire point of the persistence layer, and was not
achievable before v3.2.

**Not started** — and deliberately deferred until there is real unattended traffic
(§21 Phase 3): Prometheus `/metrics`, OpenTelemetry spans per run/step/tool call,
Grafana dashboards, per-run log archival to an object store. `monitoring/` is an empty
directory holding the slot.

## 12. Cost & time controls (consolidated)
- Per-run hard caps: `MAX_STEPS`, `MAX_TOTAL_TOKENS`, `MAX_USD`, `MAX_WALLCLOCK_S`.
- ✅ **Fixed in v3.1 (F4).** One `Budget` is created per run in the orchestrator and
  threaded through Planner, Coder, and Reviewer; all three accrue into it and totals are
  read off it rather than summed per stage. The caps in `.env` now bound the run's
  *total* spend, and the PR footer, issue comment, and `runs.cost_usd` all agree.
- Per-role model selection (§6.6) is the biggest real lever: the Coder loop dominates
  token spend, so run it cheap and spend on Planner/Reviewer.
- Prompt caching of the stable prefix (§6.6).
- Optional Task Budgets (D5).
- Cost surfaced per run in Postgres and in the PR body.

## 13. Failure modes, recovery, retention

| Failure | Handling | State |
|---|---|---|
| Clone / network failure | retry with backoff; then terminal `github_error` | built |
| Indexing failure | terminal `index_error`; never reason on an un-indexed repo | built |
| Provider error / refusal | `provider_error` / `refused`; no PR; reason recorded | built |
| Sandbox failure/timeout | returned to the model as a tool error; container auto-restarts; repeated → `sandbox_error` | built |
| No changes produced | `no_changes`; comment + stop | built |
| **Planner fails** | continue with an empty plan; the Coder gets the raw issue | built |
| **Reviewer fails** | 🔶 defaults to `approve` — indistinguishable from a real approval (F15, G8) | needs work |
| **Review re-run exhausts budget** | falls back to the last `completed` result and opens the PR from that (F3) | ✅ v3.1 |
| Duplicate webhook delivery | `webhook_events.delivery_id` unique → ignored | built |
| Concurrent runs on one issue | partial unique index on active runs; the loser catches `IntegrityError` and returns `skipped_duplicate` (F10) | ✅ v3.2 |
| Worker crash mid-run | the `running` row is marked `stale` by `_reap_stale_runs()` once older than `MAX_WALLCLOCK_S + 300s`, freeing the issue. Partial `run_steps` remain for debugging | ✅ v3.2 |
| Retention | workspaces deleted at run end; **Chroma entries never cleaned (F9)**; Postgres rows kept indefinitely and now accumulate per attempt; artifacts `[DECISION D11 — 30 days]` | partial |

## 14. Deployment topology
- **Containers/images — built:** `docker/api.Dockerfile`, `docker/worker.Dockerfile`,
  `docker/sandbox.Dockerfile` + `docker/build-sandbox.sh`.
- **Local stack — built:** `docker-compose.yml` with five services — `postgres`,
  `redis`, `migrate` (runs Alembic once and exits; `api`/`worker` wait on it), `api`,
  `worker` (`--concurrency=2`, Docker socket mounted for the sandbox). Scale with
  `docker-compose up --scale worker=3`.
- **Cloud — not started.** See §21. Azure Container Apps is the target (Phase 2); AKS
  is a later, optional learning migration (Phase 4).
- **CI/CD — not started.** No workflow beyond an empty `.github/`. §21 Phase 5.

## 15. Repository / code layout (actual, 2026-07-30)
```
auto-swe-agent/
├── agent/                       # the reasoning + integration library
│   ├── planner.py     (401)     # Planner agent — structured repair plan   [§6.8]  built*
│   ├── loop.py        (901)     # Coder agent — ReAct + Budget + tools     [§6.5]  built
│   ├── reviewer.py    (399)     # Reviewer agent — fresh-context audit     [§6.9]  built*
│   ├── schemas.py     (351)     # PlannerOutput / ReviewerOutput contracts [§6.10] built
│   ├── github.py      (904)     # REST client + hardened git + webhooks    [§6.1]  built
│   ├── retrieval.py   (533)     # tree-sitter + embeddings + ChromaDB      [§6.4]  built*
│   ├── sandbox.py     (436)     # hardened Docker isolation                [§6.7]  built
│   └── providers/               # LLMProvider + anthropic + gemini         [§6.6]  built
├── app/main.py        (651)     # FastAPI gateway + read API               [§6.2]  built*
├── workers/tasks.py   (892)     # THE ORCHESTRATOR — full pipeline         [§6.3]  built*
├── db/
│   ├── models.py      (395)     # runs / run_steps / webhook_events        [§7.1]  built*
│   ├── session.py     (118)     # lazy engine + session factory                    built
│   └── migrations/versions/     # 3 Alembic revisions (1 untracked — commit it)
├── docker/                      # api / worker / sandbox Dockerfiles               built
├── docker-compose.yml           # 5-service local stack                    [§14]   built
├── scripts/setup_postgres.sh    # (setup_webhook.sh — not started, §18 M13)
├── tests/                       # integration suite — 29 tests             [§24]   built
│   ├── conftest.py                   # shared harness: real run_issue + fakes
│   ├── test_orchestrator_critical.py # §22 F1-F4, F11       (M6)
│   ├── test_orchestrator_trace.py    # §22 F5-F7, F10       (M7)
│   └── test_agent_quality.py         # §22 F9, F12-F15      (M10/M11)
├── docs/                        # per-component design docs (8 files)
├── plan2.md                     # THIS document — the source of truth
├── newplan.md                   # multi-agent narrative + cloud phasing (absorbed here)
├── plan.md                      # historical v0.1 draft (superseded)
├── Claude.md                    # AI-assistant working context
├── test_api.py                  # ⚠️ NOT a test — a manual smoke script; rename (§25.3)
├── requirements.txt             # fully pinned to exact versions                    built
└── eval/ monitoring/ k8s/ helm/ frontend/   # all EMPTY placeholders
```
`*` = built but carries an open §22 defect. **`frontend/` should be deleted** — N1 says
we are not building one.

## 16. Open decisions

| ID | Decision | Options | Current default |
|---|---|---|---|
| D1 | Initial language support | Python-only / +JS / agnostic | Python-first |
| D2 | Model host-side, code sandbox-side | as designed / alt | **as designed** |
| D3 | GitHub auth | App / PAT | App (prod), PAT (dev) |
| D4 | Auto-retry agent-logic failures? | yes / no | **no** (infra only) |
| D5 | Adopt Task Budgets? | yes / no | start no |
| D6 | Sandbox per-call vs per-run | fresh / reuse | reuse within a run |
| D7 | Sandbox runtime hardening | Docker / gVisor / Kata | Docker + hardening |
| D8 | Vector store | ChromaDB / pgvector / Qdrant | ChromaDB |
| D9 | Deps in a no-network sandbox | pre-bake / mirror / audited install | pre-bake + mirror |
| **D10** | **How workers get Docker in cloud** | host socket / rootless DinD / nsjail / K8s Jobs / unsandboxed-trusted-only | 🔴 **OPEN — blocks §21 Phase 2** |
| D11 | Artifact/log retention | 7 / 30 / 90 days | 30 days |
| D12 | Embedding model | MiniLM / larger local | MiniLM first |
| D13 | Concurrency cap | replicas / global semaphore | HPA + global cap |
| **D14** 🆕 | One Celery task vs task-per-agent | one / per-agent | **one task — confirmed (§5)** |
| **D15** 🆕 | Structured output mechanism | JSON-in-prompt + parse / native provider schemas | 🔶 **move to native** |
| **D16** 🆕 | `runs` table shape | unique `(repo,issue)` upsert / append-only + partial index | ✅ **append-only — DONE v3.2** |
| **D17** 🆕 | Who mints `run_id` | API (pass as Celery `task_id`) / task | ✅ **API — DONE v3.2** |
| **D18** 🆕 | Reviewer failure semantics | fail-open silently / fail-open **but visibly** / fail-closed | ✅ **fail-open visibly — DONE v3.3** |
| **D19** 🆕 | Chroma index lifecycle | per-repo persistent (slug-keyed) / per-run ephemeral+cleanup | 🔶 **ephemeral+cleanup shipped; persistent still OPEN — §25.2** |
| **D20** 🆕 | Model defaults per role | all cheap / cheap Coder + strong Planner/Reviewer | 🔶 **OPEN — §25.2** |

## 17. Deep-scan audit 1 — 2026-07-02 — **ALL CLOSED** *(historical)*

A senior-engineer review of every built module: 1 high (security), 2 medium, 4 low,
plus hygiene. All fixes are in the tree and the offline self-tests pass.

- **🔴 H1 — Sandbox → host escape via `.git`** *(security)* — FIXED. Two-layer
  mitigation documented in §9. (`agent/sandbox.py`, `agent/github.py`)
- **🟠 M1 — A single test timeout stranded the rest of the run** — FIXED. `exec()`
  detects a started-then-killed sandbox and transparently restarts it. (`agent/sandbox.py`)
- **🟠 M2 — `retrieval.py` did real work at import time** — FIXED. Lazy
  `get_collection()`, lazy cache mkdir, env-overridable dirs. (`agent/retrieval.py`)
- **🟡 L1 — Index keys mixed relative & absolute paths** — FIXED via `os.path.abspath`
  at the top of the index loop. (`agent/retrieval.py`)
- **🟡 L2 — CLI budget flags silently overrode `.env`** — FIXED; flags default to `None`.
  (`agent/loop.py`)
- **🟡 L3 — Sandbox startup failure crashed the CLI** — FIXED. (`agent/loop.py`)
- **🟡 L4 — `assemble_context` could `KeyError` on sparse metadata** — FIXED.
  (`agent/retrieval.py`)
- **🧹 Hygiene** — `Claude.md` rewritten (had described a different project); stray
  `retrieval_output.txt` removed; `.gitattributes` added for LF; misleading comments
  claiming OpenAI embeddings corrected (it is local sentence-transformers).

**Deliberately not changed:** `max_output_tokens` default of 8192 (raise if real runs
show `max_tokens` truncation); `provider.count_tokens` kept but unused by `Budget`
(usage from each response is correct and free); Gemini `assistant_turn` echoing
`resp.raw` is safe today but fragile.

## 18. Build order — where we actually are

### Completed
1. **M1 — Orchestrator (`workers/tasks.py`).** ✅ Full pipeline: issue → clone →
   branch → index → agent → submit. Workspace + sandbox teardown in `finally`.
2. **M2 — Sandbox integration.** ✅ `use_sandbox` threaded through the orchestrator;
   Docker socket mounted in Compose. *(Cloud story still open — D10.)*
3. **M3 — API + webhooks (`app/main.py`).** ✅ HMAC, dedupe, enqueue, manual trigger,
   read API, health probes.
4. **M4 — Persistence & idempotency.** ✅ SQLAlchemy models, 3 Alembic migrations,
   `webhook_events` dedupe, `(repo, issue_number)` guard. *(Partially defective —
   F6, F7, F10; no stale-run reaper.)*
5. **M5 — Multi-agent split.** ✅ `planner.py`, `reviewer.py`, `schemas.py`;
   `run_with_plan` + test-first Red→Green prompting; review feedback loop; rich PR
   body; pre-work issue comment; per-agent step tagging; multi-agent DB columns.
   *(Handoffs were defective at every seam; repaired by M6 below.)*
6. **M6 — Pipeline correctness.** ✅ **(v3.1)** F1, F2, F3, F4, F11 all closed, plus a
   latent `None`-subscript crash. First integration tests added.
7. **M7 — Trace & identity integrity.** ✅ **(v3.2)** F5, F6, F7, F10 closed. Run identity
   unified, trace populated, `runs` append-only + reaper. 12 integration tests.

8. **M9 — Pre-public security.** ✅ **(v3.3)** F8 closed: token, allowlist, rate limit.
9. **M11 — Quality polish.** ✅ **(v3.3)** F12–F15 closed.
10. **M8 — Test harness.** ✅ **(v3.3)** 29 tests; `requirements.txt` pinned (F16).

### Next
11. **§21 Phase 0 — run it against real issues.** Nothing in §22 blocks this any more.
    Set `AGENT_REPO_ALLOWLIST` first (§25.1).
12. **M10 (rest) — persistent slug-keyed index.** Needs D19; do it after Phase 0 tells
    you how slow indexing really is.

### Then — the remaining roadmap
> Numbered M13+ so they never collide with the remediation milestones M6–M12 in §23.

13. **M13 — Webhook setup script.** `scripts/setup_webhook.sh --repo owner/name
    --agent-url https://.../webhooks/github`, registering the hook via the GitHub API
    so there is no manual per-repo config. *(~half a day.)*
14. **M14 — Cloud deploy.** §21 Phase 2 — Azure Container Apps. Requires D10 resolved
    and M9 done.
15. **M15 — Observability & artifacts.** §21 Phase 3 — start with the platform's
    built-in logs/metrics and **one real alert**; graduate to Prometheus/OTel/Grafana
    only when the built-ins bind. Add object-store artifact upload (§7.4).
16. **M16 — CI failure recovery.** New `check_suite`/`workflow_run` webhook handler in
    `agent/github.py`: on a failed check, fetch the log, re-invoke the Coder with the
    failure as new context, push a fix commit, comment explaining it. *(2–3 days.
    Highest-value **new** feature after remediation.)*
17. **M17 — Eval harness.** `eval/` — measured success rate on a benchmark set; tune
    effort/budgets/prompts against a number instead of a vibe.
18. **M18 *(optional)* — GitHub App.** Removes per-repo webhook setup. Do this only
    once M13's script is actually limiting you.
19. **M19 *(optional)* — Kubernetes/Helm/CI-CD.** §21 Phases 4–5. A deliberate
    learning project, never a blocker (N5).

## 19. How to run what exists today

```bash
# ── Offline self-tests — no API key, network, or daemon needed ──
python -m agent.schemas     # inter-agent contracts + JSON extraction
python -m agent.planner     # Planner (mock provider)
python -m agent.reviewer    # Reviewer (mock provider)
python -m agent.loop        # Coder tools + path-traversal guards
python -m agent.github      # REST client + webhook parsing + git helpers
python -m agent.sandbox     # sandbox isolation (skips live checks with no daemon)
python -m db.models         # ORM + constraints (in-memory SQLite)
python -m workers.tasks     # orchestrator wiring + DB round-trip
python -m app.main          # HMAC + endpoints (TestClient + SQLite)
python -m agent.retrieval   # index + retrieve (needs ML deps; run in WSL/Linux)

# ── Single-agent CLI run against a local repo ──
python -m agent.loop "Fix the off-by-one in paginate()" \
    --workspace /path/to/repo --auto-index [--sandbox]

# ── Full stack ──
docker compose up --build          # postgres + redis + migrate + api + worker
curl localhost:8000/readyz         # expects {"status":"ready"}
curl -XPOST localhost:8000/runs -H 'content-type: application/json' \
     -d '{"repo":"owner/name","issue_number":42}'

# ── Live webhook (local) ──
ngrok http 8000
# register the ngrok URL + GITHUB_WEBHOOK_SECRET as a webhook on a test repo
```

```bash
# ── Integration tests (offline, no API key) ──
python -m pytest tests/ -v   # 29 tests: pipeline correctness, trace/identity, quality

# ── Inspect a run (works now that the ids match — §22 F5) ──
curl localhost:8000/runs/<run_id>            # planner_output, reviewer_output, evidence
curl localhost:8000/runs/<run_id>/steps      # full trace, tool calls included
curl "localhost:8000/runs/<run_id>/steps?agent=planner"
```

> ⚠️ `test_api.py` in the repo root is a manual provider smoke script, not a test
> module, despite the name. The real suite is the nine `python -m <module>` self-tests
> plus `tests/`. Note that none of the self-tests cross a module boundary — which is
> precisely why every §22 finding survived. See §24.

## 20. Glossary
- **Run:** one autonomous attempt to resolve one issue.
- **Workspace:** the per-run host directory holding the cloned repo.
- **Orchestrator:** the Celery task that executes a run end-to-end (`workers/tasks.py`).
- **Planner / Coder / Reviewer:** the three agents (§6.8, §6.5, §6.9). "The agent"
  without qualification usually means the Coder.
- **ReAct loop:** the Reason→Act→Observe model-driving loop (`agent/loop.py`).
- **Handoff:** a structured object passed between agents (`PlannerOutput`,
  `ReviewerOutput`) — the contract in `agent/schemas.py`.
- **Red→Green:** the test-first workflow — write the test, prove it fails (Red), fix,
  prove it passes (Green). Both outputs go in the PR body.
- **Review round:** one Reviewer verdict plus, if changes were requested, one Coder
  re-run. Capped by `MAX_REVIEW_ROUNDS`.
- **Sandbox:** the network-isolated Docker container where untrusted tests run, with
  `.git` masked (`agent/sandbox.py`).
- **Provider:** an adapter implementing `LLMProvider` for a specific vendor.
- **Budget:** the hard caps (steps/tokens/USD/wall-clock) bounding a run.

## 21. Cloud deployment roadmap

> Cloud is deliberately deferred until the product itself is proven. **Follow the
> phases in order — each is a hard prerequisite for the next.**

### Phase 0 — Prove the agent works (before touching cloud at all)
Run the Compose stack; `ngrok http 8000`; register a real webhook on a low-stakes test
repo; open 5–10 real issues of varying difficulty and watch what the agent produces.
**Do not move to Phase 1 until it resolves issues reliably.**

> **v3.3 amendment: Phase 0 is ready to run.** The Planner sees the code, the Reviewer
> sees the whole diff, every run is inspectable via `GET /runs/{id}/steps`, and the
> manual trigger is authenticated. Nothing in §22 blocks it.
>
> Two things to do first, both one-liners: **set `AGENT_REPO_ALLOWLIST`** to your test
> repo (§25.1), and **check `AGENT_API_TOKEN` is set** — `POST /runs` is fail-closed and
> will return 503 without it.

### Phase 1 — Close the gaps before anything goes public
1. **Auth + allowlist on `POST /runs`** (F8) — a shared-secret header plus a repo
   allowlist.
2. **Resolve D10 — sandbox in the cloud.** Container Apps gives no host Docker socket.
   Evaluate rootless DinD; a subprocess sandbox (`nsjail`/`bubblewrap`) needing no
   daemon; or running unsandboxed against **trusted repos only**, restoring isolation
   at Phase 4. Record the decision in §10 and update `agent/sandbox.py`.
3. **Pin `requirements.txt`** (F16) — cloud builds must be reproducible.

### Phase 2 — First real deployment: Azure Container Apps
PaaS, not Kubernetes: hand it an image, it handles servers, networking, and scaling —
the fastest path from "works locally" to "running unattended".
1. Push `api`, `worker`, `sandbox` images to **Azure Container Registry**.
2. Provision **Azure Database for PostgreSQL** (Flexible Server) and **Azure Cache for
   Redis**.
3. Deploy `api` and `worker` as two Container Apps, wired via env vars / Key Vault
   secrets. **Mount a persistent volume for `CHROMA_DIR`** if D19 lands as "per-repo
   persistent".
4. Point the real GitHub webhook at the Azure URL; retire ngrok.
5. Let it run unattended for a few days on the test repo.
Uses the $100 GitHub Student Pack Azure credit. *Learning focus: container registries,
managed databases, cloud secrets, what PaaS means between raw VMs and K8s.*

### Phase 3 — Observability, now earned
Start with Container Apps' **built-in logs and metrics** — they answer ~80% of
questions with zero setup. Add **one real alert** ("a run failed" / "a run cost > $1").
Graduate to Prometheus + Grafana + OTel traces only once the built-ins bind. *Learning
focus: the difference between logs, metrics, and traces in practice.*

### Phase 4 — Kubernetes, deliberately, as its own project
Migrate the now-proven, now-live system from Container Apps to **AKS**. Because it
already works on Container Apps, this is a **learning exercise with a safety net**.
This is where `k8s/` and `helm/` get built for real. AKS also makes D10 easier —
node-pool socket access or DinD sidecars that Container Apps can't support.

### Phase 5 — CI/CD
GitHub Actions: lint + `pytest` (§24 — needs M8 to be worth anything) → build 3 images
→ push to ACR → `helm upgrade` to AKS. Every push to `main` deploys.

### Why this order
| Skip | Risk |
|---|---|
| Skipping remediation (§23) | You evaluate your bugs instead of your product, on a cloud bill |
| Skipping Phase 0 | You pay cloud bills to debug a broken product |
| Skipping Phase 1 | `POST /runs` is publicly abusable the moment ngrok starts |
| K8s before Phase 2 | Two hard unknowns at once ("does the agent work?" + "how does K8s work?") — this is how projects stall |
| Observability before Phase 2 | Dashboards with no real traffic to watch |

---

## 22. Deep-scan audit 2 — 2026-07-30 — multi-agent integration 🆕

A full review of the tree after the M5 multi-agent split. **16 findings: 4 critical,
5 high, 7 medium.** Every module's own self-test passed; **every finding below lives in
the seams between modules**, which is exactly the blind spot of a per-module self-test
suite (§24).

**The through-line:** the three agents are individually well built. What was broken is
what they hand each other. The Planner planned without seeing the code, the Reviewer
reviewed without seeing the new files, and the accounting didn't add up — so the
multi-agent split was paying three models' worth of cost for roughly one model's worth
of signal.

**Status as of v3.3:** ✅ **15 of 16 findings closed.** F1–F8 and F10–F16 are all
fixed and covered by tests. **F9 is half-closed** — the per-run leak is fixed, but the
full-repo re-embed on every issue needs `[DECISION D19]` from the owner (§25).
Nothing here blocks Phase 0 any more.

### ✅ Critical — ALL FIXED in v3.1 (M6)

*Each fix is locked in by a test in `tests/test_orchestrator_critical.py`; each test was
confirmed to fail against the pre-fix code before being accepted.*

**F1 — The Planner never saw the codebase.** ✅ **FIXED**
`workers/tasks.py` called `run_planner()` at step 2, *before* the clone at step 3, with
`skip_retrieval=True` and a workspace of `Path(".")`. `newplan.md` §1.1 specifies the
Planner's input as "issue text + top-k retrieved chunks"; it got issue text alone. Every
`files_to_touch` entry was guessed from an issue title by a model that had never seen
the repository — and that guess then constrained the Coder's guardrail and framed the
Reviewer.
**Fix applied:** pipeline reordered to clone → branch → index → **Planner** → comment
(§4). `skip_retrieval` dropped; the real cloned workspace is passed. A retrieval failure
still degrades to issue-text-only planning inside `run_planner()` rather than failing
the run. `skip_retrieval`'s docstring now warns against using it in the real pipeline.
**Test:** `test_planner_runs_after_clone_and_index_with_retrieval` — asserts the
workspace exists, is a git checkout, is not the cwd fallback, and that indexing
happened first.

**F2 — The Reviewer could not see newly created files.** ✅ **FIXED**
The diff was captured with plain `git diff`, which excludes untracked files — *verified
empirically*: a modified file appears, a new `test_new.py` does not. The Reviewer's
prompt explicitly asks it to check "does the diff include a new test that would have
failed before the fix", so on precisely the runs where test-first worked, it saw no test
and raised a false concern, burning a review round on a phantom.
**Fix applied:** new `_capture_diff()` helper stages (`git add -A`) then diffs the index
(`git diff --cached`). It routes through `agent.github._git` rather than raw subprocess,
so the call inherits `_GIT_HARDENING` — which matters, because by this point the repo's
own tests have run against that workspace and host-side git is a trust boundary
(§9 / audit 1 H1).
**Test:** `test_reviewer_diff_includes_files_the_coder_created`. Against the pre-fix
code this test sees a *completely empty* diff, since the scripted Coder only creates
files.

**F3 — A good fix was destroyed when a review re-run failed.** ✅ **FIXED**
The review loop reuses one `ReActAgent` and therefore one `Budget`. If round 1 consumed
most of `MAX_STEPS` (default 30) and the Reviewer requested changes, the re-run hit the
wall and returned `max_steps`. Because `agent_result` had been overwritten and PR
submission was gated on `agent_result.status == "completed"`, **no PR was opened at
all** — and the `finally` block then `rmtree`d the workspace, so a perfectly good
round-1 fix was gone permanently. A reviewer asking for a cosmetic improvement could
delete the work.
**Fix applied:** a `best_result` holds the last `completed` `RunResult`. A re-run only
replaces it on success; a re-run that raises or ends non-`completed` is logged and the
loop breaks, keeping the earlier fix. Submission is gated on `best_result is not None`.
**Test:** `test_failed_review_rerun_still_opens_a_pr` — caps `MAX_STEPS` so round 1
succeeds and the re-run cannot finish, then asserts a PR is still opened carrying the
round-1 fix.

**F4 — Budget double-counted the Coder and ignored Planner/Reviewer.** ✅ **FIXED**
`b` was the *same* `Budget` object across rounds, so `result["steps"] += b.steps` added
an already-cumulative value to itself — round 1 counted twice (same for tokens and USD).
Meanwhile `planner_usage` / `rev_usage` were persisted to `run_steps` but never added to
the run totals, never added to `cost_usd`, and **never checked against any cap** — two
of three agents ran unbounded. The reported cost was inflated and understated at once,
and G4 was not actually enforced.
**Fix applied:** the orchestrator creates **one** `Budget` for the run and passes it to
`ReActAgent` and to both `run_planner()` and `run_reviewer()` via a new `budget=`
parameter. Planner and Reviewer accrue through a small duck-typed `_accrue()` helper, so
neither module has to import `Budget` and both keep their standalone self-tests. Totals
are now *read off* the shared budget by `_sync_budget()` instead of being accumulated
per stage, which removes the double-count by construction.
**Test:** `test_budget_covers_all_three_agents_without_double_counting` — asserts exact
cost, token, and step totals across all three agents.

### 🟠 High — F5, F6, F7 FIXED in v3.2 (M7); F8, F9 still open

**F5 — Two different UUIDs both called `run_id`.** ✅ **FIXED**
`app/main.py` returned `async_result.id` (a **Celery task id**) as `"run_id"`, while
`workers/tasks.py` minted its own `uuid.uuid4()` and stored *that* as `runs.id`. The id
a caller received could therefore never match a DB row: `GET /runs/{id}` always fell
through to the Celery backend (losing `planner_output`, `reviewer_output`,
`test_evidence`) and `GET /runs/{id}/steps` **404'd every time**. The one endpoint N1
says is worth having did not work with the id anyone had.
**Fix applied (D17):** a new `_enqueue_run()` helper in `app/main.py` mints one UUID and
uses it for **both** Celery's `task_id` and a `run_id=` kwarg on the task, so the caller's
id resolves in the database *and* in the result backend. `run_issue` takes an optional
`run_id` and resolves it via `_resolve_run_id()`: explicit argument → Celery task id →
fresh UUID, so a bare `run_issue.delay(repo, n)` still works.
**Tests:** `test_run_id_passed_in_is_the_id_persisted`,
`test_run_id_falls_back_to_a_generated_uuid`, plus two assertions in the `app.main`
self-test.

**F6 — Tool calls were never persisted; the trace table was hollow.** ✅ **FIXED**
`agent/loop.py` builds each step dict with key `"tools"`; `workers/tasks.py` read
`step_data.get("tool_calls")`. The keys never matched, so `run_steps.tools` was **NULL
for every Coder step ever recorded**. §1.4 of `newplan.md` called this "your
observability layer at near-zero extra cost" — it was writing empty rows. The module
self-tests missed it because they build `RunStep` objects directly instead of going
through `_persist_step`.
**Fix applied:** read `"tools"`. One key — but the reason it survived is the interesting
part, and the fix is only trustworthy because the new test goes through the real writer
rather than constructing a `RunStep` by hand.
**Tests:** `test_run_steps_persist_tool_calls` (asserts tool names, args, and
`is_error` round-trip), `test_all_three_agents_appear_in_the_trace`.

**F7 — The re-run path mutated a primary key and orphaned its own children.** ✅ **FIXED**
`run_db = existing` then `run_db.id = run_id` rewrote the PK in place. Twelve lines
later, `query(RunStep).filter_by(run_id=existing.id).delete()` — but `existing` **was**
`run_db`, so `existing.id` was already the *new* id. It deleted nothing, and the old
`run_steps` were left pointing at a primary key that no longer existed.
**Fix applied:** the PK is never mutated. Every attempt inserts a new row (see F10).
**Test:** `test_rerunning_an_issue_preserves_the_previous_run`.

**F8 — `POST /runs` had no authentication.** ✅ **FIXED** *(security — see §9)*
No dependency, no key, no allowlist. It accepted an arbitrary `repo` slug and enqueued a
run, so anyone who found the URL could make the agent clone arbitrary repositories and
burn the LLM budget. Note the build-order hazard this exposed: §21 put "get a live URL"
*before* "security hardening" — **inverted, and an ngrok tunnel counts as live.**
**Fix applied:** `POST /runs` now requires `AGENT_API_TOKEN`, presented as either
`Authorization: Bearer <token>` or `X-Agent-Token`, compared in constant time. It is
**fail-closed**: an unset token disables the endpoint with 503 rather than leaving it
open, mirroring how `GITHUB_WEBHOOK_SECRET` already behaves, so a deployment cannot
expose it by forgetting a variable. `AGENT_REPO_ALLOWLIST` restricts which repositories
may be targeted (403 on `/runs`, silent 204 on the webhook) — necessary because auth
alone still lets a leaked token target anything. A per-process rate limit
(`RUNS_RATE_LIMIT` / `RUNS_RATE_WINDOW_S`) bounds runaway scripts.
🔶 **Known limitation:** the rate limit is in-process, so with multiple API replicas each
enforces its own budget. Adequate for now; revisit if you scale the API out.
**Tests:** six checks in the `app.main` self-test — missing token → 401, wrong token →
401, both header forms → 202, non-allowlisted repo → 403, limit → 429, unset token → 503.

**F9 — The vector store grows without bound and re-embeds everything every run.**
🟡 **HALF FIXED — the leak is closed; the re-embed needs `[DECISION D19]` (§25).**
**Fix applied:** `retrieval.drop_repo(root)` deletes every chunk indexed under a path,
and the orchestrator calls it during cleanup before deleting the workspace. The
collection no longer accumulates a full copy of the repository per run.
**Still open:** because the index is keyed on the ephemeral workspace path, each run
still re-embeds the entire repository from scratch. Fixing that means keying the index
on the **repo slug** so it persists and is genuinely incremental — a design change with
a real consequence (staleness handling, a persistent volume in §21 Phase 2), so it is
the owner's call.
**Test:** `test_vector_index_is_dropped_on_cleanup`.
One global collection `'code'`; each run indexes from a unique ephemeral path
(`.../workspaces/{run_id}/repo`), so every run writes a *complete new copy* of the
repo's chunks under a new key, and cleanup never removes them. ~100 runs on a
1000-chunk repo leaves ~100k permanently dead chunks. Query *results* are safe (the
`repo` metadata filter works), so this is cost/latency/storage, not correctness.
**Fix (D19):** key the index on the repo slug so it persists and is genuinely
incremental, or delete `where={'repo': ...}` during cleanup.

### 🟡 Medium — correctness, quality, and hygiene

**F10 — Idempotency destroyed history and raced.** ✅ **FIXED**
The unique constraint on `(repo, issue_number)` meant only one run per issue could ever
exist — re-running overwrote the record, a strange property for a system whose selling
point is a durable audit trail. The guard was also a check-then-insert with no lock, so
two near-simultaneous webhooks both saw "no existing run". And because a crashed worker
left `status='running'` forever with no reaper (§13), a single crash blocked an issue
permanently.
**Fix applied (D16):** `runs` is now **append-only**. The total unique constraint is
replaced by a *partial* unique index over `(repo, issue_number) WHERE status='running'`
(migration `c4d1e88a5f27`), so at most one run is active per issue while finished runs
accumulate. A `_reap_stale_runs()` pass marks `running` rows older than
`MAX_WALLCLOCK_S + 300s` as `stale` before the idempotency check, so a crashed worker
cannot block an issue forever.
**On the Redis lock:** the original plan called for one. It turned out to be
unnecessary — the partial unique index makes the database itself the arbiter, so a
worker that loses the insert race simply catches `IntegrityError` and stands down. That
is strictly more reliable than an advisory lock (it cannot be bypassed, and it survives
a Redis flush) and it avoids adding a dependency. Redis stays a pure broker.
**Tests:** `test_rerunning_an_issue_preserves_the_previous_run`,
`test_second_run_is_skipped_while_one_is_active`,
`test_stale_run_is_reaped_so_the_issue_is_not_blocked_forever`, plus the partial-index
checks in the `db.models` self-test.

**F11 — The Reviewer's "TEST RESULTS" were not test results.** ✅ **FIXED**
What was passed is `result["final_text"]` — the Coder's *prose summary*. The real test
output lives in the tool results inside the step trace, and the trace clips those to 300
chars, which is too short to review against.
**Fix applied:** `ReActAgent` now keeps `last_test_output` — the most recent `run_tests`
result at full length, captured in `_dispatch()`. The orchestrator passes that to the
Reviewer and refreshes it between review rounds.
**Test:** `test_reviewer_gets_real_test_output_not_prose`.

**F12 — The `files_to_touch` guardrail couldn't work and was invisible.** ✅ **FIXED**
Exact string matching meant `src/parser.py`, `./src/parser.py`, and an absolute path all
read as violations. It only emitted a `log.warning` — not in the step trace, not shown
to the model, not in the PR. `newplan.md` §1.2 says the Coder "must flag this back
rather than silently doing it", which did not happen.
**Fix applied:** `ReActAgent._plan_deviation()` resolves both the edited path and each
planned path against the workspace before comparing, so equivalent spellings match. A
real deviation is appended to the tool result the model sees, recorded in
`agent.plan_deviations`, and rendered in the PR body under **Plan deviations**. An empty
or absent `files_to_touch` constrains nothing, so it no longer flags every edit.
**Tests:** `test_guardrail_does_not_fire_on_equivalent_paths`,
`test_guardrail_flags_a_real_deviation`, `test_guardrail_is_inert_without_a_plan`,
`test_plan_deviation_reaches_the_pr_body`.

**F13 — The Coder re-ran with amnesia.** ✅ **FIXED**
`agent.run(feedback_task)` starts a fresh message list, so on round 2 the Coder had the
reviewer's concerns and the original issue but no memory of what it just did. It
re-derived everything — burning the budget that then triggered F3.
**Fix applied:** `build_feedback_task()` takes `prior_diff` and `prior_summary`, and the
orchestrator passes the staged diff plus the previous round's summary. The prompt also
states plainly that the earlier edits are **already applied**, so the second pass is an
edit rather than a rediscovery.
**Test:** `test_feedback_task_carries_the_prior_diff`.

**F14 — `extract_test_evidence` was fragile.** ✅ **FIXED**
Marker-string scanning with an arithmetic bug: `red_end = green_start - len("### Green
(after fix)")` subtracted a fixed length even when the *shorter* `### Green` marker had
matched, and could run backwards past the section start. A following loop usually
repaired it — but not when Green preceded Red.
**Fix applied:** a single regex pass over the headings in the order they actually
appear, with each section delimited by its neighbour instead of by marker arithmetic.
Tolerant of heading level, the optional parenthetical, trailing punctuation, and
wrapping code fences with info strings.
**Test:** `test_extract_test_evidence_is_robust`, eight parametrised cases including the
Green-before-Red ordering that broke the old implementation.

**F15 — The Reviewer failed open, invisibly.** ✅ **FIXED**
Provider error → approve. Parse failure → approve. Invalid verdict → approve. Combined
with F2, most degraded paths yielded a cheerful approval a human could not distinguish
from a real one.
**Fix applied (D18):** it still fails open — a broken reviewer must never block a PR —
but `ReviewerOutput` now carries `review_status` (`reviewed` | `unavailable`) separately
from `verdict`, with a `was_reviewed` convenience property. When review was unavailable
the PR body says so prominently and asks for manual review instead of printing a verdict.
Stored in the existing JSON column, so no migration was needed.
**Tests:** `test_unavailable_review_is_flagged_in_the_pr`, `test_healthy_review_is_not_flagged`,
`test_reviewer_parse_failure_marks_review_unavailable`.

**F16 — No automated tests, no pinned dependencies.** ✅ **FIXED**
`pytest --collect-only` collected **zero tests**; `test_api.py` is a manual provider
smoke script that merely looks like one. `requirements.txt` had **zero `==` pins**.
**Fix applied:** `tests/` now holds **29 integration tests** over a shared harness
(§24), and `requirements.txt` pins every dependency to an exact version verified to
resolve. `test_api.py` is still misleadingly named — see §25.

---

## 23. Remediation work order 🆕

Sequenced by dependency and value. **Do not start M13 (CI recovery) or §21 Phase 2
(cloud) until M6–M9 are done** — building on defective handoffs multiplies the work.

### ✅ M6 — Pipeline correctness *(F1, F2, F3, F4, F11)* — **DONE (v3.1)**
All five landed as one pass through `workers/tasks.py::run_issue`, plus small supporting
changes in `agent/loop.py`, `agent/planner.py`, and `agent/reviewer.py`.

1. ✅ Planner moved to after clone + index, retrieval enabled, pre-work comment moved
   with it. *(F1)*
2. ✅ `_capture_diff()` stages before diffing, via the hardened git wrapper. *(F2)*
3. ✅ `best_result` retains the last known-good `RunResult`; submission is gated on it.
   *(F3)*
4. ✅ One `Budget` threaded through all three agents; totals read off it rather than
   accumulated. *(F4)*
5. ✅ `ReActAgent.last_test_output` carries real test output to the Reviewer. *(F11)*
6. ✅ Bonus: fixed a latent `TypeError` — `result.get('error', 'unknown')[:500]` returned
   `None` (the key exists with a `None` value, so the default never fires) and crashed
   the result-comment path whenever the Coder ended as `max_tokens`. Surfaced by the F3
   regression check.

**Definition of done — met.** Verified by `tests/test_orchestrator_critical.py`
(5 tests, all passing), each confirmed to fail against the pre-fix code. All 8 module
self-tests still pass.

### ✅ M7 — Trace & identity integrity *(F5, F6, F7, F10)* — **DONE (v3.2)**
1. ✅ `_enqueue_run()` mints one UUID for Celery's `task_id` and the task's `run_id`;
   `_resolve_run_id()` handles the fallbacks. *(F5, D17)*
2. ✅ Step key corrected so tool calls persist. *(F6)*
3. ✅ `runs` is append-only; partial unique index `uq_runs_active_issue`; PK never
   mutated. Migration `c4d1e88a5f27`, verified to apply cleanly. *(F7, F10, D16)*
4. ✅ Stale-run reaper added. **The Redis lock was dropped as unnecessary** — the partial
   unique index settles the race at the database, which is stronger and adds no
   dependency. *(F10)*

**Definition of done — met.** Verified by `tests/test_orchestrator_trace.py` (7 tests):
the `run_id` a caller receives resolves to a `runs` row, Coder steps carry non-null
`tools` with faithful args, re-running preserves the prior run and its steps, a
concurrent run is skipped, and a stale row is reaped rather than blocking the issue.
Each test was confirmed to fail against the pre-fix code.

### M8 — Test harness *(F16)* — **started; extend alongside M7**
`tests/test_orchestrator_critical.py` exists and covers the M6 conditions (5 tests). It
establishes the pattern: real `run_issue`, fake GitHub client, scripted fake provider,
real temp git repo, in-memory SQLite. **Extend it with the M7 assertions** (run-id
resolution, non-null `run_steps.tools`, history preserved across re-runs). Still to do:
pin `requirements.txt`.

### ✅ M9 — Pre-public security *(F8)* — **DONE (v3.3)**
`AGENT_API_TOKEN` (fail-closed, constant-time, two header forms) + `AGENT_REPO_ALLOWLIST`
(enforced on `POST /runs` *and* the webhook) + per-process rate limiting. Six checks in
the `app.main` self-test. **Remaining caveat:** the rate limit is per API process, not
distributed.

### 🟡 M10 — Index lifecycle *(F9, D19)* — **half done (v3.3)**
✅ `retrieval.drop_repo()` + cleanup call closes the unbounded-growth leak.
🔶 **Still needs D19:** keying the index on the repo slug so it persists across runs and
stops re-embedding the whole repository every issue. See §25.

### ✅ M11 — Quality polish *(F12, F13, F14, F15)* — **DONE (v3.3)**
Guardrail paths normalised and deviations surfaced to the model, the trace, and the PR;
Coder re-runs receive their own prior diff and summary; evidence extraction rewritten as
a single ordered regex pass; reviewer failures recorded as `review_status='unavailable'`
and called out in the PR body.

### M12 — Structured output migration *(D15)*
Add `complete_structured(schema=...)` to `LLMProvider`; implement for both adapters;
retire `_extract_json` and the retry-nudge. Do this **after** M6–M9 — it is a
refactor, not a fix.

---

## 24. Testing strategy 🆕

**The problem, stated plainly:** there are nine good module self-tests and **zero tests
that cross a module boundary**. All 16 findings in §22 are boundary defects. F6 is the
cleanest illustration — the DB self-test constructs a `RunStep` by hand and passes,
while the real writer has never once persisted a tool call.

**The rule going forward: every inter-agent contract gets a test that exercises the
real writer/reader, not a hand-built stand-in.**

### Layer 1 — module self-tests *(exists, keep)*
`python -m <module>` for each of the nine modules. Fast, offline, no API key. Keep the
convention; every new module gets one.

### Layer 2 — integration tests *(12 tests — `tests/`, harness in `conftest.py`)*
Running the real `run_issue` against:
- a **fake `GitHubClient`** (the injectable `transport` already supports this),
- a **scripted fake provider** returning canned Planner JSON, Coder tool calls, and
  Reviewer JSON in sequence,
- a **real temp git repo** as the workspace,
- **SQLite in-memory** for the DB.

Cases that would have caught §22:
| Test | Catches | State |
|---|---|---|
| Planner runs post-index against the cloned repo, retrieval on | F1 | ✅ v3.1 |
| Reviewer's diff contains a Coder-created new file | F2 | ✅ v3.1 |
| Review re-run failure still opens a PR from round 1 | F3 | ✅ v3.1 |
| `cost_usd` == planner + coder + reviewer, counted once | F4 | ✅ v3.1 |
| Reviewer gets real `run_tests` output, not prose | F11 | ✅ v3.1 |
| `run_id` handed to the caller resolves to a `runs` row | F5 | ✅ v3.2 |
| `run_steps.tools` non-null, args faithful, after a tool step | F6 | ✅ v3.2 |
| All three agents appear in the trace, in order | F6 | ✅ v3.2 |
| Re-running an issue preserves the prior run's steps | F7, F10 | ✅ v3.2 |
| A second run is skipped while one is active | F10 | ✅ v3.2 |
| A stale run is reaped rather than blocking the issue | F10 | ✅ v3.2 |
| `POST /runs`: 401 / 403 / 429 / 503 paths | F8 | ✅ v3.3 |
| Workspace cleanup removes the run's vector entries | F9 | ✅ v3.3 |
| Guardrail ignores equivalent path spellings, flags real ones | F12 | ✅ v3.3 |
| Plan deviations reach the PR body | F12 | ✅ v3.3 |
| Feedback task carries the Coder's prior diff | F13 | ✅ v3.3 |
| Red/Green extraction survives reordering and fences | F14 | ✅ v3.3 |
| An unavailable review is flagged, not silently approved | F15 | ✅ v3.3 |

> **A test that passes before and after the fix proves nothing.** Every test in the
> table above was confirmed to *fail* against the pre-fix code before being accepted.
> Keep doing that — regress the fix, watch the test go red, restore.

### Layer 3 — live smoke *(manual, keep)*
`docker compose up` + ngrok + a real issue on a test repo. This is §21 Phase 0 and is
the only thing that exercises real model behaviour. **Not a substitute for Layer 2** —
it is slow, costs money, and is non-deterministic.

### Layer 4 — eval harness *(M14, later)*
`eval/` — measured success rate on a benchmark set, so prompt and budget changes can be
judged against a number.

### CI (§21 Phase 5)
Layers 1 and 2 run on every push — both are offline and need no API key, so there is no
excuse not to gate on them. Layers 3 and 4 stay manual.

---

## 25. What needs a decision from the owner 🆕

Everything in §22 that could be fixed without a judgement call has been. What remains
needs your input, roughly in the order it will bite.

### 25.1 Blocking the next milestone

**`[D10]` — how the sandbox runs in the cloud.** *Blocks §21 Phase 2.* Azure Container
Apps gives no host Docker socket, which is how the sandbox works today. Options:
rootless Docker-in-Docker inside the worker; replace Docker with a daemon-free
subprocess sandbox (`nsjail` / `bubblewrap`); or run **unsandboxed against trusted repos
only** and restore isolation at AKS (Phase 4). *Recommendation: the third, for your own
repositories, with the allowlist enforced — it is honest about the trade and unblocks
everything else. Do not point it at a repo you did not write until this is resolved.*

**`AGENT_REPO_ALLOWLIST` — set it before the first tunnel.** Currently unset, which
means no restriction. `.env` has a commented line ready. This is one line and it is the
difference between "the agent works on my repo" and "the agent works on whatever a
leaked token asks for".

### 25.2 Cost and quality

**`[D20]` — model per role.** `.env` currently runs *all three agents* on
`gemini-3.5-flash-lite`. That is a reasonable Coder and a weak Reviewer, and review
quality is the entire justification for having a third agent. *Recommendation: keep the
cheap model for the Coder loop, which dominates token spend, and set `PLANNER_MODEL` /
`REVIEWER_MODEL` to the strongest model you will pay for.* Also re-validate every model
id against the provider's current catalogue before the first cloud deploy — the ids in
this repo predate several releases.

**`[D19]` — vector index lifecycle.** The leak is fixed (F9), but every run still
re-embeds the entire repository because the index is keyed on the per-run workspace
path. Keying it on the **repo slug** would make it persist and be genuinely incremental
— much faster and cheaper per issue — but adds staleness handling (invalidate on commit
change) and needs a persistent volume in Phase 2. *Recommendation: do it, but only once
Phase 0 has told you how slow indexing actually is in practice. Premature until then.*

### 25.3 Optional, not urgent

**`[D15]` — structured output (§23 M12).** Replace JSON-in-a-prompt plus
`_extract_json` with the providers' native structured output (Anthropic forced tool use,
Gemini `response_schema`). Makes malformed output impossible rather than merely
recoverable and deletes a parsing layer. It is a refactor, not a fix — schedule it when
you next touch the providers.

**Distributed rate limiting.** The `POST /runs` limiter is per API process. Only matters
if you run more than one API replica.

**Housekeeping.** `test_api.py` in the repo root is a manual smoke script that looks
like a test module — rename it `scripts/smoke_provider.py`. `frontend/` is an empty
directory for something N1 says we will never build — delete it. And `tests/`, the two
new migrations, and this plan are all still uncommitted.

---

*End of the source of truth. Every `[DECISION]` in §16 is open for your call; any
section can be revised on request (bump the version in §0). **If you are picking up
work: read §25, then run §21 Phase 0 — nothing in §22 blocks it any more.***
