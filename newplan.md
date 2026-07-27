# auto-swe-agent — full project plan

Repo: github.com/nadeemshabir/auto-swe-agent
Last updated: July 28, 2026

---

## 0. Actual current state (pulled directly from the repo)

```
auto-swe-agent/
├── agent/
│   ├── loop.py              — ReAct loop (Reason → Act → Observe), budget controller
│   ├── github.py            — webhook parsing, issue intake, PR creation, git helpers
│   ├── retrieval.py         — tree-sitter chunking, embeddings, ChromaDB, call graph
│   ├── sandbox.py           — hardened Docker sandbox (network none, read-only FS, caps)
│   └── providers/
│       ├── base.py          — LLMProvider interface
│       ├── anthropic_provider.py
│       └── gemini_provider.py
├── app/
│   └── main.py               — FastAPI app (26KB — this is substantial, not a stub)
├── db/
│   ├── models.py
│   ├── session.py
│   └── migrations/           — Alembic, one initial migration applied
├── workers/
│   └── tasks.py               — Celery task definitions
├── docker/
│   ├── api.Dockerfile
│   ├── worker.Dockerfile
│   ├── sandbox.Dockerfile
│   └── build-sandbox.sh
├── docker-compose.yml
├── scripts/setup_postgres.sh
├── docs/                     — per-component design docs (retrieval, loop, github,
│                                 sandbox, persistence, provider abstraction)
├── plan.md, plan2.md         — your own working notes
├── test_api.py
└── requirements.txt
```

**What this tells us, component by component:**

| Layer | Status | Evidence |
|---|---|---|
| Codebase understanding | Done | `agent/retrieval.py` — tree-sitter + embeddings + ChromaDB + call graph |
| ReAct reasoning loop | Core done | `agent/loop.py` — Reason/Act/Observe, budget controller, pluggable providers |
| GitHub integration | Done | `agent/github.py` — webhook to issue parsing, PR creation, retries, token safety |
| Docker sandbox | Done | `agent/sandbox.py` — network none, read-only host FS, non-root, resource caps |
| Backend & queue | Built, not yet verified end-to-end | `app/main.py`, `workers/tasks.py`, `db/` all exist with real content — but README marks this "not started," so treat it as in progress needing verification, not untouched |
| Deployment plumbing | Partial | `docker-compose.yml` + 3 Dockerfiles exist — this is your local deploy story, already halfway to a cloud deploy |
| Observability | Not started | No `monitoring/`, no tracing/metrics code anywhere |
| Kubernetes/Helm | Not started | No `k8s/` or `helm/` folder — and per our discussion, this is optional, see section 5 |
| Frontend | Doesn't exist, and doesn't need to | GitHub is the interface — see section 4 |

One thing to reconcile before you start: your README says "Backend & queue: Not started," but `app/main.py` is 26KB and `workers/tasks.py` exists with real content. First task below is to just verify what actually works end-to-end right now, so the rest of this plan is built on ground truth, not the README's possibly-stale status line.

---

## 1. Multi-agent architecture

This is the core structural change. Today `agent/loop.py` is one ReAct loop doing everything — plan, code, and (implicitly) self-check — in a single continuous reasoning stream. Split it into three agents with a clean handoff.

### 1.1 Planner agent
**Lives in:** new file `agent/planner.py`, called first from `agent/loop.py`

**Job:** Read the issue + retrieved context from `agent/retrieval.py`, produce a structured plan — no code written yet.

**Input:** issue text, top-k retrieved chunks/call-graph neighborhood from `retrieval.py`

**Output (structured — not free text):**
```json
{
  "understanding": "...",
  "root_cause_hypothesis": "...",
  "files_to_touch": ["..."],
  "plan_steps": ["..."],
  "test_strategy": "...",
  "risk_notes": "..."
}
```

Use your strongest configured provider here (`LLM_PROVIDER=anthropic`, `claude-opus-4-8` per your existing env var setup) — this step is cheap relative to the coding step, don't economize.

### 1.2 Coder agent
**Lives in:** your existing `agent/loop.py` becomes this — it already has the ReAct tool-use loop and sandboxed execution wired to `agent/sandbox.py`. Minimal rework: it now receives the Planner's structured plan as its starting context instead of the raw issue.

**Job:** Execute `plan_steps` inside the sandbox, edit only `files_to_touch`, run tests, iterate (cap retries — e.g. 5).

**Guardrail to add:** if the coder wants to touch a file outside `files_to_touch`, it must flag this back rather than silently doing it — cheap check, prevents scope creep.

### 1.3 Reviewer agent
**Lives in:** new file `agent/reviewer.py`, called after the Coder produces a diff, before `agent/github.py` opens the PR

**Job:** Fresh-context review of the diff against the issue and the plan — catches what the Coder missed. A model reviewing its own work in the same context tends to rubber-stamp it; a fresh context with adversarial framing catches more.

**Output:**
```json
{
  "verdict": "approve" | "request_changes",
  "concerns": ["..."],
  "confidence": "high" | "medium" | "low"
}
```

If `request_changes` → back to Coder, cap at 2 rounds, then open the PR anyway flagged "needs human review."

### 1.4 Orchestration
```
Issue webhook (agent/github.py)
        |
Planner agent (agent/planner.py)  ->  structured plan
        |
Coder agent (agent/loop.py, existing)  ->  diff + test results
        |
Reviewer agent (agent/reviewer.py)  ->  approve / request_changes
        |                                 |
    approve                     request_changes (max 2 loops)
        |
agent/github.py opens PR with full trail
```

Since `workers/tasks.py` (Celery) already exists, make each agent step its own Celery task — you get retry and inspection for free. Persist each step's output via `db/models.py` — add a `run_steps` table (`run_id`, `agent_name`, `input`, `output`, `timestamp`). This becomes your observability layer at near-zero extra cost, since the DB layer already exists.

---

## 2. Demo-quality features

These sit on top of the multi-agent split and are what make a PR trustworthy to a stranger on GitHub without them pulling the branch locally.

### 2.1 Rich PR description — build in `agent/github.py`
Assemble directly from the Planner/Coder/Reviewer outputs you're now persisting:
```markdown
## Issue
<Planner's "understanding">

## Root cause
<Planner's "root_cause_hypothesis">

## Fix
<Coder's diff summary, file by file>

## Tests
<what was added/changed, pass/fail>

## Reviewer notes
<Reviewer's verdict + concerns, or "reviewed, no concerns">
```
Effort: 1-2 days. Highest impact item on this whole list.

### 2.2 Pre-work issue comment — `agent/github.py`
Right after the Planner returns, post a comment on the issue using the existing GitHub REST client:
> "Working on this. My understanding: [Planner's understanding]. Will open a PR shortly."

Effort: 1 day.

### 2.3 Test-first fixing
The Planner's `test_strategy` field already gives the Coder what it needs — have the Coder write that test first, confirm it fails, then fix the code, confirm it passes. Show both states in the PR description.

Effort: 2-3 days, mostly prompt/workflow work on top of what exists.

### 2.4 CI failure recovery
Needs a new webhook handler in `agent/github.py` for `check_suite`/`workflow_run` events. On failure: fetch the log, re-invoke the Coder agent with the failure as new context, push a fix commit, comment explaining it.

Effort: 2-3 days.

---

## 3. Backend verification (do this first, before anything else)

Since `app/main.py`, `workers/tasks.py`, and `db/` all have real content despite the README saying "not started":

1. Run `docker-compose up` locally, confirm all services (API, worker, Postgres, Redis if present) actually start
2. Fire a test webhook payload at the FastAPI endpoint, confirm it reaches a Celery task
3. Confirm the Celery task can call `agent/loop.py` end to end against a real repo
4. Update the README status table to reflect reality

This is a half-day task but it tells you exactly how much of "backend & queue" is actually done versus scaffolded, which changes how much work sections 1-2 really are.

---

## 4. Do you need a frontend? No.

GitHub is the interface for this whole system:
```
Issue opened on GitHub -> webhook -> agent works -> PR opens on GitHub
```
Everything — issue comments, the PR diff, the PR description, review status — is visible inside GitHub natively. This is also how Devin, Sweep, and Jules are actually used.

The only UI-shaped things worth having, and only if useful to you:
- A `/runs/{id}` JSON endpoint on your existing FastAPI app, to inspect a run's Planner/Coder/Reviewer trail — not a UI, just an endpoint you `curl`
- Nothing else is required to use or demo the project

---

## 5. Deployment plan

### Phase 0 — buy-time webhook setup (before building a GitHub App)
Simple script, calls the GitHub API to register a webhook automatically:
```bash
./scripts/setup_webhook.sh --repo owner/repo-name --agent-url https://your-agent-url/webhook
```
One command per repo. This removes the manual "go into repo settings and configure a webhook" step without needing a full GitHub App yet. Add this as a new `scripts/` file alongside your existing `scripts/setup_postgres.sh`.

### Phase 1 — Azure Container Apps deploy
Your `docker-compose.yml` + 3 Dockerfiles are most of the work already done.
1. Push images (built from `docker/api.Dockerfile`, `docker/worker.Dockerfile`) to Azure Container Registry
2. `az containerapp up` — can build + deploy straight from a Dockerfile
3. Set secrets (DB connection string, `ANTHROPIC_API_KEY`/`GEMINI_API_KEY`, GitHub token) via Azure's secret store
4. Point the webhook (or GitHub App, once built) at the resulting public URL

Uses your $100 GitHub Student Pack Azure credit. No Kubernetes knowledge required. Effort: about 1 day.

### Phase 2 — security hardening (before any real external traffic)
- Auth on `POST /runs` in `app/main.py`
- Verify GitHub's webhook HMAC signature in `agent/github.py` (GitHub signs every webhook payload — check it, don't just trust the payload)
- SSRF guards on any tool in `agent/loop.py` that fetches arbitrary URLs

### Phase 3 — observability
- You already get most of this for free once section 1.4's `run_steps` persistence is in place
- Optional: wire Prometheus/Grafana as originally planned in your README's 6-layer design — this is optional, a `/runs/{id}` JSON endpoint gets you most of the value for a fraction of the effort

### Phase 4 — GitHub App (optional, do only once Phase 0's script is limiting you)
Removes per-repo webhook setup entirely — users install the app once on their org, no manual config per repo. This is what Devin/Sweep/Jules use in production. Skip this until you actually have more than one or two repos wanting to use the agent.

### Phase 5 — Kubernetes / Helm / CI-CD — optional
Your README lists this as a target layer, but it is not required for the project to work, be demoed, or be valuable. Azure Container Apps (Phase 1) is a complete, production-capable deployment on its own. Only pick this up if:
- You specifically want to learn Kubernetes hands-on, or
- You hit a real scaling need Container Apps can't handle (unlikely at your current usage)

If you do it later: `k8s/` and `helm/` folders, a GitHub Actions CI/CD pipeline. Treat this as a standalone learning project you attach to auto-swe-agent, not a blocking milestone.

---

## 6. Build order

1. Backend verification (half day) — confirm what `app/`, `workers/`, `db/` actually do end to end right now
2. Pre-work issue comment (1 day) — quick, visible win
3. Rich PR description (1-2 days) — makes the current single-loop output trustworthy
4. Multi-agent split: `agent/planner.py` + `agent/reviewer.py`, rework `agent/loop.py` handoff (4-6 days — this is the biggest single chunk of work)
5. Test-first fixing (2-3 days) — natural now that Planner outputs `test_strategy`
6. Phase 0 webhook setup script (half day)
7. Azure Container Apps deploy (1 day) — get a live URL
8. Security hardening — auth + HMAC verification + SSRF guards (1-2 days)
9. CI failure recovery (2-3 days)
10. (optional) GitHub App — only if per-repo webhook setup becomes a real bottleneck
11. (optional) Prometheus/Grafana observability — only if the `/runs/{id}` endpoint isn't enough
12. (optional) Kubernetes/Helm/CI-CD — only if you want to learn K8s specifically, not because the project needs it

Total for items 1-9 (the non-optional path): roughly 3-4 weeks of focused work. Items 10-12 are extensions you can pick up anytime later without blocking a working, demoable project.