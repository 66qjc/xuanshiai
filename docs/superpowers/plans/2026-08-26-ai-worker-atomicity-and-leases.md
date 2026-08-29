# AI Worker Atomicity and Lease Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make successful AI-task finalization atomic with handler business writes and prevent queued claimed tasks from losing their leases before processing starts.

**Architecture:** Execute the completion gate and task terminal update in the handler's database session, then commit the task row and handler writes once. Protect handler writes with a nested transaction so a superseded decision can discard business output while persisting the terminal superseded state in the outer transaction. Claim one task immediately before processing it and reject expired leases at `start_task`.

**Tech Stack:** Python asyncio, SQLAlchemy `AsyncSession`, MySQL row locks/savepoints, pytest/pytest-asyncio.

## Global Constraints

- A task must never be durably `succeeded` unless its business writes are durable in the same transaction.
- A superseded task must persist `superseded` while none of its handler business writes become visible.
- Completion-time consent and revision checks remain fail-closed.
- Task status enums and external API contracts do not change.
- No database migration or dependency is introduced.
- Preserve unrelated dirty-worktree changes; do not stash, reset, commit, or push.

---

### Task 1: Atomic Handler and Task Finalization

**Files:**
- Modify: `app/workers/ai_worker.py`
- Modify: `app/services/ai/tasks.py`
- Modify: `tests/test_worker_finalize_gate.py`
- Test: `tests/test_ai_tasks.py`

**Interfaces:**
- Consumes: `complete_task(db, task_id, worker_id, result_ref, revisions)` and handler results `(result_ref, RevisionVector)`.
- Produces: optional `before_supersede: Callable[[], Awaitable[None]]` support in `complete_task`; a handler finalizer that runs completion in the handler session and performs one outer commit.

- [ ] **Step 1: Add RED tests for commit atomicity and supersede rollback**

Extend `tests/test_worker_finalize_gate.py` with tracked staged writes and transaction state. Add `test_handler_commit_failure_does_not_leave_succeeded_task`, where the handler-session commit raises and both the staged business result and staged `succeeded/result_ref` task update are rolled back. Strengthen the superseded test so it asserts the handler business write is rolled back while the superseded task update commits.

- [ ] **Step 2: Run the worker finalize tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_worker_finalize_gate.py -q`

Expected: the new tests fail because `complete_task` currently commits in `finalize_db` before the handler session commit and cannot roll the terminal task update back with business writes.

- [ ] **Step 3: Add a pre-supersede rollback hook to the completion gate**

In `app/services/ai/tasks.py`, add a keyword-only optional async `before_supersede` callback to `complete_task`. Route every completion-time supersede branch through one local helper that first awaits the callback exactly once, then calls `_supersede_guarded`. Existing callers that omit the callback retain current behavior.

- [ ] **Step 4: Finalize inside the handler session**

In `_run_with_heartbeat.invoke_handler`, open `handler_db.begin_nested()` before invoking the handler. Replace the boolean `finalize_handler` contract with a closure that accepts either no completion operation (rollback/discard) or an async completion operation receiving `handler_db`. For successful outcomes, call `complete_task` in `handler_db`, pass the nested rollback callback as `before_supersede`, release the nested transaction only on `SUCCEEDED`, and commit the outer transaction once. On supersede, the callback rolls back handler writes before `_supersede_guarded` writes the terminal state in the outer transaction. Any commit exception rolls back and propagates.

- [ ] **Step 5: Remove the separate success finalizer**

In `_process`, keep separate lifecycle sessions for `start_task` and failure recording, but replace the success-path `finish_in_session(complete_task(...))` call with the handler-session finalizer operation. Return `completed` only after that single transaction commits.

- [ ] **Step 6: Lock completion context rows**

Add `FOR UPDATE` to the completion-time `user_revision_state` lookup and active-consent lookup in `_load_current_completion_context`, so revocation/revision changes cannot commit between the final gate and the atomic outer commit.

- [ ] **Step 7: Run focused GREEN tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_worker_finalize_gate.py tests/test_ai_tasks.py -q`

Expected: all tests pass; succeeded and business data share one commit, superseded discards business writes, and legacy task state-machine coverage remains green.

### Task 2: Claim Just-in-Time and Reject Expired Leases

**Files:**
- Modify: `app/workers/ai_worker.py`
- Modify: `app/services/ai/tasks.py`
- Create: `tests/test_worker_lease_safety.py`
- Test: `tests/test_ai_tasks.py`

**Interfaces:**
- Consumes: `claim_tasks(..., limit=1)`, `_process(...)`, and `start_task(...)`.
- Produces: at most `batch_size` just-in-time claims per round; `start_task` accepts only a non-expired lease owned by the worker.

- [ ] **Step 1: Add RED tests for claim order and expired start**

Create `tests/test_worker_lease_safety.py`. Add `test_run_round_claims_next_task_only_after_previous_finishes` with patched session factory/claim/process functions that record event order and require `claim-1, process-1, claim-2, process-2`. Add a `start_task` regression in `tests/test_ai_tasks.py` that seeds an expired leased record for the same owner and expects `TASK_NOT_FOUND` without transition to running.

- [ ] **Step 2: Run the lease tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_worker_lease_safety.py tests/test_ai_tasks.py -q`

Expected: event order shows all claims occur before processing, and the expired lease currently starts.

- [ ] **Step 3: Claim immediately before processing**

Refactor `_run_round` to loop at most `batch_size` times. On each iteration, open a claim session, call `claim_tasks(..., limit=1)` with a fresh `_now()`, commit, break if no row was claimed, and immediately process the single claimed task before claiming another. Preserve queue-age metrics and returned `(claimed, completed, failed)` counts.

- [ ] **Step 4: Fence expired leases at start**

In `start_task`, reject records whose `lease_until` is null or `<= now`. Add `lease_until > :now` to the guarded `UPDATE` and pass the same `now` value so the read check and update predicate use one boundary.

- [ ] **Step 5: Run focused GREEN and static checks**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_worker_lease_safety.py tests/test_worker_finalize_gate.py tests/test_ai_tasks.py -q`

Run: `.\.venv\Scripts\python.exe -m ruff check app/workers/ai_worker.py app/services/ai/tasks.py tests/test_worker_finalize_gate.py tests/test_worker_lease_safety.py tests/test_ai_tasks.py`

Expected: all tests and Ruff pass.

