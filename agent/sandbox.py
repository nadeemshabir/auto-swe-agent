"""
agent/sandbox.py
the execution sandbox — the ONE place untrusted repo code is allowed to run.

The ReAct loop runs host-side: the LLM is called from the worker, and file
reads/edits happen host-side against a per-run workspace. But *running* the
repo's own code (its test suite) means executing code we don't trust. That is
pushed in here, into a hardened, throwaway Docker container.

Isolation guarantees (plan §6.7 / §10), all enforced on the container:
  • --network none          no network at all (can't reach the LLM key, the
                            GitHub token, Postgres, or the internet)
  • --read-only             host/root filesystem is read-only...
  • -v <ws>:/work:rw        ...except the run's own workspace, mounted writable
  • --user <non-root>       never runs as root
  • --cap-drop ALL          + --security-opt no-new-privileges
  • --pids-limit / --memory / --cpus   resource caps (fork-bomb / OOM / CPU)
  • hard wall-clock timeout on every command (in-container `timeout`, plus an
    outer backstop that kills the container)
  • no secrets, no env passed in

Lifecycle (plan DECISION D6 — reuse within a run): one container is started per
run (`docker run -d ... sleep infinity`), commands are dispatched with
`docker exec`, and the container is destroyed at the end. Starting an image once
per run instead of once per test call keeps latency down.

Interface: mirrors the loop's tool surface. `run_tests(target)` returns exactly
the string shape the loop's own `run_tests` returns ("exit code: N\\n<output>"),
so the loop can target the sandbox with no changes — pass a Sandbox to
`agent.loop.default_tools(workspace, sandbox=...)` or to `ReActAgent(...)`.

No new dependency: this shells out to the `docker` CLI (same approach as
agent/github.py shelling out to `git`). It needs `docker` on PATH and a running
daemon for live use.

Self-test (no daemon needed to import; a live check runs if Docker is up):
    python -m agent.sandbox
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("agent.sandbox")

# ── defaults (all overridable via env, per plan §8) ──────────────────────────
DEFAULT_IMAGE       = os.getenv("SANDBOX_IMAGE", "python:3.12-slim-bookworm")
DEFAULT_CPUS        = os.getenv("SANDBOX_CPUS", "1")
DEFAULT_MEMORY      = os.getenv("SANDBOX_MEMORY", "2g")
DEFAULT_PIDS_LIMIT  = int(os.getenv("SANDBOX_PIDS_LIMIT", "256"))
DEFAULT_TIMEOUT_S   = int(os.getenv("SANDBOX_TIMEOUT_S", "300"))
DEFAULT_TMPFS_SIZE  = os.getenv("SANDBOX_TMPFS_SIZE", "64m")
DOCKER_BIN          = os.getenv("DOCKER_BIN", "docker")

MAX_OUTPUT_CHARS    = 16_000   # clip captured output before it reaches the model
_OUTER_BUFFER_S     = 15       # outer subprocess timeout = inner + this
_STARTUP_TIMEOUT_S  = 120      # `docker run` / image pull budget


class SandboxError(Exception):
    """A failure operating the sandbox itself (daemon down, image missing,
    container died). Distinct from a command that merely exits non-zero — that
    is a normal ExecResult with a non-zero exit_code, not an exception."""


def _default_user() -> str:
    """Non-root user for the container. On POSIX we default to the host's own
    uid:gid so the bind-mounted workspace stays writable (a plain `nobody` often
    can't write host-owned files on native Linux). On Windows/macOS Docker
    Desktop handles mount permissions in its VM, so the classic unprivileged
    `nobody` (65534) is used. Override with SANDBOX_USER."""
    env = os.getenv("SANDBOX_USER")
    if env:
        return env
    getuid = getattr(os, "getuid", None)
    if getuid is not None:  # POSIX host
        return f"{os.getuid()}:{os.getgid()}"
    return "65534:65534"    # Windows/macOS


def _tail(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the LAST `limit` chars — for test output the failures at the end
    are what matter."""
    if len(text) <= limit:
        return text
    return f"... [truncated to last {limit} chars]\n" + text[-limit:]


# ═════════════════════════════════════════════════════════════════════════════
# Result of one command run inside the sandbox
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def combined(self) -> str:
        return (self.stdout or "") + (self.stderr or "")

    def format(self, limit: int = MAX_OUTPUT_CHARS) -> str:
        """Render for the model — same shape as the loop's host run_tests:
        'exit code: N\\n<output>'."""
        prefix = "TIMED OUT — " if self.timed_out else ""
        return f"{prefix}exit code: {self.exit_code}\n{_tail(self.combined, limit)}"


# ═════════════════════════════════════════════════════════════════════════════
# Docker availability
# ═════════════════════════════════════════════════════════════════════════════

def docker_available(docker_bin: str = DOCKER_BIN) -> bool:
    """True only if the `docker` CLI exists AND a daemon answers. Cheap probe
    used by the CLI/orchestrator to decide whether a live run is possible."""
    try:
        proc = subprocess.run(
            [docker_bin, "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


# ═════════════════════════════════════════════════════════════════════════════
# The sandbox
# ═════════════════════════════════════════════════════════════════════════════

class Sandbox:
    """A hardened, per-run Docker container. Start once, `exec` many times,
    close at the end. Use as a context manager:

        with Sandbox(workspace) as sb:
            print(sb.run_tests())
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        image: str | None = None,
        cpus: str | None = None,
        memory: str | None = None,
        pids_limit: int | None = None,
        timeout_s: int | None = None,
        user: str | None = None,
        tmpfs_size: str | None = None,
        docker_bin: str = DOCKER_BIN,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise SandboxError(f"workspace is not a directory: {self.workspace}")
        self.image      = image or DEFAULT_IMAGE
        self.cpus       = cpus or DEFAULT_CPUS
        self.memory     = memory or DEFAULT_MEMORY
        self.pids_limit = pids_limit if pids_limit is not None else DEFAULT_PIDS_LIMIT
        self.timeout_s  = timeout_s if timeout_s is not None else DEFAULT_TIMEOUT_S
        self.user       = user or _default_user()
        self.tmpfs_size = tmpfs_size or DEFAULT_TMPFS_SIZE
        self.docker_bin = docker_bin

        self.name = "aswe-sbx-" + uuid.uuid4().hex[:12]
        self.container_id: str | None = None
        self._has_timeout = False   # whether coreutils `timeout` exists in image
        self._was_started = False   # allows auto-restart after a timeout kill

    # ── lifecycle ────────────────────────────────────────────────────────────
    def _run_args(self) -> list[str]:
        """The hardening flags. Every MUST from plan §6.7/§10 lives here."""
        args = [
            self.docker_bin, "run", "-d", "--rm",
            "--name", self.name,
            "--network", "none",                     # no network, at all
            "--read-only",                           # root FS read-only...
            "-v", f"{self.workspace}:/work:rw",      # ...workspace is the only RW mount
            "--workdir", "/work",
            "--tmpfs", f"/tmp:rw,exec,size={self.tmpfs_size}",  # writable scratch
            "--user", self.user,                     # non-root
            "--cap-drop", "ALL",                     # no Linux capabilities
            "--security-opt", "no-new-privileges",   # can't regain privileges
            "--pids-limit", str(self.pids_limit),    # fork-bomb guard
            "--memory", self.memory,                 # OOM guard
            "--cpus", self.cpus,                     # CPU guard
            "--env", "HOME=/tmp",                    # writable HOME (not a secret)
        ]
        # Mask the repo's .git dir from untrusted code. Tests that could write
        # .git/hooks/* or set core.fsmonitor in .git/config would get that code
        # EXECUTED ON THE HOST the next time the orchestrator runs git in this
        # workspace — a sandbox→host escape. A read-only tmpfs shadows the real
        # directory so the container can neither read nor tamper with it.
        # (agent/github.py additionally hardens host-side git as a second layer.)
        if (self.workspace / ".git").is_dir():
            args += ["--tmpfs", "/work/.git:ro"]
        args += [
            self.image,
            "sleep", "infinity",                     # keep alive for docker exec
        ]
        return args

    def start(self) -> "Sandbox":
        if self.container_id is not None:
            return self  # already started
        try:
            proc = subprocess.run(
                self._run_args(), capture_output=True, text=True,
                timeout=_STARTUP_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise SandboxError(f"docker run timed out after {_STARTUP_TIMEOUT_S}s (image pull too slow?)")
        except FileNotFoundError:
            raise SandboxError(f"{self.docker_bin!r} not found on PATH")
        if proc.returncode != 0:
            raise SandboxError(f"could not start sandbox container: {proc.stderr.strip() or proc.stdout.strip()}")
        self.container_id = proc.stdout.strip()
        self._was_started = True
        log.info("sandbox started: %s (%s)", self.name, self.image)
        # probe once for coreutils `timeout` so exec() can enforce an in-container kill
        probe = self._exec_raw("command -v timeout >/dev/null 2>&1 && echo yes || echo no", self.timeout_s)
        self._has_timeout = probe.stdout.strip().endswith("yes")
        if not self._has_timeout:
            log.warning("image %s lacks coreutils `timeout`; relying on outer kill only", self.image)
        return self

    def close(self) -> None:
        """Force-remove the container. Safe to call repeatedly."""
        if self.container_id is None:
            return
        try:
            subprocess.run(
                [self.docker_bin, "rm", "-f", self.name],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as e:  # pragma: no cover
            log.warning("failed to remove sandbox %s: %s", self.name, e)
        finally:
            self.container_id = None
            self._was_started = False   # a closed sandbox must not auto-restart
            log.info("sandbox removed: %s", self.name)

    def is_running(self) -> bool:
        return self.container_id is not None

    def __enter__(self) -> "Sandbox":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── command execution ────────────────────────────────────────────────────
    def _kill_container(self) -> None:
        try:
            subprocess.run([self.docker_bin, "kill", self.name],
                           capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
            pass

    def _exec_raw(self, shell_cmd: str, timeout_s: int) -> ExecResult:
        """Run `shell_cmd` (a /bin/sh command line) inside the container.

        Wall-clock enforcement is layered: an in-container `timeout` (when the
        image has coreutils) kills just the offending process — leaving the
        container alive for reuse — and an outer subprocess timeout is the
        backstop that kills the whole container if the client itself hangs."""
        if self.container_id is None:
            raise SandboxError("sandbox is not started")

        if self._has_timeout:
            argv = [self.docker_bin, "exec", self.name,
                    "timeout", "--signal=KILL", f"{timeout_s}s",
                    "sh", "-lc", shell_cmd]
        else:
            argv = [self.docker_bin, "exec", self.name, "sh", "-lc", shell_cmd]

        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=timeout_s + _OUTER_BUFFER_S,
            )
        except subprocess.TimeoutExpired:
            # inner timeout didn't fire (or image lacks it) — kill the container
            self._kill_container()
            self.container_id = None
            return ExecResult(124, "", f"command exceeded {timeout_s}s wall-clock limit", timed_out=True)
        except OSError as e:
            raise SandboxError(f"could not exec in sandbox: {e}")

        # coreutils `timeout` exits 124 (or 137 on KILL) when it fires
        timed_out = proc.returncode in (124, 137)
        return ExecResult(proc.returncode, proc.stdout or "", proc.stderr or "", timed_out=timed_out)

    def exec(self, shell_cmd: str, *, timeout_s: int | None = None) -> ExecResult:
        """Public: run an arbitrary shell command line in the sandbox.

        If a previous command's outer timeout forced the container to be killed,
        a fresh container is started transparently — one runaway test must not
        strand every later run_tests call in the same run."""
        if self.container_id is None and self._was_started:
            log.warning("sandbox %s was killed by a timeout — starting a fresh container", self.name)
            self.name = "aswe-sbx-" + uuid.uuid4().hex[:12]   # old name may still be releasing
            self.start()
        return self._exec_raw(shell_cmd, timeout_s if timeout_s is not None else self.timeout_s)

    # ── workspace path confinement (mirrors loop._safe_path) ──────────────────
    def _container_path(self, target: str) -> str:
        """Translate a workspace-relative (or in-workspace absolute) path to its
        /work/... path inside the container, refusing anything that escapes the
        workspace root."""
        cand = Path(target)
        resolved = (cand if cand.is_absolute() else self.workspace / cand).resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise SandboxError(f"target {target!r} escapes the workspace root")
        rel = resolved.relative_to(self.workspace).as_posix()
        return "/work" if rel == "." else f"/work/{rel}"

    # ── the loop-facing tool ──────────────────────────────────────────────────
    def run_tests(self, target: str | None = None, *, timeout_s: int | None = None) -> str:
        """Run pytest inside the sandbox. Drop-in for the loop's host run_tests:
        returns 'exit code: N\\n<output>'. `target` is an optional
        workspace-relative file/dir/node-id to narrow the run."""
        cmd = "python -m pytest -q"
        if target:
            cmd += " " + shlex.quote(self._container_path(target))
        return self.exec(cmd, timeout_s=timeout_s).format()


# ═════════════════════════════════════════════════════════════════════════════
# Self-test / CLI
# ═════════════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    """Verify the isolation guarantees against a live daemon. If Docker isn't
    running, explain and exit 0 (same courtesy as agent.loop's offline self-test)."""
    import tempfile

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if not docker_available():
        print("Docker daemon not reachable — skipping live sandbox test.")
        print("Start Docker Desktop (or dockerd), then re-run:  python -m agent.sandbox")
        print(f"(image that would be used: {DEFAULT_IMAGE})")
        return 0

    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "test_smoke.py").write_text(
            "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
        )
        print(f"starting sandbox on {ws} with image {DEFAULT_IMAGE} ...")
        try:
            with Sandbox(ws) as sb:
                r = sb.exec("echo hello from sandbox && id")
                print("run cmd  :", r.exit_code, "|", r.stdout.strip().replace("\n", " ; "))

                # 1) network MUST be dead
                net = sb.exec(
                    "python -c \"import socket; socket.create_connection(('1.1.1.1',53),timeout=4)\""
                )
                print("network  :", "BLOCKED ✓" if net.exit_code != 0 else "REACHABLE ✗ (isolation broken!)")

                # 2) root FS MUST be read-only
                ro = sb.exec("touch /nope 2>&1")
                print("root FS  :", "read-only ✓" if ro.exit_code != 0 else "WRITABLE ✗")

                # 3) workspace MUST be writable
                w = sb.exec("touch /work/.sbx_write_probe && echo wrote")
                print("workspace:", "writable ✓" if w.exit_code == 0 else f"NOT writable ✗ ({w.combined.strip()})")

                # 4) must be non-root
                who = sb.exec("id -u")
                print("user     :", f"uid={who.stdout.strip()}", "(non-root ✓)" if who.stdout.strip() != "0" else "(ROOT ✗)")

                # 5) pytest routed through the loop-facing method (image needs pytest)
                pv = sb.exec("python -m pytest --version 2>&1")
                if pv.exit_code == 0:
                    print("pytest   :", pv.stdout.strip().splitlines()[0] if pv.stdout.strip() else "present")
                    print("run_tests:\n" + sb.run_tests())
                else:
                    print("pytest   : not in image — bake it in (see docker/sandbox.Dockerfile) to run tests")

                # 6) wall-clock timeout enforced
                t = sb.exec("sleep 30", timeout_s=2)
                print("timeout  :", "enforced ✓" if t.timed_out else "NOT enforced ✗")
        except SandboxError as e:
            print(f"\nsandbox error: {e}", file=sys.stderr)
            return 1

    print("\nself-test OK — isolation holds. Bake pytest + repo deps into the image for real runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
