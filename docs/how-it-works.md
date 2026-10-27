# How Auto-SWE Agent Works

This document explains the whole system: what happens between someone opening
an issue and a pull request appearing, what each component does, how failures
are handled, and why it is built the way it is. The per-component docs in this
folder go deeper on each part; this is the map that connects them.

Throughout, the running example is a real run: issue #43 on a test repository,
*"`evaluate("2^3^2")` returns 64, but the answer should be 512"*. The agent
traced it to the `^` operator being left-associative in a Pratt parser, fixed
it, and opened PR #44 in 12 steps for about $0.04.

---

## 1. The big picture

```
                 ┌─────────────────────── one Docker Compose stack ───────────────────────┐
GitHub ─webhook─▶│ api (FastAPI) ──▶ redis (queue) ──▶ worker (Celery)                      │
                 │      │                                 │                                │
                 │      ▼                                 ├─▶ Planner ─▶ Coder ─▶ Reviewer │
                 │  postgres ◀──────── every step ────────┤                                │
                 │                                        └─▶ sandbox containers (optional)│
                 └────────────────────────────────────────────────────────────────────────┘
                                              │
                                              ▼
                                   branch + pull request on GitHub
```

| Service | Role |
|---|---|
| `api` | Receives webhooks and manual triggers, validates them, queues a run, serves the read API |
| `redis` | The job queue between the API and the worker |
| `worker` | Runs the whole pipeline for one issue: clone, index, plan, code, review, ship |
| `postgres` | System of record: every run, every step, every webhook delivery |
| `migrate` | One-shot container that applies database migrations, then exits |

The API answers GitHub within milliseconds with `202 Accepted`. All of the slow
work (cloning, model calls, test runs) happens in the worker, so a slow run
never causes GitHub to time out and retry.

---

## 2. The life of a run

### Step 1: The webhook arrives (`app/main.py`)

GitHub sends a `POST /webhooks/github` when an issue is opened, reopened or
labelled. Before anything is queued, the gateway checks:

1. **Size.** Bodies over 1 MB (`MAX_WEBHOOK_BODY_BYTES`) are rejected with `413`.
2. **Signature.** The `X-Hub-Signature-256` header must be a valid HMAC of the
   body using `GITHUB_WEBHOOK_SECRET`, compared in constant time. If no secret is
   configured, every webhook is rejected. The endpoint fails closed.
3. **Event type.** Only `issues` events with action `opened`, `reopened` or
   `labeled` start a run. Everything else (pings, comments, other actions) gets
   `204` and is ignored.
4. **Allowlist.** The repository must be in `AGENT_REPO_ALLOWLIST`.
5. **Duplicates.** Each delivery's `X-GitHub-Delivery` ID is recorded in the
   `webhook_events` table. GitHub retries deliveries; a retry is recognised and
   does not start a second run.

If everything passes, the gateway mints a `run_id`, puts the job on the queue
and returns `202` with that ID.

The same pipeline can be started without GitHub through `POST /runs`, which
requires the `AGENT_API_TOKEN` header, is rate limited per client, and enforces
the same allowlist.

### Step 2: The worker picks it up (`workers/tasks.py`)

`run_issue()` runs inside Celery with a hard time limit (`MAX_WALLCLOCK_S`,
30 minutes by default, with a graceful stop 60 seconds earlier).

- **One active run per issue.** The database allows at most one `running` row
  per `(repo, issue_number)`. A second trigger for the same issue is recorded
  as `skipped_duplicate` instead of producing a second PR.
- **Stale run cleanup.** If a worker died mid-run (a crash, or the VM shutting
  down), its row would stay `running` forever and block the issue. Before
  starting, the worker marks any `running` row older than the time limit as
  `stale`.
- It then fetches the issue's title and body from the GitHub API.

### Step 3: Clone, branch, index (`agent/github.py`, `agent/retrieval.py`)

The repo is cloned into a fresh workspace, a branch `agent/issue-43` is
created, and every Python file is indexed for semantic search:

1. **Parse.** tree-sitter splits each file into meaningful chunks: one per
   function, method, class header and import block, rather than arbitrary
   line windows.
2. **Embed.** Each chunk is embedded with the `all-MiniLM-L6-v2` sentence
   transformer, which runs locally on CPU. Embeddings are cached by content
   hash, so unchanged code is never re-embedded.
3. **Store.** Vectors go into an in-memory ChromaDB collection that lives only
   as long as the run.

Later, any agent can ask a question in plain language ("where is operator
precedence decided?") and get back the most relevant chunks, packed into a
fixed token budget so the context never overflows.

### Step 4: The Planner (`agent/planner.py`)

The Planner makes one model call. It receives the issue and the retrieved code
and returns structured output:

- **understanding**: what the issue is asking for,
- **root cause hypothesis**: where and why the code is wrong,
- **steps**: the plan,
- **test strategy**: how the fix will be proven.

This is posted to the issue as a comment, so a human can see the agent's
reasoning before any code exists. For issue #43 it said:

> The parser in `expression.py` defines the `^` operator as left-associative
> rather than right-associative.

That was exactly right, and it was found from the symptom alone.

If the configured Planner model is unusable (overloaded, retired, or returns
nothing parseable), the Planner retries once on a fallback model from an older
generation, chosen because it has spare capacity. A run is not lost because one
model is having a bad day.

### Step 5: The Coder (`agent/loop.py`)

The Coder is a tool-using loop: the model reasons, calls a tool, sees the
result, and repeats. It gets five tools, all confined to the workspace (paths
that try to escape it are refused):

| Tool | What it does |
|---|---|
| `retrieve_context` | Semantic search over the index. Usually the first call. |
| `read_file` | Exact file contents. A file must be read before it can be edited. |
| `edit_file` | Replace a unique string in a file, or create a new file. |
| `run_tests` | Run pytest, in the sandbox if it is enabled, otherwise in the worker. |
| `list_dir` | List a directory, skipping junk folders like `__pycache__`. |

A tool failure (a missing file, an edit string that matches twice) goes back to
the model as an error it can react to. It does not crash the run. The loop ends
when the model says it is done or when the budget runs out.

If the model provider fails mid-run, the Coder keeps the work done so far
instead of throwing it away.

### Step 6: The Reviewer (`agent/reviewer.py`)

The worker captures the full diff and the last test output, and the Reviewer
reads them the way a senior engineer would. It returns `approve` or
`request_changes` with specific concerns.

- On `request_changes`, the concerns go back to the Coder for another round,
  up to 2 rounds.
- If the Reviewer's answer cannot be parsed, the change is **not** silently
  approved as reviewed. The PR is opened with a visible "automated review was
  unavailable, please review manually" banner. A missing review is never
  presented as a passed one.

### Step 7: Ship

The worker commits, pushes the branch, and opens a pull request. Its
description is built from the run itself:

- `Closes #43`, so merging the PR closes the issue,
- the issue as the Planner understood it, and the root cause,
- the Coder's summary of the fix,
- **test evidence**: the failing output before the fix and the passing output
  after, when both were captured,
- the Reviewer's verdict and concerns, plus any files changed that the Planner
  did not expect to touch,
- steps, tokens and cost.

It then comments on the issue with a link to the PR. The workspace is deleted
and the run's final status, PR URL and cost are written to the database. If the
agent finished without changing any files, no PR is opened; the run ends as
`no_changes` with a comment on the issue saying so.

---

## 3. The three agents, and why there are three

One model doing everything tends to start editing before it understands the
problem, and then grades its own work generously. Splitting the job gives each
part one responsibility:

| Agent | Calls per run | What it optimises for |
|---|---|---|
| Planner | 1 (plus a possible fallback) | Getting the diagnosis right; everything downstream depends on it |
| Coder | many (up to `MAX_STEPS`) | Cheap, fast iteration against the tests |
| Reviewer | 1 per round | Catching what the Coder missed |

Because of that difference in call volume, each role can use a different model.
A typical setup plans and reviews with a strong model and codes with a cheap one:

```bash
PLANNER_MODEL=gemini-3.1-pro-preview
CODER_MODEL=gemini-3.5-flash
REVIEWER_MODEL=gemini-3.1-pro-preview
```

**Model resolution.** For each role the worker looks at the role's own variables
(`PLANNER_PROVIDER`, `PLANNER_MODEL`), then the global `LLM_PROVIDER` /
`LLM_MODEL`, then a built-in default. A model name is never carried across
providers. If the Planner is switched to Anthropic while the global model is a
Gemini model, it gets the Anthropic default instead of being handed a model name
Anthropic doesn't recognise.

**Providers.** All agents talk to one small `LLMProvider` interface
(`agent/providers/`), with an implementation for Anthropic and one for Gemini.
Adding another provider means implementing that interface; no agent code
changes. See [llm-provider-abstraction.md](llm-provider-abstraction.md).

---

## 4. Cost control

All three agents share one budget object for the whole run. It is checked
before every model call:

| Limit | Variable | Default |
|---|---|---|
| Dollars spent | `MAX_USD` | 5.0 |
| Tokens used | `MAX_TOTAL_TOKENS` | 500,000 |
| Model calls | `MAX_STEPS` | 30 |
| Wall-clock time | `MAX_WALLCLOCK_S` | 1800 s |

Because the budget is shared, a Planner that used a lot leaves less for the
Coder; there is no way for the total to exceed the cap by each agent staying
under its own. When a limit is hit the run stops cleanly and records why.

---

## 5. The sandbox

Running a repository's tests means executing code the agent did not write. For
your own repos that is acceptable. For anyone else's, it is not: a malicious
test could read `GITHUB_TOKEN` from the environment and send it somewhere.

With `USE_SANDBOX=true`, each run gets a throwaway container
(`agent/sandbox.py`), and `run_tests` executes there instead of in the worker:

| Protection | Flag |
|---|---|
| No network at all | `--network none` |
| Read-only root filesystem, small writable `/tmp` | `--read-only`, `--tmpfs` |
| Not root, no capabilities, can't regain privileges | `--user`, `--cap-drop ALL`, `no-new-privileges` |
| Resource limits | `--cpus`, `--memory`, `--pids-limit` |
| `.git` hidden from test code | read-only tmpfs over `/work/.git` |

The `.git` mask matters more than it looks: a test that wrote a git hook into
the workspace would get that hook executed later, when the worker commits,
outside the sandbox.

The trade-off is dependencies. With no network the sandbox cannot `pip install`
anything, so the target repo's test dependencies must be built into the sandbox
image. That is why the sandbox is off by default and documented as a choice
rather than hidden. Full details: [sandbox.md](sandbox.md).

The worker needs the Docker socket to start sandbox containers, which is
effectively root on the host. That is acceptable on a machine that runs nothing
else and whose firewall only opens ports 22, 80 and 443; the
[deployment doc](deployment.md) records this as an explicit decision.

---

## 6. What gets recorded

Three tables in Postgres (`db/models.py`):

| Table | One row per | Holds |
|---|---|---|
| `runs` | run | repo, issue, status, branch, PR URL, cost, tokens, timings |
| `run_steps` | agent step | the plan, each tool call and its result, each review |
| `webhook_events` | GitHub delivery | delivery ID, used to drop retries |

Steps are written as they happen, not at the end, so a run can be watched live
and a crashed run still leaves a trace up to the point it died. Runs are
append-only: a re-run of the same issue adds a new row instead of overwriting
history.

Everything is readable over the API:

| Endpoint | Returns |
|---|---|
| `GET /runs` | Recent runs, filterable by status |
| `GET /runs/{id}` | One run: status, PR link, cost |
| `GET /runs/{id}/steps` | The full trace: plan, every tool call, every review |
| `GET /healthz` | The API process is up |
| `GET /readyz` | Redis and Postgres are reachable, and whether the sandbox is on |

Final run statuses include `completed`, `no_changes` (the agent decided nothing
needed changing), `skipped_duplicate`, `stale`, and specific failures such as
`provider_error`, `github_error`, `index_error` and `sandbox_error`, so a failed
run says which part failed.

---

## 7. When things go wrong

| Failure | What happens |
|---|---|
| Forged or unsigned webhook | Rejected before parsing |
| GitHub retries a delivery | Recognised by delivery ID; no second run |
| Same issue triggered twice | Second run recorded as `skipped_duplicate` |
| Repo not on the allowlist | Rejected at the gateway |
| Planner model unusable | Retried once on a fallback model |
| Provider outage during coding | Coder keeps the work done so far |
| Budget exhausted | Run stops cleanly with the reason recorded |
| Reviewer output unparseable | PR opened with a "review unavailable" banner, never marked as reviewed |
| GitHub API rate limit | Backs off and retries |
| Worker dies mid-run | Row marked `stale` on the next run for that issue |
| Agent changes nothing | Status `no_changes`; no empty PR |

---

## 8. Running it

**Locally:** `docker compose up`, plus a tunnel such as ngrok so GitHub can reach
`localhost:8000`. `scripts/setup_webhook.sh` registers the webhook and, when
ngrok gives you a new URL, updates it rather than adding another.

**On a server:** the same Compose file on a small VM, with Caddy in front for
HTTPS via Let's Encrypt. The reference deployment is a 2 vCPU / 4 GB ARM VM on
Azure. A VM was chosen over a container platform because the sandbox needs a
Docker daemon, which managed container platforms don't provide, and because
running Postgres and Redis as containers is far cheaper than managed versions.
The reasoning and every problem met along the way are in
[deployment.md](deployment.md).

---

## 9. Known limits

- **Python only.** Retrieval parses Python files. A repo in another language
  indexes to nothing, and the agent would work without any code context.
- **Retrieval has measured weak spots.** Module-level constants aren't indexed,
  there is no plain-text search tool, and long functions are embedded only up
  to the embedder's ~256-token limit. These are measured and ranked in
  [future-goals.md](future-goals.md).
- **No end-to-end benchmark yet.** Building an evaluation harness that measures
  fix rate across many issues is the top roadmap item, because every other
  improvement depends on being able to measure it.

---

## Where to read next

| To understand | Read |
|---|---|
| Every stage with code references | [workflow_webhook_to_pr.md](workflow_webhook_to_pr.md) |
| Indexing and search | [retrieval.md](retrieval.md) |
| The Coder's loop | [loop.md](loop.md) |
| GitHub integration | [github.md](github.md) |
| Database and idempotency | [persistence.md](persistence.md) |
| The sandbox | [sandbox.md](sandbox.md) |
| Deployment | [deployment.md](deployment.md) |
| What comes next | [future-goals.md](future-goals.md) |
