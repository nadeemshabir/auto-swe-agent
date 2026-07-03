# The Execution Sandbox (`sandbox.py`)

This document explains `agent/sandbox.py`: the one place the autonomous agent lets **untrusted code** run.

Everything else the agent does is safe by construction — reading files, editing files, and semantic search all happen host-side against a checked-out workspace, and the LLM is called from the worker process. The single dangerous act is **running the repository's own test suite**, because that executes code we did not write and cannot trust. The sandbox exists to make that act safe.

## The core idea: model host-side, code sandbox-side

A repo's tests could try to read your secrets, phone home, delete files, or mine crypto. So the agent never runs them directly. Instead:

- The **LLM runs on the host** (it needs the API key and network).
- The **untrusted code runs in a throwaway Docker container** with no network, no secrets, a read-only host filesystem, resource caps, and a hard time limit.

Inverting this — running the whole agent inside the container — would force an impossible choice: either deny the model the network it needs, or give the untrusted sandbox the network we are trying to withhold. Keeping the loop host-side and pushing *only execution* into the container gives isolation without crippling the model. *(plan DECISION D2)*

---

## What the sandbox guarantees

Every one of these is enforced on the container at launch (`Sandbox._run_args`):

| Guarantee | How | Why |
|---|---|---|
| **No network** | `--network none` | Untrusted code can't reach the internet, the LLM key, the GitHub token, or Postgres. |
| **Read-only host FS** | `--read-only` | The container can't tamper with anything on the host. |
| **Writable workspace only** | `-v <ws>:/work:rw` | The one exception — the run's own checkout, so tests can write caches/artifacts. |
| **`.git` masked** | read-only `--tmpfs /work/.git` | Untrusted code must not plant `.git/hooks/*` or `core.fsmonitor` config — host-side git would *execute* it later (sandbox→host escape). `agent/github.py` also disables hooks/fsmonitor on every host git call, as a second layer. |
| **Scratch space** | `--tmpfs /tmp` | A small writable `/tmp` (needed because the root FS is read-only). |
| **Non-root** | `--user <uid:gid>` | Even if code escapes the process, it isn't root inside the container. |
| **No capabilities** | `--cap-drop ALL` + `--security-opt no-new-privileges` | Removes Linux capabilities and blocks privilege re-escalation. |
| **Resource caps** | `--pids-limit`, `--memory`, `--cpus` | Stops fork bombs, OOM, and CPU exhaustion. |
| **Wall-clock kill** | in-container `timeout` + outer backstop | No infinite loops; a runaway command is killed. |
| **No secrets** | nothing sensitive in `--env` | Only `HOME=/tmp` is set (not a secret), so tools have a writable home. |

The `python -m agent.sandbox` self-test asserts these live against a running daemon (network blocked, root FS read-only, workspace writable, uid ≠ 0, timeout fires).

---

## Lifecycle: one container per run, reused

Starting a container image costs a second or two, and a single agent run may call `run_tests` many times. So the sandbox is **started once per run and reused** *(plan DECISION D6)*:

```
docker run -d ... sleep infinity        # start once; container idles
docker exec <container> ... <cmd>       # dispatch each test run
docker rm -f <container>                # destroy at the end of the run
```

`Sandbox` is a context manager, so the common pattern is:

```python
from agent.sandbox import Sandbox

with Sandbox(workspace) as sb:
    print(sb.run_tests())            # "exit code: 0\n..."
    print(sb.run_tests("tests/test_api.py"))
# container is force-removed on exit, even if an exception was raised
```

## The wall-clock kill, in two layers

Timeouts are enforced twice, on purpose:

1. **In-container** (`timeout --signal=KILL <t>s ...`): kills *just the offending process* and leaves the container alive, so the run can keep using it. This is the normal path (the image ships coreutils' `timeout`; `start()` probes for it).
2. **Outer backstop** (subprocess timeout on the `docker exec` client): if the client itself hangs, the whole container is killed. This is the safety net.

A timed-out command comes back as `ExecResult(timed_out=True)` with exit code 124/137, which `format()` renders with a `TIMED OUT —` prefix.

---

## How it plugs into the loop

The loop was written for this swap from day one. `agent/loop.py`'s `run_tests` tool has always returned the string shape `"exit code: N\n<output>"`; `Sandbox.run_tests()` returns exactly the same shape. Wiring is a single optional argument:

```python
# host execution (default, trusted repos / M1)
tools = default_tools(workspace)

# sandboxed execution (untrusted repos / M2)
tools = default_tools(workspace, sandbox=my_sandbox)
```

`ReActAgent(..., sandbox=my_sandbox)` threads it through, and the CLI exposes it:

```bash
python -m agent.loop "Fix ..." --workspace /repo --auto-index --sandbox
# override the image for one run:
python -m agent.loop "Fix ..." --workspace /repo --sandbox --sandbox-image auto-swe-sandbox:latest
```

The `--sandbox` flag makes the CLI check `docker_available()` (erroring out if the daemon is down), start one `Sandbox` for the whole run, and tear it down in a `finally`; `--sandbox-image` overrides `SANDBOX_IMAGE` just for that run. Only `run_tests` is routed into the container. `retrieve_context`, `read_file`, `edit_file`, and `list_dir` stay host-side against the workspace — they don't execute anything, so they don't need isolation. If the sandbox or daemon fails, that surfaces to the model as a normal recoverable tool error (`ToolError`), so the model can adapt rather than crashing the run *(plan §9)*.

## Path confinement

`run_tests` accepts an optional target (a file/dir/node id to narrow the run). `Sandbox._container_path` resolves it and refuses anything that escapes the workspace root before translating it to its `/work/...` path inside the container — the same guard the loop applies host-side, so a malicious target string can't point pytest outside the mounted workspace.

---

## Public API reference

Everything exported by `agent/sandbox.py`.

### `class Sandbox`

```python
Sandbox(
    workspace: str | Path,
    *,
    image:      str  | None = None,   # default SANDBOX_IMAGE
    cpus:       str  | None = None,   # default SANDBOX_CPUS
    memory:     str  | None = None,   # default SANDBOX_MEMORY
    pids_limit: int  | None = None,   # default SANDBOX_PIDS_LIMIT
    timeout_s:  int  | None = None,   # default SANDBOX_TIMEOUT_S (per-command)
    user:       str  | None = None,   # default host uid:gid (POSIX) / 65534:65534
    tmpfs_size: str  | None = None,   # default SANDBOX_TMPFS_SIZE
    docker_bin: str          = DOCKER_BIN,
)
```

The constructor validates that `workspace` is a directory (raises `SandboxError` otherwise) and generates a unique container name `aswe-sbx-<hex>`. It does **not** touch Docker yet — no container exists until `start()`.

**Attributes:** `workspace`, `image`, `cpus`, `memory`, `pids_limit`, `timeout_s`, `user`, `tmpfs_size`, `docker_bin`, `name` (container name), `container_id` (`None` until started).

**Methods:**

| Method | Returns | Description |
|---|---|---|
| `start()` | `Sandbox` (self) | `docker run -d` the hardened container running `sleep infinity`; records `container_id`; probes once whether the image has coreutils `timeout`. Idempotent (a second call is a no-op). Raises `SandboxError` if the daemon is unreachable, the CLI is missing, the image can't start, or startup exceeds the pull budget. |
| `close()` | `None` | `docker rm -f` the container. Safe to call repeatedly and when never started. |
| `is_running()` | `bool` | Whether a container is currently started. |
| `exec(shell_cmd, *, timeout_s=None)` | `ExecResult` | Run an arbitrary `/bin/sh` command line inside the container. `timeout_s` defaults to the instance's `timeout_s`. |
| `run_tests(target=None, *, timeout_s=None)` | `str` | Run `python -m pytest -q [target]` in the container and return the loop-shaped string `"exit code: N\n<output>"`. `target` is a workspace-relative path, confined to the workspace. |
| `__enter__` / `__exit__` | — | Context-manager sugar: `__enter__` calls `start()`, `__exit__` calls `close()`. |

Private helpers: `_run_args()` (builds the hardening flags), `_exec_raw()` (the exec + two-layer timeout engine), `_kill_container()`, `_container_path()` (workspace confinement + `/work/...` translation).

### `class ExecResult` (dataclass)

The outcome of one command. **Fields:** `exit_code: int`, `stdout: str`, `stderr: str`, `timed_out: bool = False`.

| Member | Description |
|---|---|
| `.combined` (property) | `stdout + stderr` concatenated. |
| `.format(limit=MAX_OUTPUT_CHARS)` | Renders `"exit code: N\n<output>"`, output clipped to the last `limit` chars; prepends `TIMED OUT — ` when `timed_out`. This is the exact shape the loop's `run_tests` tool returns. |

A command that merely exits non-zero (e.g. failing tests) is a normal `ExecResult` with a non-zero `exit_code` — **not** an exception.

### `docker_available(docker_bin=DOCKER_BIN) -> bool`

Cheap probe: true only if the `docker` CLI exists **and** a daemon answers (`docker version --format {{.Server.Version}}`). Used by the CLI/orchestrator to decide whether a live run is possible, and by the self-test to skip gracefully.

### `class SandboxError(Exception)`

Raised for failures operating the sandbox *itself* — daemon down, CLI missing, image won't start, container died, or a target path escaping the workspace. Distinct from a non-zero command exit (that's a normal `ExecResult`). When wired into the loop, a `SandboxError` from `run_tests` is caught and re-raised as a recoverable `ToolError` so the model can adapt.

### Module constants (env-backed defaults)

`DEFAULT_IMAGE`, `DEFAULT_CPUS`, `DEFAULT_MEMORY`, `DEFAULT_PIDS_LIMIT`, `DEFAULT_TIMEOUT_S`, `DEFAULT_TMPFS_SIZE`, `DOCKER_BIN` (see the Configuration table). Plus tuning constants: `MAX_OUTPUT_CHARS` (16 000 — output is tail-clipped to this before reaching the model), `_OUTER_BUFFER_S` (15 — outer timeout = command timeout + this), `_STARTUP_TIMEOUT_S` (120 — the `docker run` / image-pull budget).

---

## What the self-test checks

`python -m agent.sandbox` (the `_selftest()` entry point). If `docker_available()` is false it prints how to start Docker and exits 0 — importing the module and running the self-test never require a daemon. With a daemon up, it spins a throwaway workspace containing a trivial `test_smoke.py` and asserts, in order:

1. **Basic exec** — `echo` + `id` run and return output.
2. **Network is dead** — a socket connect to `1.1.1.1:53` fails (isolation intact).
3. **Root FS is read-only** — `touch /nope` fails.
4. **Workspace is writable** — `touch /work/...` succeeds.
5. **Non-root** — `id -u` is not `0`.
6. **pytest** — if present in the image, `run_tests()` is exercised end-to-end; otherwise it prints a note to bake pytest in.
7. **Wall-clock timeout** — `sleep 30` with `timeout_s=2` comes back `timed_out`.

It cleans up the container and temp workspace on exit (including on failure).

---

## The image (`docker/sandbox.Dockerfile`)

Because the container has **no network**, nothing can be `pip install`-ed at run time. Test dependencies must be **baked into the image ahead of time** *(plan DECISION D9: pre-bake)*. The provided Dockerfile starts from `python:3.12-slim-bookworm` and adds `pytest`:

```bash
docker build -f docker/sandbox.Dockerfile -t auto-swe-sandbox:latest .
SANDBOX_IMAGE=auto-swe-sandbox:latest python -m agent.sandbox
```

For a specific target repo, extend this image (or build a per-repo variant) with that repo's requirements. **In production**, pin the base image by digest for a reproducible supply chain.

## Configuration (env, plan §8)

| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_IMAGE` | `python:3.12-slim-bookworm` | Image to run (bake in test deps). |
| `SANDBOX_CPUS` | `1` | CPU limit. |
| `SANDBOX_MEMORY` | `2g` | Memory limit. |
| `SANDBOX_PIDS_LIMIT` | `256` | Max processes (fork-bomb guard). |
| `SANDBOX_TIMEOUT_S` | `300` | Per-command wall-clock limit. |
| `SANDBOX_TMPFS_SIZE` | `64m` | Size of the writable `/tmp`. |
| `SANDBOX_USER` | host `uid:gid` (POSIX) / `65534:65534` | Non-root user in the container. |
| `DOCKER_BIN` | `docker` | Docker CLI to shell out to. |

## Design notes & upgrade path

- **No new dependency.** Like `github.py` shelling out to `git`, the sandbox shells out to the `docker` CLI rather than pulling in the Docker SDK. Fewer moving parts, easy to reason about.
- **The non-root/bind-mount gotcha.** On native Linux, a bind-mounted host directory is owned by the host uid; a generic `nobody` (65534) then can't write to it. So on POSIX the default user is the host's own `uid:gid` (still non-root), which keeps `/work` writable. On Docker Desktop (Windows/macOS) the VM handles mount permissions, so `65534` is used. Override with `SANDBOX_USER`.
- **Runtime hardening.** The first release uses plain Docker with the hardening above. For truly hostile repos the upgrade path is a stronger runtime — gVisor or Kata *(plan DECISION D7)*.
- **Getting a daemon to the worker.** In production, workers should reach Docker via a rootless/dedicated daemon or Kubernetes-native per-run Jobs — never the privileged host socket *(plan DECISION D10)*.
