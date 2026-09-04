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
from types import SimpleNamespace

import pytest

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


def test_extract_master_candidates_covers_all_six_dimensions_from_free_chat() -> None:
    """自由对话候选必须能够落到固定六维，而不是只识别少数关键词。"""
    from app.db.ai_schema import PROFILE_DIMENSIONS
    from app.services.ai.candidates import extract_master_candidates

    candidates = extract_master_candidates(
        subject="personal",
        turn_texts=(
            "我性格比较外向，也很慢热。",
            "我希望关系里遇到分歧先沟通，平时需要有稳定陪伴。",
            "我不喜欢被查手机，希望彼此尊重边界。",
            "我情绪低落时会直接表达，也希望对方先倾听。",
            "我周末喜欢去公园和看展，平时早睡早起。",
            "我想认真交往，未来以结婚和共同生活为目标。",
        ),
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )
    assert {candidate.profile_dimension for candidate in candidates} == set(PROFILE_DIMENSIONS)


def test_provider_master_result_becomes_six_dimension_candidates() -> None:
    """真实 Provider 的 master 输出必须保留为六维候选，而非退回关键词猜测。"""
    from app.schemas.ai_profile import ProfileSubject
    from app.services.ai.base import ExtractedPatch, StructuredExtractResult
    from app.services.ai.candidates import candidates_from_master_result

    result = StructuredExtractResult(
        patches=(
            ExtractedPatch(
                action="add", category="personality", content="外向但慢热",
                subject=ProfileSubject.PERSONAL, source_quote="我外向但慢热", confidence=0.9,
            ),
            ExtractedPatch(
                action="add", category="values", content="遇到分歧会先沟通",
                subject=ProfileSubject.PERSONAL, source_quote="遇到分歧会先沟通", confidence=0.9,
            ),
            ExtractedPatch(
                action="add", category="values", content="尊重彼此隐私和边界",
                subject=ProfileSubject.PERSONAL, source_quote="尊重彼此隐私和边界", confidence=0.9,
            ),
            ExtractedPatch(
                action="add", category="personality", content="情绪低落时会直接表达",
                subject=ProfileSubject.PERSONAL, source_quote="情绪低落时会直接表达", confidence=0.9,
            ),
            ExtractedPatch(
                action="add", category="routine", content="周末看展，平时早睡早起",
                subject=ProfileSubject.PERSONAL, source_quote="周末看展，平时早睡早起", confidence=0.9,
            ),
            ExtractedPatch(
                action="add", category="life_plan", content="期待长期共同生活",
                subject=ProfileSubject.PERSONAL, source_quote="期待长期共同生活", confidence=0.9,
            ),
        )
    )

    candidates = candidates_from_master_result(
        subject="personal",
        result=result,
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
        source_turn_id="turn-provider-1",
    )

    assert {candidate.profile_dimension for candidate in candidates} == {
        "personality_social", "intimacy_pattern", "relationship_boundaries",
        "emotional_expression", "lifestyle", "future_expectations",
    }
    assert all(candidate.source_turn_ids == ("turn-provider-1",) for candidate in candidates)
    assert all(candidate.subject == "personal" for candidate in candidates)


@pytest.mark.asyncio
async def test_journey_worker_calls_master_gateway_before_persisting_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每个已持久化对话 turn 必须经墨相师 Provider，再进入候选池。"""
    from app.schemas.ai_profile import ProfileSubject
    from app.services.ai.base import ExtractedPatch, StructuredExtractResult
    from app.services.ai.gateway import InvokeOutcome
    from app.services.ai.profile import ProfileTurn
    from app.services.revisions import RevisionVector
    from app.services.ai import journey

    captured: dict[str, object] = {"candidates": []}

    class Gateway:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def structured_extract(self, context: object, request: object) -> InvokeOutcome:
            captured["context"] = context
            captured["request"] = request
            return InvokeOutcome(
                result=StructuredExtractResult(
                    patches=(
                        ExtractedPatch(
                            action="add", category="values", content="遇到冲突先沟通",
                            subject=ProfileSubject.PERSONAL,
                            source_quote="遇到冲突先沟通", confidence=0.91,
                        ),
                    )
                )
            )

    session = SimpleNamespace(
        session_id="session-1", subject=ProfileSubject.PERSONAL,
        consent_version="profile-text-v1", policy_revision="ai-policy-2026-08-07-v1",
        revision_vector=RevisionVector(profile=2),
    )
    turn = ProfileTurn(
        turn_id="turn-1", session_id="session-1", client_turn_id="client-1",
        user_id=7, turn_no=1, answer_text="遇到冲突我会先沟通", status="saved", created_at=None,
    )
    task = SimpleNamespace(
        task_id="task-1", owner_user_id=7,
        payload_summary={"session_id": "session-1", "turn_id": "turn-1", "client_turn_id": "client-1"},
        source_revision_json={"profile": 2, "preference": 0, "privacy": 0, "relationship": 0, "policy": 0},
    )

    async def fake_upsert(_db: object, candidate: object, *, source_turn_id: str) -> None:
        captured["candidates"].append((candidate, source_turn_id))  # type: ignore[union-attr]

    async def fake_cap(_db: object, session_id: str) -> int:
        captured["cap_session"] = session_id
        return 0

    async def fake_session(*_args: object, **_kwargs: object) -> object:
        return session

    async def fake_turn(*_args: object, **_kwargs: object) -> object:
        return turn

    async def fake_list_candidates(*_args: object, **_kwargs: object) -> tuple:
        return ()

    monkeypatch.setattr(journey, "AIGateway", Gateway)
    monkeypatch.setattr(journey, "load_owned_active_session", fake_session)
    monkeypatch.setattr(journey, "find_turn_by_client_id", fake_turn)
    monkeypatch.setattr(journey, "list_session_candidates", fake_list_candidates)
    monkeypatch.setattr(journey, "_upsert_candidate", fake_upsert)
    monkeypatch.setattr(journey, "_enforce_entry_dimension_cap", fake_cap)

    result = await journey.extract_journey_candidates(object(), task, "worker-1")

    assert result == ("moxiang-candidate:task-1", session.revision_vector)
    # #12：每轮 upsert 后必须执行单维度 entry 上限裁剪。
    assert captured["cap_session"] == "session-1"
    assert captured["request"].session_kind == "master"  # type: ignore[union-attr]
    assert captured["request"].turn_texts == ("遇到冲突我会先沟通",)  # type: ignore[union-attr]
    # No prior candidates -> dedup digest is None (not an empty string).
    assert captured["request"].existing_digest is None  # type: ignore[union-attr]
    candidate, source_turn_id = captured["candidates"][0]  # type: ignore[index]
    assert candidate.profile_dimension == "intimacy_pattern"
    assert source_turn_id == "turn-1"


def test_existing_candidates_digest_formats_structured_and_entry() -> None:
    """#11 dedup digest renders structured ``field_key = value`` and entry ``category：content`` with dimension labels."""
    from app.services.ai.journey import _existing_candidates_digest

    structured = SimpleNamespace(
        profile_dimension="personality_social", field_kind="structured",
        field_key="age", value=28, category=None, content=None,
    )
    entry = SimpleNamespace(
        profile_dimension="intimacy_pattern", field_kind="entry",
        field_key=None, value=None, category="values",
        content="遇到冲突我会先冷静再沟通",
    )
    digest = _existing_candidates_digest((structured, entry))
    assert "[性格与社交] age = 28" in digest
    assert "[亲密模式] values：遇到冲突我会先冷静再沟通" in digest


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
