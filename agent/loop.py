"""
agent/loop.py
the ReAct reasoning loop — Reason → Act → Observe.

The agent is handed a task (e.g. a GitHub issue) and a workspace (a checked-out
repo). It drives a provider-agnostic LLM through a manual agentic loop:

    while not done and within budget:
        resp = provider.complete(system, messages, tools)   # Reason
        if resp wants tools:  run them (Act) -> feed results back (Observe)
        else:                 finish

Design goals (this file is the spine, so it is deliberately defensive):

  • Provider-agnostic     — talks only to agent.providers.LLMProvider.
  • Hard budgets          — caps on steps, cumulative tokens, and USD spend.
  • Sandboxed tools       — all file access is confined to the workspace root.
  • Fail-soft tools       — a tool error is returned to the model (is_error),
                            never crashes the loop; the model can recover.
  • Observable            — every step is recorded in a structured trace.

Sandboxing here is path-confinement only. Real isolation (no network,
read-only host FS, CPU/time limits) lands in agent/sandbox.py — the tool
handlers below are written so they can be swapped for sandboxed equivalents
without touching the loop.

Run a real task:   python -m agent.loop "Fix the off-by-one in paginate()" --workspace /path/to/repo
Offline self-test: python -m agent.loop            (exercises tools, no API key needed)
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Load .env so that ANTHROPIC_API_KEY, LLM_PROVIDER, budget limits etc.
# are visible to os.getenv() throughout the process.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
except ImportError:
    pass  # python-dotenv not installed; user must export vars manually

try:
    from .providers import ProviderError, ToolCall, ToolSpec, Usage, get_provider
except ImportError:  # executed as a loose script rather than a package module
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.providers import ProviderError, ToolCall, ToolSpec, Usage, get_provider


log = logging.getLogger("agent.loop")

# ── limits (tunable) ─────────────────────────────────────────────────────────
MAX_READ_BYTES        = 200_000   # cap a single read_file
MAX_TOOL_RESULT_CHARS = 16_000    # cap any tool result before it hits context
RUN_TESTS_TIMEOUT     = 300       # seconds
RETRIEVE_TOKEN_BUDGET = 4_000     # context packed per retrieve_context call


DEFAULT_SYSTEM = """\
You are an autonomous software engineer working inside a single repository.

Goal: resolve the user's task by understanding the code, making the smallest
correct change, and verifying it with tests.

Tools:
- retrieve_context: semantic search to locate relevant code. Start here.
- read_file: read a file's exact contents before editing it.
- edit_file: make a precise change (exact-string replacement, or create a new
  file by passing an empty old_string).
- run_tests: run the test suite (or a subset) to verify your change.
- list_dir: list a directory.

Working rules:
- Make the minimal change that fixes the task. Do not refactor unrelated code,
  reformat files, or add features that were not requested.
- Always read a file (and confirm the exact text) before you edit it.
- After editing, run the tests and fix any failures you introduced.
- You are running autonomously and cannot ask the user questions mid-task. For
  reversible decisions that follow from the task, just proceed.
- When the task is complete and tests pass, give a short summary of what you
  changed and why, then stop.
"""


# ═════════════════════════════════════════════════════════════════════════════
# Budget controller
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Budget:
    """Hard caps on a single run. `exhausted()` is checked before every model
    call; `record()` accrues usage after each one.
    Defaults are read from .env (MAX_STEPS, MAX_TOTAL_TOKENS, MAX_USD),
    falling back to safe hardcoded values."""
    max_steps: int = field(default_factory=lambda: int(os.getenv('MAX_STEPS', '30')))
    max_total_tokens: int = field(default_factory=lambda: int(os.getenv('MAX_TOTAL_TOKENS', '500000')))
    max_usd: float = field(default_factory=lambda: float(os.getenv('MAX_USD', '5.0')))

    steps: int = 0
    total: Usage = field(default_factory=Usage)
    spent_usd: float = 0.0

    def exhausted(self) -> str | None:
        if self.steps >= self.max_steps:
            return "max_steps"
        if self.total.input_tokens + self.total.output_tokens >= self.max_total_tokens:
            return "token_budget"
        if self.spent_usd >= self.max_usd:
            return "usd_budget"
        return None

    def record(self, usage: Usage, cost: float) -> None:
        self.steps += 1
        self.total = self.total + usage
        self.spent_usd += cost


# ═════════════════════════════════════════════════════════════════════════════
# Tools — all file access confined to the workspace
# ═════════════════════════════════════════════════════════════════════════════
class ToolError(Exception):
    """A recoverable tool failure. Surfaced to the model as is_error, not raised
    out of the loop.

    Why a dedicated class: tool handlers need to distinguish an *expected*
    failure (bad path, file missing, ambiguous edit — things the model can see
    and correct on its next turn) from an *unexpected* bug in the tool itself.
    _dispatch() turns ToolError into a normal is_error result, while any other
    exception is logged as a tool bug. The loop never stops on a tool failure —
    only budgets, completion, refusal, or a provider error end a run."""

#makes sure LLM can't access files outside the workspace/repo
def _safe_path(workspace: Path, p: str | None) -> Path:
    """Resolve `p` (relative to workspace, or absolute) and refuse anything
    outside the workspace root — blocks path traversal (../../etc/passwd)."""
    if not p or not str(p).strip(): 
        raise ToolError("path is required")
    cand = Path(p)
    target = (cand if cand.is_absolute() else workspace / cand).resolve()
    if target != workspace and workspace not in target.parents:
        raise ToolError(f"path {p!r} escapes the workspace root")
    return target

#if output is very big then truncate it to last {limit} chars, because those are the ones which actually matter
def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"... [truncated to last {limit} chars]\n" + text[-limit:]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated to first {limit} chars]"

def _read_text(path: Path) -> str:
    """Read as UTF-8 with newlines normalized to LF. Uses read_bytes (not
    read_text) so the OS never rewrites line endings — the model always sees
    \\n, so its \\n-based edits match regardless of platform."""
    return path.read_bytes().decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def _write_text(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings, bypassing platform newline translation."""
    path.write_bytes(text.encode("utf-8"))

#imports retrieval module when needed - lazy import otherwise it will import the whole module every time when called. + it handles the import error also
def _load_retrieval():
    """Lazy import — retrieval pulls in heavy ML deps and a Chroma client."""
    try:
        from . import retrieval
    except ImportError:
        from agent import retrieval
    return retrieval


def default_tools(workspace: Path, sandbox=None) -> dict[str, tuple[ToolSpec, "callable"]]:
    """Build the default tool set bound to `workspace`.
    Returns name -> (spec, handler). A handler takes the model's args dict and
    returns a string, or raises ToolError.

    If `sandbox` (an agent.sandbox.Sandbox) is given, `run_tests` is dispatched
    into the hardened container instead of running on the host — the only piece
    that executes untrusted repo code. Everything else (retrieval, reads, edits,
    listing) stays host-side against the workspace, as designed."""

    # retrieve_context — semantic search over the indexed codebase.
    # Takes `query` (required) and `k` (optional, default 8 = max chunks).
    # Embeds the query with the local sentence-transformers model (no API
    # call, no network) and searches the ChromaDB vector store; returns a
    # token-budgeted context string (see agent/retrieval.py).
    def retrieve_context(args: dict) -> str:
        query = args.get("query")
        if not query:
            raise ToolError("query is required")
        try:
            k = int(args.get("k", 8))
        except (TypeError, ValueError):
            raise ToolError("k must be an integer")
        if k < 1:
            raise ToolError("k must be >= 1")
        try:
            retrieval = _load_retrieval()
        except ImportError as e:
            raise ToolError(f"retrieval is unavailable (missing dependency): {e}")
        try:
            ctx = retrieval.assemble_context(
                query, repo=str(workspace), k=k,
                token_budget=RETRIEVE_TOKEN_BUDGET,
            )
        except Exception as e:
            raise ToolError(f"retrieval failed: {e}")
        return ctx or "(no relevant code found — try another query, or index the repo first)"
        
    #read file tool - it reads the file and returns the content of the file
    # it takes path as argument
    # it returns the content of the file in the form of string
    # path is required
    # if the file is not found it will raise ToolError
    # if the file is not a file it will raise ToolError
    def read_file(args: dict) -> str:
        target = _safe_path(workspace, args.get("path"))
        if not target.exists():
            raise ToolError(f"file not found: {args.get('path')}")
        if not target.is_file():
            raise ToolError(f"not a file: {args.get('path')}")
        try:
            data = target.read_bytes()
        except OSError as e:
            raise ToolError(f"could not read {args.get('path')}: {e}")
        truncated = len(data) > MAX_READ_BYTES
        text = data[:MAX_READ_BYTES].decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        if truncated:
            text += f"\n... [file truncated at {MAX_READ_BYTES} bytes]"
        return text

    #edit file tool - it edits the file and returns the content of the file
    # it takes path, old_string and new_string as arguments
    # it returns the content of the file in the form of string
    # path is required
    # old_string is required
    # new_string is optional and it defaults to ""
    def edit_file(args: dict) -> str:
        path = args.get("path")
        old = args.get("old_string", "")
        new = args.get("new_string", "") or ""
        target = _safe_path(workspace, path)

        if old == "":                      # create a new file
            if target.exists():
                raise ToolError(
                    f"{path} already exists; to modify it pass the exact text to "
                    f"replace as old_string"
                )
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_text(target, new)
            except OSError as e:
                raise ToolError(f"could not create {path}: {e}")
            return f"created {path} ({len(new)} bytes)"

        if not target.exists():
            raise ToolError(f"file not found: {path}")
        try:
            content = _read_text(target)
        except OSError as e:
            raise ToolError(f"could not read {path}: {e}")
        count = content.count(old)
        if count == 0:
            raise ToolError(
                "old_string not found — it must match the file exactly, "
                "including whitespace and indentation"
            )
        if count > 1:
            raise ToolError(
                f"old_string matches {count} places; add surrounding context so "
                f"it is unique"
            )
        try:
            _write_text(target, content.replace(old, new, 1))
        except OSError as e:
            raise ToolError(f"could not write {path}: {e}")
        return f"edited {path} (1 replacement)"

    #run tests tool - it runs the tests and returns the output of the tests
    # it takes target as argument
    # it returns the output of the tests in the form of string
    # target is optional and it defaults to ""
    def run_tests(args: dict) -> str:
        target = args.get("target") or ""

        # Sandboxed path: dispatch into the isolated container (untrusted code).
        if sandbox is not None:
            try:
                from .sandbox import SandboxError
            except ImportError:
                from agent.sandbox import SandboxError
            try:
                return sandbox.run_tests(target or None)
            except SandboxError as e:
                # a sandbox/daemon failure is a recoverable tool error — the
                # model sees it and can adapt (plan §9).
                raise ToolError(f"sandbox error: {e}")

        target_arg = str(_safe_path(workspace, target)) if target else str(workspace)

        # Use the workspace's own venv Python if it exists, so tests run
        # with the correct dependencies — not the agent's own venv.
        ws_python = workspace / '.venv' / 'bin' / 'python'
        if not ws_python.exists():
            ws_python = workspace / '.venv' / 'Scripts' / 'python.exe'  # Windows
        python = str(ws_python) if ws_python.exists() else sys.executable

        cmd = [python, "-m", "pytest", "-q", target_arg]
        try:
            proc = subprocess.run(
                cmd, cwd=str(workspace), capture_output=True, text=True,
                timeout=RUN_TESTS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"tests timed out after {RUN_TESTS_TIMEOUT}s")
        except (FileNotFoundError, OSError) as e:  # pragma: no cover
            raise ToolError(f"could not launch pytest: {e}")
        out = _tail((proc.stdout or "") + (proc.stderr or ""), MAX_TOOL_RESULT_CHARS)
        return f"exit code: {proc.returncode}\n{out}"
    
    #list directory tool - it lists the directory and returns the content of the directory
    # it takes path as argument
    # it returns the content of the directory in the form of string
    # path is optional and it defaults to "."
    def list_dir(args: dict) -> str:
        target = _safe_path(workspace, args.get("path", "."))
        if not target.is_dir():
            raise ToolError(f"not a directory: {args.get('path')}")
        skip = {".venv", "__pycache__", ".git", "node_modules", ".chroma", ".embedding_cache"}
        out = []
        try:
            children = sorted(target.iterdir())
        except OSError as e:
            raise ToolError(f"could not list {args.get('path')}: {e}")
        for child in children:
            if child.name in skip:
                continue
            out.append(child.name + ("/" if child.is_dir() else ""))
            if len(out) >= 500:
                out.append("... [more entries omitted]")
                break
        return "\n".join(out) or "(empty)"

    return {
        "retrieve_context": (
            ToolSpec(
                "retrieve_context",
                "Semantic search over the repository. Returns the most relevant "
                "code chunks for a natural-language query. Use this first to locate code.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to search for."},
                        "k": {"type": "integer", "description": "Max chunks (default 8)."},
                    },
                    "required": ["query"],
                },
            ),
            retrieve_context,
        ),
        "read_file": (
            ToolSpec(
                "read_file",
                "Read a file's exact contents. Always read before editing.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repo-relative path."},
                    },
                    "required": ["path"],
                },
            ),
            read_file,
        ),
        "edit_file": (
            ToolSpec(
                "edit_file",
                "Make a precise edit by replacing old_string with new_string "
                "(old_string must match exactly and be unique). To create a new "
                "file, pass an empty old_string and the full contents as new_string.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repo-relative path."},
                        "old_string": {"type": "string", "description": "Exact text to replace; empty to create a file."},
                        "new_string": {"type": "string", "description": "Replacement text."},
                    },
                    "required": ["path", "new_string"],
                },
            ),
            edit_file,
        ),
        "run_tests": (
            ToolSpec(
                "run_tests",
                "Run the test suite with pytest. Optionally pass a target path to "
                "run a subset. Returns the exit code and output.",
                {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Optional file/dir/node id; empty runs all."},
                    },
                },
            ),
            run_tests,
        ),
        "list_dir": (
            ToolSpec(
                "list_dir",
                "List the entries of a directory in the repository.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repo-relative dir (default '.')."},
                    },
                },
            ),
            list_dir,
        ),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Result
# ═════════════════════════════════════════════════════════════════════════════

#dataclass for storing the result of the run
@dataclass
class RunResult:
    status: str            # completed | refused | max_steps | token_budget
                          #  | usd_budget | max_tokens | provider_error
                          #  | index_error | error
    final_text: str
    steps: list[dict]
    budget: Budget

    def summary(self) -> str:
        """
        Return a one-line summary of the run.
        """
        b = self.budget
        return (
            f"[{self.status}] steps={b.steps} "
            f"tokens={b.total.input_tokens + b.total.output_tokens} "
            f"cost=${b.spent_usd:.4f}"
        )

 
# ═════════════════════════════════════════════════════════════════════════════
# The agent
# ═════════════════════════════════════════════════════════════════════════════


class ReActAgent:
    def __init__(
        self,
        workspace: str | Path = ".",
        provider=None,
        budget: Budget | None = None,
        system: str | None = None,
        tools: dict | None = None,
        max_output_tokens: int = 8_192,
        auto_index: bool = False,
        sandbox=None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {self.workspace}")
        self.provider = provider or get_provider()
        self.budget = budget or Budget()
        self.system = system or DEFAULT_SYSTEM
        self.max_output_tokens = max_output_tokens
        self.tools = tools or default_tools(self.workspace, sandbox=sandbox)
        self._specs = [spec for spec, _ in self.tools.values()]
        # auto_index: parse + embed the workspace (local sentence-transformers
        # model) into ChromaDB before the run, so retrieve_context has data.
        self._auto_index = auto_index

    # ── tool dispatch ─────────────────────────────────────────────────────────
    #tool dispatch: a function that takes a tool call and returns the result of the tool call
    def _dispatch(self, call: ToolCall) -> tuple[str, bool]:
        """Run one tool. Returns (content, is_error). Never raises."""
        entry = self.tools.get(call.name)
        if entry is None:
            return (f"unknown tool: {call.name}", True)
        _, handler = entry
        try:
            result = handler(call.args or {})
            return (_clip(str(result), MAX_TOOL_RESULT_CHARS), False)
        except ToolError as e:
            return (str(e), True)
        except Exception as e:  # defensive: a buggy tool must not kill the loop
            log.exception("tool %s crashed", call.name)
            return (f"tool '{call.name}' failed unexpectedly: {e}", True)

    # ── main loop ─────────────────────────────────────────────────────────────
    #the main loop of the agent that takes a task and returns the result of the task
    #takes a task as a string and returns a RunResult object
    #uses the provider, tools, budget, system prompt, and max_output_tokens to complete the task
    
    def run(self, task: str) -> RunResult:
        if not task or not task.strip():
            raise ValueError("task must be a non-empty string")

        if self._auto_index:
            log.info("indexing workspace %s ...", self.workspace)
            try:
                self._load_and_index()
            except Exception as e:
                log.error("indexing failed: %s", e)
                return RunResult("index_error", f"failed to index workspace: {e}", [], self.budget)

        messages = [self.provider.user_message(task)]
        steps: list[dict] = []
        final_text = ""

        while True:
            reason = self.budget.exhausted()
            if reason:
                log.warning("budget exhausted: %s", reason)
                return RunResult(reason, final_text, steps, self.budget)

            try:
                resp = self.provider.complete(
                    system=self.system,
                    messages=messages,
                    tools=self._specs,
                    max_tokens=self.max_output_tokens,
                )
            except ProviderError as e:
                log.error("provider error: %s", e)
                return RunResult("provider_error", str(e), steps, self.budget)
            except Exception as e:  # unexpected SDK / parsing failure
                log.exception("unexpected error during model call")
                return RunResult("error", f"unexpected error: {e}", steps, self.budget)

            try:
                cost = self.provider.cost_usd(resp.usage)
            except Exception:
                cost = 0.0   # a pricing glitch must never break a run
            self.budget.record(resp.usage, cost)

            try:
                messages.append(self.provider.assistant_turn(resp))
            except Exception as e:
                log.error("could not append assistant turn: %s", e)
                return RunResult("provider_error", f"malformed provider response: {e}", steps, self.budget)

            if resp.text:
                final_text = resp.text

            step = {
                "n": self.budget.steps,
                "stop_reason": resp.stop_reason,
                "text": _clip(resp.text, 500),
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "tools": [],
            }

            # refusal — surface and stop
            if resp.stop_reason == "refusal":
                steps.append(step)
                return RunResult("refused", final_text or "(model refused)", steps, self.budget)

            # tool calls — Act, then Observe
            if resp.tool_calls:
                results = []
                for call in resp.tool_calls:
                    content, is_error = self._dispatch(call)
                    results.append((call, content, is_error))
                    step["tools"].append({
                        "name": call.name,
                        "args": call.args,
                        "is_error": is_error,
                        "result": _clip(content, 300),
                    })
                try:
                    messages.append(self.provider.tool_result_message(results))
                except Exception as e:
                    log.error("could not build tool results: %s", e)
                    return RunResult("provider_error", f"could not return tool results: {e}", steps, self.budget)
                steps.append(step)
                continue

            # server-side pause (rare without server tools) — resume by re-calling
            if resp.stop_reason == "pause_turn":
                steps.append(step)
                continue

            # end_turn / max_tokens / stop_sequence — done
            steps.append(step)
            status = "max_tokens" if resp.stop_reason == "max_tokens" else "completed"
            return RunResult(status, final_text, steps, self.budget)

    def _load_and_index(self) -> None:
        retrieval = _load_retrieval()
        retrieval.index_repo(str(self.workspace))


# ═════════════════════════════════════════════════════════════════════════════
# Convenience + CLI
# ═════════════════════════════════════════════════════════════════════════════

def run(task: str, workspace: str | Path = ".", **kwargs) -> RunResult:
    """One-shot helper: build an agent and run a task."""
    return ReActAgent(workspace=workspace, **kwargs).run(task)


def _selftest() -> None:
    """Exercise the tools offline (no API key / no model call)."""
    import tempfile

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d).resolve()
        tools = default_tools(ws)

        def run_tool(name, **a):
            return tools[name][1](a)

        print("create:", run_tool("edit_file", path="pkg/calc.py",
                                   old_string="", new_string="def add(a, b):\n    return a + b\n"))
        print("list:  ", run_tool("list_dir", path=".").replace("\n", " "))
        print("read:  ", repr(run_tool("read_file", path="pkg/calc.py")[:40]))
        print("edit:  ", run_tool("edit_file", path="pkg/calc.py",
                                   old_string="a + b", new_string="a + b  # sum"))

        # path-traversal must be refused
        try:
            run_tool("read_file", path="../../etc/passwd")
            print("FAIL: traversal not blocked")
        except ToolError as e:
            print("guard: blocked traversal ->", e)

        # ambiguous edit must be rejected
        ws.joinpath("dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
        try:
            run_tool("edit_file", path="dup.py", old_string="x = 1", new_string="x = 2")
            print("FAIL: ambiguous edit not caught")
        except ToolError as e:
            print("guard: ambiguous edit ->", e)

    print("\nself-test OK - tools work; set ANTHROPIC_API_KEY and pass a task to run for real.")


def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Run the autonomous SWE agent on a task.")
    p.add_argument("task", nargs="?", help="The task / issue text. Omit for an offline self-test.")
    p.add_argument("--workspace", default=".", help="Repo to work in (default: cwd).")
    p.add_argument("--auto-index", action="store_true", help="Index the repo before running.")
    p.add_argument("--sandbox", action="store_true",
                   help="Run tests inside a hardened Docker container (needs a running daemon).")
    p.add_argument("--sandbox-image", default=None, help="Override SANDBOX_IMAGE for this run.")
    # default=None so an unset flag falls through to Budget's .env-driven
    # defaults (MAX_STEPS / MAX_USD) instead of silently overriding them.
    p.add_argument("--max-steps", type=int, default=None,
                   help="Cap on model calls (default: MAX_STEPS env or 30).")
    p.add_argument("--max-usd", type=float, default=None,
                   help="Cap on USD spend (default: MAX_USD env or 5.0).")
    args = p.parse_args(argv)

    if not args.task:
        _selftest()
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Optional hardened sandbox for test execution. Started once, reused for the
    # whole run (DECISION D6), torn down in the finally.
    sandbox = None
    if args.sandbox:
        try:
            from .sandbox import Sandbox, SandboxError, docker_available
        except ImportError:
            from agent.sandbox import Sandbox, SandboxError, docker_available
        if not docker_available():
            print("error: --sandbox given but the Docker daemon is not reachable.", file=sys.stderr)
            return 1
        try:
            sandbox = Sandbox(args.workspace, image=args.sandbox_image)
            sandbox.start()
        except SandboxError as e:
            print(f"error: could not start sandbox: {e}", file=sys.stderr)
            return 1

    budget_overrides = {}
    if args.max_steps is not None:
        budget_overrides["max_steps"] = args.max_steps
    if args.max_usd is not None:
        budget_overrides["max_usd"] = args.max_usd

    try:
        result = run(
            args.task,
            workspace=args.workspace,
            auto_index=args.auto_index,
            budget=Budget(**budget_overrides),
            sandbox=sandbox,
        )
    except (ProviderError, ValueError) as e:
        # e.g. missing SDK / API key, or a bad workspace path
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if sandbox is not None:
            sandbox.close()
    print("\n" + "=" * 70)
    print(result.summary())
    print("=" * 70)
    print(result.final_text)
    return 0 if result.status in ("completed", "max_tokens") else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
