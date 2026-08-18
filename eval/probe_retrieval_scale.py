"""Probe 2: does dense-only retrieval hold up on a real-sized corpus?"""
import logging, os, sys
logging.disable(logging.INFO)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from agent import retrieval

# index only the project's own source, not venv
n = 0
for sub in ("agent", "app", "db", "workers", "tests"):
    n += retrieval.index_repo(os.path.join(REPO, sub))
print(f"indexed {n} chunks from the real repo\n")

# Exact-identifier lookups: things a stack trace or an issue would name directly.
EXACT = [
    "_reap_stale_runs",
    "MAX_REVIEW_ROUNDS",
    "_verify_signature",
    "drop_repo",
    "chunk_hash",
]
print("=== exact identifier lookup, k=5 (does the named symbol come back?) ===")
hit = miss = 0
for ident in EXACT:
    hits = retrieval.retrieve(ident, repo=None, k=5)
    names = [h["metadata"].get("name", "?") for h in hits]
    found = any(ident in str(nm) or ident in h["code"][:200]
                for nm, h in zip(names, hits))
    rank = next((i + 1 for i, nm in enumerate(names) if ident in str(nm)), None)
    status = f"rank {rank}" if rank else ("in-body only" if found else "*** MISS ***")
    hit += bool(rank); miss += not rank
    print(f"  {ident:22} -> {status:14}  got: {', '.join(str(x)[:22] for x in names[:3])}")

print(f"\n  exact-name recall@5: {hit}/{len(EXACT)}")

print("\n=== behavioural queries on the real repo, k=5 ===")
BEHAV = [
    ("how are webhook deliveries deduplicated",      "webhook/dedupe logic"),
    ("where is the per-run dollar budget enforced",  "budget enforcement"),
    ("how does the agent recover from a provider outage", "provider fallback"),
]
for q, want in BEHAV:
    hits = retrieval.retrieve(q, repo=None, k=5)
    print(f"\n  q: {q!r}   (want: {want})")
    for h in hits:
        md = h["metadata"]
        print(f"     {h['distance']:.3f}  {os.path.basename(md.get('file','?')):18} {str(md.get('name'))[:38]}")
