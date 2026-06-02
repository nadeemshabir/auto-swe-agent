"""
agent/retrieval.py
Week 1: Codebase understanding engine.

Pipeline:
    walk_py_files  ──▶  parse_file  ──▶  embed_chunk  ──▶  index_repo (ChromaDB)
                                                                │
                                            retrieve  ◀─────────┘
                                                │
                                                ▼
                                        assemble_context  (token-budgeted)

Also: build_call_graph for who-calls-what static analysis.
"""

import os
import ast
import json
import hashlib
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Parser
from sentence_transformers import SentenceTransformer
import chromadb
import tiktoken


# ───────────────────────────────────────────────────────────────────────────
# Parsers and models — loaded once, lazily where expensive
# ───────────────────────────────────────────────────────────────────────────

PY_LANGUAGE = Language(tree_sitter_python.language())
PY_PARSER   = Parser(PY_LANGUAGE)

_EMB_MODEL = None
def get_embedder():
    """Load the embedding model on first call, reuse afterwards."""
    global _EMB_MODEL
    if _EMB_MODEL is None:
        print("Loading embedding model (first run downloads ~80MB)...")
        _EMB_MODEL = SentenceTransformer('all-MiniLM-L6-v2')
    return _EMB_MODEL

# Tokenizer for context-budget accounting (use OpenAI's cl100k for OpenAI;
# for Anthropic we estimate conservatively at ~4 chars/token elsewhere)
TOKENIZER = tiktoken.get_encoding('cl100k_base')

# ChromaDB — persistent local store
CHROMA_CLIENT = chromadb.PersistentClient(path='.chroma')
COLLECTION    = CHROMA_CLIENT.get_or_create_collection('code')

# Embedding cache — skip work if chunk text hasn't changed
CACHE_DIR = Path('.embedding_cache')
CACHE_DIR.mkdir(exist_ok=True)


# ───────────────────────────────────────────────────────────────────────────
# Day 1 — walk + parse
# ───────────────────────────────────────────────────────────────────────────

def walk_py_files(root: str):
    """Yield paths of every .py file under `root`, skipping junk folders."""
    SKIP = {'.venv', '__pycache__', '.git', 'node_modules', '.chroma', '.embedding_cache'}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for fname in filenames:
            if fname.endswith('.py'):
                yield os.path.join(dirpath, fname)


def parse_file(path: str) -> list[dict]:
    """Parse a Python file and extract top-level functions, classes, imports."""
    with open(path, 'rb') as f:
        source = f.read()
    
    tree = PY_PARSER.parse(source)
    chunks = []
    
    for node in tree.root_node.children:
        kind, name = None, None
        
        if node.type == 'function_definition':
            kind = 'function'
            name = node.child_by_field_name('name').text.decode()
        elif node.type == 'class_definition':
            kind = 'class'
            name = node.child_by_field_name('name').text.decode()
        elif node.type in ('import_statement', 'import_from_statement'):
            kind = 'import'
            name = node.text.decode().strip()
        
        if kind:
            chunks.append({
                'kind':       kind,
                'name':       name,
                'code':       node.text.decode(),
                'start_line': node.start_point[0] + 1,
                'end_line':   node.end_point[0] + 1,
                'file':       path,
            })
    return chunks


# ───────────────────────────────────────────────────────────────────────────
# Day 2 — embed with disk cache
# ───────────────────────────────────────────────────────────────────────────

def chunk_hash(code: str) -> str:
    """SHA1 of chunk text — used as the cache key."""
    return hashlib.sha1(code.encode()).hexdigest()


def embed_chunk(code: str) -> list[float]:
    """Return embedding vector. Cached on disk by content hash."""
    key = chunk_hash(code)
    cache_file = CACHE_DIR / f"{key}.json"
    
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)
    
    vector = get_embedder().encode(code).tolist()
    with open(cache_file, 'w') as f:
        json.dump(vector, f)
    return vector


# ───────────────────────────────────────────────────────────────────────────
# Day 3 — index into ChromaDB
# ───────────────────────────────────────────────────────────────────────────

def index_repo(root: str):
    """Walk repo, parse, embed, store every chunk in ChromaDB."""
    n_indexed = 0
    
    for fpath in walk_py_files(root):
        for c in parse_file(fpath):
            chunk_id = f"{c['file']}:{c['start_line']}-{c['end_line']}:{c['kind']}"
            vector   = embed_chunk(c['code'])
            
            COLLECTION.upsert(                       # upsert = insert OR update
                ids        = [chunk_id],
                embeddings = [vector],
                documents  = [c['code']],
                metadatas  = [{
                    'file':       c['file'],
                    'kind':       c['kind'],
                    'name':       c['name'],
                    'start_line': c['start_line'],
                    'end_line':   c['end_line'],
                }],
            )
            n_indexed += 1
    
    return n_indexed


# ───────────────────────────────────────────────────────────────────────────
# Day 4 — retrieve top-K relevant chunks for a query
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
# Day 5 — call graph (who calls what) using Python's built-in ast module
# ───────────────────────────────────────────────────────────────────────────

def build_call_graph(root: str) -> dict[str, set[str]]:
    """
    Build a {caller_function: {called_function, ...}} map across the repo.
    Uses Python's `ast` module — static, no execution.
    """
    graph: dict[str, set[str]] = {}
    
    for fpath in walk_py_files(root):
        try:
            with open(fpath) as f:
                tree = ast.parse(f.read())
        except SyntaxError:
            continue  # skip unparseable files
        
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
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
# Day 5 — context assembler: pack top-K chunks under a token budget
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
        cost   = len(TOKENIZER.encode(block))
        
        if used + cost > token_budget:
            break
        out_parts.append(block)
        used += cost
    
    return '\n'.join(out_parts)


# ───────────────────────────────────────────────────────────────────────────
# Smoke test — runs only when this file is executed directly
# ───────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    test_repo = '/mnt/d/AI projects/test-repo'
    
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
            print(f"   [{md['kind']:8}] {md['name'][:40]:40}  ({md['file'].split('/')[-1]}:{md['start_line']})  dist={r['distance']:.3f}")
    
    print("\n=== CALL GRAPH ===") 
    graph = build_call_graph(test_repo)
    for caller, callees in graph.items():
        short = caller.split('/')[-1]
        print(f"   {short} → {sorted(callees)}")
    
    print("\n=== CONTEXT ASSEMBLER (under 500 tokens) ===")
    ctx = assemble_context("arithmetic operations", k=5, token_budget=500)
    print(ctx)
    print(f"\n(Total: {len(TOKENIZER.encode(ctx))} tokens)")