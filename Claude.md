# Claude Project Context — Autonomous SWE Agent

This file gives an AI assistant (Claude) the context it needs to work on this
repository. Keep it accurate: wrong context is worse than no context.

## What this project is

A production-grade autonomous software engineer: it watches GitHub repos for
issues and — with no human in the loop — clones the repo, builds a semantic
understanding of the codebase, drives an LLM through a Reason→Act→Observe
loop that reads/edits files and runs tests in a hardened Docker sandbox, and
opens a pull request when it has a verified fix.

**`plan2.md` is the single source of truth** — full architecture, decisions,
current build state, the deep-scan audit, and the build order, all in one
place. Read it before proposing structural changes. (`plan.md` is the
superseded v0.1 draft, kept only for history.)

## Layout and status

| Path | What | Status |
|---|---|---|
| `agent/retrieval.py` | tree-sitter chunking → sentence-transformers embeddings → ChromaDB | built |
| `agent/loop.py` | ReAct loop, budget controller, workspace-confined tools | built |
| `agent/providers/` | `LLMProvider` interface + Anthropic/Gemini adapters | built |
| `agent/github.py` | stdlib REST client, git helpers (hardened), webhook parsing | built |
| `agent/sandbox.py` | per-run hardened Docker container for untrusted test execution | built |
| `docker/sandbox.Dockerfile` | sandbox execution image (pytest pre-baked) | built |
| `workers/tasks.py` | Celery orchestrator: issue → clone → index → loop → PR | **next (M1)** |
| `app/main.py` | FastAPI webhook receiver + read API | not started (M3) |
| `db/`, `monitoring/`, `k8s/`, `helm/` | persistence, observability, deploy | not started |

## Conventions and invariants (do not break these)

- **Security invariants:** untrusted repo code runs ONLY inside the sandbox
  (`--network none`, read-only host FS, non-root, resource caps, `.git`
  masked). Host-side git always runs with hooks/fsmonitor disabled
  (`_GIT_HARDENING` in `agent/github.py`). Tokens are never persisted to
  `.git/config` and are redacted from every error/log. No secrets ever enter
  the sandbox or the system prompt.
- **Provider-agnostic loop:** `agent/loop.py` imports only
  `agent.providers.base` types — never a vendor SDK directly.
- **Fail-soft tools:** tool handlers raise `ToolError` for expected failures;
  the loop converts them to `is_error` results. A tool must never crash a run.
- **No new dependencies without cause:** `github.py` is stdlib-only by design;
  `sandbox.py` shells out to the `docker` CLI deliberately.
- **Offline self-tests:** every `agent/*.py` module is runnable as
  `python -m agent.<module>` with no API key / network / daemon and must keep
  passing. Extend the self-test when you change a module.
- **Line endings:** internal file I/O normalizes to LF (`_read_text` /
  `_write_text` in loop.py); `.gitattributes` enforces LF for source files.

## Working with the author

Nadeem is a third-year B.Tech student (IIT Bombay) building this as an
8-week advanced AI-engineering project, targeting ML/AI engineering roles.
Claude acts as a senior engineer: explain the *why* behind designs, flag
security and cost implications explicitly, and prefer minimal, verifiable
changes over large rewrites.
