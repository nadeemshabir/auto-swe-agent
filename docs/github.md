# GitHub Integration (`agent/github.py`) — Deep Dive

This is the full reference for `agent/github.py`. It is written so that if you read it top to bottom you understand **where this piece fits in the project**, **what it does and why**, **how every part of the code works**, and **what to build next**. It is longer than a typical README on purpose — it doubles as the design notes and the onboarding doc for this module.

---

## 0. Where this fits — the project so far

The autonomous SWE agent is a pipeline. An issue comes in on one end, a pull request comes out the other, with no human in the loop:

```
  GitHub issue
       │
       ▼
 ┌───────────────┐   ┌──────────────────┐   ┌───────────────┐   ┌──────────────┐
 │  github.py    │──▶│  retrieval.py    │──▶│   loop.py     │──▶│  github.py   │
 │  read issue   │   │  understand repo │   │  ReAct: plan, │   │  branch,     │
 │  → task text  │   │  (RAG over AST)  │   │  edit, test   │   │  commit, PR  │
 └───────────────┘   └──────────────────┘   └───────────────┘   └──────────────┘
       (in)               (context)              (brain)              (out)
```

What already existed before this module:

| Component | File | Role | Status |
|---|---|---|---|
| Codebase understanding | `agent/retrieval.py` | Parses the repo's ASTs (tree-sitter), embeds chunks (sentence-transformers), stores them in ChromaDB, and assembles token-budgeted context for a query. | Done |
| ReAct loop ("the brain") | `agent/loop.py` | Drives an LLM through Reason → Act → Observe, with a `Budget` controller and workspace-confined tools (`read_file`, `edit_file`, `run_tests`, …). | Done |
| Provider abstraction | `agent/providers/` | Lets the loop run on Anthropic **or** Gemini via one env var, behind an `LLMProvider` interface. | Done |

**This module — `agent/github.py` — is the two ends of the pipeline:** the *input* (turn a GitHub issue into the task string the loop consumes) and the *output* (turn the loop's file edits into a branch, a commit, a push, and a pull request). It is what makes the agent's work reach the outside world. Before it, the loop could fix code in a local folder; after it, the agent can be handed a real issue number and answer with a real PR.

The deliberate non-goal: this module does **not** decide *how* to fix anything (that's the loop) and does **not** run untrusted code in isolation (that's `sandbox.py`, still to come). It only handles the GitHub I/O.

---

## 1. The mental model: REST client + git helpers + glue

`github.py` has **two independent halves and a thin layer of glue**. Keeping them independent matters: the REST half can be tested with zero git, the git half can be tested with zero network, and either could be replaced without touching the other.

1. **REST client (`GitHubClient`)** — talks to GitHub's HTTP API to read issues and open/find/comment on PRs. Pure standard library.
2. **Git helpers** — shell out to the `git` CLI to clone, branch, commit, and push the actual code.
3. **Glue** — `parse_webhook_event` (a webhook payload → an `Issue`), `Issue.to_task()` (an issue → the loop's task string), and `submit_changes()` (commit → push → open PR in one idempotent call).

### Two design rules that shaped everything

- **Add no dependency.** The project already installs heavy ML/LLM packages. The GitHub layer adds *nothing* — the REST client is built on `urllib` (stdlib), and git operations call the `git` binary you already have. The only runtime requirements are `git` on `PATH` and `GITHUB_TOKEN` in the environment.
- **Make it testable offline.** Network code that can only be tested against the live API is, in practice, never tested. So the HTTP transport, the sleep function, and the clock are all *injected* into `GitHubClient`. In tests we pass fakes; in production it uses the real ones. The entire retry/backoff/redaction machinery runs in `_selftest` with no network and no mocking library (more in §6).

---

## 2. The value objects

These are the plain data shapes that flow through the module. Both are `@dataclass`es with `from_api` constructors, so the messy provider JSON is normalized in exactly one place.

### `Issue`

```python
@dataclass
class Issue:
    repo: str                 # "owner/name" — threaded in separately (see below)
    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    author: str = ""
    html_url: str = ""
```

Why `repo` is a separate field and not read from the issue JSON: the REST issue object and the webhook payload put the repository slug in *different* places (the webhook has it under `repository.full_name`, the issue object doesn't carry it at all in a convenient form). Rather than dig for it differently in each path, we always pass it in explicitly. `from_api(data, repo)` does the normalization — note it defends against labels being either `{"name": ...}` objects (REST) or bare strings, and strips empties.

The important method is **`to_task()`** — this is the seam between this module and the loop:

```python
def to_task(self) -> str:
    lines = [
        f"Resolve GitHub issue #{self.number} in repository {self.repo}.",
        "",
        f"Title: {self.title or '(no title)'}",
    ]
    if self.labels:
        lines.append(f"Labels: {', '.join(self.labels)}")
    lines += ["", "Issue description:", self.body or "(no description provided)"]
    return "\n".join(lines)
```

It gives the model the title, labels, body, and an explicit `#N in owner/repo` reference (so any commit/PR text the model writes can cite the issue) — but it says **nothing about how to fix the bug**. That instruction lives in `loop.py`'s `DEFAULT_SYSTEM` prompt. Keeping the "what" (this issue) separate from the "how" (the loop's method) means we can change the agent's working style without touching GitHub parsing, and vice versa.

### `PullRequest`

```python
@dataclass
class PullRequest:
    number: int
    html_url: str
    head: str        # branch the changes live on
    base: str        # branch the PR merges into
    created: bool    # True if we opened it now, False if it already existed
```

The `created` flag is what makes re-runs honest: the caller can tell whether it opened a fresh PR or adopted an existing one (see idempotency in §4).

---

## 3. The transport abstraction (why the client is testable at all)

Before the client itself, understand the layer beneath it. A *transport* is just a function with this shape:

```python
# (method, url, headers, data_or_None, timeout) -> _RawResponse
Transport = Callable[[str, str, dict, "bytes | None", float], _RawResponse]
```

and a `_RawResponse` is a tiny struct:

```python
@dataclass
class _RawResponse:
    status: int
    headers: dict[str, str]   # keys lower-cased — HTTP header names are case-insensitive
    body: bytes
```

The default transport, `_urllib_transport`, wraps `urllib`. The subtle and important part is its **error contract**:

- An HTTP *status* — including `403`, `404`, `500` — is **returned** as a normal `_RawResponse`. `urllib` raises `HTTPError` for non-2xx, so we catch it and convert it back into a response object. This is what lets the retry logic *inspect* a `403`'s headers to decide whether it's a rate limit.
- A *connection-level* failure — DNS failure, connection refused, reset, timeout — never produced an HTTP response, so it is **raised** as `_TransportError`. The retry logic treats this category separately (it can always be retried; there are no headers to consult).

```python
def _urllib_transport(method, url, headers, data, timeout) -> _RawResponse:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _RawResponse(resp.status,
                                {k.lower(): v for k, v in resp.headers.items()},
                                resp.read())
    except urllib.error.HTTPError as e:           # 4xx/5xx → still a response
        body = e.read() if ... else b""
        return _RawResponse(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
    except urllib.error.URLError as e:            # never connected → raise
        raise _TransportError(str(getattr(e, "reason", e)))
```

Because `GitHubClient` accepts *any* callable with this signature, the test suite passes a **fake transport** that returns canned `_RawResponse`s — simulating a rate-limited `403` followed by a `200`, a `422` conflict, a `401`, etc. — and the entire client behaves exactly as it would against real GitHub, with no socket ever opened. That is the single most important design choice in the file.

---

## 4. The REST client (`GitHubClient`) in detail

### Construction

```python
def __init__(self, token=None, *, api_url=None, transport=None,
             max_retries=_MAX_RETRIES, timeout=_HTTP_TIMEOUT,
             sleep=time.sleep, now=time.time):
    self.token   = token or os.getenv("GITHUB_TOKEN") or ""
    self.api_url = (api_url or os.getenv("GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")
    self._transport = transport or _urllib_transport
    ...
    self._sleep = sleep      # injected → tests pass a no-op / recorder
    self._now   = now        # injected → tests pass a fixed clock
```

`token`, `api_url`, `transport`, `sleep`, and `now` all have production defaults but can be overridden. The three injected behaviours (`transport`, `sleep`, `now`) exist purely so the retry/backoff logic is deterministic and instant under test.

### The request engine: `_request`

Every public method funnels through `_request`, which is the heart of the client. The loop is short, so here it is in full with annotations:

```python
def _request(self, method, path, *, body=None, params=None):
    url = self._url(path, params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = self._headers(has_body=data is not None)

    last_desc = "unknown error"
    for attempt in range(self.max_retries + 1):          # 1 try + N retries
        try:
            resp = self._transport(method, url, headers, data, self.timeout)
        except _TransportError as e:                     # never connected
            last_desc = f"network error: {self._redact(str(e))}"
            if attempt < self.max_retries:
                self._wait(self._backoff(attempt), attempt, last_desc)
                continue                                 # retry
            raise GitHubError(f"{method} {self._safe_url(url)} failed: {last_desc}")

        if self._should_retry(resp) and attempt < self.max_retries:
            delay = self._retry_delay(resp, attempt)     # how long to wait
            self._wait(delay, attempt, f"HTTP {resp.status} (will retry)")
            continue                                     # retry

        return self._parse(resp, method, url)            # success or hard fail
```

Read it as three outcomes per attempt:

1. **Couldn't connect** → retry with plain backoff, or give up after the budget.
2. **Connected, but the status says "try again"** → retry after a smartly-chosen delay.
3. **Connected with a final answer** (2xx, or a 4xx we shouldn't retry) → hand off to `_parse`, which either returns JSON or raises.

The headers (`_headers`) always include `Accept: application/vnd.github+json`, the pinned `X-GitHub-Api-Version`, and a `User-Agent` (GitHub rejects requests without one). `Authorization: Bearer <token>` is added only when a token is present, and `Content-Type: application/json` only when there's a body.

### Deciding *whether* to retry: `_should_retry` + `_is_rate_limited`

```python
def _should_retry(self, resp):
    if resp.status in _RETRYABLE_STATUS:      # {429, 500, 502, 503, 504}
        return True
    if resp.status == 403:                    # 403 is the tricky one
        return self._is_rate_limited(resp)
    return False
```

`429` and the `5xx` family are always transient — retry. The interesting case is **`403`**: GitHub returns `403` for *both* "you don't have permission" (never retry — it'll fail forever) *and* "you hit a rate limit" (definitely retry — it'll succeed later). We disambiguate by the headers:

```python
@staticmethod
def _is_rate_limited(resp):
    if resp.headers.get("retry-after"):                 # secondary rate limit
        return True
    if resp.headers.get("x-ratelimit-remaining") == "0":# primary rate limit
        return True
    return False
```

A genuine permission `403` has neither header, so it correctly falls through to "don't retry" and surfaces immediately as an error.

### Deciding *how long* to wait: `_retry_delay` + `_backoff`

```python
def _retry_delay(self, resp, attempt):
    retry_after = resp.headers.get("retry-after")       # 1. explicit instruction
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), _BACKOFF_CAP)
    if resp.headers.get("x-ratelimit-remaining") == "0":# 2. wait for quota reset
        reset = resp.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            wait = float(reset) - self._now()           # reset is an epoch second
            return max(0.0, min(wait, _BACKOFF_CAP))
    return self._backoff(attempt)                       # 3. fall back to backoff
```

The priority order matters:

1. If GitHub sent a **`Retry-After`** (it does for *secondary* rate limits — the abuse/concurrency limiter), obey it exactly.
2. Otherwise, if we're out of **primary** quota, `X-RateLimit-Reset` is the Unix timestamp when the quota refills — wait precisely until then (this is why `now` is injectable: the test pins it so the math is deterministic).
3. Otherwise (a plain `5xx`/`429` with no timing hint), fall back to exponential backoff with jitter:

```python
@staticmethod
def _backoff(attempt):                       # attempt = 0, 1, 2, ...
    base = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)   # 1s, 2s, 4s, 8s … cap 30s
    return base + random.uniform(0.0, base * 0.25)             # + jitter
```

Every delay is capped at `_BACKOFF_CAP` (30s) so a misbehaving server can never make the agent hang for minutes. The jitter avoids a thundering herd if many runs back off in lockstep. `_wait` logs the reason and only actually sleeps when `delay > 0` (a subtlety the self-test caught — see §7).

### Turning a response into a result: `_parse`

```python
def _parse(self, resp, method, url):
    text = resp.body.decode("utf-8", "replace") if resp.body else ""
    if 200 <= resp.status < 300:
        return json.loads(text) if text.strip() else {}   # empty body → {}
    # non-2xx: surface GitHub's own "message" (+ "errors"), token-scrubbed
    ...
    raise GitHubError(f"{method} {url} -> {resp.status}: {message}", status=resp.status)
```

Two things worth noting: a successful-but-empty body (e.g. a `204`) returns `{}` rather than crashing on `json.loads("")`; and the raised `GitHubError` carries the HTTP **`status`** as an attribute, which is what makes `create_pull_request` able to specifically catch `422`.

### Secret hygiene

`_redact(text)` replaces the token with `***` wherever it appears, and every error/URL is run through it before being surfaced (`_safe_url`). The token can therefore never end up in a log line, an exception message, or a traceback.

### The public endpoints (built on top of all that)

- **`get_issue(repo, number)`** → `Issue`. Validates the slug, GETs `/repos/{repo}/issues/{n}`. The issues endpoint *also* returns PRs (a PR is an issue with a `pull_request` key), so it guards against that and raises a clear error rather than handing a PR to the loop as if it were an issue.
- **`get_default_branch(repo)`** → `str`. GETs `/repos/{repo}` and reads `default_branch` (so we cut feature branches off `main`/`master`/whatever the repo actually uses, instead of assuming).
- **`comment_on_issue(repo, number, body)`** → the comment's `html_url`. For posting status back ("I'm on it" / "opened #42").
- **`find_pull_request(repo, head, base)`** → `PullRequest | None`. Lists open PRs for a branch. GitHub wants the head qualified as `owner:branch`, so it adds the owner prefix if missing.
- **`create_pull_request(repo, head=, base=, title=, body=, draft=)`** → `PullRequest`. The **idempotency** centrepiece:

```python
try:
    data = self._request("POST", f"/repos/{repo}/pulls", body={...})
    return PullRequest.from_api(data, created=True)
except GitHubError as e:
    if e.status == 422:                      # "a pull request already exists"
        existing = self.find_pull_request(repo, head, base)
        if existing is not None:
            return existing                  # adopt it, created=False
    raise
```

If the agent is re-run on the same issue, the branch already has an open PR, and GitHub answers `422`. Instead of crashing, the client finds and returns the existing PR. Combined with the deterministic branch name (§5) and `--force-with-lease` push, this makes the **whole pipeline safely re-runnable**.

---

## 5. The git helpers in detail

These are module-level functions (not methods) that wrap the `git` CLI. They all go through one helper:

```python
def _git(workspace, *args, token=None, check=True, timeout=_GIT_TIMEOUT):
    cmd = ["git", "-C", str(workspace), *args]           # -C runs git *in* the repo
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        detail = _redact_token((proc.stderr or proc.stdout or "").strip(), token)
        raise GitHubError(f"git {args[0]} {args[1]} failed (exit {...}): {detail}")
    return proc
```

It handles the three ways git can go wrong: not installed (`FileNotFoundError` → friendly message), hangs (`TimeoutExpired`), or exits non-zero (`GitHubError` with the token scrubbed). `check=False` lets a caller inspect the return code instead of raising (used by `commit_all`).

### `clone` — and how the token stays off disk

```python
def clone(repo, dest, *, token=None, base=None, depth=1) -> Path:
    auth_url = _authenticated_url(repo, token)           # https://x-access-token:TOKEN@github.com/owner/repo.git
    args = ["clone"] + (["--depth", str(depth)] if depth > 0 else []) \
                     + (["--branch", base] if base else []) + [auth_url, str(dest)]
    proc = subprocess.run(["git", *args], ...)           # clone authenticates with the token
    ...
    _git(dest, "remote", "set-url", "origin", _clean_url(repo), token=token)  # scrub it back out
    configure_identity(dest)
    return dest
```

The problem: to clone a **private** repo you must authenticate, but a normal `git clone https://token@host/...` writes that token into `.git/config` under `remote.origin.url`, where it sits on disk for the life of the checkout. The fix: clone with the authenticated URL, then **immediately rewrite the remote back to the clean, token-free URL**. The token was used transiently for the fetch and is now gone from disk. (`push` later re-supplies it inline, §below.) `depth=1` shallow-clones by default for speed; pass `0` for full history. `configure_identity` is called so commits will work even in a bare CI container.

### `current_branch` — the unborn-branch gotcha

```python
def current_branch(workspace) -> str:
    proc = _git(workspace, "branch", "--show-current")
    return proc.stdout.strip() or "HEAD"
```

The obvious implementation, `git rev-parse --abbrev-ref HEAD`, **fails on a brand-new repo with no commits yet** ("ambiguous argument 'HEAD'"). `git branch --show-current` returns the branch name even on such an *unborn* branch, and an empty string when detached (we map that to `"HEAD"`). The self-test caught this exact failure (§7).

### `create_branch` — deterministic, idempotent

```python
def create_branch(workspace, branch, *, base=None) -> str:
    base = base or current_branch(workspace)
    if base and base != branch:
        _git(workspace, "checkout", "-B", branch, base)   # create/reset from base
    else:
        _git(workspace, "checkout", "-B", branch)
    return base                                            # caller uses this as the PR base
```

`-B` (capital) creates the branch or *resets* it if it already exists — so re-running is safe. It returns the base branch it cut from, which the caller threads into the PR as the merge target. Branch names come from:

```python
def branch_for_issue(number) -> str:
    return f"agent/issue-{number}"          # e.g. "agent/issue-42"
```

Deterministic on purpose: issue #42 always maps to `agent/issue-42`, so a re-run updates the same branch and therefore the same PR, instead of littering the repo with `agent-fix-1`, `agent-fix-2`, …

### `commit_all` — returns a boolean so we never open an empty PR

```python
def commit_all(workspace, message) -> bool:
    _git(workspace, "add", "-A")
    staged = _git(workspace, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:               # exit 0 = nothing staged
        return False
    _git(workspace, "commit", "-m", message)
    return True
```

`git diff --cached --quiet` exits `0` when the index is empty and `1` when there's something staged — so it's a clean "is there anything to commit?" probe. Returning `False` lets the orchestrator skip the push-and-PR entirely when the agent made no changes (a failed run shouldn't produce an empty PR).

### `push` — inline auth, force-with-lease

```python
def push(workspace, branch, *, repo, token=None, force=True):
    auth_url = _authenticated_url(repo, token)
    args = ["push"] + (["--force-with-lease"] if force else []) + [auth_url, f"{branch}:{branch}"]
    _git(workspace, *args, token=token)       # token redacted on failure
```

The authenticated URL is passed **inline to this one command** rather than stored as the remote — same secret-hygiene principle as `clone`. The default `--force-with-lease` is the safe kind of force: a re-run can update the issue's branch, but git refuses if the remote has commits we haven't seen (so we never clobber someone else's work on that branch). The token is passed to `_git` only so it can be scrubbed from any error message.

### `configure_identity`

Sets `user.name` / `user.email` locally (from `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL`, defaulting to the agent's own identity). Without this, `git commit` fails in a clean environment that has no global identity configured — exactly the situation inside a fresh container or CI job.

---

## 6. The glue

### `parse_webhook_event` — and the infinite-loop guards

A webhook receiver (coming in `app/main.py`) will hand raw payloads to this function. It returns an `Issue` to act on, or `None` to ignore:

```python
def parse_webhook_event(payload, *, event_type="issues", actions=_ACTIONABLE_ISSUE_ACTIONS):
    if not isinstance(payload, dict): return None
    if event_type != "issues": return None
    if payload.get("action") not in actions: return None     # only opened/reopened/labeled
    issue = payload.get("issue")
    if not isinstance(issue, dict) or "pull_request" in issue: return None   # it's a PR, not an issue
    repo = (payload.get("repository") or {}).get("full_name", "").strip()
    if not repo or not _REPO_SLUG.match(repo): return None
    if (issue.get("user") or {}).get("type") == "Bot": return None           # don't react to bots
    return Issue.from_api(issue, repo)
```

It returns `None` rather than raising because a webhook endpoint receives a *firehose* of events and should quietly ignore the irrelevant majority — raising would turn every "comment created" event into an error. The guards encode hard-won automation lessons:

- **Only `opened` / `reopened` / `labeled`** trigger a run. `edited` is deliberately excluded — otherwise fixing a typo in the issue body would re-run the whole agent.
- **`"pull_request" in issue`** detects that the payload is actually a PR (GitHub's issue events include PRs) and skips it. *(Note: this checks key **presence**, not truthiness — an earlier truthiness check was a bug, because the field can be an empty-ish object. See §7.)*
- **Bot authors are skipped.** This is the classic infinite-loop trap: if the agent (or any bot) opens an issue or comments, and that fires a webhook that starts another run, you get runaway automation. Filtering `user.type == "Bot"` cuts the loop.

### `submit_changes` — the output pipeline in one call

```python
def submit_changes(workspace, *, repo, branch, base, title, body="",
                   token=None, client=None, draft=False) -> PullRequest | None:
    if not commit_all(workspace, f"{title}\n\n{body}".strip()):
        return None                                          # nothing changed → no PR
    push(workspace, branch, repo=repo, token=token)
    client = client or GitHubClient(token=token)
    return client.create_pull_request(repo, head=branch, base=base,
                                       title=title, body=body, draft=draft)
```

This is the single idempotent "turn the agent's edits into a PR" call the orchestrator will use: commit → push → open-or-adopt PR, returning `None` when there was nothing to propose.

---

## 7. The offline self-test (`python -m agent.github`)

Running the module with no arguments runs `_selftest()` — ~32 assertions, **no token and no network required**, exit code `0` on success. It is the proof that the robustness described above actually holds. It covers:

- **Repo-slug validation** — accepts `owner/repo`, rejects `noslash`, `a/b/c`, `/x`, `x/`, …
- **Webhook parsing** — parses an `opened` issue; ignores `edited`, PRs, and bot authors; extracts labels; confirms `to_task()` contains the title.
- **The REST client against a fake transport** — this is the centrepiece. A stateful fake transport simulates a primary-rate-limit `403` **then** a `200`, and the test asserts the client *retried*, *slept once*, and *returned the issue*. It then drives `create_pull_request` through both the happy path and a `422` → "adopt existing PR" path, and a `401` whose error message is checked to confirm **the token does not leak**.
- **The git helpers against a real temp repo** — `git init`, identity, an initial commit, `create_branch`, dirty/clean detection, and `commit_all` returning `True` then `False`.

Because the fake transport returns canned `_RawResponse`s and `sleep`/`now` are stubbed, all of this runs in milliseconds offline.

### Three real bugs the self-test caught (worth remembering)

Writing the test *before* trusting the code paid off immediately:

1. **Unborn-branch crash.** `current_branch` originally used `git rev-parse --abbrev-ref HEAD`, which dies on a repo with no commits. Fixed by switching to `git branch --show-current`.
2. **PR detection via truthiness.** The webhook guard first checked `if issue.get("pull_request")` — but that field can be a falsy-ish empty object, so PRs slipped through. Fixed to `if "pull_request" in issue` (presence, not truthiness). The same fix was applied in `get_issue`.
3. **Zero-second backoff.** With a rate-limit `reset` equal to "now", `_retry_delay` correctly computed `0.0`, and `_wait` (correctly) doesn't sleep for `0` — but the test *asserted* a sleep happened. The test was tightened to use a future reset, confirming the wait-until-reset path actually waits.

---

## 8. Configuration

| Env var | Purpose | Default |
|---|---|---|
| `GITHUB_TOKEN` | PAT or GitHub App installation token. Required for any real API call or push. | — |
| `GITHUB_API_URL` | Set for GitHub Enterprise. The git host is derived from it automatically. | `https://api.github.com` |
| `GIT_AUTHOR_NAME` | Committer name for the agent's commits. | `auto-swe-agent` |
| `GIT_AUTHOR_EMAIL` | Committer email. | `auto-swe-agent@users.noreply.github.com` |

All are loaded from `.env` at import time (via `python-dotenv` if installed), mirroring how `loop.py` bootstraps config.

---

## 9. How to run it

```bash
# Offline self-test — no token, no network. Proves retries, rate-limit backoff,
# token redaction, webhook parsing, and the git helpers all work.
python -m agent.github

# Fetch a real issue and print it as the loop's task (needs GITHUB_TOKEN):
python -m agent.github --issue owner/repo#42
```

And the end-to-end shape the orchestrator will assemble:

```python
from agent.github import GitHubClient, clone, create_branch, branch_for_issue, submit_changes
from agent.loop import ReActAgent

client = GitHubClient()                                   # reads GITHUB_TOKEN
issue  = client.get_issue("owner/repo", 42)

ws     = clone(issue.repo, "/tmp/work-42",
               base=client.get_default_branch(issue.repo))
branch = branch_for_issue(issue.number)                   # "agent/issue-42"
base   = create_branch(ws, branch)

ReActAgent(workspace=ws, auto_index=True).run(issue.to_task())   # the brain edits files

pr = submit_changes(ws, repo=issue.repo, branch=branch, base=base,
                    title=f"Fix #{issue.number}: {issue.title}",
                    body=f"Closes #{issue.number}.", client=client)
print(pr.html_url if pr else "no changes — nothing to propose")
```

---

## 10. Next steps

The pipeline now has a working brain (`loop.py`) and working hands (`github.py`), but they aren't yet wired together or isolated. In priority order:

1. **`workers/tasks.py` — the orchestrator (highest leverage).** This is the function that runs the end-to-end shape from §9 as a background job: fetch issue → clone → index → run the loop → `submit_changes`. It is what makes the agent *actually autonomous*. It should: post a "working on it" comment, enforce a wall-clock/`Budget` limit per run, handle the loop finishing without changes (no PR), and post the PR link (or the failure reason) back on the issue. It depends only on the two modules that now exist.

2. **`agent/sandbox.py` — Docker isolation (the missing safety layer).** Today the loop's `run_tests` tool executes the *repository's own test suite* directly on the host — fine for trusted repos, unacceptable for arbitrary ones. `sandbox.py` should run each job in a fresh container with **no network, a read-only host filesystem, and CPU/time limits**, exposing the same tool surface (`read_file`/`edit_file`/`run_tests`) so the loop can be pointed at it without changes (loop.py was written with this swap in mind). Build this before running the agent on any untrusted issue.

3. **`app/main.py` — the FastAPI webhook receiver.** Verifies the GitHub webhook HMAC signature, calls `parse_webhook_event`, and enqueues a Celery job (the `workers/tasks.py` function) for each actionable issue. This is the live "issue appears → run starts" front door.

4. **Observability & deploy (`monitoring/`, `k8s/`, `helm/`).** Prometheus metrics off the loop's existing structured trace, Grafana dashboards, and Kubernetes/Helm manifests.

A natural sequence: **`workers/tasks.py` next** (it ties together what already works and gives an end-to-end demo against a trusted repo), then **`sandbox.py`** (so it's safe on untrusted repos), then **`app/main.py`** (so it's live from webhooks).

> Housekeeping reminder, unrelated to this module: the repo's `git origin` still points at the wrong project (`MOMS-Claim-Reconciliation-Pipeline`). Point it at a dedicated repo before committing further.
