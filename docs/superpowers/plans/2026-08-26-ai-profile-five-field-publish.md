# AI Profile Five-Field Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the published AI-profile contract consistently require at least five confirmed fields.

**Architecture:** Keep `MIN_CONFIRMED_FIELDS_TO_PUBLISH = 5` as the single service-layer enforcement point. Align the workspace product decision, public API contract, and in-memory publish tests with that existing production rule; no schema or endpoint shape changes are required.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy async services, pytest, Markdown product/API contracts.

## Global Constraints

- Published revisions contain only fields whose `confirmation_status` is `confirmed`.
- A draft is publishable only when it contains at least 5 confirmed fields.
- Preserve all unrelated dirty-worktree changes; do not stash, reset, commit, or push.
- Do not add dependencies or database migrations.
- Keep the existing `202 Accepted` publish response and idempotency behavior compatible.

---

### Task 1: Freeze the Five-Field Product and API Contract

**Files:**
- Modify: `../PRODUCT.md`
- Modify: `docs/api/AI画像.md`

**Interfaces:**
- Consumes: `MIN_CONFIRMED_FIELDS_TO_PUBLISH = 5` in `app/services/ai/profile.py`.
- Produces: A product and frontend-facing contract that both state `confirmed_count >= 5`.

- [ ] **Step 1: Update the product decision before backend artifacts**

Add an AI-profile rule under the current 墨相 section: a draft may be published only after at least five fields are explicitly confirmed; suggested, rejected, and deleted fields do not count and never enter a revision.

- [ ] **Step 2: Update the API change record and publish contract**

In `docs/api/AI画像.md`, change the publish permission, precondition, boundary, and `AI_INPUT_INVALID` rows from “at least one confirmed field” to “at least five confirmed fields”. Document the response field `narrative_task_id` as an optional asynchronous narrative-generation task ID and show it in the `202` response example.

- [ ] **Step 3: Check contract wording**

Run: `rg -n "至少一项|至少 5|narrative_task_id|AI_INPUT_INVALID" PRODUCT.md xuanshiai-backend/docs/api/AI画像.md`

Expected: no publish rule still permits one confirmed field; the five-field rule and optional task ID are documented.

### Task 2: Align Publish Tests with the Five-Field Rule

**Files:**
- Modify: `tests/test_ai_profile_publish.py`
- Test: `tests/test_ai_profile_publish.py`

**Interfaces:**
- Consumes: `publish_profile_draft(... expected_revision, idempotency_key)` and `MIN_CONFIRMED_FIELDS_TO_PUBLISH`.
- Produces: A reusable five-field fixture, a four-field rejection regression, and five-field success coverage.

- [ ] **Step 1: Turn the stale suite into the desired regression contract**

Extend `_PUBLISHABLE_CONFIRMED_FIELDS` with valid `education_level` and `height_cm` fields. Rename the minimum-count test to `test_publish_requires_at_least_five_confirmed_fields`, seed exactly four confirmed fields, and assert the safe error mentions `5 confirmed`.

- [ ] **Step 2: Align positive-path assertions**

Update confirmed-only key lists and history `field_count` assertions from three items to all five fixture items. Keep one suggested field in the confirmed-only test and assert it remains excluded.

- [ ] **Step 3: Run the focused publish suite**

Run: `pytest tests/test_ai_profile_publish.py -q`

Expected: all tests pass, proving four confirmed fields are rejected and five confirmed fields publish successfully.

- [ ] **Step 4: Run static checks for the touched Python test**

Run: `ruff check tests/test_ai_profile_publish.py app/services/ai/profile.py`

Expected: zero Ruff diagnostics.

