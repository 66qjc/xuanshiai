"""Phase 1 candidate/build_invite schema contract tests (Contract v1.1).

These tests are intentionally written before the implementation is added so that
they fail with ``KeyError``/``AttributeError`` until the contract surface
(candidate + build_invite tables + journey_stage + profile_dimension columns)
is present in ``app.db.ai_schema`` and the reviewed migration runner. They
are also the unit-level guard for fresh bootstrap and incremental migration
parity (no schema drift between ``database_setup_marriage.py`` and
``migrations/ai/20260901_01_moxiang_journey_up.sql``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_ROOT = REPO_ROOT / "migrations" / "ai"
AI_SCHEMA_FILE = REPO_ROOT / "app" / "db" / "ai_schema.py"
DATABASE_SETUP_FILE = REPO_ROOT / "database_setup_marriage.py"


# ---------------------------------------------------------------------------
# Pure-source helpers (no DB): the SQL DDL contract must live in ai_schema.py
# and in the reviewed migration so fresh bootstrap and incremental migration
# agree on column types.
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_ai_schema_exports_candidate_table_ddl() -> None:
    """ai_schema.AI_TABLES must contain ``ai_profile_candidate`` DDL."""
    source = _read(AI_SCHEMA_FILE)
    assert '"ai_profile_candidate"' in source or "'ai_profile_candidate'" in source, (
        "AI_TABLES must declare ai_profile_candidate bootstrap DDL"
    )


def test_ai_schema_exports_build_invite_table_ddl() -> None:
    """ai_schema.AI_TABLES must contain ``ai_profile_build_invite`` DDL."""
    source = _read(AI_SCHEMA_FILE)
    assert '"ai_profile_build_invite"' in source or "'ai_profile_build_invite'" in source, (
        "AI_TABLES must declare ai_profile_build_invite bootstrap DDL"
    )


def _extract_ddl_block(source: str, table_key: str) -> str:
    """Return the DDL string for ``table_key`` from the AI_TABLES dictionary.

    The block starts at ``"table_key":`` and runs until the next ``",`` line that
    closes the AI_TABLES entry. This avoids matching comments that mention the
    table name.
    """
    needle = f'"{table_key}":'
    start = source.find(needle)
    assert start != -1, f"AI_TABLES key {table_key} not found"
    # Walk forward until we hit the closing `,``\n    "` or the trailing `}`.
    end_markers = ('\n    }', '\n    ,', '\n    },')
    cursor = start
    while True:
        next_end = min(
            (source.find(marker, cursor + 1) for marker in end_markers if source.find(marker, cursor + 1) != -1),
            default=-1,
        )
        if next_end == -1:
            raise AssertionError(f"could not find end of DDL block for {table_key}")
        candidate_end = next_end
        # Stop at the first marker that closes the entry cleanly.
        block = source[start:candidate_end + len('\n    }')]
        if block.count("{") - block.count("}") <= 0:
            return block
        cursor = candidate_end + 1


def test_ai_schema_candidate_table_ddl_declares_contract_columns() -> None:
    """``ai_profile_candidate`` DDL must declare every Contract v1.1 column."""
    source = _read(AI_SCHEMA_FILE)
    block = _extract_ddl_block(source, "ai_profile_candidate")
    required = [
        "candidate_id",
        "session_id",
        "user_id",
        "subject",
        "profile_dimension",
        "field_kind",
        "field_key",
        "category",
        "content",
        "value_json",
        "confidence",
        "source_turn_ids",
        "source_span",
        "consent_version",
        "policy_revision",
        "status",
        "content_hash",
    ]
    for column in required:
        assert column in block, f"ai_profile_candidate DDL missing column {column}"
    # Status enum must include every Contract v1.1 state.
    for state in ("active", "promoted", "dismissed", "expired"):
        assert state in block, f"ai_profile_candidate.status enum missing {state}"
    # Unique key on (session_id, content_hash) is the semantic-dedup anchor.
    assert "uk_candidate_session_hash" in block, (
        "ai_profile_candidate must declare uk_candidate_session_hash (session_id, content_hash)"
    )


def test_ai_schema_build_invite_table_ddl_declares_contract_columns() -> None:
    """``ai_profile_build_invite`` DDL must declare every Contract v1.1 column."""
    source = _read(AI_SCHEMA_FILE)
    block = _extract_ddl_block(source, "ai_profile_build_invite")
    required = [
        "invite_id",
        "session_id",
        "user_id",
        "subject",
        "status",
        "trigger_kind",
        "invite_no",
        "summary_json",
        "effective_turn_count_at_create",
        "dimension_count",
        "candidate_count",
        "snoozed_at_effective_turn_count",
        "accepted_at",
        "snoozed_at",
        "expired_at",
        "active_slot",
    ]
    for column in required:
        assert column in block, f"ai_profile_build_invite DDL missing column {column}"
    for state in ("pending", "accepted", "snoozed", "expired"):
        assert state in block, f"ai_profile_build_invite.status enum missing {state}"
    assert "auto" in block and "manual" in block, (
        "ai_profile_build_invite.trigger_kind must allow auto and manual"
    )
    assert "uk_ai_profile_build_invite_pending" in block, (
        "ai_profile_build_invite must enforce a single pending invite per session"
    )


def test_ai_schema_legacy_helpers_cover_journey_stage_and_dimension_columns() -> None:
    """bootstrap helpers must add ``journey_stage`` and ``profile_dimension``."""
    source = _read(AI_SCHEMA_FILE)
    assert "journey_stage" in source, (
        "ai_schema.py must declare a helper that adds journey_stage to ai_profile_session"
    )
    assert "profile_dimension" in source, (
        "ai_schema.py must declare helpers adding profile_dimension to draft_field + revision_field"
    )
    # Helper names exposed for database_setup_marriage.py registration.
    assert "ensure_ai_profile_journey_columns" in source, (
        "ai_schema.py must expose ensure_ai_profile_journey_columns() for fresh bootstrap parity"
    )


def test_ai_schema_helper_is_idempotent() -> None:
    """The journey helper must use SHOW COLUMNS / SHOW INDEXES (idempotent)."""
    source = _read(AI_SCHEMA_FILE)
    assert "ensure_ai_profile_journey_columns" in source
    helper_match = re.search(
        r"def ensure_ai_profile_journey_columns.*?(?=\ndef |\Z)",
        source,
        flags=re.DOTALL,
    )
    assert helper_match is not None, "ensure_ai_profile_journey_columns function not found"
    body = helper_match.group(0)
    assert "SHOW COLUMNS" in body or "information_schema.columns" in body, (
        "ensure_ai_profile_journey_columns must check existing columns before ALTER"
    )


# ---------------------------------------------------------------------------
# Reviewed migration parity
# ---------------------------------------------------------------------------


def test_reviewed_migration_files_exist() -> None:
    """The reviewed migration pair must exist on disk for Phase 1."""
    up_path = MIGRATION_ROOT / "20260901_01_moxiang_journey_up.sql"
    down_path = MIGRATION_ROOT / "20260901_01_moxiang_journey_down.sql"
    assert up_path.is_file(), f"missing migration: {up_path}"
    assert down_path.is_file(), f"missing migration: {down_path}"


def test_reviewed_migration_up_declares_required_columns() -> None:
    """The up SQL must add journey_stage, profile_dimension and the two tables."""
    text = _read(MIGRATION_ROOT / "20260901_01_moxiang_journey_up.sql")
    assert "ai_profile_candidate" in text
    assert "ai_profile_build_invite" in text
    assert "journey_stage" in text, "up migration must add journey_stage to ai_profile_session"
    assert "profile_dimension" in text, (
        "up migration must add profile_dimension to draft/revision field tables"
    )
    # Single-pending invite unique key must exist on the build_invite table.
    assert "uk_ai_profile_build_invite_pending" in text
    # session+content_hash unique anchor for the candidate table.
    assert "uk_candidate_session_hash" in text


def test_reviewed_migration_down_reverses_every_addition() -> None:
    """The down SQL must drop every column, index and table the up SQL adds."""
    text = _read(MIGRATION_ROOT / "20260901_01_moxiang_journey_down.sql")
    assert "ai_profile_candidate" in text, "down must drop ai_profile_candidate"
    assert "ai_profile_build_invite" in text, "down must drop ai_profile_build_invite"
    assert "journey_stage" in text, "down must drop journey_stage"
    assert "profile_dimension" in text, "down must drop profile_dimension"
    assert "uk_candidate_session_hash" in text
    assert "uk_ai_profile_build_invite_pending" in text


def test_reviewed_migration_up_is_generated_column_safe() -> None:
    """Down migration must drop generated columns / indexes before the column itself."""
    text = _read(MIGRATION_ROOT / "20260901_01_moxiang_journey_down.sql")
    # active_slot must be dropped explicitly (GENERATED ALWAYS ... STORED).
    assert "active_slot" in text
    # All candidate and build_invite indexes must be dropped explicitly.
    assert "uk_candidate_session_hash" in text
    assert "uk_ai_profile_build_invite_pending" in text
    assert "uk_ai_profile_build_invite_no" in text


# ---------------------------------------------------------------------------
# Manifest wiring
# ---------------------------------------------------------------------------


def test_manifest_records_moxiang_journey_version() -> None:
    """The new migration must be registered in the manifest with both checksums."""
    import json

    manifest = json.loads(_read(MIGRATION_ROOT / "manifest.json"))
    versions = manifest.get("versions", [])
    found = [v for v in versions if v.get("version") == "20260901_01_moxiang_journey"]
    assert found, "manifest.json missing 20260901_01_moxiang_journey entry"
    entry = found[0]
    assert entry["up"] == "20260901_01_moxiang_journey_up.sql"
    assert entry["down"] == "20260901_01_moxiang_journey_down.sql"
    for direction in ("up", "down"):
        sha = entry.get("sha256", {}).get(direction, "")
        assert len(sha) == 64, f"manifest sha256.{direction} must be SHA-256 hex"


def test_manifest_requires_new_tables() -> None:
    """新表在 ``up`` 迁移 SQL 中声明,不强制入 ``requires``。

    ``requires`` 是"前后状态都需要存在"的最小集合——把新表塞进去会让
    ``manage_ai_migration.py down`` 之后的 ``verify --expect previous`` 失败。
    改用"新表至少被一个 version 的 up SQL 引用"作为契约。
    """
    import json

    manifest = json.loads(_read(MIGRATION_ROOT / "manifest.json"))
    new_tables = {"ai_profile_candidate", "ai_profile_build_invite"}
    # 1. 至少一个版本的 up 文件存在
    versions = manifest.get("versions", [])
    up_files = [MIGRATION_ROOT / str(v["up"]) for v in versions]
    referenced: set[str] = set()
    for path in up_files:
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8")
        for name in new_tables:
            if f"`{name}`" in body or f" {name} " in body or f"IF NOT EXISTS `{name}`" in body:
                referenced.add(name)
    missing = new_tables - referenced
    assert not missing, (
        f"新表 {missing} 必须在某个 manifest up SQL 中声明,而不是塞进 requires"
    )


def test_manifest_checksum_matches_lf_normalised_sql() -> None:
    """LF-normalised hash of every up/down SQL must match the manifest entry."""
    import hashlib
    import json

    manifest = json.loads(_read(MIGRATION_ROOT / "manifest.json"))
    versions = manifest.get("versions", [])
    for entry in versions:
        for direction in ("up", "down"):
            sql_path = MIGRATION_ROOT / entry[direction]
            assert sql_path.is_file(), f"missing SQL: {entry[direction]}"
            raw = sql_path.read_bytes().replace(b"\r\n", b"\n")
            actual = hashlib.sha256(raw).hexdigest()
            expected = entry.get("sha256", {}).get(direction, "")
            assert actual == expected, (
                f"{entry['version']}/{direction}: LF-normalised hash {actual} != manifest {expected}"
            )


# ---------------------------------------------------------------------------
# database_setup_marriage.py must register the helper, not duplicate SQL
# ---------------------------------------------------------------------------


def test_database_setup_registers_journey_helper() -> None:
    """database_setup_marriage.py must invoke the helper, not copy the SQL."""
    source = _read(DATABASE_SETUP_FILE)
    assert "ensure_ai_profile_journey_columns" in source, (
        "database_setup_marriage.py must call ensure_ai_profile_journey_columns"
    )
    # Guard: the production bootstrap must NOT carry a raw candidate/invite
    # CREATE TABLE that bypasses the schema helper.
    assert "CREATE TABLE IF NOT EXISTS `ai_profile_candidate`" not in source, (
        "database_setup_marriage.py must not duplicate ai_profile_candidate DDL"
    )
    assert "CREATE TABLE IF NOT EXISTS `ai_profile_build_invite`" not in source, (
        "database_setup_marriage.py must not duplicate ai_profile_build_invite DDL"
    )


# ---------------------------------------------------------------------------
# Pytest parametrised contract: required state enums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    ["active", "promoted", "dismissed", "expired"],
)
def test_candidate_status_enum_includes(state: str) -> None:
    """Contract v1.1: candidate.status must enumerate every state."""
    source = _read(AI_SCHEMA_FILE)
    assert state in source, f"candidate.status enum missing {state}"


@pytest.mark.parametrize(
    "state",
    ["pending", "accepted", "snoozed", "expired"],
)
def test_build_invite_status_enum_includes(state: str) -> None:
    """Contract v1.1: build_invite.status must enumerate every state."""
    source = _read(AI_SCHEMA_FILE)
    assert state in source, f"build_invite.status enum missing {state}"


@pytest.mark.parametrize(
    "dimension",
    [
        "personality_social",
        "intimacy_pattern",
        "lifestyle",
        "emotional_expression",
        "relationship_boundaries",
        "future_expectations",
    ],
)
def test_dimension_names_appear_in_bootstrap(dimension: str) -> None:
    """Contract v1.1: every fixed dimension name must appear in the schema helper.

    The DDL uses VARCHAR(64) so the column itself is open, but the helper
    must document the six dimensions to keep prompts and validators aligned.
    """
    source = _read(AI_SCHEMA_FILE)
    assert dimension in source, f"dimension enum missing {dimension}"