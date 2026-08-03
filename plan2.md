# Autonomous SWE Agent — Master Plan & Source of Truth

> **Status:** ACTIVE — this is the single source of truth · **Version:** 4.0 · **Date:** 2026-08-03 · **Owner:** Nadeem
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
| **3.2** | **2026-07-30** | **M7 landed — F5, F6, F7, F10 closed.** One run id now spans API, Celery, and the `runs` row, so the read API is reachable. Tool calls actually persist. `runs` is **append-only** with a partial unique index on active runs (migration `c4d1e88a5f27`), replacing the PK-mutating re-run path; the index also settles the concurrency race at the database, so **no Redis lock is needed** (D16/D17 resolved). Stale-run reaper added. Test harness extracted to `tests/conftest.py`; `tests/test_orchestrator_trace.py` added — **12 tests total**. **7 findings remain: 2 High (F8, F9), 5 Medium (F12–F16).** |
| **3.3** | **2026-07-30** | **M9, M11 and most of M10 landed — F8, F12, F13, F14, F15, F16 closed; F9 half-closed.** `POST /runs` now requires a shared secret (fail-closed), honours a repo allowlist, and is rate limited. Guardrail paths normalised and deviations surfaced in the trace and PR. Reviewer failures are now visibly `review_status='unavailable'` instead of silently "approved". Red/Green extraction rewritten. Coder re-runs receive their own prior diff. Vector index dropped on cleanup. `requirements.txt` fully pinned. **29 tests.** **Only D19 (persistent index) and the decision-gated items remain — see §25.** |
| **3.4** | **2026-07-31** | **F9 fully closed; `[D19]` decided: the vector store is in-memory.** `PersistentClient` → `EphemeralClient`, `CHROMA_DIR` removed, the stale on-disk `.chroma` store deleted. Persisting it bought no reuse (every run indexes a unique workspace path) while costing orphaned vectors on worker death, file contention between workers, and a volume to provision in Phase 2. `drop_repo()` retained — a long-lived Celery worker would otherwise accumulate across runs. The content-hash **embedding cache stays persisted**; that is where the real re-indexing saving lives. **All 16 findings now closed.** |
| **3.5** | **2026-07-31** | **Per-role provider/model config for all three agents, and a new finding (F17) fixed.** All resolution centralised in `agent/providers/resolve_role()`: role vars → `LLM_*` → a hardcoded default that **can never be empty**. Added `CODER_PROVIDER`/`CODER_MODEL` (the Coder had no override at all); removed the Reviewer's surprising chain through `PLANNER_*`. **F17:** a model id could leak across providers — `PLANNER_PROVIDER=anthropic` with a Gemini `LLM_MODEL` sent a Gemini model name to the Anthropic API. Also fixed the `app.main` self-test, which depended on ambient `AGENT_REPO_ALLOWLIST`. **50 tests.** |
| **3.6** | **2026-07-31** | **M13 — webhook setup script.** `scripts/setup_webhook.sh` registers/updates the GitHub webhook via the API (`--list` / `--ping` / `--delete` too), removing the manual per-repo setup step. Idempotent by webhook *path*, so an ngrok restart updates the existing hook instead of leaving a dead one behind. Subscribes to `issues` only. Verified against the live API. |
| **3.7** | **2026-07-31** | **D20 decided — the last open decision.** Planner and Reviewer on `gemini-3.1-pro-preview`, Coder on `gemini-3.5-flash`; ids taken from the account's live catalogue rather than any document. **F18 found and fixed:** a model missing from `PRICING` was silently costed at the pro rate — the configured `gemini-3.5-flash-lite` was priced at ~22x its real rate, inflating every PR footer and tripping `MAX_USD` far too early. Pricing table filled out and the fallback now warns. |
| **3.8** | **2026-07-31** | **`[D10]` decided — deploy target changed from Azure Container Apps to a single VM running the existing compose stack.** Container Apps offers no host Docker daemon, so it would have forced the sandbox to be replaced or abandoned; a VM keeps `agent/sandbox.py` running **unchanged**, preserving the §9 threat model and the H1 fix. Residual risk (socket mount = root on host) is recorded in §10 with the conditions that make it acceptable. §21 Phase 2 rewritten; TLS via a reverse proxy is now the one new piece of work. **No decisions remain open.** |
| **3.9** | **2026-07-31** | **`[D10]` revised — container platform with the sandbox off, behind a flag.** The webhook path was already running `use_sandbox=False`, so the VM was preserving a capability that was not switched on. New `USE_SANDBOX` env var gates it end to end (`POST /runs` can still override per request), so enabling it later is a config change and **not a code change** — the actual requirement. Accepted trade while off: repo tests run in the worker container alongside the tokens, which is why `AGENT_REPO_ALLOWLIST` must stay set. VM analysis retained in §10 for when the sandbox is needed. |
| **4.0** | **2026-08-03** | **Deployed to production** — Azure VM, live at `auto-swe-nadeem.centralindia.cloudapp.azure.com` with TLS, all five containers healthy, webhook delivering. §21 Phase 2 marked done; **`docs/deployment.md` added** as the operational record (what runs, ten problems hit and what each actually was, how to change things, known gaps). **Four new findings from the deploy itself (F19–F22):** `USE_SANDBOX` never passed through Compose; the CPU-torch fix skipped on arm64 (9.75 GB → 3.03 GB once corrected); a worker healthcheck that could never pass; `.gitignore` missing `.env.*`. ReadMe re-baselined. |

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
| (5) Sandbox | **built, off in deployment** | `agent/sandbox.py` is complete; `USE_SANDBOX=false` on the container platform. A config flip, not a code change (§10 D10) |
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

### 6.4 Codebase understanding / retrieval (`agent/retrieval.py`) — **built** ✅
- **Pipeline:** tree-sitter chunking (functions/methods/classes; decorated defs
  unwrapped; methods indexed individually + a class-header chunk to dodge embedding
  truncation; imports) → batched sentence-transformers embeddings → ChromaDB upsert
  with **delete-by-file first** → `build_call_graph` for structural neighbours.
  `assemble_context(query, repo, k, token_budget)` packs the most relevant chunks under
  a token budget.
- **Isolation:** results *are* correctly scoped — `retrieve()` filters on a `repo`
  metadata field set to `os.path.abspath(root)`. Cross-repo leakage into results does
  **not** occur.
- ✅ **F9 fixed (v3.3 + v3.4):** the vector store is now **in-memory**
  (`EphemeralClient`, no `CHROMA_DIR`), and `drop_repo(root)` clears a run's chunks at
  cleanup so a long-lived worker process does not accumulate them. See §7.3 for why
  ephemeral is the right shape here, not a compromise. The repo is re-indexed per run;
  the persisted **embedding cache** is what keeps that cheap.
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
- **Selection — one resolver for all three roles.** `resolve_role(role)` returns a
  validated `(provider, model)` for `planner` | `coder` | `reviewer`:
  1. `<ROLE>_PROVIDER` / `<ROLE>_MODEL`, if set
  2. `LLM_PROVIDER` / `LLM_MODEL`, if set
  3. `DEFAULT_PROVIDER` / `DEFAULT_MODELS[provider]` — **hardcoded and never empty**

  Rule 3 is the guarantee: however half-filled `.env` is, resolution terminates at a real
  provider and a real model. **A global model is inherited only when the resolved
  provider matches the global provider** — a model id is provider-specific, and
  inheriting one across providers was a live bug (§22 F17). Unknown provider names raise
  rather than silently defaulting. `get_provider()` applies the same rule for direct
  callers (the CLI, self-tests).
- **`DEFAULT_MODELS` is the single source of truth** for "what model when nobody said" —
  edit it there, not in the adapters.

**Models in use (D20, decided 2026-07-31):**

| Role | Model | Why |
|---|---|---|
| Planner | `gemini-3.1-pro-preview` | one call per run; plan quality sets everything downstream |
| Reviewer | `gemini-3.1-pro-preview` | one call per run; catching what the Coder missed is the whole point of the third agent |
| Coder | `gemini-3.5-flash` | runs the ReAct loop and dominates token spend |

The Coder sits one tier above `-lite` deliberately. The loop does multi-step tool calling
with exact-string edits, which is demanding; starting at the weakest tier risks reading
"flash-lite cannot drive this tool loop" as "the architecture does not work". Drop to
`-lite` as a cost optimisation *after* Phase 0 establishes a baseline.

> Ids came from the account's live catalogue (`client.models.list()`), not from any
> document — that is the check to repeat whenever these are revisited. Note
> `gemini-3.1-pro-preview` is a **preview** model (every 3.x pro variant is): fine for
> Phase 0, but re-check before Phase 2. `gemini-2.5-pro` is the stable fallback.

**Cost tracking:** each adapter's `PRICING` dict feeds the `MAX_USD` cap only. A model
missing from it is costed at the pro rate and now **logs a warning**, because a silent
mispricing is what made `gemini-3.5-flash-lite` cost out at 22× its real rate (§22 F18).
- **Embeddings (`[ASSUMPTION A1 / DECISION D12]`):** We use `all-MiniLM-L6-v2` via `sentence-transformers`.
  - **Why?** It is a small, fast, completely open-source model that runs locally on the CPU. Using a larger local model would slow down the Celery worker, and using a paid API model (like OpenAI `text-embedding-3`) would burn money every time a repo is indexed. MiniLM guarantees **no API cost and no network calls**.
  - **Constraints:** Its ~256-token chunk limit strictly drives the Python chunking strategy in §6.4.

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
- ✅ **`[DECISION D10]` decided (§10):** deployed with `USE_SANDBOX=false` on a
  container platform; the flag turns it on unchanged on any Docker-daemon host. Runs
  fail loudly (`sandbox_error`) if the flag is set without a daemon, so the
  misconfiguration cannot pass silently.
- 🔶 **`[DECISION D9]` still open and a prerequisite for turning it on:** no network
  inside, and a bare `python -m pytest` against the *image's* site-packages, so the
  image must carry the target repo's test dependencies.
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
- 🔶 `[DECISION D15]` — **Move to Native Structured Output.**
  - **The Problem:** Currently, the system asks the AI via a text prompt to "output JSON" and uses a brittle regex scanning layer (`_extract_json`) to extract and parse that JSON from free-form text. If the LLM makes a typo, we rely on defensive Python defaults to prevent crashes.
  - **The Solution:** Providers like Anthropic, Gemini, and OpenAI now support native structured output (passing a Python schema directly into the API request).
  - **Action:** We must migrate the adapters to use native schemas. This mathematically forces the API to return 100% perfectly formatted JSON that matches our expected contract, making malformed handoffs impossible and allowing us to delete the brittle `_extract_json` parsing logic entirely.

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

### 7.3 Vector store — ChromaDB, **in-memory** — **built** ✅ *(D19 decided)*
- Chunk embeddings + metadata (file path, symbol, start/end lines, repo). Written by
  §6.4, read by `retrieve_context`. Opened lazily (§17 M2). Chunk IDs and the
  delete-by-file filter keyed on **absolute** paths (§17 L1).
- ✅ **F9 closed / `[DECISION D19]` decided: ephemeral.** v3.4 switched
  `PersistentClient` → `EphemeralClient` and removed `CHROMA_DIR` entirely. The original
  plan's "one collection per repo, persisted, incremental across runs" was never what
  the code did and, on inspection, is not what this design wants: each run clones into a
  unique workspace and indexes under that path, so an on-disk store only ever held data
  that was about to be deleted. Persisting it bought no reuse and cost one real failure
  mode — a worker dying before cleanup orphaned its vectors on disk permanently — plus
  file contention between concurrent workers and a persistent volume to provision in
  Phase 2. In memory, a dead process takes its index with it.
- **`drop_repo()` is still called at cleanup.** A Celery worker is long-lived across
  many runs, so ephemeral storage alone bounds growth to a process lifetime; the drop
  bounds it to a single run.
- **The persistent piece that matters is the embedding cache** (`EMBEDDING_CACHE_DIR`,
  keyed by content hash): unchanged files skip the model on every subsequent run. That
  is where the re-indexing saving actually lives, and it is unaffected by this change.
- **Trade-off accepted:** the repo is re-indexed per run. If Phase 0 shows that to be
  slow in practice, the answer is a warm cache or a slug-keyed persistent index — but
  measure first.
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
| `PLANNER_PROVIDER` / `PLANNER_MODEL` | per-role override for §6.8 | → `LLM_*` → hardcoded default | built |
| `CODER_PROVIDER` / `CODER_MODEL` | per-role override for §6.5 | → `LLM_*` → hardcoded default | built |
| `REVIEWER_PROVIDER` / `REVIEWER_MODEL` | per-role override for §6.9 | → `LLM_*` → hardcoded default | built |
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
| `EMBEDDING_CACHE_DIR` | embedding cache (content-hash keyed). *There is no `CHROMA_DIR` — the vector store is in-memory (§7.3).* | under `agent/` | built |
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

### `[DECISION D10 — DECIDED 2026-07-31, revised]` The sandbox in the cloud

**Chosen: deploy to an Azure container platform with the sandbox OFF, behind a config
flag — and move to a Docker-daemon host only when the sandbox is actually needed.**

The reasoning that got here matters, because the first version of this decision reached
for a VM and that was over-built for the present state.

**The observation that changed it:** the webhook path was *already* running with the
sandbox off — `_enqueue_run(run_issue, repo, number)` took the `use_sandbox=False`
default, so every real run to date has executed tests host-side in the worker container.
The VM was being provisioned to preserve a capability that was not switched on. For
first-party repositories behind `AGENT_REPO_ALLOWLIST`, that is a defensible trade, and
a container platform is simpler, cheaper to idle, and has less to operate.

**How it stays a config flip, not a rewrite** (the actual requirement): `USE_SANDBOX`
now gates it end to end. `false` on the container platform; `true` on any host with a
Docker daemon. `POST /runs` may still override per request, which is how you smoke-test
the sandbox without changing deployment config. **No code changes when the time comes.**

**What you accept while it is off.** A repository's test suite executes inside the
worker container, which holds `GITHUB_TOKEN`, the LLM API key, and the database
credentials in its environment. A malicious or compromised test can read all of them.
That is fine for code you wrote; it is **not** fine the first time this points at a
repository you did not. The allowlist is what keeps that boundary honest, so it must
stay set.

**When to move, and to what.** The moment a non-first-party repository is in scope, flip
`USE_SANDBOX=true` — which requires relocating to a host with a Docker daemon, since
Container Apps and ACI provide none. That is the VM (host socket, conditions in the
"residual risk" note below) or AKS with per-run Jobs. Setting the flag without a daemon
does **not** silently degrade: the run ends `sandbox_error`.

**The other prerequisite, easy to miss:** the sandbox has no network and runs a bare
`python -m pytest` against the *image's* site-packages. So the sandbox image must carry
the target repo's test dependencies — `[DECISION D9]`, still open and a bigger job than
the flag.

---

**If/when the VM path is taken, the original analysis stands:**

**Chosen there: the host socket, on a dedicated, disposable VM.** The worker mounts
`/var/run/docker.sock`, exactly as `docker-compose.yml` already does locally. No change
to `agent/sandbox.py`.

**Why.** The alternatives — rootless DinD, a daemon-free subprocess sandbox
(`nsjail` / `bubblewrap`), Kubernetes-native per-run Jobs, or running unsandboxed — are
all workarounds for a constraint that exists only on PaaS: no host Docker daemon. A VM
has one, so the question largely dissolves. Crucially this is the only option under
which **the sandbox survives intact**: network isolation, read-only host FS, the `.git`
tmpfs mask, dropped capabilities, resource caps. The unsandboxed option would have meant
executing untrusted repository code with no isolation at all, discarding the §9 threat
model and the H1 fix that audit 1 was built around. No amount of managed-platform
convenience is worth that.

**The residual risk, stated plainly.** Mounting the Docker socket into the worker means
**a container escape is root on the host**. That is what D10 was originally about, and
choosing a VM does not remove it — it makes the blast radius acceptable, *conditionally*.
The conditions are load-bearing:

- The VM runs **nothing else you care about**. It is disposable: worst case you destroy
  and rebuild it.
- `GITHUB_TOKEN` on that host is scoped to the repositories in `AGENT_REPO_ALLOWLIST`
  and nothing more.
- Inbound is firewalled to 22 and 443. **Port 8000 is never published**; TLS terminates
  at a reverse proxy in front of `api` (§21 Phase 2).

If any of those stops being true — the box picks up another service, or the token is
broadened — revisit this. The qualifier is doing the security work, not the choice.

**Upgrade path.** Rootless Docker on the same VM is a strictly better boundary and a
drop-in improvement whenever you want it. gVisor/Kata (D7) or per-run Kubernetes Jobs
remain the answer for genuinely hostile repositories.

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
- **Cloud — not started.** See §21. A single **VM running this same compose stack** is
  the target (Phase 2), so the local and cloud topologies are identical; AKS is a
  later, optional learning migration (Phase 4).
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
├── scripts/
│   ├── setup_postgres.sh        # one-time local Postgres bootstrap
│   └── setup_webhook.sh         # register/update the GitHub webhook  [§18 M13]  built
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
| **D10** | The sandbox in the cloud | VM w/ host socket / container platform, sandbox off / AKS Jobs | ✅ **container platform, `USE_SANDBOX=false`; VM or AKS when the sandbox is needed — DECIDED v3.9 (§10)** |
| D11 | Artifact/log retention | 7 / 30 / 90 days | 30 days |
| D12 | Embedding model | MiniLM / larger local | MiniLM first |
| D13 | Concurrency cap | replicas / global semaphore | HPA + global cap |
| **D14** 🆕 | One Celery task vs task-per-agent | one / per-agent | **one task — confirmed (§5)** |
| **D15** 🆕 | Structured output mechanism | JSON-in-prompt + parse / native provider schemas | 🔶 **move to native** |
| **D16** 🆕 | `runs` table shape | unique `(repo,issue)` upsert / append-only + partial index | ✅ **append-only — DONE v3.2** |
| **D17** 🆕 | Who mints `run_id` | API (pass as Celery `task_id`) / task | ✅ **API — DONE v3.2** |
| **D18** 🆕 | Reviewer failure semantics | fail-open silently / fail-open **but visibly** / fail-closed | ✅ **fail-open visibly — DONE v3.3** |
| **D19** 🆕 | Vector index lifecycle | per-repo persistent (slug-keyed) / **in-memory, per-run** | ✅ **in-memory — DECIDED v3.4** |
| **D20** 🆕 | Model defaults per role | all cheap / cheap Coder + strong Planner/Reviewer | ✅ **cheap Coder + strong Planner/Reviewer — DECIDED v3.7** |

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
   Docker socket mounted in Compose. *(Same shape in the cloud — D10 decided, §10.)*
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
12. **M10 — Index lifecycle.** ✅ **(v3.4)** F9 closed; D19 decided as in-memory.

### Then — the remaining roadmap
> Numbered M13+ so they never collide with the remediation milestones M6–M12 in §23.

13. **M13 — Webhook setup script.** ✅ **(v3.6)** `scripts/setup_webhook.sh --repo
    owner/name --url https://host` registers the hook via the GitHub API, so there is no
    manual per-repo config. Also `--list`, `--ping`, `--delete`. Idempotent **by webhook
    path**, not full URL, so restarting ngrok updates the existing hook instead of
    piling up dead ones. Subscribes to `issues` only — the sole actionable event today
    (M16 will add `check_suite`). Verified against the live API.
14. **M14 — Cloud deploy.** §21 Phase 2 — a single VM running the existing compose
    stack, behind TLS. D10 and M9 are both done, so this is unblocked.
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
ngrok http 8000                                    # note the https URL it prints
bash scripts/setup_webhook.sh --repo owner/name --url https://<id>.ngrok-free.app
bash scripts/setup_webhook.sh --repo owner/name --ping    # expect a 204 delivery
bash scripts/setup_webhook.sh --repo owner/name --list    # shows the last delivery
```

> The script reads `GITHUB_TOKEN` (needs `admin:repo_hook`) and
> `GITHUB_WEBHOOK_SECRET` from `.env`, and prints neither. It matches an existing hook
> by **path**, so re-running it after an ngrok restart updates that hook's URL rather
> than adding a second one.

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
Run the Compose stack; `ngrok http 8000`; register the webhook with
`scripts/setup_webhook.sh` (M13); open 5–10 real issues of varying difficulty and watch
what the agent produces. **Do not move to Phase 1 until it resolves issues reliably.**

> **v3.3 amendment: Phase 0 is ready to run.** The Planner sees the code, the Reviewer
> sees the whole diff, every run is inspectable via `GET /runs/{id}/steps`, and the
> manual trigger is authenticated. Nothing in §22 blocks it.
>
> Two things to do first, both one-liners: **set `AGENT_REPO_ALLOWLIST`** to your test
> repo (§25.1), and **check `AGENT_API_TOKEN` is set** — `POST /runs` is fail-closed and
> will return 503 without it.

### Phase 1 — Close the gaps before anything goes public
1. ✅ **Auth + allowlist on `POST /runs`** (F8) — done in v3.3.
2. ✅ **D10 resolved: a dedicated VM with a real Docker daemon** (§10). No code change
   to `agent/sandbox.py` is needed, which is the entire point of the choice.
3. ✅ **Pin `requirements.txt`** (F16) — done in v3.3.

### ✅ Phase 2 — DONE 2026-08-03: a single VM running the existing compose stack

> **Live at `https://auto-swe-nadeem.centralindia.cloudapp.azure.com`.**
> Azure VM `auto-swe-vm`, Standard B2pls_v2 (2 vCPU, 4 GB, **arm64**), Central India,
> Ubuntu 24.04, Docker 29.7.1. Five containers: caddy, api, worker, postgres, redis —
> all healthy. Let's Encrypt TLS. Auto-shutdown 11:59 PM IST. ~$16/mo VM + ~$5 disk.
>
> **`docs/deployment.md` is the operational record** — what runs, the ten problems hit
> getting there and what each actually was, how to change config/code/schema/repos
> afterwards, and the known gaps. Read that before touching the deployment; this
> section is the rationale only.
>
> Verified in production rather than assumed: all four Alembic revisions applied to real
> Postgres — including `c4d1e88a5f27`, whose **PostgreSQL branch had only ever been
> tested against SQLite** — and the resulting partial index was inspected directly.
> `/readyz` returns `sandbox: off`, confirming `USE_SANDBOX` is read through Compose.
> GitHub webhook #660604062 reports `204 OK`.
>
> **Deliberately not used:** Azure Container Registry (the VM builds its own images) and
> Azure Database for PostgreSQL (Postgres runs in the compose stack) — both created
> during the abandoned Container Apps attempt and since deleted. Together they were
> costing more than the VM.

**Why a VM and not a PaaS.** This was reconsidered on 2026-07-31 and the answer changed.
The earlier plan targeted Azure Container Apps, which offers no host Docker socket — so
it would have forced the sandbox to be replaced or abandoned, and the abandonment option
meant running untrusted repository code with no isolation at all. A plain VM has a real
Docker daemon, so `agent/sandbox.py` runs **unchanged**: `--network none`, read-only host
FS, the `.git` tmpfs mask, dropped capabilities, resource caps. That preserves the whole
§9 threat model, including the H1 escape fix, which is worth far more than the
operational convenience PaaS would have bought.

It is also a genuine 1:1 with local: `docker-compose.yml` already works and already
mounts the socket. There is almost no new abstraction to learn, which is exactly what
this phase should optimise for.

**Steps**
1. Provision a small VM (Azure B-series on the Student Pack credit, an EC2 instance, or
   a DigitalOcean droplet — any of them, this is not a differentiated choice). Install
   Docker Engine + the compose plugin.
2. **Lock the box down first:** firewall to ports 22 and 443 only — **never expose 8000
   directly**; SSH by key; unattended security upgrades on.
3. Clone the repo, create `.env` from your local one, **regenerating every secret**
   (`AGENT_API_TOKEN`, `GITHUB_WEBHOOK_SECRET`). Set `AGENT_REPO_ALLOWLIST`.
4. **Add TLS — the one genuinely new piece of work.** GitHub will not send a webhook to
   `http://`, and the stack is plain HTTP on `:8000` with no reverse proxy. Add Caddy to
   the compose file (≈5 lines, automatic Let's Encrypt) in front of `api`, and stop
   publishing `8000` on the host.
5. **Decide where Postgres lives** (see the data note below).
6. `docker compose up -d`, then `curl https://<host>/readyz`.
7. Point the webhook at it: `scripts/setup_webhook.sh --repo owner/name --url
   https://<host>` — the script updates the existing ngrok hook in place rather than
   adding a second one. Then `--ping` to confirm delivery. Retire ngrok.
8. Let it run unattended for a few days on the test repo.

**Data — do not skip this.** `postgres_data` is a compose volume on the VM's disk, so
losing the instance loses your entire run history, which is the audit trail this whole
persistence layer exists to provide. Either point `DATABASE_URL` at a managed Postgres
and run only `api` + `worker` + `redis` in compose (cleaner, barely more expensive), or
schedule `pg_dump` to object storage. No volume is needed for the vector store — D19
landed as in-memory (§7.3) — but a volume for `EMBEDDING_CACHE_DIR` is worth having,
since that cache is what keeps re-indexing cheap.

**What you are taking on.** You now own a machine: OS patching, firewall, log rotation,
disk usage, reboots. PaaS did that invisibly. This is the honest recurring cost of
keeping the sandbox, and it is the right trade — but it is not free.

*Learning focus: real ops — TLS and certificates, firewalls, systemd, backups, and what
"you own the host" actually means. Arguably more foundational than PaaS, and a better
run-up to Phase 4 than Container Apps would have been.*

### Phase 3 — Observability, now earned
Start with `docker compose logs` and the VM provider's built-in host metrics (CPU,
memory, disk) — they answer ~80% of questions with zero setup. **Watch disk**: it is
the failure mode a single VM actually hits, via logs and Docker images. Add **one real alert** ("a run failed" / "a run cost > $1").
Graduate to Prometheus + Grafana + OTel traces only once the built-ins bind. *Learning
focus: the difference between logs, metrics, and traces in practice.*

### Phase 4 — Kubernetes, deliberately, as its own project
Migrate the now-proven, now-live system from the VM to **AKS**. Because it already
works on the VM, this is a **learning exercise with a safety net** — if AKS goes
sideways the VM keeps serving. This is where `k8s/` and `helm/` get built for real.
It is also the natural place to improve on D10's host socket: per-run Kubernetes Jobs
give each run its own isolated execution context instead of a shared daemon.

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

**Status as of v4.0:** ✅ **all 22 findings closed**, every one covered by a test that
was confirmed to fail against the pre-fix code. Nothing in this audit blocks §21 Phase 0.
What remains is decisions, not defects — see §25.

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

**F9 — The vector store grew without bound and re-embedded everything every run.**
✅ **FIXED (v3.3 + v3.4), `[DECISION D19]` decided: in-memory.**
One global collection `'code'`, with each run indexing from a unique ephemeral path
(`.../workspaces/{run_id}/repo`), so every run wrote a *complete new copy* of the repo's
chunks under a new key and cleanup never removed them — ~100 runs on a 1000-chunk repo
left ~100k permanently dead chunks. Query *results* were always safe (the `repo`
metadata filter works), so this was cost, latency, and storage rather than correctness.
**Fix applied, in two steps.** v3.3 added `retrieval.drop_repo(root)`, called during
cleanup, so a run's chunks no longer outlive it. v3.4 went further and made the store
**ephemeral** (`chromadb.EphemeralClient()`), deleting `CHROMA_DIR` and the on-disk
`.chroma` store entirely.

The second step is the more interesting one, because it reframes the finding. The
original plan called for "one collection per repo, persisted, incremental across runs" —
but that is not what this design wants. Every run clones into a *unique* workspace and
indexes under that path, so an on-disk store only ever held data that was about to be
deleted. Persisting it bought no reuse whatsoever, and cost: a worker dying before
cleanup orphaned vectors on disk permanently, two workers in one container contended
over the same files, and Phase 2 would have needed a volume provisioned for it. In
memory, a dead process takes its index with it — the failure mode cannot exist.

`drop_repo()` is still called, because a Celery worker is long-lived across many runs:
ephemeral storage bounds growth to a process lifetime, the drop bounds it to one run.
And the persistence that *does* pay for itself — the content-hash embedding cache — is
untouched, so unchanged files still skip the model on every subsequent run.
**Trade-off accepted:** the repo is re-indexed per run. Measure in Phase 0 before
optimising.
**Test:** `test_vector_index_is_dropped_on_cleanup`.

### 🟠 Added 2026-07-31 — found while implementing per-role model config

**F17 — A model name could leak across providers.** ✅ **FIXED**
Both adapters resolved their model as `model or os.getenv("LLM_MODEL", <default>)`.
So with the global config on Gemini and only `PLANNER_PROVIDER=anthropic` set — exactly
the override D20 recommends — the Planner built the **Anthropic** adapter and handed it
the **Gemini** `LLM_MODEL`, calling the Anthropic API with `gemini-3.5-flash-lite`.
Confirmed by direct construction before the fix. It would have failed at the first live
Planner call with a confusing upstream error, and only for the mixed-provider setup, so
it would have looked like a provider outage rather than a config bug.

Two related gaps surfaced with it: the **Coder had no per-role override at all**
(`ReActAgent` called bare `get_provider()`), and the **Reviewer chained through
`PLANNER_*`**, so configuring the Planner silently reconfigured the Reviewer.

**Fix applied:** all resolution moved into `agent/providers/resolve_role(role)`, one
implementation for all three roles: role-specific var → global `LLM_*` → a hardcoded
non-empty default. A global model is inherited **only when the resolved provider matches
the global provider**, so an id never crosses providers. `CODER_PROVIDER` /
`CODER_MODEL` added; the Reviewer's chain through `PLANNER_*` removed. Unknown provider
names now raise instead of silently falling back, so a typo fails loudly.
`get_provider()` applies the same rule, protecting direct callers like the CLI.
**Tests:** `tests/test_provider_resolution.py`, 15 tests covering all three roles,
the never-empty guarantee, cross-provider isolation, and validation.

**F18 — A model missing from `PRICING` was silently costed at ~22× its real rate.**
✅ **FIXED**
`cost_usd()` fell back to `PRICING.get(self.model, (1.25, 10.0))` — the *pro* rate — with
no signal. `gemini-3.5-flash-lite`, the model actually configured, had no entry, so every
run was priced at $11.25 per 1M/1M instead of $0.50. Two consequences: the cost in every
PR footer and `runs.cost_usd` was inflated, and `MAX_USD` tripped roughly 22× too early,
cutting runs short for no reason. Nothing in the logs said so.
**Fix applied:** entries added for every model in the current catalogue tier
(pro / flash / flash-lite). The fallback stays at the pro rate — over-estimating is the
safe direction for a spend cap — but now **logs a warning once per model**, so the next
model change cannot reintroduce this silently.
**Caveat:** the rates themselves still cannot be verified programmatically; the API
exposes ids and token limits, not prices. Check them against the pricing page before
relying on `MAX_USD`.

### 🟠 Added 2026-08-03 — found while deploying to the VM

*Four defects that only surfaced against real infrastructure. None was reachable by
local testing, which is the point worth remembering: the deployment was itself a test,
and it found things fifty passing tests did not.*

**F19 — `USE_SANDBOX` was not passed through Compose.** ✅ **FIXED**
The flag existed in `.env` and was read by `app/main.py`, but `docker-compose.yml`
never forwarded it into the containers. Setting it would have appeared to work and
silently done nothing — the worst kind of config bug, since the failure is invisible.
**Fix:** added to the `x-agent-env` block. `/readyz` now echoes `sandbox: on|off` so
the effective value is verifiable from outside the host.

**F20 — The CPU-only torch fix was skipped on arm64.** ✅ **FIXED**
The fix that took the worker image from 9.74 GB to 3.19 GB on x86 was guarded to x86
only, on the reasoning that ARM Linux torch is CPU-only anyway. That is outdated: PyPI
publishes CUDA builds for aarch64 (Grace/GH200-class servers). The ARM build produced
`torch 2.13.0+cu130` with 2.9 GB of nvidia-* and 652 MB of triton — **9.75 GB**, the
exact problem the fix existed to prevent.
**Fix:** guard removed after testing rather than reasoning — `pip download` against the
CPU index on the ARM host returned a 148 MB aarch64 wheel. **9.75 GB → 3.03 GB.**

**F21 — The worker healthcheck could never pass.** ✅ **FIXED**
`docker/worker.Dockerfile` pinged `celery@$$HOSTNAME`. In `sh`, `$$` is the shell's
PID, so it resolved to `celery@131HOSTNAME` — a node that has never existed. The
container reported `unhealthy` permanently while the worker was connected to the broker
and consuming tasks normally. It had been failing since the line was written.
**Fix:** dropped `--destination`; `inspect ping` broadcasts and any pong answers the
real question. Confirmed against the deployed container: old form FAIL, new form PASS.
*A healthcheck that can only fail is worse than none — it teaches you to ignore
container status, so the day the worker really dies you will not notice.*

**F22 — `.gitignore` covered `.env` but not `.env.bak.*`.** ✅ **FIXED**
A timestamped backup created before editing `.env` was swept in by `git add -A` and
only stopped by GitHub push protection. Nothing leaked.
**Fix:** `.gitignore` now covers `.env`, `.env.*` and `*.env`, verified against
`.env.bak.123`, `.env.local` and `prod.env`.

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

### ✅ M10 — Index lifecycle *(F9, D19)* — **DONE (v3.3 + v3.4)**
`retrieval.drop_repo()` clears a run's chunks at cleanup, and the store itself is now
**in-memory** (`EphemeralClient`, no `CHROMA_DIR`) — D19 decided as ephemeral rather
than slug-keyed persistent, because persistence bought no reuse in this design and cost
real failure modes (§7.3). The persisted embedding cache is untouched.

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

**Nothing is blocking.** Every decision in §16 is settled and every §22 finding is
closed. What is left is execution, in this order:

1. **§21 Phase 0** — run the stack locally, register the webhook with
   `scripts/setup_webhook.sh`, open real issues, watch what the agent produces.
2. **§21 Phase 2** — the VM deploy (D10 decided, §10). The only genuinely new piece of
   work in it is **TLS**: GitHub will not post a webhook to `http://`, and the stack is
   plain HTTP on `:8000` today. Add Caddy in front of `api` and stop publishing 8000.
3. Decide **where Postgres lives** on the VM before you rely on the run history —
   managed instance or scheduled `pg_dump`. A compose volume on a single disk is not a
   backup (§21 Phase 2, "Data").

### 25.2 Cost and quality

*(`[D20]` was the last open decision and is now settled — see §6.6. Ids were taken from
the account's live catalogue via `client.models.list()`, not from any document.)*

*(`[D19]` was open here in v3.3 and is now decided — the vector store is in-memory, see
§7.3. Revisit only if Phase 0 shows per-run indexing to be slow in practice, and measure
before changing anything.)*

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
