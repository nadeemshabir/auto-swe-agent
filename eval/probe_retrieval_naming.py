"""Probe: how does dense-only retrieval handle badly-named functions?"""
import logging, os, sys
logging.disable(logging.INFO)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from agent import retrieval

FIX = os.path.join(HERE, "fixtures", "badly_named")

n = retrieval.index_repo(FIX)
print(f"indexed {n} chunks from fixture\n")

# How many tokens does each chunk carry, vs the embedder's ~256 limit?
print("=== chunk sizes vs embedder limit (256 tokens) ===")
for c in retrieval.parse_file(os.path.join(FIX, "core.py")):
    if c["kind"] in ("function", "method"):
        t = retrieval.count_tokens(c["code"])
        flag = "  <-- TRUNCATED at embed time" if t > 256 else ""
        print(f"  {c['name']:28} ~{t:4} tokens{flag}")

QUERIES = [
    ("behaviour, target is weird-named", "split a list into fixed size pages",        "zorblat"),
    ("EXACT NAME of weird function",     "zorblat",                                   "zorblat"),
    ("behaviour, long weird-named fn",   "deduplicate user records by email address", "frobnicate_wobble"),
    ("EXACT NAME, long weird fn",        "frobnicate_wobble",                         "frobnicate_wobble"),
    ("control: well-named function",     "send onboarding email to new user",         "send_welcome_email"),
    ("nothing relevant exists",          "parse XML configuration from disk",         None),
]

print("\n=== retrieval results (k=3) ===")
for label, q, want in QUERIES:
    hits = retrieval.retrieve(q, repo=FIX, k=3)
    names = [(h["metadata"].get("name"), h["distance"]) for h in hits]
    if want is None:
        verdict = "returns junk anyway"
    else:
        rank = next((i + 1 for i, (nm, _) in enumerate(names) if nm == want), None)
        verdict = f"FOUND at rank {rank}" if rank else "*** NOT FOUND in top 3 ***"
    print(f"\n[{label}]")
    print(f"  query : {q!r}")
    print(f"  want  : {want}")
    print(f"  got   : " + ", ".join(f"{nm}({d:.3f})" for nm, d in names))
    print(f"  ->      {verdict}")
