# The ReAct Agent Loop (`loop.py`)

This document explains the architecture and workflow of the `agent/loop.py` module, together with the pluggable `agent/providers/` package it depends on. This module is the **brain** of the autonomous SWE agent: it takes a task (like a GitHub issue) and a workspace (a checked-out repo), then drives an LLM through a Reason → Act → Observe cycle until the task is done — making code edits and running tests along the way.

## Core Loop

The agent repeats a single cycle until it finishes or runs out of budget:

1. **Reason**: Send the conversation so far to the LLM and ask what to do next.
2. **Act**: If the LLM asks to use a tool (search, read, edit, test), run that tool.
3. **Observe**: Feed the tool's result back into the conversation.
4. **Repeat**: Loop back to step 1 with the new information, until the model says it's done.

This is the classic **ReAct** pattern (Reason + Act), implemented as a *manual* loop so we keep full control over budgets, logging, and safety.

---

### 1. The Provider Abstraction (`agent/providers/`)

The loop never talks to a specific AI company's code directly. Instead it talks to a generic `LLMProvider` interface, and a thin "adapter" translates to whichever vendor is selected. This means the agent can run on **Anthropic Claude** or **Google Gemini** by changing one environment variable (`LLM_PROVIDER`), with no change to the loop itself.

- **`base.py`**: Defines the contract — the `LLMProvider` interface plus shared data shapes (`ToolSpec`, `ToolCall`, `LLMResponse`, `Usage`). The loop only ever uses these neutral shapes.
- **`anthropic_provider.py`**: The Claude adapter. Defaults to `claude-opus-4-8`, uses adaptive thinking and the effort setting, and reads the key from `ANTHROPIC_API_KEY`.
- **`gemini_provider.py`**: The Gemini adapter. Uses the `google-genai` SDK with manual function calling, defaults to `gemini-2.5-pro`, and reads `GEMINI_API_KEY`.
- **`get_provider()`**: A factory that picks the right adapter at runtime based on the `LLM_PROVIDER` env var (default `anthropic`).

Each adapter is responsible for the messy vendor-specific details: how to phrase a request, how tools are declared, what a tool call looks like coming back, how to count tokens, and how to price usage.

### 2. The Budget Controller (`Budget`)

Because every LLM call costs money and time, the agent runs on a strict allowance. Before *every* model call, the loop checks whether any limit has been hit:

- **`max_steps`**: the maximum number of Reason→Act cycles (default 30).
- **`max_total_tokens`**: a cap on cumulative input + output tokens.
- **`max_usd`**: a hard ceiling on estimated dollar spend.

After each call, `record()` adds the new token usage and cost. If any limit is reached, the run stops cleanly with a status explaining why (e.g. `usd_budget`), rather than running forever.

### 3. The Tools (`default_tools`)

The model can't touch the repository directly — it can only *request* actions through a fixed set of tools. Each tool is a small Python function bound to the workspace:

- **`retrieve_context`**: semantic code search (calls the `retrieval.py` engine) to locate relevant code.
- **`read_file`**: read a file's exact contents before editing.
- **`edit_file`**: make a precise change by replacing an exact, unique string — or create a new file by passing an empty `old_string`.
- **`run_tests`**: run `pytest` (optionally on a subset) to verify a change.
- **`list_dir`**: list a directory's contents.

Two safety properties are built into every tool:
- **Sandboxed paths**: `_safe_path()` resolves every path and refuses anything outside the workspace root, blocking path-traversal attacks like `../../etc/passwd`.
- **Consistent newlines**: files are read and written as UTF-8 with LF endings (using `read_bytes`/`write_bytes`), so the model's `\n`-based edits match correctly even on Windows.

### 4. The Agent (`ReActAgent.run`)

This is the engine that ties it all together. Given a task, it builds the conversation and runs the loop:

1. Check the budget — stop if exhausted.
2. Ask the provider for the next step (`provider.complete`).
3. Record the usage and cost, and append the model's reply to the conversation.
4. Decide what happened based on the model's `stop_reason`:
   - **Refusal** → stop and report it.
   - **Tool calls** → run each one via `_dispatch`, send the results back, and loop again.
   - **Finished** (`end_turn`) → return the final answer.

A key robustness feature is `_dispatch`: when a tool fails, the error is captured and handed *back to the model* as a failed result (so it can try a different approach), instead of crashing the whole run. A buggy tool can never kill the loop.

---

### Additional Features

#### Structured Trace & Result (`RunResult`)
Every run returns a `RunResult` containing the final text, the budget summary (steps, tokens, cost), and a step-by-step trace of what the agent did — which tools it called, with what arguments, and the outcomes. This makes the agent's behaviour observable and is the natural hook for the project's monitoring layer.

#### Offline Self-Test (`_selftest`)
Running `python -m agent.loop` with no arguments runs a self-test that exercises all the file tools and safety guards (creating, reading, editing, the traversal block, and the ambiguous-edit guard) **without needing an API key or any AI model**. This lets you verify the tooling works before spending money on a real run.

#### Command-Line Interface (`_main`)
The module is runnable directly. `python -m agent.loop "Fix the bug in X" --workspace /path/to/repo` runs a real task, with flags to control the budget (`--max-steps`, `--max-usd`) and to index the repo first (`--auto-index`). The exit code reflects whether the agent completed successfully.
