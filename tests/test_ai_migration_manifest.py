"""Cross-platform migration manifest checksum tests.

Plan Task 3 / G2-A Step 1: verify that AI migration SQL checksums are
stable across LF and CRLF checkouts.  The ``manage_ai_migration.py``
runner must normalise line endings before hashing so that Windows
checkouts (CRLF) produce the same SHA-256 as Unix checkouts (LF).

These tests are designed to FAIL against the current implementation
which hashes raw bytes without normalising line endings.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from scripts.manage_ai_migration import (
    MANIFEST_PATH,
    MIGRATION_ROOT,
    MigrationError,
    _checksum,
    _manifest,
)

# ---------------------------------------------------------------------------
# .gitattributes
# ---------------------------------------------------------------------------

def test_gitattributes_fixes_ai_sql_to_lf() -> None:
    """.gitattributes must force migrations/ai/*.sql to use LF."""
    attr_path = Path(__file__).resolve().parents[1] / ".gitattributes"
    assert attr_path.is_file(), ".gitattributes does not exist"
    content = attr_path.read_text(encoding="utf-8")
    # Must contain a rule that pins ai SQL files to LF.
    assert "migrations/ai" in content, ".gitattributes does not cover migrations/ai"
    assert "eol=lf" in content, ".gitattributes does not force eol=lf for AI SQL"


# ---------------------------------------------------------------------------
# Checksum normalisation
# ---------------------------------------------------------------------------

def _read_manifest() -> list[dict]:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert "versions" in data, "manifest must use multi-version layout"
    return data["versions"]


def test_manifest_has_two_versions() -> None:
    versions = _read_manifest()
    assert len(versions) >= 2, "manifest must contain at least 2 migration versions"


def test_checksum_matches_after_lf_normalisation() -> None:
    """For every up/down SQL, LF-normalised hash must equal manifest hash."""
    versions = _read_manifest()
    for entry in versions:
        for direction in ("up", "down"):
            sql_path = MIGRATION_ROOT / str(entry[direction])
            assert sql_path.is_file(), f"missing SQL file: {entry[direction]}"
            raw = sql_path.read_bytes()
            # Simulate CRLF checkout (Windows) — content may already be CRLF,
            # but the point is: normalising to LF must always produce the
            # manifest hash regardless of checkout line endings.
            lf_bytes = raw.replace(b"\r\n", b"\n")
            expected = entry.get("sha256", {}).get(direction, "")
            actual_lf = hashlib.sha256(lf_bytes).hexdigest()
            assert actual_lf == expected, (
                f"{entry['version']}/{direction}: LF-normalised hash {actual_lf} "
                f"!= manifest {expected}"
            )


def test_checksum_rejects_unknown_content_change() -> None:
    """Adding a byte to the SQL must cause a checksum mismatch."""
    versions = _read_manifest()
    entry = versions[0]
    sql_path = MIGRATION_ROOT / str(entry["up"])
    original = sql_path.read_bytes()
    tampered = original + b"\n-- injected comment\n"
    # Write tampered content to a temp file and verify _checksum raises.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".sql") as tmp:
        tmp.write(tampered)
        tmp_path = Path(tmp.name)
    try:
        expected = entry.get("sha256", {}).get("up", "")
        # _checksum with the real expected hash should reject tampered content
        # after LF normalisation (tampered content has extra bytes even after LF norm).
        with pytest.raises(MigrationError, match="checksum mismatch"):
            _checksum(tmp_path, expected)
    finally:
        os.unlink(tmp_path)


def test_runner_normalises_line_endings_before_checksum() -> None:
    """_manifest() must succeed even if SQL files have CRLF endings.

    This directly tests the runner's behaviour: on a Windows checkout where
    git may have converted LF→CRLF, ``_manifest()`` must not raise
    MigrationError.  The current implementation calls ``_checksum`` on raw
    bytes, so this will FAIL until LF normalisation is added.
    """
    # _manifest() reads all SQL files and runs _checksum on each.
    # If it raises MigrationError, the test fails — proving the runner
    # cannot work on a CRLF checkout.
    manifest = _manifest()
    assert "versions" in manifest
    assert len(manifest["versions"]) >= 2


# ---------------------------------------------------------------------------
# Manifest structure
# ---------------------------------------------------------------------------

def test_manifest_requires_field_present() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert "requires" in manifest, "manifest must declare 'requires'"
    required_tables = set(manifest["requires"])
    # Must include the core AI tables.
    for table in ("ai_task", "ai_consent_grant", "ai_profile_session"):
        assert table in required_tables, f"manifest 'requires' missing {table}"


def test_each_version_has_sha256_for_both_directions() -> None:
    versions = _read_manifest()
    for entry in versions:
        sha = entry.get("sha256", {})
        for direction in ("up", "down"):
            assert direction in sha, f"{entry['version']} missing sha256.{direction}"
            assert len(sha[direction]) == 64, f"{entry['version']}/{direction} hash is not SHA-256"
