# Future Goals

What this project should become next, and why — ordered by value per day of work.

Written 2026-08-18. Unlike `plan2.md` (the architectural source of truth) this
doc is about **what isn't built yet**. Where a claim here is measurable, it was
measured; the probe scripts are in `eval/` and the numbers below are their real
output, not estimates.

---

## 0. The one-paragraph version

The plumbing is solid — gateway, queue, persistence, security, deployment, and
the three-agent pipeline all work and are tested. **Retrieval is the weakest
layer**, and it fails in ways that are silent, which is the worst property a
retrieval system can have. Nothing measures quality end to end, so every
improvement below is currently an opinion. Fix that first.

---

## 1. Build the eval harness — do this before anything else

`eval/` holds two diagnostic probes and one fixture today. That is not an eval
harness; it is the seed of one.

**Why it comes first:** every item in this document is a hypothesis until there
is a number attached to it. Without eval you cannot tell a prompt improvement
from a regression, you cannot justify a model change, and you will tune the
system on vibes and remember the runs that flattered it.

**What it needs:**

| Piece | Detail |
|---|---|
| Fixture repos | A handful of small repos with known bugs and known correct fixes |
| Retrieval ground truth | For each issue, which files/chunks *should* have been retrieved |
| `recall@k` | Did the right code reach the prompt at all? Isolates retrieval from reasoning. |
| Fix rate | Did the pipeline produce a diff that passes the hidden tests? |
| Cost per solved issue | The number that makes model/budget choices decidable |

Split those two metrics deliberately. When a run fails you need to know whether
the agent reasoned badly or was never shown the relevant code — those have
opposite fixes, and today they are indistinguishable.

**Effort:** ~1 day for a first useful version. Highest leverage in the repo.

---

## 2. Retrieval

This section is evidence-based. Reproduce with:

```bash
python eval/probe_retrieval_naming.py    # 6-chunk fixture, controlled naming cases
python eval/probe_retrieval_scale.py     # 445 chunks from this repo
```

### 2.1 What already works — don't "fix" this

A common worry is that **badly named functions defeat semantic search**: if a
function is called `zorblat` but paginates a list, how would an embedding know?

It largely does know, because the name is not what gets embedded.
[`index_repo()`](../agent/retrieval.py) embeds `c['code']` — the **whole chunk**,
body and docstring included. Measured on a fixture built for exactly this case:

| Query | Target | Result |
|---|---|---|
| `"split a list into fixed size pages"` | `zorblat` (meaningless name) | **rank 1**, dist 1.454 |
| `"zorblat"` (exact name) | `zorblat` | **rank 1**, dist 0.933 |
| `"send onboarding email to new user"` | `send_welcome_email` (control) | **rank 1**, dist 0.541 |

Exact-identifier lookup on the real 445-chunk repo: **recall@5 = 4/5, all at rank
1** (`_reap_stale_runs`, `_verify_signature`, `drop_repo`, `chunk_hash`).

> **Correction to an earlier claim.** A previous draft of the README asserted
> that dense embeddings fail on exact identifiers and that BM25 was therefore
> urgently needed. The measurement above does not support that at this corpus
> size — identifiers are embedded *inside* their chunk text, so they match.
> BM25/hybrid search is still worth doing eventually, but it is **not** the
> problem, and it should not be prioritised over the four real defects below.

### 2.2 Module-level constants are never indexed at all

The one exact-name **miss** was `MAX_REVIEW_ROUNDS`. Not a ranking failure — the
chunk does not exist. [`parse_file()`](../agent/retrieval.py) only emits chunks
for `function_definition`, `class_definition`, `import_statement` and
`import_from_statement`. Verified:

```
chunk kinds for workers/tasks.py: {'import': 8, 'function': 8}
MAX_REVIEW_ROUNDS indexed as a chunk? False
any constant-like chunk?  []
```

Every config constant, module-level lookup table, dispatch dict, regex, and
registry — in every repo the agent will ever see — is invisible to search. No
embedding or reranking improvement can recover this.

**Fix:** emit a chunk for top-level `expression_statement`/assignment nodes,
ideally grouping consecutive constants into one chunk so the index doesn't fill
with one-liners.

**Effort:** small — a branch in the existing parser loop. Do it first.

### 2.3 Oversized chunks are silently truncated

`all-MiniLM-L6-v2` has a ~256-token limit. Longer chunks are embedded on their
first ~256 tokens and the rest is invisible to search **with no warning**.

This is where bad naming genuinely does bite, and the probe demonstrates it. The
fixture's `frobnicate_wobble` is 397 tokens; it deduplicates user records by
email, but only says so ~40 lines in, past the cutoff:

```
q: "deduplicate user records by email address"
   1.621  send_welcome_email     <-- unrelated function wins
   1.625  frobnicate_wobble      <-- the correct answer, rank 2
```

An unrelated email function outranked it on its own core purpose. So the honest
form of the naming concern is: **a bad name only hurts when the meaning is also
outside the embedded window.** Combined, they fail.

**Fix options:** split oversized chunks into overlapping windows; or embed a
generated signature+docstring summary for anything over the limit; or at minimum
log a warning at index time so the failure stops being silent.

### 2.4 There is no relevance floor

`retrieve()` always returns `k` results. Query a fixture containing no XML code
at all with `"parse XML configuration from disk"` and it returns three chunks at
confident-looking distances. `assemble_context()` then packs that noise into the
prompt indistinguishably from a real hit.

**Fix:** a distance cutoff, so "I found nothing relevant" becomes an expressible
answer. A model told *nothing matched* will go read files; a model handed
plausible-looking irrelevant code will reason about the wrong code.

### 2.5 Tests crowd out the implementation

On the real repo, `"how does the agent recover from a provider outage"` returns
**4 test functions in the top 5**, with the actual implementation
`ReActAgent._switch_to_fallback` down at rank 4. Tests describe behaviour in
natural language, so they embed beautifully — and the agent's job is to edit the
implementation.

**Fix:** down-weight test files at query time, or retrieve them into a separate
labelled bucket so the Coder knows which is which.

### 2.6 Python only

`walk_py_files()` filters for `.py` and only `tree_sitter_python` is loaded.
Point this at a TypeScript, Go, or Java repo and it indexes **zero chunks** — the
agent then works blind from `list_dir` and guesses, with no error raised.

**Fix:** a language registry mapping extension → tree-sitter grammar → the node
types that count as chunkable. The rest of the pipeline is language-agnostic
already. This is the single biggest expansion of what the project can be pointed
at.

### 2.7 The call graph is built but never used

`build_call_graph()` works and is called from nothing but the `__main__` smoke
test. The prize: after the dense hits return, expand one hop along callers and
callees, so the agent sees *the code its change will break*, not only the code
that matched the words. Half of this is already written.

### 2.8 Lower priority

- **Reranking.** Retrieve 30 cheaply, cross-encode, keep 8. Standard second stage,
  usually a large precision win. Worth doing *after* 2.2–2.5, since reranking a
  candidate set that structurally excludes constants doesn't help.
- **Query construction.** The Planner passes raw issue text as the query
  ([`_retrieve_context`](../agent/planner.py)). Issues are user-voice ("login is
  broken on mobile"); code is implementer-voice (`validate_session_token`). HyDE
  or multi-query fan-out bridges that.
- **Hybrid BM25.** See the correction in 2.1 — real, but not urgent.

---

## 3. The agent loop

**No literal search tool.** The toolbox is `retrieve_context`, `read_file`,
`edit_file`, `run_tests`, `list_dir`. There is no `grep`. If an issue quotes an
identifier from a stack trace, the agent has no exact-match escape hatch — it
must semantic-search or read files one at a time. This is the cheapest, most
reliable tool in any coding agent and its absence is felt most exactly when
retrieval fails, which is the worst possible time. **Highest-value loop change.**

**Also missing:** `run_command` (let it run the linter and type-checker it keeps
trying to invoke) and `git_diff` (let it review its own work before the Reviewer
does).

**`edit_file` is brittle by design.** It requires an exact, unique `old_string`,
so one wrong space burns a whole step on a `ToolError`. Indentation-tolerant
matching or a line-range replacement mode converts wasted steps into progress.

**No history compaction.** Context grows every step until `max_steps` stops it.
Summarising older turns buys more real work inside the same budget.

**Serial investigation.** "Check these four files" is four sequential
round-trips. Read-only sub-agents fanning out would cut exploration wall-clock.

---

## 4. Features worth building

**CI failure recovery** *(M16 in plan2.md — the highest-value new feature)*. The
agent's PR fails CI → `check_suite` webhook → fetch the failing log → re-enter
the Coder loop with the failure as context → push a fix commit → comment
explaining it. Today the agent's responsibility ends at "PR opened", which is
exactly where a real engineer's begins.

**Respond to human review comments.** A reviewer writes "this doesn't handle
None" and the agent pushes a fix. The Reviewer agent already exists; this points
the same machinery at `pull_request_review_comment` and closes the loop with
actual humans.

**A dashboard.** `frontend/` is empty, and `/runs/{id}/steps` already returns
everything a UI needs: every step, tool call, cost, timing, and verdict. A
single-page trace viewer turns "50 integration tests pass" into something you can
*show* someone.

**Repo-wide and scheduled work.** Nothing requires an issue as the trigger. "Fix
every TODO in this directory", weekly dependency-bump PRs, or a scheduled
coverage-gap pass are the same machinery with a different front door.

**Difficulty-aware model routing.** Model choice is static config. A cheap
classifier on the issue text — typo fix vs. architectural change — routes trivial
issues cheap and hard ones expensive, strictly better than one fixed compromise.

**A pre-PR self-check gate.** Before pushing: full suite, linter, and a diff
sanity check — did it edit files the plan never mentioned? Did it delete tests to
make them pass? Cheap insurance against the failure modes that erode trust
fastest.

---

## 5. Infrastructure

- **Turn the sandbox back on.** Built and hardened; off because of VM
  constraints, not code. A config flip plus a memory check.
- **Observability.** `monitoring/` is empty. Start with *one real alert* — "a run
  failed", "a run cost > $1" — before any dashboard exists.
- **Per-run Kubernetes Jobs.** The natural upgrade over sharing a Docker socket:
  each run gets a genuinely isolated execution context. Its own project, never a
  blocker.
- **CI/CD.** Lint + pytest → build images → deploy. Worth doing now the suite is real.
- **Papercut:** the README's quick start once said `cp .env.example .env`, but no
  such file exists — and `.gitignore` matches `.env.*`, so it would be ignored
  anyway. Needs a committed template plus a `!.env.example` negation rule.

---

## Suggested order

| # | Work | Why here |
|---|---|---|
| 1 | Eval harness (§1) | Everything else becomes measurable |
| 2 | Index module constants (§2.2) | Structural gap; small fix |
| 3 | Relevance floor (§2.4) | Makes failure visible instead of silent |
| 4 | `grep` tool (§3) | The escape hatch for when retrieval misses |
| 5 | Chunk truncation (§2.3) | The real form of the bad-naming problem |
| 6 | Test down-weighting (§2.5) | Prompt budget currently spent on the wrong files |
| 7 | Multi-language indexing (§2.6) | Biggest expansion of scope |
| 8 | CI failure recovery (§4) | Highest-value new capability |

Items 2–6 are each small. Together they address every measured defect in the
retrieval layer, and none of them requires a new dependency.
