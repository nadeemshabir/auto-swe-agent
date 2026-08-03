# Codebase Retrieval Engine (`retrieval.py`)

This document explains the architecture and workflow of the `agent/retrieval.py` module. This module acts as the "codebase understanding engine" for the autonomous SWE agent, allowing it to index, search, and assemble context from Python codebases.

## Core Pipeline

The retrieval pipeline consists of four main steps:

1. **Walk**: Recursively traverse the target project to find all Python files.
2. **Parse**: Convert Python files into an Abstract Syntax Tree (AST) to extract logical chunks (functions, classes, methods).
3. **Embed**: Translate the text of these chunks into dense vector representations.
4. **Index/Retrieve**: Store vectors in a database for fast semantic searching.

---

### 1. File Discovery (`walk_py_files`)

The engine traverses the given root directory using Python's `os.walk`. It ignores common "junk" directories like `.venv`, `__pycache__`, `.git`, `.chroma`, and `.embedding_cache` to ensure it only indexes actual source code. It yields the absolute paths of all discovered `.py` files.

### 2. Smart Code Parsing (`parse_file` and `_unwrap`)

Instead of splitting code blindly by line counts, the engine uses **Tree-sitter** to understand the syntax of Python code. This allows it to extract semantic chunks:
- **Standalone Functions**: Extracted cleanly.
- **Classes**: Large classes are not stored as massive blobs (which would overflow the AI's token limits). Instead, they are broken down:
  - Every method inside the class is extracted as its own chunk (e.g., `Class.method`).
  - A "header" chunk is created containing just the class name, signature, and docstring.
- **Decorators**: A custom `_unwrap()` function ensures that functions with decorators (like `@app.route` or `@property`) are not skipped, and the decorator code is kept intact alongside the function.
- **Imports**: Import statements are saved as their own chunks.

### 3. Batched Embedding & Caching (`embed_many`)

The engine uses `sentence-transformers` (`all-MiniLM-L6-v2`) to turn code chunks into mathematical vectors.
Because calculating embeddings is computationally expensive, the engine employs a robust caching mechanism:
- A local `.embedding_cache` directory saves the vector for each chunk.
- The cache key is generated using a SHA-1 hash of both the code's text *and* the embedding model's name. 
- During indexing, if a chunk's text hasn't changed, the engine instantly loads its vector from the cache. Any uncached chunks are processed in a single fast batch.

### 4. Vector Database (`index_repo` & `retrieve`)

Chunks are held in an **in-memory ChromaDB** collection (`chromadb.EphemeralClient()`), built fresh for each run and discarded with the process. Nothing is written to disk.

**Why in-memory** (plan2.md §16 D19, §22 F9): every run clones into a unique workspace and indexes under that path, then drops the index at cleanup — so an on-disk store only ever held data that was about to be deleted. Persisting it added no reuse and one real failure mode: a worker that died before cleanup left orphaned vectors on disk forever. It also meant a persistent volume to provision in the cloud, and file contention between concurrent workers in the same container. In memory, a dead process simply takes its index with it.

What actually makes re-indexing cheap is the **embedding cache** (`agent/.embedding_cache`, keyed by content hash) — that *is* persisted, so unchanged files skip the model on every subsequent run.

- **Indexing (`index_repo`)**: When a file is indexed, the engine first *deletes* any existing chunks belonging to that file. It then inserts the fresh chunks, so line-number shifts from editing files don't leave stale duplicates. Each chunk saves metadata including its file path, line numbers, and the repository it belongs to.
- **Retrieval (`retrieve`)**: Given a natural language query, the engine embeds the query and searches for the closest matching code chunks. The search is filtered by `repo` to prevent cross-pollution between projects sharing the collection.
- **Cleanup (`drop_repo`)**: Deletes every chunk indexed under a path. Still required despite the in-memory store, because a Celery worker process is long-lived and handles many runs — this bounds the collection to one run rather than one process lifetime. The orchestrator calls it during cleanup.

> **Consequence for CLI use:** an index only lives as long as the process that built it, so `python -m agent.loop` must be given `--auto-index`. The orchestrator always indexes after cloning, so the webhook pipeline is unaffected.

---

### Additional Features

#### Context Assembly (`assemble_context`)
The LLM has a strict reading limit ("token budget"). This function takes a query, retrieves the most relevant chunks, and packs them into a single text document. It uses a lightweight `count_tokens` estimator to ensure the compiled document never exceeds the specified token budget. 

#### Static Call Graph (`build_call_graph`)
Using Python's built-in `ast` module, this function analyzes the entire repository to build a map of which functions call which other functions. It correctly maps both standard `def` and modern `async def` functions, providing the AI agent with crucial relational context for the codebase.
