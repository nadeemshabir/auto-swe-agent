"""
agent/retrieval.py
Week 1: Codebase understanding engine.

Pipeline:
    walk_py_files  ──▶  parse_file  ──▶  embed_many  ──▶  index_repo (ChromaDB)
                                                                │
                                            retrieve  ◀─────────┘
                                                │
                                                ▼
                                        assemble_context  (token-budgeted)

Also: build_call_graph for who-calls-what static analysis.

Note: token accounting here uses a provider-neutral local estimate
(see count_tokens). This module deliberately does NOT depend on any single
LLM provider's tokenizer — the agent can run on Anthropic Claude or Gemini.
For exact, budget-critical counts the agent loop should call the chosen
provider's own token-counting API. See docs/llm-provider-abstraction.md.
"""

import os
import ast
import json
import math
import hashlib
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Parser
from sentence_transformers import SentenceTransformer
import chromadb


# ───────────────────────────────────────────────────────────────────────────
# Paths anchored to this file, not the current working directory, so the
# store is stable no matter where the process is launched from.
# ───────────────────────────────────────────────────────────────────────────

BASE_DIR  = Path(__file__).resolve().parent
CHROMA_DIR = BASE_DIR / '.chroma'
CACHE_DIR  = BASE_DIR / '.embedding_cache'
CACHE_DIR.mkdir(exist_ok=True)


# ───────────────────────────────────────────────────────────────────────────
# Parsers and models — loaded once, lazily where expensive
# ───────────────────────────────────────────────────────────────────────────

PY_LANGUAGE = Language(tree_sitter_python.language())
PY_PARSER   = Parser(PY_LANGUAGE)

EMBED_MODEL_NAME = 'all-MiniLM-L6-v2'   # max sequence length ~256 tokens

_EMB_MODEL = None
def get_embedder():
    """Load the embedding model on first call, reuse afterwards."""
    global _EMB_MODEL
    if _EMB_MODEL is None:
        print(f"Loading embedding model {EMBED_MODEL_NAME} (first run downloads ~80MB)...")
        _EMB_MODEL = SentenceTransformer(EMBED_MODEL_NAME)
    return _EMB_MODEL

# ChromaDB — persistent local store
CHROMA_CLIENT = chromadb.PersistentClient(path=str(CHROMA_DIR))
COLLECTION    = CHROMA_CLIENT.get_or_create_collection('code')


# ───────────────────────────────────────────────────────────────────────────
# Token accounting — provider-neutral local estimate
# ───────────────────────────────────────────────────────────────────────────

CHARS_PER_TOKEN = 3.5   # conservative for source code across tokenizers

def count_tokens(text: str) -> int:
    """Provider-neutral local token estimate.

    Runs on every context assembly, so it must be cheap and dependency-free.
    It is an estimate — for exact, budget-critical counts the agent loop
    should call the active provider's token-counting API (Anthropic's
    messages.count_tokens, or Gemini's count_tokens).
    """
    return math.ceil(len(text) / CHARS_PER_TOKEN)


# ───────────────────────────────────────────────────────────────────────────
# — walk + parse
# ───────────────────────────────────────────────────────────────────────────

def walk_py_files(root: str):
    """Yield paths of every .py file under `root`, skipping junk folders."""
    SKIP = {'.venv', '__pycache__', '.git', 'node_modules', '.chroma', '.embedding_cache'}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for fname in filenames:
            if fname.endswith('.py'):
                yield os.path.join(dirpath, fname)


def _unwrap(node):
    """A top-level def/class may be wrapped in a `decorated_definition`
    (e.g. @app.route, @property, @celery.task). Return the inner
    function_definition/class_definition node, or the node itself."""
    if node.type == 'decorated_definition':
        return node.child_by_field_name('definition')
    return node


def parse_file(path: str) -> list[dict]:
    """Parse a Python file into semantic chunks.

    Emits one chunk per top-level function, one per class method
    (named ``Class.method``), a header chunk per class (signature +
    docstring, excluding method bodies so it stays under the embedder's
    token limit), and one per import statement. Decorated defs/classes are
    unwrapped so their decorators are included but the kind/name is correct.
    """
    with open(path, 'rb') as f:
        source = f.read()

    tree = PY_PARSER.parse(source)
    chunks = []

    for node in tree.root_node.children:
        real = _unwrap(node)

        if real is None:
            continue

        if real.type == 'function_definition':
            name = real.child_by_field_name('name').text.decode()
            chunks.append({
                'kind':       'function',
                'name':       name,
                'code':       node.text.decode(),      # outer node keeps decorators
                'start_line': node.start_point[0] + 1,
                'end_line':   node.end_point[0] + 1,
                'file':       path,
            })

        elif real.type == 'class_definition':
            class_name = real.child_by_field_name('name').text.decode()
            body = real.child_by_field_name('body')

            # collect (outer, inner) for each method, unwrapping decorators
            methods = []
            if body is not None:
                for child in body.children:
                    inner = _unwrap(child)
                    if inner is not None and inner.type == 'function_definition':
                        methods.append((child, inner))

            # one chunk per method
            for outer, inner in methods:
                mname = inner.child_by_field_name('name').text.decode()
                chunks.append({
                    'kind':       'method',
                    'name':       f"{class_name}.{mname}",
                    'code':       outer.text.decode(),
                    'start_line': outer.start_point[0] + 1,
                    'end_line':   outer.end_point[0] + 1,
                    'file':       path,
                })

            # class "header" chunk: signature + docstring, no method bodies
            if methods:
                header_end_byte = methods[0][0].start_byte
                header_code = source[node.start_byte:header_end_byte].decode('utf-8', 'replace').rstrip()
                header_end_line = max(methods[0][0].start_point[0], node.start_point[0] + 1)
            else:
                header_code = node.text.decode()
                header_end_line = node.end_point[0] + 1

            chunks.append({
                'kind':       'class',
                'name':       class_name,
                'code':       header_code,
                'start_line': node.start_point[0] + 1,
                'end_line':   header_end_line,
                'file':       path,
            })

        elif node.type in ('import_statement', 'import_from_statement'):
            chunks.append({
                'kind':       'import',
                'name':       node.text.decode().strip(),
                'code':       node.text.decode(),
                'start_line': node.start_point[0] + 1,
                'end_line':   node.end_point[0] + 1,
                'file':       path,
            })

    return chunks


# ───────────────────────────────────────────────────────────────────────────
#— embed with disk cache (batched)
# ───────────────────────────────────────────────────────────────────────────

def chunk_hash(code: str) -> str:
    """SHA1 of (embedder, chunk text) — the cache key. Including the model
    name means switching embedders never returns stale vectors."""
    return hashlib.sha1(f"{EMBED_MODEL_NAME}\0{code}".encode()).hexdigest()


def embed_many(codes: list[str]) -> list[list[float]]:
    """Return embedding vectors for a list of chunk texts.

    Cached on disk by content hash; cache misses are encoded in a single
    batch (much faster than one-at-a-time)."""
    vectors: list[list[float] | None] = [None] * len(codes)
    misses = []  # (index, code, cache_file)

    for i, code in enumerate(codes):
        cache_file = CACHE_DIR / f"{chunk_hash(code)}.json"
        if cache_file.exists():
            with open(cache_file) as f:
                vectors[i] = json.load(f)
        else:
            misses.append((i, code, cache_file))

    if misses:
        encoded = get_embedder().encode([c for _, c, _ in misses])
        for (i, _code, cache_file), vec in zip(misses, encoded):
            v = vec.tolist()
            vectors[i] = v
            with open(cache_file, 'w') as f:
                json.dump(v, f)

    return vectors  # type: ignore[return-value]


def embed_chunk(code: str) -> list[float]:
    """Embed a single chunk (convenience wrapper over embed_many)."""
    return embed_many([code])[0]


# ───────────────────────────────────────────────────────────────────────────
#  — index into ChromaDB
# ───────────────────────────────────────────────────────────────────────────

def index_repo(root: str):
    """Walk repo, parse, embed, store every chunk in ChromaDB.

    Re-indexing is safe: each file's existing chunks are deleted before its
    fresh chunks are written, so edits (which shift line numbers and thus
    chunk IDs) don't leave orphaned duplicates behind."""
    n_indexed = 0

    for fpath in walk_py_files(root):
        chunks = parse_file(fpath)
        if not chunks:
            continue

        # drop any stale chunks from a previous index of this file
        COLLECTION.delete(where={'file': fpath})

        vectors = embed_many([c['code'] for c in chunks])
        ids = [f"{c['file']}:{c['start_line']}-{c['end_line']}:{c['kind']}" for c in chunks]

        COLLECTION.upsert(
            ids        = ids,
            embeddings = vectors,
            documents  = [c['code'] for c in chunks],
            metadatas  = [{
                'file':       c['file'],
                'kind':       c['kind'],
                'name':       c['name'],
                'start_line': c['start_line'],
                'end_line':   c['end_line'],
            } for c in chunks],
        )
        n_indexed += len(chunks)

    return n_indexed


# ───────────────────────────────────────────────────────────────────────────
#  — retrieve top-K relevant chunks for a query
# ───────────────────────────────────────────────────────────────────────────

def retrieve(query: str, k: int = 5) -> list[dict]:
    """Return top-K chunks most semantically similar to the query."""
    qvec = get_embedder().encode(query).tolist()
    res  = COLLECTION.query(query_embeddings=[qvec], n_results=k)

    # Chroma returns parallel lists; zip into per-result dicts
    out = []
    for i in range(len(res['ids'][0])):
        out.append({
            'id':       res['ids'][0][i],
            'code':     res['documents'][0][i],
            'metadata': res['metadatas'][0][i],
            'distance': res['distances'][0][i],   # smaller = more similar
        })
    return out


# ───────────────────────────────────────────────────────────────────────────
#— call graph (who calls what) using Python's built-in ast module
# ───────────────────────────────────────────────────────────────────────────

def build_call_graph(root: str) -> dict[str, set[str]]:
    """
    Build a {caller_function: {called_function, ...}} map across the repo.
    Uses Python's `ast` module — static, no execution.
    """
    graph: dict[str, set[str]] = {}

    for fpath in walk_py_files(root):
        try:
            with open(fpath, encoding='utf-8') as f:
                tree = ast.parse(f.read())
        except (SyntaxError, UnicodeDecodeError):
            continue  # skip unparseable files

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                caller = f"{fpath}::{node.name}"
                graph.setdefault(caller, set())

                # find every Call(...) inside this function's body
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        # callee could be `foo()` (Name) or `obj.foo()` (Attribute)
                        if isinstance(inner.func, ast.Name):
                            graph[caller].add(inner.func.id)
                        elif isinstance(inner.func, ast.Attribute):
                            graph[caller].add(inner.func.attr)
    return graph


# ───────────────────────────────────────────────────────────────────────────
# — context assembler: pack top-K chunks under a token budget
# ───────────────────────────────────────────────────────────────────────────

def assemble_context(query: str, k: int = 10, token_budget: int = 4000) -> str:
    """
    Retrieve top-K chunks for `query`, pack as many as fit under `token_budget`.
    Returns one string ready to drop into an LLM prompt.
    """
    chunks = retrieve(query, k=k)

    out_parts = []
    used = 0

    for c in chunks:
        header = f"\n# {c['metadata']['file']} (lines {c['metadata']['start_line']}-{c['metadata']['end_line']})\n"
        block  = header + c['code']
        cost   = count_tokens(block)

        if used + cost > token_budget:
            break
        out_parts.append(block)
        used += cost

    return '\n'.join(out_parts)


# ───────────────────────────────────────────────────────────────────────────
# Smoke test — runs only when this file is executed directly
#   usage: python retrieval.py [path-to-repo]
# ───────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    test_repo = sys.argv[1] if len(sys.argv) > 1 else str(BASE_DIR.parent)

    print(f"\n=== INDEXING {test_repo} ===")
    n = index_repo(test_repo)
    print(f"Indexed {n} chunks.\n")

    print("=== RETRIEVAL TEST ===")
    queries = [
        "function that adds two numbers",
        "how to read a file from disk",
        "class for arithmetic operations",
    ]
    for q in queries:
        print(f"\n🔍 Query: {q!r}")
        for r in retrieve(q, k=3):
            md = r['metadata']
            fname = os.path.basename(md['file'])
            print(f"   [{md['kind']:8}] {md['name'][:40]:40}  ({fname}:{md['start_line']})  dist={r['distance']:.3f}")

    print("\n=== CALL GRAPH ===")
    graph = build_call_graph(test_repo)
    for caller, callees in graph.items():
        short = os.path.basename(caller)
        print(f"   {short} → {sorted(callees)}")

    print("\n=== CONTEXT ASSEMBLER (under 500 tokens) ===")
    ctx = assemble_context("arithmetic operations", k=5, token_budget=500)
    print(ctx)
    print(f"\n(Total ≈ {count_tokens(ctx)} tokens)")
