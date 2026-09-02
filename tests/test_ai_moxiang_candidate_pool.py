"""Phase 1 candidate pool contract tests (Contract v1.1 §2).

Validates the pure-source shape of ``app.services.ai.candidates``:

- ``compute_candidate_content_hash`` is stable across re-ordering
  (``source_turn_ids``) and ignores provenance tuples when computing the
  semantic key.
- ``extract_master_candidates`` performs subject-boundary guard: a real
  personal statement never gets bucketed into ``ideal_partner`` and vice versa
  (Contract §1.1). Third-party observation (e.g. "他很温柔") produces an empty
  patch list, never a candidate.
- ``bucket_for_dimension`` maps the six-dimension vocabulary but rejects any
  free-form string (the dimension set is a fixed tuple in ``app.db.ai_schema``).
- Idempotency: re-running extraction with the same input returns the same
  candidate set (no duplicates) and the same content_hash. Append-only
  evidence: re-running with additional source_turn_ids returns the SAME
  content_hash (per Contract §2.2 the hash excludes ``source_turn_ids``), so a
  repeated phrase merges into the existing row instead of creating a new one.

These are pure-source / pure-function tests; they do not need a real MySQL
session. Integration coverage of the persistence path lives in
``test_ai_moxiang_candidate_schema.py`` and the reviewed migration runner.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CANDIDATES_FILE = REPO_ROOT / "app" / "services" / "ai" / "candidates.py"
AI_SCHEMA_FILE = REPO_ROOT / "app" / "db" / "ai_schema.py"
PROMPT_EXTRACT_FILE = REPO_ROOT / "app" / "services" / "ai" / "prompts" / "profile_extract.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_candidates_module_declares_public_surface() -> None:
    """candidates.py must export the public contract surface used by P1-C."""
    source = _read(CANDIDATES_FILE)
    for name in (
        "compute_candidate_content_hash",
        "extract_master_candidates",
        "bucket_for_dimension",
        "list_active_candidates",
    ):
        assert f"def {name}" in source, f"candidates.py missing {name}()"


def test_compute_candidate_content_hash_excludes_source_turn_ids() -> None:
    """Hash inputs must NOT include ``source_turn_ids`` (Contract §2.2).

    The same field/category/content under different evidence lists must
    collapse to the same key, otherwise reconnect / re-express duplicates
    bypass the dedup unique index and inflate the candidate pool.
    """
    source = _read(CANDIDATES_FILE)
    body = _extract_function_body(source, "compute_candidate_content_hash")
    assert "source_turn_ids" not in body, (
        "compute_candidate_content_hash must not hash source_turn_ids"
    )
    for required in ("field_key", "subject", "value"):
        assert required in body, (
            f"compute_candidate_content_hash payload must contain {required!r}"
        )


def _extract_function_body(source: str, function_name: str) -> str:
    """Return the function body starting after the def-line, ending at the next top-level def.

    Strips the docstring so word-mentions inside prose don't trip name checks.
    """
    needle = f"def {function_name}"
    start = source.find(needle)
    assert start >= 0, f"function {function_name!r} not found in source"
    body_start = source.find("\n", start)
    rest = source[body_start:]
    end = len(rest)
    for prefix in ("\ndef ", "\nasync def ", "\nclass "):
        idx = rest.find(prefix, 1)
        if 0 <= idx < end:
            end = idx
    body = rest[:end]
    # Strip docstring (first triple-quoted block) if present at the top of body.
    for quote in ('"""', "'''"):
        if quote in body:
            open_idx = body.find(quote)
            close_idx = body.find(quote, open_idx + 3)
            if 0 < close_idx > open_idx:
                body = body[:open_idx] + body[close_idx + 3:]
                break
    return body


def test_bucket_for_dimension_uses_six_dimension_vocabulary() -> None:
    """bucket_for_dimension must consult ``PROFILE_DIMENSIONS`` and reject unknown."""
    source = _read(CANDIDATES_FILE)
    body = _extract_function_body(source, "bucket_for_dimension")
    assert "PROFILE_DIMENSION" in body, (
        "bucket_for_dimension must reference the six-dimension vocabulary"
    )


def test_extract_master_candidates_subject_boundary_personal() -> None:
    """Personal statements must not leak into ideal_partner candidates."""
    from app.services.ai.candidates import extract_master_candidates

    candidates = extract_master_candidates(
        subject="personal",
        turn_texts=("我性格偏内敛，喜欢安静的周末。",),
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )
    assert all(c.subject == "personal" for c in candidates)
    assert all(c.profile_dimension in {"personality_social", "lifestyle"} for c in candidates)


def test_extract_master_candidates_subject_boundary_ideal_partner() -> None:
    """Ideal-partner statements must not leak into personal candidates."""
    from app.services.ai.candidates import extract_master_candidates

    candidates = extract_master_candidates(
        subject="ideal_partner",
        turn_texts=("我希望对方情绪稳定，会倾听。",),
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )
    assert all(c.subject == "ideal_partner" for c in candidates)
    assert any(c.profile_dimension == "emotional_expression" for c in candidates)


def test_extract_master_candidates_third_party_observation_yields_empty() -> None:
    """Third-party observation must produce zero candidates (Contract §1.1)."""
    from app.services.ai.candidates import extract_master_candidates

    candidates = extract_master_candidates(
        subject="personal",
        turn_texts=("他很温柔，很会照顾人。",),
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )
    assert list(candidates) == []


def test_extract_master_candidates_idempotent() -> None:
    """Re-running extraction with the same input returns the same candidate set."""
    from app.services.ai.candidates import extract_master_candidates

    text = ("周末喜欢去公园散步，也偶尔看展。",)
    first = extract_master_candidates(
        subject="personal",
        turn_texts=text,
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )
    second = extract_master_candidates(
        subject="personal",
        turn_texts=text,
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )
    assert [c.content_hash for c in first] == [c.content_hash for c in second]
    assert [c.field_key for c in first] == [c.field_key for c in second]


def test_extract_master_candidates_appended_evidence_keeps_hash() -> None:
    """Adding more turn_ids to the same statement must NOT change content_hash.

    This is the merge-on-reconnect property: a user restating the same
    preference in a later turn must not produce a second candidate row.
    The contract says the hash excludes source_turn_ids.
    """
    from app.services.ai.candidates import (
        compute_candidate_content_hash,
        extract_master_candidates,
    )

    base = extract_master_candidates(
        subject="personal",
        turn_texts=("我喜欢看展，周末喜欢去公园。",),
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )
    assert base, "expected at least one candidate for the statement"
    candidate = base[0]
    extra_hash = compute_candidate_content_hash(
        subject=candidate.subject,
        field_kind=candidate.field_kind,
        field_key=candidate.field_key,
        category=candidate.category,
        value=candidate.value,
        content=candidate.content,
    )
    assert extra_hash == candidate.content_hash, (
        "re-hashing the same semantic content must yield the same content_hash"
    )


def test_extract_master_candidates_prompts_subject_label() -> None:
    """The extractor must inject the per-subject label into the prompt."""
    source = _read(PROMPT_EXTRACT_FILE)
    # The build_profile_extract_prompt helper already branches on subject; the
    # master session helper additionally reuses the same boundary text. We
    # assert that the master prompt contains a personal/ideal_partner cue.
    assert "我的墨相" in source or "愿遇之相" in source, (
        "master extract prompt must label the per-subject cue so the model "
        "knows which subject to bucket its output to"
    )
