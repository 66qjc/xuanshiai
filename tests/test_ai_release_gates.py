"""AI-CORE Task 12 acceptance contract: redaction, release gate and release evidence.

The three Step 1 tests mirror the task brief verbatim.  ``settings`` is a fresh
``Settings`` instance (no env file so the environment is ``development`` and the
approval flags default to ``False``); ``release_evidence`` is a
:class:`~app.services.ai.flags.ReleaseEvidence` carrying the four required
OpenAPI paths and the explicit Phase 4/5 launch conditions.

The release-verification tests at the bottom exercise ``scripts/verify_ai_release.py``
end to end: ``--target production`` must never crash with a traceback, must
print the stable ``production_provider_must_not_be_mock`` blocker, exit 2 and
write a report (review I-1); ``--target internal`` keeps its behaviour.

Task 10 Step3 新增 ``--target internal|production`` 分支断言：production 恒
NO-GO、internal 缺证据 NO-GO、``--target`` 优先于 ``--environment``、``--environment``
保留兼容期。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.ai.audit import redact_ai_log
from app.services.ai.flags import ReleaseEvidence, evaluate_ai_release_gate

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_ai_release.py"

REQUIRED_AI_PATHS = (
    "/api/v1/ai/tasks/{task_id}",
    "/api/v1/ai/profile-sessions",
    "/api/v1/ai/search-drafts",
    "/api/v1/ai/compatibility/{target_user_id}",
)


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture()
def release_evidence() -> ReleaseEvidence:
    return ReleaseEvidence(
        required_paths=REQUIRED_AI_PATHS,
        phase4_requires_dpa=True,
        phase5_requires_fairness_review=True,
        blockers=(),
    )


def test_redaction_removes_prompt_and_sensitive_identifiers() -> None:
    safe = redact_ai_log({
        "prompt": "原始回答",
        "phone": "13800000000",
        "id_card": "110101199001010000",
        "task_id": "at_01J",
        "request_id": "req_01J",
    })
    assert "prompt" not in safe
    assert "phone" not in safe
    assert "id_card" not in safe
    assert safe["task_id"] == "at_01J"


def test_release_gate_is_disabled_without_three_approvals(settings, release_evidence) -> None:
    settings.ai_master_enabled = True
    settings.ai_policy_approved = False
    decision = evaluate_ai_release_gate(settings, release_evidence)
    assert decision.enabled is False
    assert decision.code == "AI_FEATURE_DISABLED"


def test_openapi_and_future_phase_gates_are_explicit(release_evidence) -> None:
    assert release_evidence.required_paths
    assert release_evidence.phase4_requires_dpa
    assert release_evidence.phase5_requires_fairness_review


def _load_verify_module() -> Any:
    """Load ``scripts/verify_ai_release.py`` as a module for unit checks."""
    spec = importlib.util.spec_from_file_location("verify_ai_release", VERIFY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def local_tmp_dir() -> Any:
    """A writable scratch dir inside the project root (subprocess tests).

    pytest's system ``tmp_path`` lives outside the working tree and can be
    blocked by sandboxed runners; the verify script subprocess must be able to
    create its report file, so keep the scratch dir inside the repo instead.
    """
    scratch = Path(tempfile.mkdtemp(prefix="verify-gates-", dir=PROJECT_ROOT))
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_verify(
    target: str,
    report: Path,
    *,
    environment: str | None = None,
    evidence_bundle: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run the release verify script as a subprocess and return its result.

    Task 10 Step3：``--target`` 是 required 参数，``--environment`` 与
    ``--evidence-bundle`` 可选。``PYTHONPATH`` 设为 ``PROJECT_ROOT`` 以保证子进程
    import 的是本地 ``app``；``stdin`` 指向 ``DEVNULL`` 避免 Windows pytest
    重定向 stdin 引发 ``WinError 6``。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable,
        str(VERIFY_SCRIPT),
        "--target",
        target,
        "--report",
        str(report),
    ]
    if environment is not None:
        cmd.extend(["--environment", environment])
    if evidence_bundle is not None:
        cmd.extend(["--evidence-bundle", str(evidence_bundle)])
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def test_production_mode_does_not_crash_and_reports_stable_blockers(
    local_tmp_dir: Path,
) -> None:
    report = local_tmp_dir / "ai-release-evidence.json"
    result = _run_verify("production", report)
    # 契约（review I-1）：production 模式不得带 traceback 崩溃。
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    # 稳定 blocker + 退出码 2 + 写报告。
    assert result.returncode == 2
    assert "release_gate=disabled-until-approved" in result.stdout
    assert "blocker=production_provider_must_not_be_mock" in result.stdout
    assert report.exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["target"] == "production"
    assert payload["environment"] == "production"
    assert payload["release_gate"] == "disabled-until-approved"
    assert "production_provider_must_not_be_mock" in payload["blockers"]
    assert "master_disabled" in payload["blockers"]


def test_testing_mode_behavior_is_unchanged(local_tmp_dir: Path) -> None:
    report = local_tmp_dir / "ai-release-evidence.json"
    # ``--target internal`` 应解析为 environment=testing（除非显式给 development）。
    result = _run_verify("internal", report)
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    # Task 12 报告：internal 模式当前因证据不全退出码 2。
    assert result.returncode == 2
    assert "release_gate=disabled-until-approved" in result.stdout
    # production 专属 blocker 不应出现在 internal 模式。
    assert "blocker=production_provider_must_not_be_mock" not in result.stdout
    assert report.exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["target"] == "internal"
    assert payload["environment"] == "testing"
    assert "production_provider_must_not_be_mock" not in payload["blockers"]


def test_production_settings_are_built_via_environment_assertion() -> None:
    module = _load_verify_module()
    settings = module._build_settings("production")
    assert settings.environment == "production"
    assert settings.auto_init_db is False
    blockers = module._config_blockers(settings)
    assert "production_provider_must_not_be_mock" in blockers
    assert "master_disabled" in blockers
    assert "policy_not_approved" in blockers


# ----------------------------------------------------------------------
# Task 10 Step3：``--target`` 分支断言
# ----------------------------------------------------------------------


def test_target_is_required() -> None:
    """``--target`` 是 required；缺失时 argparse 必须 exit 2 且不写报告。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), "--report", "ignored.json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    assert result.returncode != 0
    # argparse 缺 required 参数时输出格式可能因 Python 版本略有差异，只校验关键字。
    assert "--target" in result.stderr
    assert "required" in result.stderr.lower()


def test_target_production_forces_production_environment(
    local_tmp_dir: Path,
) -> None:
    """``--target production`` 即使显式给 ``--environment testing`` 也必须强制 production。"""
    report = local_tmp_dir / "ai-release-evidence.json"
    result = _run_verify("production", report, environment="testing")
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert result.returncode == 2
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["target"] == "production"
    # ``_resolve_environment_from_target`` 在 target=production 下强制 production。
    assert payload["environment"] == "production"
    assert "production_provider_must_not_be_mock" in payload["blockers"]


def test_target_internal_rejects_production_environment(
    local_tmp_dir: Path,
) -> None:
    """``--target internal --environment production`` 必须降级到 testing，绝不走 production。"""
    report = local_tmp_dir / "ai-release-evidence.json"
    result = _run_verify("internal", report, environment="production")
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert result.returncode == 2
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["target"] == "internal"
    # internal target 禁止 production environment，应回退 testing。
    assert payload["environment"] == "testing"
    assert "production_provider_must_not_be_mock" not in payload["blockers"]


def test_internal_target_missing_evidence_bundle_is_no_go(
    local_tmp_dir: Path,
) -> None:
    """internal target 缺证据 bundle 时必须 NO-GO（exit 2）且不误报 GO。"""
    report = local_tmp_dir / "ai-release-evidence.json"
    # 指向不存在的 bundle 路径，确保触发 evidence_bundle_not_found。
    missing_bundle = local_tmp_dir / "does-not-exist.json"
    result = _run_verify("internal", report, evidence_bundle=missing_bundle)
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert result.returncode == 2
    assert "release_gate=disabled-until-approved" in result.stdout
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["target"] == "internal"
    assert payload["release_gate"] == "disabled-until-approved"
    # 必须有证据缺失 blocker，不能空 blockers。
    assert payload["blockers"]
    assert "evidence_bundle_not_found" in payload["blockers"]


def test_production_target_is_always_no_go(local_tmp_dir: Path) -> None:
    """production target 在当前修复阶段恒 NO-GO，绝不误报 GO。"""
    report = local_tmp_dir / "ai-release-evidence.json"
    result = _run_verify("production", report)
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert result.returncode == 2
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["release_gate"] == "disabled-until-approved"
    assert "production_provider_must_not_be_mock" in payload["blockers"]
    assert "master_disabled" in payload["blockers"]
    assert "policy_not_approved" in payload["blockers"]
