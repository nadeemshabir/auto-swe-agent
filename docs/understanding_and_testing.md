# Auto-SWE-Agent — Understanding & Testing Record

> Use this file to track your progress as you read and test each part of the project.
> Check off boxes as you go. Add your own notes under each section.

---

## Mental Model

```
providers/base.py       ← shared language (types)
       ↓
providers/              ← talks to Claude / Gemini
       ↓
retrieval.py            ← reads & understands the repo (no LLM needed)
sandbox.py              ← runs code safely in Docker
github.py               ← talks to GitHub
       ↓
loop.py                 ← the brain that uses ALL of the above
       ↓
app/main.py             ← the door into the system (stub for now)
```

---

## Phase 1 — Understanding the Code

Read in this order. Always read the `docs/` file before the `agent/` file.
The docs explain **why**, the code shows **how**.

### Environment & Dependencies
- [ ] `.env` — what API keys and config exist
- [ ] `requirements.txt` — what libraries the project uses

**Notes:**
```
(write your notes here)
```

---

### Providers Layer (types + LLM adapters)
- [ ] `agent/providers/base.py` — core data types: `ToolSpec`, `ToolCall`, `Usage`, `LLMResponse`, `LLMProvider`
- [ ] `docs/llm-provider-abstraction.md` — why providers are abstracted away from the loop
- [ ] `agent/providers/__init__.py` — `get_provider()` factory, driven by `LLM_PROVIDER` env var
- [ ] `agent/providers/anthropic_provider.py` — how Claude is called
- [ ] `agent/providers/gemini_provider.py` — how Gemini is called

**Notes:**
```
(write your notes here)
```

---

### Retrieval Layer (codebase understanding engine)
- [ ] `docs/retrieval.md` — design intent for the retrieval pipeline
- [ ] `agent/retrieval.py` — walk → parse (tree-sitter) → embed → ChromaDB → retrieve

Pipeline:
```
walk_py_files → parse_file → embed_many → index_repo (ChromaDB)
                                                  │
                              retrieve  ◀──────────┘
                                  │
                                  ▼
                          assemble_context  (token-budgeted)
```

**Notes:**
```
(write your notes here)
```

---

### Sandbox Layer (safe code execution)
- [ ] `docs/sandbox.md` — Docker isolation design (no network, read-only FS, resource caps)
- [ ] `agent/sandbox.py` — starts one Docker container per run, dispatches via `docker exec`

**Notes:**
```
(write your notes here)
```

---

### GitHub Layer (integration with the outside world)
- [ ] `docs/github.md` — REST client + git CLI helpers design
- [ ] `agent/github.py` — `GitHubClient`, `clone`, `create_branch`, `submit_changes`

**Notes:**
```
(write your notes here)
```

---

### Loop Layer (the ReAct brain)
- [ ] `docs/loop.md` — ReAct loop design: Reason → Act → Observe
- [ ] `agent/loop.py` — the spine that wires providers + retrieval + sandbox + github

**Notes:**
```
(write your notes here)
```

---

### Entrypoint
- [ ] `app/main.py` — currently an empty stub; will become the orchestrator entry

**Notes:**
```
(write your notes here)
```

---

## Phase 2 — Testing (Bottom-Up Order)

### Prerequisites
```powershell
# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Verify packages
pip check
```
- [ ] venv activated
- [ ] `pip check` passes cleanly

---

### Step 1 — `providers/base.py` (no API, no network)
```powershell
python -c "from agent.providers.base import ToolSpec, ToolCall, Usage, LLMResponse, LLMProvider; print('OK')"
```
- [ ] Passed
- Date tested: ___________
- Notes:

---

### Step 2 — `providers/__init__.py` (factory routing)
```powershell
python -c "
import os; os.environ['LLM_PROVIDER'] = 'anthropic'
from agent.providers import get_provider
p = get_provider()
print(p.name, p.model)
"
```
- [ ] Passed
- Date tested: ___________
- Notes:

---

### Step 3 — `providers/anthropic_provider.py` (needs `ANTHROPIC_API_KEY`)
```powershell
python -c "
from agent.providers import get_provider
p = get_provider()
resp = p.complete(
    system='You are a test.',
    messages=[p.user_message('Say hello in one word.')],
    tools=[],
    max_tokens=10,
)
print(resp.text, '| tokens:', resp.usage)
"
```
- [ ] Passed
- Date tested: ___________
- Notes:

---

### Step 4 — `retrieval.py` (no API key needed ← best starting point)
```powershell
python -m agent.retrieval
```
Or target a specific folder:
```powershell
python agent/retrieval.py "D:\AI projects\auto-swe-agent\agent"
```
Expected: indexes repo → prints query results with distance scores → prints call graph
- [ ] Passed
- Date tested: ___________
- Notes:

---

### Step 5 — `sandbox.py` (needs Docker running)
```powershell
# Check Docker first
docker --version

# Built-in self-test
python -m agent.sandbox
```
Expected: container starts → command runs → container destroyed
- [ ] Docker installed & running
- [ ] Passed
- Date tested: ___________
- Notes:

---

### Step 6 — `github.py` (needs `GITHUB_TOKEN`)
```powershell
# Offline self-test (no token needed)
python -m agent.github

# Live test
python -c "
from agent.github import GitHubClient
c = GitHubClient()
issue = c.get_issue('nadeemshabir/auto-swe-agent', 1)
print(issue.title, issue.number)
"
```
- [ ] Offline test passed
- [ ] Live test passed
- Date tested: ___________
- Notes:

---

### Step 7 — `loop.py` (the full system)
```powershell
# Offline dry-run (no API key needed)
python -m agent.loop

# Real task on this repo
python -m agent.loop "Add a docstring to walk_py_files" --workspace "D:\AI projects\auto-swe-agent"
```
Expected offline: all tool functions exercised, trace printed
Expected live: step-by-step ReAct trace, file edits, cost summary
- [ ] Offline dry-run passed
- [ ] Live run passed
- Date tested: ___________
- Notes:

---

## Quick Reference — What Needs What

| Module | Role | API Key? | Docker? | Network? |
|---|---|---|---|---|
| `providers/base.py` | Data types | ❌ | ❌ | ❌ |
| `providers/__init__.py` | Factory | ❌ | ❌ | ❌ |
| `providers/anthropic_provider.py` | Claude adapter | ✅ Anthropic | ❌ | ✅ |
| `providers/gemini_provider.py` | Gemini adapter | ✅ Gemini | ❌ | ✅ |
| `retrieval.py` | Code indexing | ❌ | ❌ | ❌ |
| `sandbox.py` | Safe execution | ❌ | ✅ | ❌ |
| `github.py` | GitHub integration | ✅ GitHub | ❌ | ✅ |
| `loop.py` | ReAct brain | ✅ LLM | ✅ | ✅ |

---

## Issues & Blockers Log

| Date | Module | Issue | Status |
|---|---|---|---|
| | | | |
