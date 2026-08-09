"""AI release verification: aggregate evidence and decide the release gate.

Task 12 / unified plan §12.3 / §13.4 的上线证据聚合器。脚本只读，绝不修改任何
生产开关。它汇总：

1. 配置门禁：approvals（``ai_policy_approved``/``ai_provider_approved``）、保留期
   （``ai_retention_policy_version``）与 Provider（生产禁止 mock）。
2. 数据库表：16 张 AI 表 + 3 张 derivation 表（可连接时核对）。
3. OpenAPI 四路径：tasks/{task_id}、profile-sessions、search-drafts、
   compatibility/{target_user_id}。
4. 隐私矩阵、mock 失败注入、删除回放、shadow 报告、回滚演练证据。

任何一项缺失都计入稳定 blocker；有 blocker 时 gate 为
``disabled-until-approved`` 且退出码 2。报告写入 ``--report`` 指向的 JSON 文件。

用法：:

    uv run python scripts/verify_ai_release.py \\
        --environment testing --report artifacts/ai-release-evidence.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.main import app
from pydantic import ValidationError

# 统一方案 §13.4 / 执行计划 §7 要求的四组 AI 对外路径。
REQUIRED_AI_PATHS = (
    "/api/v1/ai/tasks/{task_id}",
    "/api/v1/ai/profile-sessions",
    "/api/v1/ai/search-drafts",
    "/api/v1/ai/compatibility/{target_user_id}",
)

# 16 张 AI 表 + 3 张 derivation 表（统一方案 §10；Task 5/9 交付）。
AI_TABLE_NAMES = (
    "ai_consent_grant",
    "ai_task",
    "ai_generation_audit",
    "ai_profile_session",
    "ai_profile_turn",
    "ai_profile_draft",
    "ai_profile_draft_field",
    "ai_profile_revision",
    "ai_profile_revision_field",
    "ai_profile_summary",
    "ai_search_draft",
    "ai_search_condition",
    "ai_search_snapshot",
    "ai_search_result",
    "ai_feature_projection",
    "ai_compatibility_snapshot",
)
DERIVATION_TABLE_NAMES = (
    "user_revision_state",
    "derivation_outbox",
    "derivation_consumer_receipt",
)

ROOT = Path(__file__).resolve().parents[1]


def _db_connect_params(settings: Settings) -> dict[str, Any] | None:
    """Translate ``settings.database_url`` into synchronous pymysql params."""
    from urllib.parse import unquote, urlsplit

    url = settings.database_url.replace("mysql+aiomysql://", "mysql://", 1)
    parsed = urlsplit(url)
    if not (
        parsed.scheme == "mysql"
        and parsed.hostname
        and parsed.username
        and parsed.password is not None
        and parsed.port
    ):
        return None
    database = parsed.path.lstrip("/")
    if not database:
        return None
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password),
        "database": database,
        "charset": "utf8mb4",
    }


def _config_blockers(settings: Settings) -> list[str]:
    """Config gate blockers (§6.6 / §13.4 item 3)."""
    blockers: list[str] = []
    if not settings.ai_master_enabled:
        blockers.append("master_disabled")
    if not settings.ai_policy_approved:
        blockers.append("policy_not_approved")
    if not settings.ai_provider_approved:
        blockers.append("provider_not_approved")
    if not settings.ai_retention_policy_version:
        blockers.append("retention_policy_missing")
    if settings.environment == "production" and settings.ai_provider == "mock":
        blockers.append("production_provider_must_not_be_mock")
    return blockers


def _table_blockers(settings: Settings) -> tuple[list[str], dict[str, Any]]:
    """Verify the 16 AI + 3 derivation tables when the DB is reachable."""
    evidence: dict[str, Any] = {}
    params = _db_connect_params(settings)
    if params is None:
        return (
            ["database_unreachable"],  # 证据缺失：无法核对表结构
            {"tables": {"verified": False, "reason": "unparseable database_url"}},
        )
    try:
        import pymysql

        with pymysql.connect(**params) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s",
                    (params["database"],),
                )
                existing = {row[0] for row in cur.fetchall()}
    except Exception:  # noqa: BLE001 - 连接失败按证据缺失处理
        return (
            ["database_unreachable"],
            {"tables": {"verified": False, "reason": "connection_failed"}},
        )
    missing_ai = [name for name in AI_TABLE_NAMES if name not in existing]
    missing_derivation = [
        name for name in DERIVATION_TABLE_NAMES if name not in existing
    ]
    blockers: list[str] = []
    if missing_ai:
        blockers.append(f"missing_ai_tables:{','.join(missing_ai)}")
    if missing_derivation:
        blockers.append(f"missing_derivation_tables:{','.join(missing_derivation)}")
    evidence["tables"] = {
        "verified": True,
        "ai_tables_verified": len(AI_TABLE_NAMES) - len(missing_ai),
        "ai_tables_expected": len(AI_TABLE_NAMES),
        "derivation_tables_verified": len(DERIVATION_TABLE_NAMES) - len(missing_derivation),
        "derivation_tables_expected": len(DERIVATION_TABLE_NAMES),
        "missing_ai_tables": missing_ai,
        "missing_derivation_tables": missing_derivation,
    }
    return blockers, evidence


def _file_contains(path: Path, needles: tuple[str, ...]) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return all(needle in text for needle in needles)


def _static_evidence_blockers() -> tuple[list[str], dict[str, Any]]:
    """Aggregate static release evidence delivered by Tasks 1-12."""
    blockers: list[str] = []
    evidence: dict[str, Any] = {}

    # OpenAPI 四路径（§12.3：完整 API/OpenAPI）。
    spec_paths = set(app.openapi().get("paths", {}))
    missing_paths = [path for path in REQUIRED_AI_PATHS if path not in spec_paths]
    if missing_paths:
        blockers.append(f"missing_openapi_paths:{','.join(missing_paths)}")
    evidence["openapi"] = {
        "required_paths": list(REQUIRED_AI_PATHS),
        "missing": missing_paths,
    }

    # 隐私矩阵（§12.1 隐私集成 / §5.3）。
    privacy_ok = (
        (ROOT / "docs/ai/AI_PRODUCT_SECURITY_DECISIONS.md").exists()
        and (ROOT / "tests/test_candidate_visibility.py").exists()
    )
    if not privacy_ok:
        blockers.append("privacy_matrix_evidence_missing")
    evidence["privacy_matrix"] = {"verified": privacy_ok}

    # mock 失败注入（§12.1 任务并发：429/5xx/schema-invalid/policy）。
    mock_failure_ok = _file_contains(
        ROOT / "tests/test_ai_schema_and_provider.py",
        ("failures=", "AI_TEMPORARILY_UNAVAILABLE", "AI_POLICY_DENIED"),
    )
    if not mock_failure_ok:
        blockers.append("mock_failure_injection_missing")
    evidence["mock_failure_injection"] = {"verified": mock_failure_ok}

    # 删除回放（§12.1 M04 回放 / §13.3 数据回滚）。
    deletion_replay_ok = _file_contains(
        ROOT / "tests/test_ai_profile_publish.py",
        ("cleanup", "invalidat"),
    )
    if not deletion_replay_ok:
        blockers.append("deletion_replay_missing")
    evidence["deletion_replay"] = {"verified": deletion_replay_ok}

    # shadow 报告（§12.1 M06 双向 / §12.2 M06 指标）。
    shadow_ok = _file_contains(
        ROOT / "tests/test_ai_compatibility.py",
        ("shadow", "display_eligible"),
    )
    if not shadow_ok:
        blockers.append("shadow_report_missing")
    evidence["shadow_report"] = {"verified": shadow_ok}

    # 回滚演练证据（§13.4 item 5：回滚演练成功后才可启用）。
    rollback_drill = ROOT / "artifacts" / "ai-rollback-drill.json"
    if not rollback_drill.exists():
        blockers.append("rollback_drill_not_evidenced")
    else:
        try:
            rollback_drill.read_text(encoding="utf-8")
            evidence["rollback_drill"] = {"verified": True}
        except OSError:
            blockers.append("rollback_drill_not_evidenced")
            evidence["rollback_drill"] = {"verified": False}

    return blockers, evidence


# 全部业务 task_type。缺一个 handler，独立 Worker 就会把该类型任务打回
# AI_FEATURE_DISABLED failed（final review C-1/C-2/C-3 的 gate 盲区）。
REQUIRED_WORKER_TASK_TYPES = (
    "profile_extract",
    "search_parse",
    "search_execute",
    "compatibility",
    "profile_projection",
    "cleanup",
)


def _handler_registration_blockers() -> tuple[list[str], dict[str, Any]]:
    """Verify every business task_type has a registered worker handler (C-1/C-2).

    Importing ``app.workers.ai_worker`` runs ``register_business_handlers``, so a
    standalone ``python -m app.workers.ai_worker`` process can dispatch all six
    task types.  Any missing handler is a blocker (exit 2) — the gate must never
    pass while M03/M06/投影/清理任务在真实 Worker 中无声失败。
    """
    from app.workers import ai_worker as worker_module

    registered = set(worker_module.TASK_HANDLERS)
    missing = [t for t in REQUIRED_WORKER_TASK_TYPES if t not in registered]
    blockers: list[str] = []
    if missing:
        blockers.append(f"missing_worker_handlers:{','.join(missing)}")
    evidence = {
        "worker_handlers": {
            "required_task_types": list(REQUIRED_WORKER_TASK_TYPES),
            "registered": sorted(registered),
            "missing": missing,
        }
    }
    return blockers, evidence


def _consumer_scheduling_blockers() -> tuple[list[str], dict[str, Any]]:
    """Verify the cleanup consumer has a production scheduling entry point (C-3).

    ``run_cleanup_consumer_round``（删除/撤回的异步传播）必须能通过 Worker 的
    ``--consumers`` 模式调度（``python -m app.workers.ai_worker --consumers``），
    否则删除传播在生产只是死代码。
    """
    worker_source = ROOT / "app" / "workers" / "ai_worker.py"
    try:
        text = worker_source.read_text(encoding="utf-8")
    except OSError:
        return (
            ["cleanup_consumer_not_schedulable"],
            {"cleanup_consumer": {"verified": False, "reason": "worker source unreadable"}},
        )
    schedulable = (
        "run_cleanup_consumer_round" in text
        and "--consumers" in text
        and "run_cleanup_consumer_round(db, worker_id, _now(), batch_size)" in text
    )
    blockers = [] if schedulable else ["cleanup_consumer_not_schedulable"]
    evidence = {
        "cleanup_consumer": {
            "verified": schedulable,
            "scheduling": "python -m app.workers.ai_worker --consumers",
        }
    }
    return blockers, evidence


def _build_settings(environment: str) -> Settings:
    """Build read-only Settings for the requested environment.

    ``production``/``staging`` deliberately do not construct a full production
    ``Settings``: a bare ``Settings(environment="production", ...)`` trips the
    unrelated SMS/WeChat/payment mock validators in ``config.validate_test_providers``
    before the AI gate logic can run, crashing the script with an unhandled
    traceback (review I-1).  Instead we build a deterministic minimal ``Settings``
    in a test-mode base environment (defaults + process env only; ``_env_file=None``)
    and then apply the requested environment as an explicit assertion, so the
    production-specific blockers in :func:`_config_blockers` remain reachable.
    The script never writes any production switch; it only reads configuration.
    """
    base_environment = (
        environment if environment in {"development", "testing"} else "testing"
    )
    settings = Settings(
        _env_file=None,
        environment=base_environment,
        auto_init_db=environment not in {"staging", "production"},
    )
    if environment not in {"development", "testing"}:
        settings.environment = environment  # 显式断言目标环境（不重跑 test-only 校验器）
    return settings


def _write_report(path: str, payload: dict[str, Any]) -> None:
    report = Path(path)
    if report.parent and str(report.parent) != ".":
        report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI 上线证据聚合与 release gate 判定（只读，不改开关）"
    )
    parser.add_argument(
        "--environment",
        default="testing",
        choices=("development", "testing", "staging", "production"),
        help="目标环境；默认 testing",
    )
    parser.add_argument(
        "--report",
        default="artifacts/ai-release-evidence.json",
        help="证据 JSON 输出路径",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = _build_settings(args.environment)
    except ValidationError as exc:
        # 兜底：任何 Settings 构造失败（例如 shell/进程环境注入非法配置）都计为
        # 稳定 blocker 收尾，绝不带 traceback 崩溃——统一契约 exit 2 + 写报告。
        payload = {
            "environment": args.environment,
            "release_gate": "disabled-until-approved",
            "decision_code": "AI_FEATURE_DISABLED",
            "blockers": ["settings_invalid"],
            "config": {},
            "evidence": {"settings": {"verified": False, "error": str(exc)}},
        }
        _write_report(args.report, payload)
        print(f"environment={args.environment}")
        print("release_gate=disabled-until-approved")
        print("blocker=settings_invalid")
        print(f"report={args.report}")
        return 2

    # 延迟导入以保持模块可 import（-h/--help 无需依赖全部服务）。
    from app.services.ai.flags import ReleaseEvidence, evaluate_ai_release_gate

    blockers = _config_blockers(settings)
    table_blockers, table_evidence = _table_blockers(settings)
    static_blockers, static_evidence = _static_evidence_blockers()
    handler_blockers, handler_evidence = _handler_registration_blockers()
    consumer_blockers, consumer_evidence = _consumer_scheduling_blockers()
    blockers.extend(table_blockers)
    blockers.extend(static_blockers)
    blockers.extend(handler_blockers)
    blockers.extend(consumer_blockers)

    evidence = ReleaseEvidence(
        required_paths=REQUIRED_AI_PATHS,
        phase4_requires_dpa=True,
        phase5_requires_fairness_review=True,
        blockers=tuple(dict.fromkeys(blockers)),
    )
    decision = evaluate_ai_release_gate(settings, evidence)

    payload = {
        "environment": args.environment,
        "release_gate": decision.release_gate,
        "decision_code": decision.code,
        "blockers": list(decision.blockers),
        "config": {
            "ai_master_enabled": settings.ai_master_enabled,
            "ai_policy_approved": settings.ai_policy_approved,
            "ai_provider_approved": settings.ai_provider_approved,
            "ai_retention_policy_version": settings.ai_retention_policy_version,
            "ai_provider": settings.ai_provider,
        },
        "evidence": {
            **table_evidence,
            **static_evidence,
            **handler_evidence,
            **consumer_evidence,
            "phase4_requires_dpa": evidence.phase4_requires_dpa,
            "phase5_requires_fairness_review": evidence.phase5_requires_fairness_review,
        },
    }
    _write_report(args.report, payload)

    print(f"environment={args.environment}")
    print(f"release_gate={decision.release_gate}")
    for blocker in decision.blockers:
        print(f"blocker={blocker}")
    print(f"report={args.report}")

    # 任何证据缺失或门禁未通过都必须以退出码 2 收尾，绝不误报通过。
    return 2 if decision.blockers else 0


if __name__ == "__main__":
    sys.exit(main())
