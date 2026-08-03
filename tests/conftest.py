"""
tests/conftest.py
Shared integration-test harness for the orchestrator.

Provides the `pipeline` fixture: it drives the REAL workers.tasks.run_issue
against fakes for GitHub and the LLM, with a real temporary git repository and
an in-memory SQLite database. See plan2.md §24 Layer 2.

Every module in this project has a good offline self-test, and not one of them
crosses a module boundary — which is exactly why the §22 findings survived. This
harness exists to cover that gap.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.github as gh
import agent.loop as loop_mod
import agent.planner as planner_mod
import agent.retrieval as retrieval_mod
import agent.reviewer as reviewer_mod
from agent.providers import LLMResponse, ToolCall, Usage
from agent.schemas import PlannerOutput, ReviewerOutput

# Fake pricing: $1 per 1000 tokens, so accounting assertions read plainly.
PLANNER_USAGE = Usage(500, 500)      # 1000 tokens -> $1.00
REVIEWER_USAGE = Usage(250, 250)     # 500 tokens  -> $0.50
CODER_CALL_USAGE = Usage(100, 100)   # 200 tokens  -> $0.20 per model call

PR_URL = "https://github.com/octo/test/pull/7"


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeIssue:
    repo, number, title = "octo/test", 42, "paginate() drops the last page"
    body, labels, author = "Off-by-one in paginate().", ["bug"], "alice"

    def to_task(self) -> str:
        return f"Issue #42: {self.title}\n\n{self.body}"


class FakePR:
    number = 7
    html_url = PR_URL


class FakeClient:
    def __init__(self, *a, **k):
        self.comments: list[str] = []

    def get_issue(self, repo, number):
        return FakeIssue()

    def comment_on_issue(self, repo, number, body):
        self.comments.append(body)
        return "https://github.com/octo/test/issues/42#comment"


class FakeProvider:
    """Replays a scripted list of LLMResponses; prices at $1 per 1000 tokens."""

    name, model = "fake", "fake-1"

    def __init__(self, scripted):
        self._scripted = scripted
        self._i = 0

    def complete(self, *, system, messages, tools, max_tokens):
        resp = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return resp

    def user_message(self, text):
        return {"role": "user", "content": text}

    def assistant_turn(self, resp):
        return {"role": "assistant", "content": resp.text}

    def tool_result_message(self, results):
        return {"role": "user", "content": "tool results"}

    def count_tokens(self, **kwargs):
        return 10

    def cost_usd(self, usage):
        return (usage.input_tokens + usage.output_tokens) / 1000.0


def coder_script() -> list[LLMResponse]:
    """Coder turns: create a NEW test file, run tests, summarize with Red/Green.

    The file is *created* rather than modified on purpose: `git diff` without
    staging would not show it, which is defect F2.
    """
    return [
        LLMResponse(
            text="", stop_reason="tool_use", usage=CODER_CALL_USAGE,
            tool_calls=[ToolCall("t1", "edit_file", {
                "path": "tests/test_paginate.py",
                "old_string": "",
                "new_string": "def test_last_page():\n    assert paginate(10, 3) == 4\n",
            })],
        ),
        LLMResponse(
            text="", stop_reason="tool_use", usage=CODER_CALL_USAGE,
            tool_calls=[ToolCall("t2", "run_tests", {})],
        ),
        LLMResponse(
            text=("Fixed the off-by-one in paginate().\n"
                  "### Red (before fix)\nE   assert 3 == 4\n1 failed\n"
                  "### Green (after fix)\n1 passed\n"),
            stop_reason="end_turn", usage=CODER_CALL_USAGE, tool_calls=[],
        ),
    ]


# ── harness ──────────────────────────────────────────────────────────────────

class Harness:
    """Callable test harness. `pipeline(...)` runs one full issue→PR pipeline."""

    PR_URL = PR_URL
    coder_script = staticmethod(coder_script)

    def __init__(self, tmp: Path, monkeypatch):
        self._tmp = tmp
        self._mp = monkeypatch
        self._n = 0

    def session(self):
        """A DB session against the harness's in-memory database."""
        from db.session import _get_session_factory
        return _get_session_factory()()

    def __call__(self, coder_calls=None, reviewer_verdicts=("approve",),
                 run_id=None, name=None, planner_files=None,
                 reviewer_unavailable=False):
        """Run one pipeline.

        planner_files        override the plan's files_to_touch (for F12)
        reviewer_unavailable make the Reviewer report review_status='unavailable' (F15)
        """
        self._n += 1
        name = name or f"run{self._n}"
        coder_calls = coder_calls if coder_calls is not None else coder_script()
        captured: dict = {"planner_calls": [], "reviewer_calls": [],
                          "indexed": False, "dropped_index": []}

        # A real git repo to clone from — _capture_diff runs real git against it.
        origin = self._tmp / f"{name}-origin"
        origin.mkdir(parents=True, exist_ok=True)
        (origin / "paginate.py").write_text("def paginate(n, per):\n    return n // per\n")
        subprocess.run(["git", "init", "-q", str(origin)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(origin), "config", "user.email", "t@t.c"], check=True)
        subprocess.run(["git", "-C", str(origin), "config", "user.name", "tester"], check=True)
        subprocess.run(["git", "-C", str(origin), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(origin), "commit", "-qm", "init"], check=True)

        def fake_clone(repo, dest, **k):
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", str(origin), str(dest)],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", str(dest), "config", "user.email", "a@a.c"], check=True)
            subprocess.run(["git", "-C", str(dest), "config", "user.name", "agent"], check=True)
            return dest

        def fake_submit(workspace, *, repo, branch, base, title, body, **k):
            captured["pr_body"] = body
            captured["pr_title"] = title
            return FakePR()

        def spy_index(root):
            captured["indexed"] = True
            return 0

        def spy_drop(root):
            captured["dropped_index"].append(str(root))
            return True

        def spy_planner(issue_text, workspace, **kw):
            captured["planner_calls"].append({
                "workspace": str(workspace),
                "exists": Path(workspace).is_dir(),
                "is_git_repo": (Path(workspace) / ".git").exists(),
                "indexed_first": captured["indexed"],
                "kwargs": dict(kw),
            })
            if kw.get("budget") is not None:
                kw["budget"].record(PLANNER_USAGE, 1.0)
            return PlannerOutput(
                understanding="paginate() drops the last page.",
                root_cause_hypothesis="Integer division truncates the remainder.",
                files_to_touch=(list(planner_files) if planner_files is not None
                                else ["paginate.py", "tests/test_paginate.py"]),
                plan_steps=["Use ceiling division in paginate()"],
                test_strategy="Add test_last_page() in tests/test_paginate.py",
                risk_notes="None",
            ), PLANNER_USAGE

        verdicts = list(reviewer_verdicts)

        def spy_reviewer(issue_text, plan, diff, test_output, **kw):
            captured["reviewer_calls"].append({"diff": diff, "test_output": test_output})
            if kw.get("budget") is not None:
                kw["budget"].record(REVIEWER_USAGE, 0.5)
            if reviewer_unavailable:
                # What run_reviewer() returns when the provider is down.
                return ReviewerOutput(
                    verdict="approve",
                    summary="Review skipped due to provider error: API is down",
                    confidence="low",
                    review_status="unavailable",
                ), REVIEWER_USAGE
            verdict = verdicts.pop(0) if verdicts else "approve"
            return ReviewerOutput(
                verdict=verdict,
                concerns=[] if verdict == "approve" else ["handle the empty page case"],
                confidence="high",
                summary=f"verdict={verdict}",
            ), REVIEWER_USAGE

        mp = self._mp
        mp.setattr(gh, "GitHubClient", FakeClient)
        mp.setattr(gh, "clone", fake_clone)
        mp.setattr(gh, "submit_changes", fake_submit)
        mp.setattr(retrieval_mod, "index_repo", spy_index)
        mp.setattr(retrieval_mod, "drop_repo", spy_drop)
        mp.setattr(planner_mod, "run_planner", spy_planner)
        mp.setattr(reviewer_mod, "run_reviewer", spy_reviewer)
        mp.setattr(loop_mod, "get_provider_for_role",
                   lambda *a, **k: FakeProvider(coder_calls))

        import workers.tasks as wt
        mp.setattr(wt, "WORKSPACE_ROOT", self._tmp / f"{name}-ws")

        # .run() executes the task body synchronously — no broker needed.
        result = wt.run_issue.run("octo/test", 42, False, run_id)
        return result, captured


@pytest.fixture
def pipeline(monkeypatch):
    """Yields a Harness bound to a fresh in-memory database and temp directory."""
    from db.session import _get_engine, _get_session_factory, init_db, reset_singletons

    tmpdir = tempfile.TemporaryDirectory()

    reset_singletons()
    _get_engine("sqlite:///:memory:")
    _get_session_factory("sqlite:///:memory:")
    init_db()

    yield Harness(Path(tmpdir.name), monkeypatch)

    reset_singletons()
    tmpdir.cleanup()
