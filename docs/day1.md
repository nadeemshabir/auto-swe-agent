# Day 1 — Project Setup + Tree-sitter Parser

**Date:** Tuesday, June 2, 2026
**Week:** 1 (Codebase Understanding Engine)
**Status:** ✅ Complete

---

## What we accomplished today

1. ✅ Installed and configured **WSL2 + Ubuntu 24.04** on Windows 11
2. ✅ Set up **Docker Desktop** with WSL integration
3. ✅ Created **Python virtualenv** on D drive
4. ✅ Built the complete **project folder scaffold**
5. ✅ Created a **test repo** with sample Python files
6. ✅ Wrote the first piece of `agent/retrieval.py` — a **tree-sitter parser** that extracts functions, classes, and imports from any Python file

---

## Environment setup (one-time)

### Tools installed
| Tool | Purpose | Where |
|------|---------|-------|
| WSL2 | Run Linux inside Windows | Windows |
| Ubuntu 24.04 | Linux operating system | WSL2 |
| Python 3.12 | Programming language | Ubuntu |
| pip | Python package installer | Ubuntu |
| Docker Desktop | Sandbox for AI-generated code | Windows + WSL |
| Antigravity IDE | Code editor with VS Code base | Windows |

### Key concepts learned
- **WSL2** = a Linux computer running inside Windows
- **`/mnt/d/`** = how Ubuntu sees your Windows D drive
- **virtualenv (`.venv`)** = isolated Python environment per project (no library conflicts)
- **`source .venv/bin/activate`** = "enter" the project environment
- **Docker container** = isolated box where untrusted code can run safely

---

## Folder structure

```
D:\AI projects\
│
├── auto-swe-agent\                  ← MAIN PROJECT
│   ├── .venv\                       (virtualenv — gitignored)
│   ├── .git\                        (git repo)
│   ├── .github\
│   │   └── workflows\               (CI/CD configs — Week 6)
│   ├── agent\
│   │   ├── retrieval.py             ✅ Day 1 work lives here
│   │   ├── loop.py                  (Week 2 — empty)
│   │   ├── sandbox.py               (Week 3 — empty)
│   │   └── github.py                (Week 4 — empty)
│   ├── app\
│   │   └── main.py                  (Week 5 — empty)
│   ├── workers\
│   │   └── tasks.py                 (Week 5 — empty)
│   ├── eval\                        (Week 8)
│   ├── frontend\                    (Week 7)
│   ├── helm\                        (Week 6)
│   ├── k8s\                         (Week 6)
│   ├── monitoring\                  (Week 7)
│   ├── .env                         (API keys — gitignored)
│   ├── .gitignore                   (ignores .venv, .env, .chroma, __pycache__)
│   └── requirements.txt             (library shopping list)
│
└── test-repo\                       ← THROWAWAY TARGET REPO
    ├── .git\
    ├── calculator.py                (sample file with functions + class)
    └── utils.py                     (sample file with imports + functions)
```

---

## Libraries installed (Week 1)

In `requirements.txt`:
```
tree-sitter>=0.21
tree-sitter-python
sentence-transformers
chromadb
tiktoken
```

| Library | What it does |
|---------|-------------|
| `tree-sitter` | Parses code into a tree structure (functions, classes, etc.) |
| `tree-sitter-python` | Python grammar for tree-sitter |
| `sentence-transformers` | Converts text → vectors (Day 2) |
| `chromadb` | Vector database for fast search (Day 3) |
| `tiktoken` | Counts tokens for LLM context budgeting (Day 5) |

---

## Code written today — `agent/retrieval.py`

### What it does
Given a folder path, it:
1. Walks through every subfolder (skipping junk like `.venv`, `.git`, `__pycache__`)
2. Finds all `.py` files
3. Parses each one with tree-sitter
4. Extracts every top-level function, class, and import with line numbers

### Functions built
- `walk_py_files(root)` — generator that yields paths to all `.py` files in a folder
- `parse_file(path)` — parses one Python file and returns a list of symbol dicts

### Output format
Each parsed symbol looks like:
```python
{
    'kind':       'function' | 'class' | 'import',
    'name':       'add',
    'code':       'def add(a, b):\n    return a + b',
    'start_line': 1,
    'end_line':   2,
}
```

### Verified output
```
📄 /mnt/d/AI projects/test-repo/calculator.py
   [function] add                          lines 1-2
   [function] subtract                     lines 4-5
   [class   ] Calculator                   lines 7-14

📄 /mnt/d/AI projects/test-repo/utils.py
   [import  ] import os                    lines 1-1
   [import  ] import json                  lines 2-2
   [import  ] from calculator import ...   lines 3-3
   [function] read_file                    lines 5-7
   [function] write_json                   lines 9-11
```

---

## Issues hit and how we fixed them

| Issue | Fix |
|-------|-----|
| Couldn't download Ubuntu via curl (slow + cert errors) | Installed via Microsoft Store instead |
| Python 3.11 not in Ubuntu 24.04 repos | Used Python 3.12 (newer, fine) |
| pip not installed | `sudo apt install python3-pip python3-venv -y` |
| Docker "command not found" in WSL | Enabled WSL Integration in Docker Desktop settings |
| `tree-sitter-languages` incompatible with new tree-sitter | Switched to `tree-sitter-python` directly |
| Terminal was PowerShell instead of Ubuntu | Set default profile to Ubuntu (WSL) in Antigravity |

---

## Daily workflow (to remember)

Every time you sit down to code:

```bash
# 1. Open Antigravity IDE
# 2. Open terminal (Ctrl + `)
# 3. Confirm it's Ubuntu (WSL), not PowerShell
# 4. Navigate to project + activate venv:
cd "/mnt/d/AI projects/auto-swe-agent"
source .venv/bin/activate
# 5. You should see (.venv) at start of prompt — now you can code
```

---

## What's next — Day 2

**Chunking + embedding.**

Right now we extract symbols as raw text. But text alone can't be searched by meaning. Tomorrow:

- **Chunk** files smartly (by function/class, not by fixed line counts) so each piece fits in MiniLM's 256-token window
- **Embed** each chunk with `all-MiniLM-L6-v2` — converts code into a 384-number vector that captures meaning
- **Cache** embeddings by content hash so we don't re-embed unchanged files (roadmap warns this is critical)

End goal of Day 2: every chunk in our test repo has a vector ready to be stored in ChromaDB on Day 3.

---

## Git checkpoint

Today's commits on `master`:
- `Week 0: project scaffold`
- `Week 0: add requirements.txt`
- `Week 1 Day 1: tree-sitter parser extracts functions/classes/imports`