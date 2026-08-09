"""AI-CORE Task 12 acceptance contract: redaction, release gate and release evidence.

The three Step 1 tests mirror the task brief verbatim.  ``settings`` is a fresh
``Settings`` instance (no env file so the environment is ``development`` and the
approval flags default to ``False``); ``release_evidence`` is a
:class:`~app.services.ai.flags.ReleaseEvidence` carrying the four required
OpenAPI paths and the explicit Phase 4/5 launch conditions.

The release-verification tests at the bottom exercise ``scripts/verify_ai_release.py``
end to end: ``--environment production`` must never crash with a traceback, must
print the stable ``production_provider_must_not_be_mock`` blocker, exit 2 and
write a report (review I-1); ``--environment testing`` keeps its behaviour.
"""

from __future__ import annotations

import importlib.util
import json
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


def _run_verify(environment: str, report: Path) -> subprocess.CompletedProcess:
    """Run the release verify script as a subprocess and return its result."""
    return subprocess.run(
        [
            sys.executable,
            str(VERIFY_SCRIPT),
            "--environment",
            environment,
            "--report",
            str(report),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
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
    assert payload["environment"] == "production"
    assert payload["release_gate"] == "disabled-until-approved"
    assert "production_provider_must_not_be_mock" in payload["blockers"]
    assert "master_disabled" in payload["blockers"]


def test_testing_mode_behavior_is_unchanged(local_tmp_dir: Path) -> None:
    report = local_tmp_dir / "ai-release-evidence.json"
    result = _run_verify("testing", report)
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    # Task 12 报告：testing 模式当前因证据不全退出码 2。
    assert result.returncode == 2
    assert "release_gate=disabled-until-approved" in result.stdout
    # production 专属 blocker 不应出现在 testing 模式。
    assert "blocker=production_provider_must_not_be_mock" not in result.stdout
    assert report.exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
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
