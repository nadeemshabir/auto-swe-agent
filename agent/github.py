"""
agent/github.py
GitHub integration — the agent's connection to the outside world.

The ReAct loop (agent/loop.py) is the brain; this module is the hands that
reach into GitHub. It has two independent halves plus a little glue:

  1. REST client (`GitHubClient`) — fetch an issue, open / find a pull request,
     comment back on an issue. Pure standard library (urllib), so it adds no
     dependency. It is deliberately defensive about the things that actually
     break GitHub automations in production: rate limits (primary *and*
     secondary) and transient 5xx / network blips, both handled with bounded
     exponential backoff that honours `Retry-After` / `X-RateLimit-Reset`.
     The HTTP transport is injectable, so the whole client is testable offline.

  2. Git helpers — clone a repo, branch, commit the agent's edits, push. Thin,
     defensive wrappers over the `git` CLI. The auth token is never written to
     the repo's stored config (we clone/authenticate, then scrub the remote)
     and is redacted from any surfaced error or log line.

  glue: `parse_webhook_event` turns an `issues` webhook payload into an `Issue`,
  and `Issue.to_task()` turns an issue into the task string the loop consumes.
  `submit_changes()` ties git + REST together: commit -> push -> open/refresh PR.

Auth:  set GITHUB_TOKEN (a PAT or a GitHub App installation token).
       Optional GITHUB_API_URL for GitHub Enterprise (default api.github.com).

End-to-end shape (the orchestrator in workers/tasks.py will wire this up):

    issue   = client.get_issue("owner/repo", 42)
    ws      = clone(issue.repo, "/tmp/work", token=tok)
    base    = create_branch(ws, branch_for_issue(42))   # off the default branch
    ReActAgent(workspace=ws).run(issue.to_task())        # the brain edits files
    pr      = submit_changes(ws, repo=issue.repo, branch=branch_for_issue(42),
                             base=base, title=..., body=..., token=tok,
                             client=client)

Offline self-test:  python -m agent.github     (no token / no network needed)
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Load .env so GITHUB_TOKEN / GITHUB_API_URL are visible to os.getenv(),
# consistent with how agent/loop.py bootstraps configuration.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; the user exports vars manually

log = logging.getLogger("agent.github")

__all__ = [
    "GitHubError",
    "Issue",
    "PullRequest",
    "GitHubClient",
    "parse_webhook_event",
    "branch_for_issue",
    "clone",
    "create_branch",
    "configure_identity",
    "has_uncommitted_changes",
    "commit_all",
    "current_branch",
    "push",
    "submit_changes",
]

# ── configuration / constants ────────────────────────────────────────────────

DEFAULT_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "auto-swe-agent"

# A GitHub "owner/repo" slug. Owners and repo names allow alnum, '-', '_', '.'.
_REPO_SLUG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/"
                        r"[A-Za-z0-9_.-]+$")

# Statuses worth retrying. 403 is conditional (only when it is a rate limit),
# so it is handled separately rather than listed here.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_GIT_TIMEOUT = 600          # seconds for any single git invocation
_HTTP_TIMEOUT = 30          # seconds per HTTP attempt
_MAX_RETRIES = 4            # extra attempts after the first (so 5 tries total)
_BACKOFF_BASE = 1.0         # seconds; delay ~= base * 2**attempt + jitter
_BACKOFF_CAP = 30.0         # never sleep longer than this between attempts


class GitHubError(Exception):
    """Any failure talking to GitHub — REST (network/HTTP) or git CLI.

    `status` is the HTTP status code when the failure came from a REST call
    (None for git/network errors), so callers can branch on e.g. 404 vs 422."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


# ═════════════════════════════════════════════════════════════════════════════
# Value objects
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Issue:
    """A GitHub issue, normalized from either the REST API or a webhook payload.

    `repo` is the "owner/name" slug the issue lives in — webhooks carry it in a
    different place than the issue object itself, so we always thread it in."""
    repo: str
    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    author: str = ""
    html_url: str = ""

    @classmethod
    def from_api(cls, data: dict, repo: str) -> "Issue":
        if not isinstance(data, dict):
            raise GitHubError("issue payload is not an object")
        labels = [
            (lbl.get("name", "") if isinstance(lbl, dict) else str(lbl))
            for lbl in (data.get("labels") or [])
        ]
        return cls(
            repo=repo,
            number=int(data.get("number", 0)),
            title=(data.get("title") or "").strip(),
            body=(data.get("body") or "").strip(),
            labels=[name for name in labels if name],
            author=((data.get("user") or {}).get("login") or ""),
            html_url=data.get("html_url") or "",
        )

    def to_task(self) -> str:
        """Render the issue as the task string fed to the ReAct loop.

        We give the model the title, body, and labels, plus an explicit
        reference so any PR/commit text it writes can cite the issue. We do NOT
        instruct it on *how* to fix — loop.py's system prompt owns the method."""
        lines = [
            f"Resolve GitHub issue #{self.number} in repository {self.repo}.",
            "",
            f"Title: {self.title or '(no title)'}",
        ]
        if self.labels:
            lines.append(f"Labels: {', '.join(self.labels)}")
        lines += ["", "Issue description:", self.body or "(no description provided)"]
        return "\n".join(lines)


@dataclass
class PullRequest:
    """A pull request the agent opened or found."""
    number: int
    html_url: str
    head: str          # the branch the changes live on
    base: str          # the branch the PR merges into
    created: bool      # True if we created it now, False if it already existed

    @classmethod
    def from_api(cls, data: dict, *, created: bool) -> "PullRequest":
        return cls(
            number=int(data.get("number", 0)),
            html_url=data.get("html_url") or "",
            head=((data.get("head") or {}).get("ref") or ""),
            base=((data.get("base") or {}).get("ref") or ""),
            created=created,
        )


# ═════════════════════════════════════════════════════════════════════════════
# HTTP transport (injectable so the client is testable offline)
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class _RawResponse:
    """A transport-level HTTP response. Header keys are lower-cased so callers
    never have to worry about case (HTTP header names are case-insensitive)."""
    status: int
    headers: dict[str, str]
    body: bytes


class _TransportError(Exception):
    """A connection-level failure (DNS, refused, reset, timeout) — distinct from
    an HTTP error status, which the transport returns as a normal _RawResponse."""


# A transport is: (method, url, headers, data_or_None, timeout) -> _RawResponse.
Transport = Callable[[str, str, dict, "bytes | None", float], _RawResponse]


def _urllib_transport(method: str, url: str, headers: dict,
                      data: bytes | None, timeout: float) -> _RawResponse:
    """Default transport over urllib. Returns a _RawResponse for *any* HTTP
    status (including 4xx/5xx) and raises _TransportError only for failures that
    never produced an HTTP response, so the retry logic can treat them apart."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _RawResponse(
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read(),
            )
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:  # pragma: no cover - body already consumed
            pass
        return _RawResponse(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
    except urllib.error.URLError as e:
        raise _TransportError(str(getattr(e, "reason", e)))
    except (TimeoutError, OSError) as e:  # pragma: no cover - socket-level
        raise _TransportError(str(e))


# ═════════════════════════════════════════════════════════════════════════════
# REST client
# ═════════════════════════════════════════════════════════════════════════════

class GitHubClient:
    """A small, robust GitHub REST client.

    Only the handful of endpoints the agent actually needs are exposed. Every
    call goes through `_request`, which owns auth headers, retries, rate-limit
    backoff, and error normalization. `transport`, `sleep`, and `now` are
    injectable purely so the whole thing can be exercised offline in _selftest.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        api_url: str | None = None,
        transport: Transport | None = None,
        max_retries: int = _MAX_RETRIES,
        timeout: float = _HTTP_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ):
        self.token = token or os.getenv("GITHUB_TOKEN") or ""
        self.api_url = (api_url or os.getenv("GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")
        self._transport = transport or _urllib_transport
        self.max_retries = max(0, int(max_retries))
        self.timeout = float(timeout)
        self._sleep = sleep
        self._now = now

    # ── public endpoints ─────────────────────────────────────────────────────

    def get_issue(self, repo: str, number: int) -> Issue:
        """Fetch a single issue. Raises GitHubError(status=404) if it is gone."""
        _validate_repo(repo)
        data = self._request("GET", f"/repos/{repo}/issues/{int(number)}")
        # The issues endpoint also returns PRs; a PR has a "pull_request" key.
        if isinstance(data, dict) and "pull_request" in data:
            raise GitHubError(f"{repo}#{number} is a pull request, not an issue")
        return Issue.from_api(data, repo)

    def get_default_branch(self, repo: str) -> str:
        """Return the repo's default branch (e.g. 'main')."""
        _validate_repo(repo)
        data = self._request("GET", f"/repos/{repo}")
        branch = (data or {}).get("default_branch")
        if not branch:
            raise GitHubError(f"could not determine default branch for {repo}")
        return branch

    def comment_on_issue(self, repo: str, number: int, body: str) -> str:
        """Post a comment on an issue (or PR). Returns the comment's html_url."""
        _validate_repo(repo)
        if not body or not body.strip():
            raise GitHubError("comment body is empty")
        data = self._request(
            "POST", f"/repos/{repo}/issues/{int(number)}/comments",
            body={"body": body},
        )
        return (data or {}).get("html_url", "")

    def find_pull_request(self, repo: str, head: str, base: str) -> PullRequest | None:
        """Return the open PR for `head` -> `base`, or None. `head` is a branch
        name; GitHub wants it namespaced as 'owner:branch' for cross-fork safety."""
        _validate_repo(repo)
        owner = repo.split("/", 1)[0]
        qualified = head if ":" in head else f"{owner}:{head}"
        data = self._request(
            "GET", f"/repos/{repo}/pulls",
            params={"head": qualified, "base": base, "state": "open"},
        )
        if isinstance(data, list) and data:
            return PullRequest.from_api(data[0], created=False)
        return None

    def create_pull_request(
        self, repo: str, *, head: str, base: str, title: str,
        body: str = "", draft: bool = False,
    ) -> PullRequest:
        """Open a PR. Idempotent: if one already exists for `head` -> `base`
        (GitHub answers 422), the existing PR is fetched and returned instead."""
        _validate_repo(repo)
        if not title or not title.strip():
            raise GitHubError("pull request title is empty")
        try:
            data = self._request(
                "POST", f"/repos/{repo}/pulls",
                body={"title": title, "head": head, "base": base,
                      "body": body, "draft": bool(draft)},
            )
            return PullRequest.from_api(data, created=True)
        except GitHubError as e:
            if e.status == 422:
                # 422 most commonly means "a pull request already exists" — adopt
                # it so a re-run on the same issue is a no-op, not a crash.
                existing = self.find_pull_request(repo, head, base)
                if existing is not None:
                    log.info("PR for %s already exists: #%s", head, existing.number)
                    return existing
            raise

    # ── request engine ───────────────────────────────────────────────────────

    def _url(self, path: str, params: dict | None = None) -> str:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _headers(self, has_body: bool) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(self, method: str, path: str, *, body=None, params=None):
        """Perform one REST call with retries. Returns parsed JSON (dict/list),
        or {} for an empty 2xx body. Raises GitHubError on a non-retryable
        failure or once retries are exhausted."""
        url = self._url(path, params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = self._headers(has_body=data is not None)

        last_desc = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._transport(method, url, headers, data, self.timeout)
            except _TransportError as e:
                last_desc = f"network error: {self._redact(str(e))}"
                if attempt < self.max_retries:
                    self._wait(self._backoff(attempt), attempt, last_desc)
                    continue
                raise GitHubError(f"{method} {self._safe_url(url)} failed: {last_desc}")

            if self._should_retry(resp) and attempt < self.max_retries:
                delay = self._retry_delay(resp, attempt)
                last_desc = f"HTTP {resp.status}"
                self._wait(delay, attempt, f"{last_desc} (will retry)")
                continue

            return self._parse(resp, method, url)

        # Defensive: the loop always returns or raises above.
        raise GitHubError(f"{method} {self._safe_url(url)} failed: {last_desc}")  # pragma: no cover

    def _should_retry(self, resp: _RawResponse) -> bool:
        if resp.status in _RETRYABLE_STATUS:
            return True
        # A 403 is retryable only when it is a rate limit (primary or secondary),
        # not when it is a genuine permission error.
        if resp.status == 403:
            return self._is_rate_limited(resp)
        return False

    @staticmethod
    def _is_rate_limited(resp: _RawResponse) -> bool:
        if resp.headers.get("retry-after"):
            return True                                  # secondary rate limit
        if resp.headers.get("x-ratelimit-remaining") == "0":
            return True                                  # primary rate limit
        return False

    def _retry_delay(self, resp: _RawResponse, attempt: int) -> float:
        # Honour an explicit Retry-After (seconds) when present.
        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), _BACKOFF_CAP)
        # Otherwise, if we are out of primary quota, wait until the reset epoch.
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset = resp.headers.get("x-ratelimit-reset")
            if reset and reset.isdigit():
                wait = float(reset) - self._now()
                return max(0.0, min(wait, _BACKOFF_CAP))
        return self._backoff(attempt)

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with full jitter, capped."""
        base = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
        return base + random.uniform(0.0, base * 0.25)

    def _wait(self, delay: float, attempt: int, why: str) -> None:
        log.warning("github: %s - retrying in %.1fs (attempt %d/%d)",
                    why, delay, attempt + 1, self.max_retries)
        if delay > 0:
            self._sleep(delay)

    def _parse(self, resp: _RawResponse, method: str, url: str):
        text = resp.body.decode("utf-8", "replace") if resp.body else ""
        if 200 <= resp.status < 300:
            if not text.strip():
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise GitHubError(f"malformed JSON from {self._safe_url(url)}: {e}")
        # Non-2xx: build the most helpful message GitHub gave us.
        message = ""
        try:
            payload = json.loads(text) if text.strip() else {}
            message = payload.get("message", "")
            errors = payload.get("errors")
            if errors:
                message += " — " + json.dumps(errors)
        except json.JSONDecodeError:
            message = text[:500]
        message = self._redact(message) or f"HTTP {resp.status}"
        raise GitHubError(
            f"{method} {self._safe_url(url)} -> {resp.status}: {message}",
            status=resp.status,
        )

    # ── secret hygiene ───────────────────────────────────────────────────────

    def _redact(self, text: str) -> str:
        if self.token and self.token in text:
            return text.replace(self.token, "***")
        return text

    def _safe_url(self, url: str) -> str:
        return self._redact(url)


# ═════════════════════════════════════════════════════════════════════════════
# Webhook parsing
# ═════════════════════════════════════════════════════════════════════════════

# Issue actions that should kick off an agent run. "opened" is the common case;
# "reopened"/"labeled" let a maintainer re-trigger. "edited" is intentionally
# excluded — body edits would re-run the agent on every typo fix.
_ACTIONABLE_ISSUE_ACTIONS = frozenset({"opened", "reopened", "labeled"})


def parse_webhook_event(
    payload: dict, *, event_type: str = "issues",
    actions: frozenset[str] = _ACTIONABLE_ISSUE_ACTIONS,
) -> Issue | None:
    """Turn an `issues` webhook payload into an Issue, or None if the event is
    not one we should act on (wrong event type, non-actionable action, a PR
    rather than an issue, or a bot author to avoid feedback loops).

    Returning None rather than raising is deliberate: a webhook receiver gets a
    firehose of events and should quietly ignore the irrelevant majority."""
    if not isinstance(payload, dict):
        return None
    if event_type != "issues":
        # Only issue events start a run today; PR/push events are out of scope.
        return None
    if payload.get("action") not in actions:
        return None

    issue = payload.get("issue")
    if not isinstance(issue, dict) or "pull_request" in issue:
        return None  # missing, or actually a pull request

    repo = ((payload.get("repository") or {}).get("full_name") or "").strip()
    if not repo or not _REPO_SLUG.match(repo):
        return None

    # Skip bot-authored issues so the agent never reacts to its own comments
    # or another automation, which is a classic infinite-loop trap.
    user = issue.get("user") or {}
    if user.get("type") == "Bot":
        return None

    return Issue.from_api(issue, repo)


# ═════════════════════════════════════════════════════════════════════════════
# Git helpers
# ═════════════════════════════════════════════════════════════════════════════

def branch_for_issue(number: int) -> str:
    """Deterministic branch name so re-running on the same issue updates the
    same branch (and thus the same PR) instead of piling up new ones."""
    return f"agent/issue-{int(number)}"


def _authenticated_url(repo: str, token: str | None) -> str:
    """Build the clone/push URL for github.com, embedding the token if given.
    The token is used transiently (clone, then scrubbed; push, passed inline) —
    never persisted into the repo's remote config."""
    host = "github.com"
    api = os.getenv("GITHUB_API_URL", "")
    if api and "api.github.com" not in api:
        # Enterprise: api host is e.g. https://ghe.acme.com/api/v3
        host = urllib.parse.urlparse(api).netloc or host
    if token:
        return f"https://x-access-token:{token}@{host}/{repo}.git"
    return f"https://{host}/{repo}.git"


def _clean_url(repo: str) -> str:
    return _authenticated_url(repo, None)


def _redact_token(text: str, token: str | None) -> str:
    if token and token in text:
        text = text.replace(token, "***")
    # Also catch the token even if only the userinfo form survived.
    return re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", text)


# Host-side git must never execute code the repo (or the sandbox that ran the
# repo's tests) controls. A malicious workspace could carry .git/hooks/* or a
# core.fsmonitor command in .git/config; either would run ON THE HOST during a
# plain `git add/commit/push`. These -c overrides neutralize both vectors for
# every git invocation this module makes (the sandbox additionally masks .git
# from the container — see agent/sandbox.py — this is defense in depth).
_GIT_HARDENING = [
    "-c", "core.hooksPath=/dev/null",   # never run repo-provided hooks
    "-c", "core.fsmonitor=",            # never run a repo-configured fsmonitor
]


def _git(workspace: str | Path, *args: str, token: str | None = None,
         check: bool = True, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a git command inside `workspace`. On failure (when check=True) raises
    GitHubError with the token scrubbed from the message. Returns the completed
    process so callers can inspect returncode/stdout when check=False."""
    cmd = ["git", *_GIT_HARDENING, "-C", str(workspace), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise GitHubError("git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise GitHubError(f"git {args[0] if args else ''} timed out after {timeout}s")
    if check and proc.returncode != 0:
        detail = _redact_token((proc.stderr or proc.stdout or "").strip(), token)
        op = " ".join(str(a) for a in args[:2])
        raise GitHubError(f"git {op} failed (exit {proc.returncode}): {detail}")
    return proc


def configure_identity(workspace: str | Path, *, name: str | None = None,
                       email: str | None = None) -> None:
    """Set the committer identity locally so commits succeed in clean CI
    environments that have no global git config. Defaults are agent-specific."""
    name = name or os.getenv("GIT_AUTHOR_NAME", "auto-swe-agent")
    email = email or os.getenv("GIT_AUTHOR_EMAIL", "auto-swe-agent@users.noreply.github.com")
    _git(workspace, "config", "user.name", name)
    _git(workspace, "config", "user.email", email)


def clone(repo: str, dest: str | Path, *, token: str | None = None,
          base: str | None = None, depth: int = 1) -> Path:
    """Clone `repo` into `dest` and return the path. Uses the token to authent-
    icate (so private repos work), then scrubs the remote URL back to a clean,
    token-free form so the secret never lands in `.git/config`. Sets a local
    committer identity. `base` checks out a specific branch; `depth` shallow-
    clones (pass 0 for a full clone)."""
    _validate_repo(repo)
    token = token or os.getenv("GITHUB_TOKEN") or None
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise GitHubError(f"clone destination is not empty: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    auth_url = _authenticated_url(repo, token)
    args = ["clone"]
    if depth and depth > 0:
        args += ["--depth", str(depth)]
    if base:
        args += ["--branch", base]
    args += [auth_url, str(dest)]
    # Run via a workspace-less git (no -C; the repo does not exist yet).
    proc = subprocess.run(["git", *_GIT_HARDENING, *args],
                          capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    if proc.returncode != 0:
        detail = _redact_token((proc.stderr or proc.stdout or "").strip(), token)
        raise GitHubError(f"git clone failed (exit {proc.returncode}): {detail}")

    # Scrub credentials from the persisted remote, then set the agent identity.
    _git(dest, "remote", "set-url", "origin", _clean_url(repo), token=token)
    configure_identity(dest)
    return dest


def current_branch(workspace: str | Path) -> str:
    """Return the current branch name, or 'HEAD' if detached. Uses
    `branch --show-current`, which (unlike `rev-parse HEAD`) also works on an
    unborn branch — a freshly `git init`'d repo with no commits yet."""
    proc = _git(workspace, "branch", "--show-current")
    return proc.stdout.strip() or "HEAD"


def create_branch(workspace: str | Path, branch: str, *,
                  base: str | None = None) -> str:
    """Create (or reset) `branch` and check it out. Returns the base branch the
    new branch was cut from — the caller passes this as the PR's base. If `base`
    is None, the currently checked-out branch is used as the base."""
    if not branch or not branch.strip():
        raise GitHubError("branch name is required")
    base = base or current_branch(workspace)
    # -B is idempotent: create the branch, or reset it to base if it exists.
    if base and base != branch:
        _git(workspace, "checkout", "-B", branch, base)
    else:
        _git(workspace, "checkout", "-B", branch)
    return base


def has_uncommitted_changes(workspace: str | Path) -> bool:
    """True if the working tree has staged or unstaged changes."""
    proc = _git(workspace, "status", "--porcelain")
    return bool(proc.stdout.strip())


def commit_all(workspace: str | Path, message: str) -> bool:
    """Stage every change and commit. Returns True if a commit was made, False
    if there was nothing to commit (so the caller can skip opening an empty PR)."""
    if not message or not message.strip():
        raise GitHubError("commit message is required")
    _git(workspace, "add", "-A")
    # `diff --cached --quiet` exits 0 when the index is empty -> nothing staged.
    staged = _git(workspace, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return False
    # --no-verify is belt-and-braces on top of _GIT_HARDENING's hooksPath
    # override: agent commits must never trigger repo-controlled hook code.
    _git(workspace, "commit", "--no-verify", "-m", message)
    return True


def push(workspace: str | Path, branch: str, *, repo: str,
         token: str | None = None, force: bool = True) -> None:
    """Push `branch` to origin. The authenticated URL is passed inline to a
    single `git push` rather than stored, so the token never persists. Defaults
    to --force-with-lease: a re-run on the same issue should update the branch,
    but never clobber commits it has not seen."""
    _validate_repo(repo)
    token = token or os.getenv("GITHUB_TOKEN") or None
    auth_url = _authenticated_url(repo, token)
    args = ["push"]
    if force:
        args.append("--force-with-lease")
    args += [auth_url, f"{branch}:{branch}"]
    _git(workspace, *args, token=token)


# ═════════════════════════════════════════════════════════════════════════════
# High-level orchestration
# ═════════════════════════════════════════════════════════════════════════════

def submit_changes(
    workspace: str | Path, *, repo: str, branch: str, base: str,
    title: str, body: str = "", token: str | None = None,
    client: GitHubClient | None = None, draft: bool = False,
) -> PullRequest | None:
    """Commit the agent's edits, push the branch, and open (or refresh) a PR.

    Returns the PullRequest, or None if the working tree had no changes to
    commit (nothing to propose). Wraps the three steps so the orchestrator in
    workers/tasks.py has a single, idempotent call for "turn edits into a PR"."""
    token = token or os.getenv("GITHUB_TOKEN") or None
    if not commit_all(workspace, f"{title}\n\n{body}".strip()):
        log.info("no changes to commit for %s; skipping PR", branch)
        return None
    push(workspace, branch, repo=repo, token=token)
    client = client or GitHubClient(token=token)
    return client.create_pull_request(
        repo, head=branch, base=base, title=title, body=body, draft=draft,
    )


# ── validation helper ─────────────────────────────────────────────────────────

def _validate_repo(repo: str) -> None:
    if not repo or not _REPO_SLUG.match(repo):
        raise GitHubError(f"invalid repository slug {repo!r}; expected 'owner/name'")


# ═════════════════════════════════════════════════════════════════════════════
# Offline self-test  (no token, no network — exercises the robustness paths)
# ═════════════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    import tempfile

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        status = "ok  " if cond else "FAIL"
        print(f"  [{status}] {name}" + (f" — {extra}" if extra and not cond else ""))
        if not cond:
            failures.append(name)

    print("repo-slug validation")
    for good in ("owner/repo", "a-b.c/d_e.f"):
        try:
            _validate_repo(good); check(f"accepts {good!r}", True)
        except GitHubError:
            check(f"accepts {good!r}", False)
    for bad in ("", "noslash", "a/b/c", "/x", "x/"):
        try:
            _validate_repo(bad); check(f"rejects {bad!r}", False)
        except GitHubError:
            check(f"rejects {bad!r}", True)

    print("webhook parsing")
    payload = {
        "action": "opened",
        "repository": {"full_name": "octo/widget"},
        "issue": {
            "number": 7, "title": "Crash on empty input",
            "body": "Steps to reproduce...", "html_url": "https://github.com/octo/widget/issues/7",
            "user": {"login": "alice", "type": "User"},
            "labels": [{"name": "bug"}, {"name": "p1"}],
        },
    }
    issue = parse_webhook_event(payload)
    check("parses opened issue", issue is not None and issue.number == 7)
    check("captures repo", issue is not None and issue.repo == "octo/widget")
    check("captures labels", issue is not None and issue.labels == ["bug", "p1"])
    check("to_task mentions title", issue is not None and "Crash on empty input" in issue.to_task())
    check("ignores non-actionable action",
          parse_webhook_event({**payload, "action": "edited"}) is None)
    check("ignores PRs",
          parse_webhook_event({**payload, "issue": {**payload["issue"], "pull_request": {}}}) is None)
    check("ignores bot authors",
          parse_webhook_event({**payload, "issue": {**payload["issue"], "user": {"type": "Bot"}}}) is None)
    check("branch_for_issue is deterministic", branch_for_issue(7) == "agent/issue-7")

    print("REST client — retry + redaction (fake transport, no network)")
    SECRET = "ghp_SECRETTOKEN12345"
    calls = {"n": 0}

    def fake_transport(method, url, headers, data, timeout):
        calls["n"] += 1
        # First call: simulate a primary rate-limit 403, then succeed.
        if calls["n"] == 1:
            return _RawResponse(403, {"x-ratelimit-remaining": "0",
                                      "x-ratelimit-reset": "5"}, b'{"message":"rate limited"}')
        if method == "GET" and url.endswith("/issues/7"):
            body = json.dumps({"number": 7, "title": "T", "body": "B",
                               "user": {"login": "alice"}, "labels": []}).encode()
            return _RawResponse(200, {}, body)
        if method == "POST" and url.endswith("/pulls"):
            assert SECRET not in url
            body = json.dumps({"number": 42, "html_url": "https://github.com/octo/widget/pull/42",
                               "head": {"ref": "agent/issue-7"}, "base": {"ref": "main"}}).encode()
            return _RawResponse(201, {}, body)
        return _RawResponse(404, {}, b'{"message":"not found"}')

    slept: list[float] = []
    client = GitHubClient(token=SECRET, transport=fake_transport,
                          sleep=lambda s: slept.append(s), now=lambda: 0.0)
    got = client.get_issue("octo/widget", 7)
    check("retried past rate limit", calls["n"] == 2)
    check("backed off once", len(slept) == 1)
    check("returned issue after retry", got.number == 7)
    pr = client.create_pull_request("octo/widget", head="agent/issue-7", base="main", title="Fix")
    check("opened PR", pr.number == 42 and pr.created)

    # 422 -> adopt existing PR
    calls["n"] = 99

    def conflict_transport(method, url, headers, data, timeout):
        if method == "POST":
            return _RawResponse(422, {}, b'{"message":"A pull request already exists"}')
        return _RawResponse(200, {}, json.dumps([
            {"number": 5, "html_url": "u", "head": {"ref": "agent/issue-7"}, "base": {"ref": "main"}}
        ]).encode())

    c2 = GitHubClient(token=SECRET, transport=conflict_transport, sleep=lambda s: None)
    pr2 = c2.create_pull_request("octo/widget", head="agent/issue-7", base="main", title="Fix")
    check("adopts existing PR on 422", pr2.number == 5 and not pr2.created)

    # Error message must never leak the token.
    def forbidden_transport(method, url, headers, data, timeout):
        return _RawResponse(401, {}, f'{{"message":"bad creds {SECRET}"}}'.encode())

    c3 = GitHubClient(token=SECRET, transport=forbidden_transport, sleep=lambda s: None)
    try:
        c3.get_issue("octo/widget", 7)
        check("raises on 401", False)
    except GitHubError as e:
        check("raises on 401", e.status == 401)
        check("redacts token in error", SECRET not in str(e), str(e))

    print("git helpers (real git, temp repo, no remote)")
    if _git_available():
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "repo"
            ws.mkdir()
            _git(ws, "init", "-q")
            configure_identity(ws)
            # Make an initial commit so HEAD exists (mirrors a real clone).
            (ws / "README.md").write_text("hello\n", encoding="utf-8")
            check("initial commit_all commits", commit_all(ws, "init") is True)
            base = create_branch(ws, branch_for_issue(7))
            check("created branch", current_branch(ws) == "agent/issue-7")
            check("branch cut from base", base in ("master", "main"))
            check("clean tree after branch", not has_uncommitted_changes(ws))
            (ws / "fix.py").write_text("x = 1\n", encoding="utf-8")
            check("dirty tree detected", has_uncommitted_changes(ws))
            check("commit_all commits change", commit_all(ws, "fix") is True)
            check("commit_all no-ops when clean", commit_all(ws, "again") is False)
            check("token redaction helper", "***" in _redact_token(
                "https://x-access-token:ghp_x@github.com/o/r.git", "ghp_x"))
    else:
        print("  [skip] git not on PATH")

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s) failed -> {failures}")
        return 1
    print("self-test OK — github client, webhook parsing, and git helpers all work.")
    print("Set GITHUB_TOKEN and pass --issue owner/repo#N to fetch a real issue.")
    return 0


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="GitHub integration for the autonomous SWE agent.")
    p.add_argument("--issue", metavar="OWNER/REPO#N",
                   help="Fetch and print an issue as a task. Omit for the offline self-test.")
    args = p.parse_args(argv)

    if not args.issue:
        return _selftest()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    m = re.match(r"^(.+?)#(\d+)$", args.issue.strip())
    if not m:
        print("error: --issue must look like owner/repo#123", file=sys.stderr)
        return 2
    repo, number = m.group(1), int(m.group(2))
    try:
        issue = GitHubClient().get_issue(repo, number)
    except GitHubError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("=" * 70)
    print(issue.to_task())
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
