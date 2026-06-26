# Autonomous SWE Agent

An AI system that autonomously reads GitHub issues, writes code to fix them, tests the code, and opens Pull Requests. Built over 8 weeks as an advanced AI engineering project.

## Project Overview

The goal is to build a production-grade autonomous AI software engineer that:

1. Watches a GitHub repo for new issues via webhook
2. Spins up an isolated Docker sandbox 
3. Uses an LLM to understand the codebase, plan a fix, write code, run tests, and debug failures
4. Opens a Pull Request with the fix and explanation
5. Does all of this without any human involvement from issue creation to PR opening

This project covers the full stack of an agentic AI system:

- GitHub integration (webhooks, issues, PRs)
- Codebase understanding (tree-sitter parsing, sentence-transformers embedding, ChromaDB vector store)
- ReAct reasoning loop (multi-step Reason → Act → Observe)
- Sandboxed code execution (Docker SDK, network isolation, filesystem constraints, timeouts)
- Backend orchestration (FastAPI, Celery, Redis, PostgreSQL)
- Full observability (Prometheus metrics, Grafana dashboards, OpenTelemetry tracing)
- Production deployment (Kubernetes, Helm, CI/CD with GitHub Actions)

## System Architecture

The system has six layers, each independently testable and deployable:

1. **GitHub Integration** - webhooks, issue parsing, repo cloning, PR creation
2. **Codebase Understanding** - AST parsing, embedding, vector search, call graph analysis 
3. **Planning & Reasoning** - ReAct loop (Reason → Act → Observe), tools, memory, budget
4. **Sandboxed Execution** - isolated Docker containers, network/filesystem constraints 
5. **Backend & Queue** - FastAPI orchestrator, Celery workers, Redis broker, PostgreSQL store
6. **Observability & DevOps** - metrics, traces, logs, dashboards, Kubernetes, CI/CD

## Key Features

- **ReAct reasoning loop:** Full multi-step Reason → Act → Observe → Reason cycle (5-20 steps per issue)
- **Codebase-aware retrieval:** Parses ASTs, builds call graphs, uses hybrid semantic + structural search 
- **Sandboxed execution:** Fresh isolated Docker container per run, no network access, read-only host FS
- **Cost management:** Hard cap on LLM calls, sandbox CPU/time budget, token usage tracking
- **GitHub integration:** Live webhook-to-PR pipeline, no simulated APIs
- **Production observability:** Distributed tracing, Prometheus metrics, Grafana dashboards, Kubernetes

## Status

By component (built out of strict week order):

- **Environment setup:** Done ✅
- **Codebase understanding** (`agent/retrieval.py`): Done ✅ — tree-sitter chunking, embeddings, ChromaDB, call graph, token-budgeted context
- **ReAct reasoning loop** (`agent/loop.py` + `agent/providers/`): Core done ✅ — manual agentic loop, budget controller, sandboxed tools, pluggable Anthropic/Gemini providers
- **GitHub integration** (`agent/github.py`): Not started
- **Docker sandbox** (`agent/sandbox.py`): Not started
- **Backend & queue** (`app/`, `workers/`): Not started
- **Observability & deployment** (`monitoring/`, `k8s/`, `helm/`): Not started

## Usage

```bash
# 1. Install dependencies (into your virtual environment)
pip install -r requirements.txt

# 2. Verify the agent's tools and safety guards — no API key or model needed
python -m agent.loop

# 3. Pick a provider and set its key in .env
#    ANTHROPIC_API_KEY=...                       # default provider (claude-opus-4-8)
#    GEMINI_API_KEY=...   with LLM_PROVIDER=gemini  # alternative (gemini-2.5-pro)

# 4. Run the agent on a real task against a repo (indexes it first)
python -m agent.loop "Fix the off-by-one in paginate()" --workspace /path/to/repo --auto-index
```

The model and effort are configurable via env vars (`LLM_PROVIDER`, `LLM_MODEL`,
`LLM_EFFORT`); budgets via CLI flags (`--max-steps`, `--max-usd`).

## Development

The `docs/` folder has deep-dives on each component built so far:

- [docs/retrieval.md](docs/retrieval.md) — the codebase understanding engine
- [docs/loop.md](docs/loop.md) — the ReAct agent loop and tools
- [docs/llm-provider-abstraction.md](docs/llm-provider-abstraction.md) — ADR: pluggable Anthropic/Gemini providers

Core tech stack:

- Python 3.12
- LLM: pluggable — Anthropic Claude **or** Google Gemini (see [docs/llm-provider-abstraction.md](docs/llm-provider-abstraction.md))
- Docker + Kubernetes 
- FastAPI + Celery
- Prometheus + Grafana
- GitHub Actions CI/CD

The reasoning core is not hard-wired to one vendor. The ReAct loop talks to an
`LLMProvider` interface, and a thin adapter selects Anthropic or Gemini at
runtime via the `LLM_PROVIDER` env var (default `anthropic`, model
`claude-opus-4-8`). See the ADR linked above for the contract and rationale.

## About the Author

This project was built by Nadeem, a third-year B.Tech student at IIT Bombay interning at Alimento Agro Foods. Nadeem is preparing for ML/AI engineering roles post-graduation.