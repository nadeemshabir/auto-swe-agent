# Database Persistence & Idempotency (`db/` package)

This document explains the architecture, schema design, and operational workflow of the PostgreSQL persistence layer implemented in **Milestone 4 (M4)**. 

The persistence layer converts the agent from a stateless task execution loop into a durable **system of record** capable of recording every execution run, step-by-step trace, and webhook event.

---

## Architecture Overview

The database subsystem is structured around three main objectives:
1. **Durable Observability**: Storing the history of every execution run, along with the precise step traces and LLM tool calls.
2. **Idempotency & Deduplication**: Ensuring the agent is resilient to network issues (such as duplicate webhook events from GitHub) and doesn't run duplicate tasks simultaneously for the same issue.
3. **Observation APIs**: Providing standard paginated API endpoints for querying run histories and replay trace playbacks.

```mermaid
graph TD
    Webhook[GitHub Webhook Ingress] -->|HMAC Verification| API[API Gateway: app/main.py]
    API -->|Deduplicate delivery_id| DB_Dedupe[(PostgreSQL: webhook_events)]
    API -->|Enqueue Task| Celery[Celery Broker: Redis]
    Celery -->|Process Task| Worker[Celery Worker: workers/tasks.py]
    Worker -->|Create/Update Run| DB_Run[(PostgreSQL: runs)]
    Worker -->|Write step trace incrementally| DB_Steps[(PostgreSQL: run_steps)]
    API_Query[Observability APIs: GET /runs] -->|Fetch History & Steps| DB_Run
```

---

## 1. Database Schema (`db/models.py`)

We define three SQLAlchemy ORM models, matching the specification in `plan2.md §7.1`:

### A. The `runs` Table
Tracks the lifecycle of an autonomous issue-fixing attempt.
* **`id`**: Unique UUID assigned to the run.
* **`repo` & `issue_number`**: Identifies the GitHub repository and issue. 
  * *Idempotency Guard*: A `UniqueConstraint("repo", "issue_number")` ensures that only one entry exists for a given issue. Retriggering the same issue updates this row rather than adding duplicate runs.
* **`status`**: Current status of the run (`queued`, `running`, `completed`, `no_changes`, `refused`, `budget_exhausted`, `provider_error`, `sandbox_error`, `github_error`, `index_error`, `error`).
* **`branch`, `pr_number`, `pr_url`**: PR and git metadata.
* **Cost & Token tracking**: Captures `steps_used`, `input_tokens`, `output_tokens`, and `cost_usd` for budget auditing.
* **Timestamps**: Timezone-aware `started_at` and `finished_at`.

### B. The `run_steps` Table
Logs each ReAct cycle step (Reason -> Act -> Observe) in a run.
* **Incremental Writes**: Steps are written to this table *as they happen* inside the agent loop. If the Celery worker crashes, the partial trace is still stored for observation.
* **`run_id`**: Foreign key to `runs.id` with `ondelete="CASCADE"`.
* **`n`**: 0-indexed step number. A unique constraint on `(run_id, n)` prevents trace duplication.
* **`stop_reason`**: The reason the model returned (e.g. `tool_use`, `end_turn`, `max_tokens`, `refusal`).
* **`tools`**: Stored as a JSON block containing tool names, arguments, responses, and whether the tool call erred.
  * *Portability*: Renders as a native `JSONB` column on PostgreSQL for indexability, falling back to standard `JSON` on SQLite during unit tests.

### C. The `webhook_events` Table
An audit log and deduplication registry for webhooks.
* **`delivery_id`**: The unique GitHub event ID (`X-GitHub-Delivery` header).
  * *Deduplication Guard*: A unique index on `delivery_id` rejects duplicate webhook deliveries.
* **`action_taken`**: Records what was done (`enqueued`, `skipped`, `duplicate`).

---

## 2. Session & Engine Management (`db/session.py`)

Database connectivity is managed through a lazy engine singleton:
- **Connection Pooling**: Configured with `pool_size=5` and `max_overflow=10` to support concurrent Celery workers, and `pool_pre_ping=True` to automatically drop stale connections.
- **SQLite Fallback**: If the URL starts with `sqlite://`, the factory automatically injects `StaticPool` and disables thread checks. This allows in-memory SQLite instances to share state across sessions in unit tests without dropping the database between connections.
- **`get_session()`**: A context manager that handles transactions. It auto-commits on success, auto-rolls back on exception, and guarantees connection return.

---

## 3. Database Migrations (Alembic)

Database schema updates are managed using **Alembic**.

### Key Configurations
* **`alembic.ini`**: Points to the target PostgreSQL URL (`postgresql://agent:agent@localhost:5432/auto_swe_agent`).
* **`db/migrations/env.py`**: Reads `Base.metadata` from `db.models` to enable the `--autogenerate` engine, making schema diffing automatic.

### Migration Commands
To apply the database schema or bring it up to date:
```bash
# Run from the project root inside the venv
alembic upgrade head
```

---

## 4. Idempotency & Webhook Deduplication

To prevent duplicate work and race conditions, the system uses two layers of idempotency guards:

1. **Ingress Deduplication (API level)**:
   When GitHub sends a webhook, `app/main.py` checks `webhook_events` for the `X-GitHub-Delivery` ID. If it is already in the database, it responds immediately with `202 Accepted ("duplicate webhook ignored")` and skips enqueuing the Celery job.

2. **Execution Serialization (Worker level)**:
   When `run_issue` starts, it opens a database session and queries the `runs` table. If there is already a run with `status="running"` for that `(repo, issue_number)`, the task logs a skip message and exits. If a previous run is present but completed/erred, it updates the existing row with clean stats, purges any historical steps, and proceeds.

---

## 5. REST Query API (`app/main.py`)

Several database-backed observability endpoints are available:

* **`GET /runs`**: Fetches the execution history, sorted by `started_at` DESC.
  * *Parameters*: `status` (filter), `repo` (filter), `limit` (max 100).
  * *Keyset Pagination*: Implemented using a `cursor` parameter. Clients pass the UUID of the last item received to retrieve the next page cleanly.
* **`GET /runs/{run_id}`**: Retrieves the metadata for a specific run. If the run is not yet in the database, it falls back to polling Celery's result backend.
* **`GET /runs/{run_id}/steps`**: Returns the full step-by-step trace of tool execution and LLM responses.
* **`GET /readyz`**: Liveness probe checks connections to both Redis and PostgreSQL.

---

## 6. Offline Testing & Verification

Each layer includes a mock-backed offline test suite that can be run without Redis or PostgreSQL:

* **Models Self-Test**: Validates schemas, constraints, and serialization logic:
  ```bash
  python -m db.models
  ```
* **Task Self-Test**: Validates worker orchestrator wiring, run updates, and step-saving routines:
  ```bash
  python -m workers.tasks
  ```
* **API Gateway Self-Test**: Launches a mock FastAPI server, tests webhook deduplication, pagination, and health routes using `starlette.testclient`:
  ```bash
  python -m app.main
  ```
