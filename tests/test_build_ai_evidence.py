"""Task 10 Step1：``build_ai_evidence`` 的 TDD 失败测试。

本测试只写不跑（证据治理分支硬约束：禁止运行 pytest/ruff/python 脚本来验证）。
它锁定证据构建器必须满足的契约，供下一轮在解除运行禁令后驱动红/绿。

契约要点（与计划 Task 10 Step1 对齐）：

1. artifact 必须包含 schema_version、branch、commit_sha、dirty、environment、
   command、exit_codes、input/output hashes、reviewer、result 等字段。
2. 空 JSON、旧 SHA、未知命令、非零 exit_code、result≠PASS 都必须抛
   ``EvidenceBuildError``。
3. 通过注入 ``GitState`` 避免依赖真实 git 状态。
4. ``validate_evidence_shape`` 对缺字段、坏 SHA、坏 hash、重复 command id、
   非法 gate 状态都返回 blocker。
5. redact：``Bearer xxx``、``user:pass@host``、手机号、身份证号在 argv/metadata
   中被脱敏。
6. production target + mock provider → blocker
   ``production_provider_must_not_be_mock``。
7. 未审查（review_status=PENDING）→ blocker ``independent_review_not_complete``。
8. 用真实临时文件写 ``ai-command-result-v1`` 记录 + stdout/stderr 文件，调用
   ``build_evidence``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.build_ai_evidence import (
    COMMAND_RESULT_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    EvidenceBuildError,
    GitState,
    build_evidence,
    redact_argv,
    redact_text,
    redact_value,
    validate_evidence_shape,
)

# 固定测试用 GitState，避免依赖真实 git 状态。
FIXED_GIT_STATE = GitState(
    commit_sha="0123456789abcdef0123456789abcdef01234567",
    branch="codex/ai-g5-g7-20260817",
    dirty=False,
)
FIXED_COMMIT_SHA = FIXED_GIT_STATE.commit_sha
FIXED_BRANCH = FIXED_GIT_STATE.branch


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _write_command_result(
    tmp_path: Path,
    *,
    command_id: str = "g5.static.ruff",
    gate: str = "G5",
    evidence_kind: str = "static-analysis",
    argv: list[str] | None = None,
    exit_code: int = 0,
    result: str = "PASS",
    commit_sha: str = FIXED_COMMIT_SHA,
    branch: str = FIXED_BRANCH,
    dirty: bool = False,
    environment: str = "testing",
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    stdout_text: str = "All checks passed!\n",
    stderr_text: str = "",
    input_artifacts: list[str] | None = None,
    output_artifacts: list[str] | None = None,
) -> Path:
    """在临时目录写一条合法的 ``ai-command-result-v1`` 记录 + stdout/stderr 文件。"""
    started = started_at or _now()
    finished = finished_at or (started + timedelta(seconds=1))
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = raw_dir / f"{command_id}.stdout.log"
    stderr_path = raw_dir / f"{command_id}.stderr.log"
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")

    result_path = raw_dir / f"{command_id}.command-result.json"
    record = {
        "schema_version": COMMAND_RESULT_SCHEMA_VERSION,
        "id": command_id,
        "gate": gate,
        "evidence_kind": evidence_kind,
        "command": " ".join(argv or ["ruff", "check", "app"]),
        "argv": argv or ["ruff", "check", "app"],
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "finished_at": finished.isoformat().replace("+00:00", "Z"),
        "exit_code": exit_code,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "input_artifacts": input_artifacts or [],
        "output_artifacts": output_artifacts or [],
        "commit_sha": commit_sha,
        "branch": branch,
        "dirty": dirty,
        "environment": environment,
        "result": result,
        "recorded_by": "evidence-tdd-fixture",
    }
    result_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result_path


# ----------------------------------------------------------------------
# Step 1a：合法 artifact 必须包含计划要求的全部字段
# ----------------------------------------------------------------------


def test_legal_artifact_contains_required_fields(tmp_path: Path) -> None:
    """合法构造的证据必须包含计划点名的所有关键字段。"""
    result_path = _write_command_result(tmp_path)
    payload = build_evidence(
        [result_path],
        target="internal",
        environment="testing",
        reviewer="alice",
        review_status="REVIEWED",
        policy_version="ai-policy-2026-08-07-v1",
        provider="mock",
        git_state=FIXED_GIT_STATE,
    )

    # 计划 Task 10 Step1 明确点名的字段。
    for field in (
        "schema_version",
        "branch",
        "commit_sha",
        "dirty",
        "environment",
        "commands",
        "exit_codes",
        "input_artifact_hashes",
        "output_artifact_hashes",
        "reviewer",
        "result",
    ):
        assert field in payload, f"artifact 缺少字段 {field}"

    assert payload["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert payload["branch"] == FIXED_BRANCH
    assert payload["commit_sha"] == FIXED_COMMIT_SHA
    assert payload["dirty"] is False
    assert payload["environment"] == "testing"
    assert payload["commands"], "commands 不能为空"
    command = payload["commands"][0]
    # command 子结构必须携带 argv、exit_code、stdout/stderr hash、result。
    for sub in (
        "id",
        "gate",
        "evidence_kind",
        "command",
        "argv",
        "started_at",
        "finished_at",
        "exit_code",
        "stdout_path",
        "stdout_sha256",
        "stderr_path",
        "stderr_sha256",
        "result_file",
        "result_file_sha256",
        "input_hashes",
        "output_hashes",
        "result",
    ):
        assert sub in command, f"command 缺少子字段 {sub}"
    assert payload["exit_codes"][command["id"]] == 0
    assert payload["reviewer"]["name"] == "alice"
    assert payload["reviewer"]["status"] == "REVIEWED"
    # 单条命令但只覆盖 G5 → 其余 gate NOT_RUN → 整体 NO-GO。
    assert payload["result"] == "NO-GO"


# ----------------------------------------------------------------------
# Step 1b：反例必须抛 EvidenceBuildError
# ----------------------------------------------------------------------


def test_empty_result_file_is_rejected(tmp_path: Path) -> None:
    empty_path = tmp_path / "empty.command-result.json"
    empty_path.write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceBuildError):
        build_evidence(
            [empty_path],
            target="internal",
            environment="testing",
            reviewer="alice",
            git_state=FIXED_GIT_STATE,
        )


def test_stale_commit_sha_is_rejected(tmp_path: Path) -> None:
    stale = GitState(
        commit_sha="ffffffffffffffffffffffffffffffffffffffff",
        branch=FIXED_BRANCH,
        dirty=False,
    )
    result_path = _write_command_result(tmp_path)
    with pytest.raises(EvidenceBuildError, match="stale commit_sha"):
        build_evidence(
            [result_path],
            target="internal",
            environment="testing",
            reviewer="alice",
            git_state=stale,
        )


def test_unknown_command_argv_is_rejected(tmp_path: Path) -> None:
    # ``rm -rf /`` 不在 command allowlist 中。
    result_path = _write_command_result(
        tmp_path,
        command_id="g5.unsafe.rm",
        argv=["rm", "-rf", "/"],
    )
    with pytest.raises(EvidenceBuildError, match="unknown or unsafe command"):
        build_evidence(
            [result_path],
            target="internal",
            environment="testing",
            reviewer="alice",
            git_state=FIXED_GIT_STATE,
        )


def test_non_zero_exit_code_is_rejected(tmp_path: Path) -> None:
    result_path = _write_command_result(
        tmp_path,
        command_id="g5.failing.pytest",
        evidence_kind="unit-tests",
        argv=["pytest", "tests/test_build_ai_evidence.py", "-q"],
        exit_code=1,
    )
    with pytest.raises(EvidenceBuildError, match="non-zero exit_code"):
        build_evidence(
            [result_path],
            target="internal",
            environment="testing",
            reviewer="alice",
            git_state=FIXED_GIT_STATE,
        )


def test_result_not_pass_is_rejected(tmp_path: Path) -> None:
    result_path = _write_command_result(
        tmp_path,
        command_id="g5.fail.result",
        result="FAIL",
        exit_code=0,
    )
    with pytest.raises(EvidenceBuildError, match="result must be PASS"):
        build_evidence(
            [result_path],
            target="internal",
            environment="testing",
            reviewer="alice",
            git_state=FIXED_GIT_STATE,
        )


def test_dirty_state_mismatch_is_rejected(tmp_path: Path) -> None:
    result_path = _write_command_result(tmp_path, dirty=True)
    with pytest.raises(EvidenceBuildError, match="dirty state mismatch"):
        build_evidence(
            [result_path],
            target="internal",
            environment="testing",
            reviewer="alice",
            git_state=FIXED_GIT_STATE,  # dirty=False
        )


def test_duplicate_command_id_is_rejected(tmp_path: Path) -> None:
    first = _write_command_result(tmp_path, command_id="g5.dup.id")
    # 复制一份到不同路径但相同 id，触发 duplicate command id 检测。
    second_dir = tmp_path / "raw2"
    second_dir.mkdir(parents=True, exist_ok=True)
    second_record = json.loads(first.read_text(encoding="utf-8"))
    second_path = second_dir / "g5.dup.id.command-result.json"
    second_record["stdout_path"] = str(second_dir / "g5.dup.id.stdout.log")
    second_record["stderr_path"] = str(second_dir / "g5.dup.id.stderr.log")
    (second_dir / "g5.dup.id.stdout.log").write_text("ok\n", encoding="utf-8")
    (second_dir / "g5.dup.id.stderr.log").write_text("", encoding="utf-8")
    second_path.write_text(
        json.dumps(second_record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(EvidenceBuildError, match="duplicate command id"):
        build_evidence(
            [first, second_path],
            target="internal",
            environment="testing",
            reviewer="alice",
            git_state=FIXED_GIT_STATE,
        )


# ----------------------------------------------------------------------
# Step 1c：validate_evidence_shape 对结构问题返回 blocker
# ----------------------------------------------------------------------


def test_validate_shape_empty_object_returns_blocker() -> None:
    blockers = validate_evidence_shape({})
    assert "evidence_empty_or_not_object" in blockers


def test_validate_shape_missing_required_fields_return_blockers() -> None:
    blockers = validate_evidence_shape({"schema_version": EVIDENCE_SCHEMA_VERSION})
    assert any(name.startswith("evidence_missing_field:") for name in blockers)


def test_validate_shape_bad_sha_returns_blocker() -> None:
    payload = _minimal_valid_payload()
    payload["commit_sha"] = "not-a-sha"
    blockers = validate_evidence_shape(payload)
    assert "evidence_commit_sha_invalid" in blockers


def test_validate_shape_bad_hash_returns_blocker() -> None:
    payload = _minimal_valid_payload()
    payload["output_artifact_hashes"] = {"bad/path": "not-a-hash"}
    blockers = validate_evidence_shape(payload)
    assert any(
        name.startswith("evidence_hash_invalid:output_artifact_hashes")
        for name in blockers
    )


def test_validate_shape_duplicate_command_id_returns_blocker() -> None:
    payload = _minimal_valid_payload()
    payload["commands"] = [*payload["commands"], payload["commands"][0]]
    blockers = validate_evidence_shape(payload)
    assert any(name.startswith("evidence_command_duplicate:") for name in blockers)


def test_validate_shape_invalid_gate_status_returns_blocker() -> None:
    payload = _minimal_valid_payload()
    payload["gates"]["G5"] = {"status": "MAYBE", "command_ids": []}
    blockers = validate_evidence_shape(payload)
    assert "evidence_gate_status_invalid:G5" in blockers


# ----------------------------------------------------------------------
# Step 1d：redact 脱敏
# ----------------------------------------------------------------------


def test_redact_text_masks_bearer_url_credentials_phone_id_card() -> None:
    assert "Bearer [REDACTED]" in redact_text("Authorization: Bearer abcdef1234567890")
    assert "[REDACTED]@" in redact_text("https://user:pass@host.example/path")
    assert "[REDACTED_PHONE]" in redact_text("contact 13800000000 now")
    assert "[REDACTED_ID_CARD]" in redact_text("id=110101199001010000")


def test_redact_argv_masks_sensitive_flags() -> None:
    redacted = redact_argv(["pytest", "--token", "super-secret-value", "tests"])
    assert "super-secret-value" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_value_masks_sensitive_keys() -> None:
    redacted = redact_value({"api_key": "sk-xxxx", "phone": "13800000000"}, "api_key")
    assert redacted["api_key"] == "[REDACTED]"


# ----------------------------------------------------------------------
# Step 1e：production target + mock provider → blocker
# ----------------------------------------------------------------------


def test_production_target_with_mock_provider_adds_blocker(tmp_path: Path) -> None:
    result_path = _write_command_result(tmp_path, environment="production")
    payload = build_evidence(
        [result_path],
        target="production",
        environment="production",
        reviewer="alice",
        review_status="REVIEWED",
        policy_version="ai-policy-2026-08-07-v1",
        provider="mock",  # production 禁用 mock
        git_state=FIXED_GIT_STATE,
    )
    assert "production_provider_must_not_be_mock" in payload["blockers"]
    assert payload["result"] == "NO-GO"


def test_production_target_requires_production_environment(tmp_path: Path) -> None:
    result_path = _write_command_result(tmp_path, environment="testing")
    with pytest.raises(EvidenceBuildError, match="production environment"):
        build_evidence(
            [result_path],
            target="production",
            environment="testing",
            reviewer="alice",
            git_state=FIXED_GIT_STATE,
        )


# ----------------------------------------------------------------------
# Step 1f：未审查 → blocker
# ----------------------------------------------------------------------


def test_pending_review_adds_blocker(tmp_path: Path) -> None:
    result_path = _write_command_result(tmp_path)
    payload = build_evidence(
        [result_path],
        target="internal",
        environment="testing",
        reviewer="alice",
        review_status="PENDING",  # 未审查
        policy_version="ai-policy-2026-08-07-v1",
        provider="mock",
        git_state=FIXED_GIT_STATE,
    )
    assert "independent_review_not_complete" in payload["blockers"]
    assert payload["result"] == "NO-GO"


# ----------------------------------------------------------------------
# 辅助：构造一份结构合法的 payload 给 validate_evidence_shape 用
# ----------------------------------------------------------------------


def _minimal_valid_payload() -> dict:
    """构造一份能通过 ``validate_evidence_shape`` 的最小 payload。"""
    started = _now().isoformat().replace("+00:00", "Z")
    finished = (_now() + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    command = {
        "id": "g5.demo",
        "gate": "G5",
        "evidence_kind": "static-analysis",
        "command": "ruff check app",
        "argv": ["ruff", "check", "app"],
        "started_at": started,
        "finished_at": finished,
        "exit_code": 0,
        "stdout_path": "artifacts/raw/g5.demo.stdout.log",
        "stdout_sha256": "0" * 64,
        "stderr_path": "artifacts/raw/g5.demo.stderr.log",
        "stderr_sha256": "0" * 64,
        "result_file": "artifacts/raw/g5.demo.command-result.json",
        "result_file_sha256": "0" * 64,
        "input_hashes": {},
        "output_hashes": {},
        "result": "PASS",
    }
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "artifact_type": "command-bundle",
        "target": "internal",
        "branch": FIXED_BRANCH,
        "commit_sha": FIXED_COMMIT_SHA,
        "dirty": False,
        "environment": "testing",
        "generated_at": finished,
        "policy_version": "ai-policy-2026-08-07-v1",
        "provider": "mock",
        "model": None,
        "schema_versions": {
            "evidence": EVIDENCE_SCHEMA_VERSION,
            "command_result": COMMAND_RESULT_SCHEMA_VERSION,
        },
        "commands": [command],
        "exit_codes": {"g5.demo": 0},
        "input_artifact_hashes": {
            "artifacts/raw/g5.demo.command-result.json": "0" * 64
        },
        "output_artifact_hashes": {},
        "reviewer": {
            "name": "alice",
            "role": "independent-evidence-reviewer",
            "status": "REVIEWED",
        },
        "gates": {
            "G0": {"status": "NOT_RUN", "command_ids": []},
            "G1": {"status": "NOT_RUN", "command_ids": []},
            "G2": {"status": "NOT_RUN", "command_ids": []},
            "G3": {"status": "NOT_RUN", "command_ids": []},
            "G4": {"status": "NOT_RUN", "command_ids": []},
            "G5": {"status": "PASS", "command_ids": ["g5.demo"]},
            "G6": {"status": "NOT_RUN", "command_ids": []},
            "G7": {"status": "NOT_RUN", "command_ids": []},
        },
        "blockers": [],
        "result": "PASS",
    }
