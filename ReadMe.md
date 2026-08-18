# Autonomous SWE Agent

**Open a GitHub issue. Get a pull request back. No human in the middle.**

This is an AI system that watches a repository, reads new issues, figures out
what the code actually does, writes a fix, runs the tests, and opens a PR
explaining itself — end to end, unattended.

It has been live on an Azure VM since 2026-08-03.

---

## The 60-second version

You file an issue:

> **Title:** `paginate() drops the last item on the final page`
> **Body:** When total items is an exact multiple of page_size we get an empty trailing page.

Roughly 90 seconds later, three things have happened without anyone touching a keyboard:

1. A comment appears on your issue explaining what the agent thinks is wrong and how it plans to fix it.
2. A branch `agent/issue-42` is pushed.
3. A PR opens, with a failing-test-then-passing-test writeup and a diff.

What happened in between:

| Stage | What it does |
|---|---|
| **Gateway** | Verifies the webhook's HMAC signature, de-duplicates it, mints a `run_id`, returns `202` immediately |
| **Worker** | Clones the repo into a throwaway workspace, cuts a branch, indexes every Python file into a vector store |
| **Planner** | Reads the issue + retrieved code, writes a hypothesis, a step list, and a test strategy |
| **Coder** | A ReAct loop — search, read, edit, run tests, observe, repeat — until the tests go green |
| **Reviewer** | Reads the final diff like a senior engineer would and returns approve / request-changes |
| **Back to Coder** | On request-changes, the feedback goes back into the loop (up to 2 rounds) |
| **Ship** | Commit, push, open the PR, comment on the issue, tear down the workspace |

Every step of that is written to Postgres as it happens, so you can watch a run
unfold at `GET /runs/{id}/steps` instead of guessing from logs.

---

## Why this was interesting to build

Most "AI writes code" demos work because the code fits in the prompt. This one
doesn't get that luxury — it's pointed at real repositories, so nearly every
hard part is about **what to show the model and what to do when it's wrong**:

- **It can't see the codebase.** So there's a retrieval layer: tree-sitter parses
  files into semantic chunks (one per function, method, class header, import),
  a local embedding model turns those into vectors, and a token-budgeted
  assembler packs the best ones into the prompt.
- **It runs code it has never seen.** So there's a hardened Docker sandbox —
  no network, read-only host filesystem, non-root, CPU and wall-clock capped.
- **It can spend real money.** So one budget object is shared across all three
  agents. When it's exhausted, the run stops cleanly rather than quietly costing $40.
- **Providers fall over.** The Planner falls back to a stable model when the
  configured one is unusable, and the Coder survives a mid-run provider outage
  instead of losing everything.
- **Webhooks fire twice.** So delivery IDs are de-duplicated and `(repo, issue_number)`
  is guarded — GitHub retrying doesn't get you two PRs.

The reasoning core isn't wired to one vendor. All three agents talk to an
`LLMProvider` interface and you pick provider *and* model per role.

---

## Architecture

Six layers, each testable on its own:

```
GitHub  ──▶  FastAPI gateway  ──▶  Redis/Celery  ──▶  Planner ─▶ Coder ─▶ Reviewer
                    │                                     │
                    ▼                                     ▼
               PostgreSQL                          Docker sandbox
            (runs, steps, events)                 (tests run in here)
```

<details>
<summary><b>Full webhook-to-PR sequence diagram</b> (click to expand)</summary>

```mermaid
sequenceDiagram
    autonumber
    actor GitHub as "GitHub Webhook"
    participant Gateway as "FastAPI Gateway (app/main.py)"
    participant Broker as "Redis / Celery (workers/tasks.py)"
    participant DB as "PostgreSQL (db/models.py)"
    participant LocalGit as "Git & Indexer (agent/github.py & retrieval.py)"
    participant Planner as "Planner Agent (agent/planner.py)"
    participant Sandbox as "Docker Sandbox (agent/sandbox.py)"
    participant Coder as "Coder Agent ReAct (agent/loop.py)"
    participant Reviewer as "Reviewer Agent (agent/reviewer.py)"
    participant GHRest as "GitHub REST API"

    GitHub->>Gateway: POST /webhooks/github (HMAC Signature)
    Gateway->>Gateway: _verify_signature() & parse_webhook_event()
    Gateway->>DB: Check WebhookEvent (deduplication)
    Gateway->>Gateway: Mint run_id (UUID)
    Gateway->>Broker: run_issue.delay(repo, issue_number, use_sandbox, run_id)
    Gateway-->>GitHub: 202 Accepted (run_id)

    Broker->>DB: Persist Run record (status="running")
    Broker->>GHRest: client.get_issue(repo, issue_number)
    GHRest-->>Broker: Issue metadata

    Broker->>LocalGit: clone() & create_branch("agent/issue-N")
    Broker->>LocalGit: index_repo() (Tree-Sitter + ChromaDB Ephemeral)

    Broker->>Planner: run_planner(issue_task, workspace)
    LocalGit-->>Planner: Codebase retrieval (top-k chunks)
    Planner-->>Broker: PlannerOutput (understanding, steps, hypothesis, test_strategy)
    Broker->>DB: Persist Planner Step
    Broker->>GHRest: client.comment_on_issue() [Pre-work comment]

    opt If use_sandbox == True
        Broker->>Sandbox: Sandbox(workspace).start()
        Sandbox->>Sandbox: Spin up Docker container (--network none, --read-only, --user non-root)
    end

    Broker->>Coder: ReActAgent.run_with_plan(task, plan, sandbox)
    loop ReAct Tool Execution Loop
        Coder->>LocalGit: view_file / replace_file_content / search
        alt Tool == run_tests (Sandboxed execution)
            Coder->>Sandbox: sandbox.run_tests(target)
            Sandbox->>Sandbox: docker exec aswe-sbx-XXX pytest
            Sandbox-->>Coder: ExecResult (exit code + stdout/stderr)
        else Host file inspection/edit
            LocalGit-->>Coder: File content / edit status
        end
        Coder->>DB: Persist Coder Step (incremental)
    end
    Coder-->>Broker: RunResult (completed, final_text)

    Broker->>LocalGit: _capture_diff() (git add -A && git diff --cached)
    Broker->>Reviewer: run_reviewer(task, plan, diff, test_output)
    Reviewer-->>Broker: ReviewerOutput (verdict: approve/request_changes)
    Broker->>DB: Persist Reviewer Step

    alt Verdict == request_changes (up to MAX_REVIEW_ROUNDS)
        Broker->>Coder: agent.run(feedback_task with prior diff & feedback)
        Coder->>Sandbox: Re-run sandboxed tests as needed
    end

    Broker->>LocalGit: submit_changes() (commit_all & push)
    Broker->>GHRest: client.create_pull_request()
    GHRest-->>Broker: PullRequest (pr_url, pr_number)

    Broker->>GHRest: client.comment_on_issue() [Final PR comment]
    opt If use_sandbox == True
        Broker->>Sandbox: sandbox.close() (docker rm -f container)
    end
    Broker->>LocalGit: drop_repo() & Cleanup workspace
    Broker->>DB: Final update (status="completed", pr_url, cost, finished_at)
```

</details>

### The Coder's toolbox

The ReAct loop gets five tools, all scoped to the workspace and path-traversal guarded:

| Tool | What it does |
|---|---|
| `retrieve_context` | Semantic search over the indexed repo. The starting point for every run. |
| `read_file` | Exact file contents. Required before editing. |
| `edit_file` | Unique-string replacement, or file creation. |
| `run_tests` | pytest — inside the sandbox when it's enabled, on the host otherwise. |
| `list_dir` | Directory listing, junk folders filtered. |

---

## Quick start

```bash
# 1. Configure
#    Required: GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET, AGENT_API_TOKEN,
#              AGENT_REPO_ALLOWLIST, and GEMINI_API_KEY or ANTHROPIC_API_KEY
$EDITOR .env

# 2. Bring up the stack — API, worker, Redis, Postgres, migrations
docker compose up --build -d
curl localhost:8000/readyz          # {"status":"ready", ..., "sandbox":"off"}

# 3. Expose the gateway to GitHub
ngrok http 8000

# 4. Register the webhook — one command, no clicking through repo settings
bash scripts/setup_webhook.sh --repo owner/name --url https://<id>.ngrok-free.app
bash scripts/setup_webhook.sh --repo owner/name --ping     # expect 204

# 5. Open an issue and watch.
```

`setup_webhook.sh` is idempotent **by webhook path**, so re-running it after an
ngrok restart updates the existing hook instead of piling up dead ones. It also
takes `--list`, `--ping` and `--delete`.

### Running it directly, no GitHub involved

```bash
pip install -r requirements.txt
python -m agent.loop "Fix the off-by-one in paginate()" \
    --workspace /path/to/repo --auto-index
```

Budgets come from `--max-steps` / `--max-usd`; the model comes from env.

### Configuration worth knowing about

Each agent picks its own provider and model. Resolution goes role-specific vars →
`LLM_*` → a hardcoded default that **can never be empty**:

```bash
PLANNER_MODEL=gemini-3.1-pro-preview     # one call per run — plan quality sets everything downstream
CODER_MODEL=gemini-3.5-flash             # the ReAct loop; dominates token spend, so run it cheap
REVIEWER_MODEL=gemini-3.1-pro-preview    # one call per run — catching what the Coder missed
```

A model id is provider-specific and is never inherited across providers: setting
`PLANNER_PROVIDER=anthropic` while `LLM_PROVIDER=gemini` falls through to the
Anthropic default rather than handing a Gemini model name to the Anthropic API.

```bash
USE_SANDBOX=false            # run tests in the hardened container (needs a Docker daemon)
AGENT_REPO_ALLOWLIST=...     # repos the agent may touch — enforced on webhook AND POST /runs
MAX_USD=5.0                  # per-run spend cap, shared across all three agents
MAX_STEPS=30                 # cap on Coder model calls
MAX_REVIEW_ROUNDS=2          # Reviewer → Coder feedback rounds
```

⚠️ These are read **once at process start**. After editing `.env`:
`docker compose up -d --force-recreate api worker`.

---

## Where things stand

| Component | Status | Notes |
|---|---|---|
| Codebase understanding (`agent/retrieval.py`) | ✅ Done | tree-sitter chunking, embeddings, in-memory ChromaDB, token-budgeted context |
| Multi-agent reasoning (`planner.py`, `loop.py`, `reviewer.py`) | ✅ Done | one shared token/USD budget across all three, per-role model selection |
| GitHub integration (`agent/github.py`) | ✅ Done | rate-limit backoff, HMAC verification, idempotent PR creation |
| Backend & queue (`app/`, `workers/`, `db/`) | ✅ Done | FastAPI, Celery, Redis, Postgres + Alembic, append-only run history |
| Security | ✅ Done | HMAC webhooks, authenticated `POST /runs` (fail-closed), repo allowlist, rate limiting, TLS via Caddy |
| Cloud deployment | ✅ Done | single Azure VM, same Compose stack, Let's Encrypt, auto-shutdown |
| Docker sandbox (`agent/sandbox.py`) | ⚙️ Built, **off** | `USE_SANDBOX=false` in deployment — a config flip, not a code change |
| Observability (`monitoring/`) | ⏳ Not started | `/runs/{id}/steps` covers most of the need today |
| Eval harness (`eval/`) | ⏳ Not started | **the most important gap — see below** |

### Testing

```bash
python -m pytest tests/ -v    # 50 integration tests, no API key needed
python -m agent.loop          # per-module self-tests (also: planner, reviewer,
                              # github, sandbox, schemas, db.models, workers.tasks, app.main)
```

Every test was confirmed to **fail** against the pre-fix code before being
accepted — a test that passes either way proves nothing.

---

## Where this goes next

Full detail, with measurements, in **[docs/future-goals.md](docs/future-goals.md)**.

The short version: the plumbing is solid, **retrieval is the weakest layer**, and
nothing measures quality end to end — so fix the measuring first.

| # | Work | Why |
|---|---|---|
| 1 | **Eval harness** — `eval/` is a seed, not a harness | Every other item is an opinion until recall@k and fix-rate exist |
| 2 | **Index module-level constants** | `parse_file()` only chunks functions, classes and imports — every config constant in every repo is invisible to search |
| 3 | **Add a relevance floor** | `retrieve()` always returns `k` hits, so "nothing matched" is indistinguishable from a real result |
| 4 | **Add a `grep` tool** | There is no literal search at all — the escape hatch is missing exactly when retrieval fails |
| 5 | **Handle oversized chunks** | The embedder truncates at ~256 tokens, silently, so long functions are indexed on their first 40 lines |
| 6 | **Down-weight test files** | A behavioural query returns 4 tests in the top 5; the implementation lands at rank 4 |
| 7 | **Multi-language indexing** | Only Python is parsed — a TS or Go repo indexes to *zero chunks* and the agent works blind |
| 8 | **CI failure recovery** | The agent's job currently ends where a real engineer's begins |

Beyond that: a trace dashboard (`frontend/` is empty and `/runs/{id}/steps`
already serves everything a UI needs), replying to human PR review comments,
difficulty-aware model routing, and a pre-PR self-check gate.

These findings are measured, not assumed — the probes are checked in:

```bash
python eval/probe_retrieval_naming.py    # controlled naming cases
python eval/probe_retrieval_scale.py     # 445 chunks from this repo
```

One useful negative result: **badly named functions are *not* the weakness they
look like.** Retrieval embeds the whole chunk, not the name, so a meaningless
`zorblat()` that paginates is still found at rank 1 for "split a list into fixed
size pages". Bad naming only bites when the meaning is *also* past the truncation
window — see §2.1 and §2.3 of the doc.

---

## Documentation

The `docs/` folder goes deep on each component:

| Doc | Covers |
|---|---|
| [deployment.md](docs/deployment.md) | How it's deployed, what broke getting there, how to change it |
| [retrieval.md](docs/retrieval.md) | The codebase understanding engine |
| [loop.md](docs/loop.md) | The ReAct agent loop and its tools |
| [github.md](docs/github.md) | Issue intake, PR output, git helpers |
| [sandbox.md](docs/sandbox.md) | The hardened Docker execution sandbox |
| [persistence.md](docs/persistence.md) | Schema, migrations, append-only runs |
| [llm-provider-abstraction.md](docs/llm-provider-abstraction.md) | ADR: pluggable Anthropic/Gemini providers |
| [future-goals.md](docs/future-goals.md) | What to build next, and the measurements behind the ordering |

[plan2.md](plan2.md) is the single source of truth for architecture, decisions
and open work — including §22, an audit that found 18 defects in the multi-agent
integration and how each was fixed.

Kubernetes/Helm and Prometheus/Grafana were in the original design and are
deliberately **not** built yet — see `plan2.md` §N5 and §21. A single VM is a
complete deployment for this workload, and `/runs/{id}/steps` covers most of what
dashboards would.

## Stack

Python 3.12 · FastAPI · Celery · Redis · PostgreSQL · Docker Compose · Caddy ·
Azure VM · tree-sitter · sentence-transformers · ChromaDB · Anthropic Claude or
Google Gemini, selectable per agent role.

## About

Built by Nadeem, a third-year B.Tech student at IIT Bombay, interning at Alimento
Agro Foods and heading for ML/AI engineering roles after graduation.
