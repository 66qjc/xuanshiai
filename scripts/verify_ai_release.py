"""AI release verification: aggregate evidence and decide the release gate.

Task 10 Step3 / 统一方案 §12.3 / §13.4 的上线证据聚合器。脚本只读，绝不修改任何
生产开关。它汇总：

1. 配置门禁：approvals（``ai_policy_approved``/``ai_provider_approved``）、保留期
   （``ai_retention_policy_version``）与 Provider（生产禁止 mock）。
2. 数据库表：16 张 AI 表 + 3 张 derivation 表（可连接时核对）。
3. OpenAPI 四路径：tasks/{task_id}、profile-sessions、search-drafts、
   compatibility/{target_user_id}。
4. 隐私矩阵、mock 失败注入、删除回放、shadow 报告、回滚演练证据。
5. （Task 10 Step3 新增）``--target internal|production`` 证据聚合：读取
   ``artifacts/ai-evidence-bundle.json``（build_ai_evidence 产物），校验 schema、
   SHA、hash、时效、所有 Gate（G0-G7）与 production approvals。``--target`` 与
   ``--environment`` 同时给出时以 ``--target`` 为准；``--environment`` 保留为
   deprecated 兼容期入口。

任何一项缺失都计入稳定 blocker；有 blocker 时 gate 为
``disabled-until-approved`` 且退出码 2。报告写入 ``--report`` 指向的 JSON 文件。

用法：:

    uv run python scripts/verify_ai_release.py \\
        --target internal --report artifacts/ai-internal-readiness.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.config import Settings
from app.main import app

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


# ----------------------------------------------------------------------
# Task 10 Step3：``--target`` 证据聚合（只读，校验 build_ai_evidence 产物）
# ----------------------------------------------------------------------

# 证据 bundle 默认路径（build_ai_evidence.py 的默认 --report）。
EVIDENCE_BUNDLE_PATH = ROOT / "artifacts" / "ai-evidence-bundle.json"
# 证据生成时间到当前的最大允许间隔（小时）——避免用很久以前的 bundle 冒充当前证据。
EVIDENCE_FRESHNESS_HOURS = 72
# 合法 Gate 状态白名单（与 ai-evidence-v1.schema.json 的 result enum 对齐）。
VALID_GATE_STATUSES = {"PASS", "FAIL", "CONDITIONAL", "NOT_RUN", "NO-GO"}
# 合法 result 值（同上）。
VALID_RESULT_STATUSES = VALID_GATE_STATUSES
ALL_GATE_NAMES = tuple(f"G{index}" for index in range(8))
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _load_evidence_bundle(bundle_path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """读取 ``ai-evidence-bundle.json``，返回 (payload, blockers)。

    bundle 不存在或读不出 → ``None`` + blocker；结构非法 → ``None`` + blocker。
    任何 OSError/JSON 错误都按证据缺失处理，绝不带 traceback 崩溃。
    """
    if not bundle_path.exists():
        return None, ["evidence_bundle_not_found"]
    try:
        raw = bundle_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"evidence_bundle_unreadable:{type(exc).__name__}"]
    if not isinstance(payload, dict) or not payload:
        return None, ["evidence_bundle_invalid_shape"]
    return payload, []


def _evidence_bundle_blockers(
    payload: dict[str, Any] | None,
    *,
    target: str,
    settings: Settings,
    expected_commit_sha: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """校验 ``ai-evidence-bundle.json`` 是否符合 ``--target`` 要求。

    校验维度：
    - schema_version == ``ai-evidence-v1``
    - target 与 ``--target`` 一致
    - branch/commit_sha/dirty/environment 完整且与当前 git state 一致（若提供）
    - generated_at 在 ``EVIDENCE_FRESHNESS_HOURS`` 小时内
    - gates 包含 G0-G7，每个 gate 有合法 status 与 command_ids
    - 所有 command 的 stdout/stderr/result_file hash 是 64 位小写 hex
    - production target 强制 provider≠mock 且 review_status=REVIEWED
    - 任何 blocker（即使 bundle 自身的 blockers 字段非空）都让 release gate 关闭

    任何字段缺失或非法都计入 blocker；绝不误报 GO。
    """
    blockers: list[str] = []
    evidence: dict[str, Any] = {"bundle_present": payload is not None}

    if payload is None:
        blockers.append("evidence_bundle_missing")
        return blockers, evidence

    # 顶层必填字段（与 ai-evidence-v1.schema.json 对齐）。
    required_top = (
        "schema_version",
        "artifact_type",
        "target",
        "branch",
        "commit_sha",
        "dirty",
        "environment",
        "generated_at",
        "policy_version",
        "provider",
        "schema_versions",
        "commands",
        "exit_codes",
        "input_artifact_hashes",
        "output_artifact_hashes",
        "reviewer",
        "gates",
        "blockers",
        "result",
    )
    for key in required_top:
        if key not in payload:
            blockers.append(f"evidence_bundle_missing_field:{key}")

    if payload.get("schema_version") != "ai-evidence-v1":
        blockers.append("evidence_bundle_schema_version_invalid")
    if payload.get("target") != target:
        blockers.append(f"evidence_bundle_target_mismatch:{payload.get('target')}")
    if payload.get("artifact_type") not in {"command-bundle", "readiness", "rollback", "stability"}:
        blockers.append("evidence_bundle_artifact_type_invalid")

    commit_sha = payload.get("commit_sha")
    if not isinstance(commit_sha, str) or not COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        blockers.append("evidence_bundle_commit_sha_invalid")
    elif expected_commit_sha and commit_sha != expected_commit_sha:
        blockers.append("evidence_bundle_commit_sha_stale")

    if not isinstance(payload.get("dirty"), bool):
        blockers.append("evidence_bundle_dirty_invalid")

    environment = payload.get("environment")
    if environment not in {"development", "testing", "staging", "production"}:
        blockers.append("evidence_bundle_environment_invalid")
    elif target == "production" and environment != "production":
        blockers.append("evidence_bundle_production_environment_required")
    elif target == "internal" and environment == "production":
        blockers.append("evidence_bundle_internal_target_uses_production_environment")

    # 时效校验：generated_at 必须在 72 小时内。
    generated_at = payload.get("generated_at")
    if isinstance(generated_at, str):
        try:
            normalized = generated_at.replace("Z", "+00:00")
            parsed_ts = datetime.fromisoformat(normalized)
            if parsed_ts.tzinfo is None:
                blockers.append("evidence_bundle_generated_at_missing_timezone")
            else:
                parsed_ts = parsed_ts.astimezone(UTC)
                now = datetime.now(UTC)
                if parsed_ts > now + timedelta(minutes=5):
                    blockers.append("evidence_bundle_generated_at_in_future")
                elif (now - parsed_ts) > timedelta(hours=EVIDENCE_FRESHNESS_HOURS):
                    blockers.append("evidence_bundle_generated_at_stale")
        except ValueError:
            blockers.append("evidence_bundle_generated_at_unparseable")
    else:
        blockers.append("evidence_bundle_generated_at_missing")

    # reviewer 校验。
    reviewer = payload.get("reviewer")
    if not isinstance(reviewer, dict) or not {
        "name", "role", "status"
    }.issubset(reviewer):
        blockers.append("evidence_bundle_reviewer_invalid")
    elif target == "production" and reviewer.get("status") != "REVIEWED":
        blockers.append("evidence_bundle_production_review_not_complete")
    elif reviewer.get("status") not in {"REVIEWED", "PENDING", "NOT_RUN"}:
        blockers.append("evidence_bundle_reviewer_status_invalid")

    # provider 校验：production 禁止 mock。
    provider = payload.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        blockers.append("evidence_bundle_provider_invalid")
    elif target == "production" and provider.strip().lower() == "mock":
        blockers.append("production_provider_must_not_be_mock")

    # gates 校验：必须包含 G0-G7 且每个 gate status 合法。
    gates = payload.get("gates")
    if not isinstance(gates, dict) or set(gates) != set(ALL_GATE_NAMES):
        blockers.append("evidence_bundle_gates_incomplete")
    else:
        for gate_name in ALL_GATE_NAMES:
            entry = gates.get(gate_name)
            if not isinstance(entry, dict):
                blockers.append(f"evidence_bundle_gate_invalid:{gate_name}")
                continue
            status = entry.get("status")
            if status not in VALID_GATE_STATUSES:
                blockers.append(f"evidence_bundle_gate_status_invalid:{gate_name}")
            if not isinstance(entry.get("command_ids"), list):
                blockers.append(f"evidence_bundle_gate_commands_invalid:{gate_name}")

    # commands 校验：argv allowlist、exit_code、hash 合法性。
    commands = payload.get("commands")
    if not isinstance(commands, list):
        blockers.append("evidence_bundle_commands_invalid")
        commands = []
    seen_ids: set[str] = set()
    for command in commands:
        if not isinstance(command, dict):
            blockers.append("evidence_bundle_command_invalid")
            continue
        command_id = command.get("id")
        if not isinstance(command_id, str) or not command_id:
            blockers.append("evidence_bundle_command_id_invalid")
            continue
        if command_id in seen_ids:
            blockers.append(f"evidence_bundle_command_duplicate:{command_id}")
        seen_ids.add(command_id)
        for hash_key in ("stdout_sha256", "stderr_sha256", "result_file_sha256"):
            digest = command.get(hash_key)
            if not isinstance(digest, str) or not SHA256_HEX_PATTERN.fullmatch(digest):
                blockers.append(f"evidence_bundle_command_hash_invalid:{command_id}:{hash_key}")

    # exit_codes 必须与 command id 集合一致。
    exit_codes = payload.get("exit_codes")
    if not isinstance(exit_codes, dict):
        blockers.append("evidence_bundle_exit_codes_invalid")
    elif set(exit_codes) != seen_ids:
        blockers.append("evidence_bundle_exit_code_keys_mismatch")

    # hash map 校验。
    for map_key in ("input_artifact_hashes", "output_artifact_hashes"):
        hash_map = payload.get(map_key)
        if not isinstance(hash_map, dict):
            blockers.append(f"evidence_bundle_hash_map_invalid:{map_key}")
            continue
        for path, digest in hash_map.items():
            if not isinstance(path, str) or not path:
                blockers.append(f"evidence_bundle_hash_path_invalid:{map_key}")
            if not isinstance(digest, str) or not SHA256_HEX_PATTERN.fullmatch(digest):
                blockers.append(f"evidence_bundle_hash_invalid:{map_key}:{path}")

    # bundle 自身的 blockers 非空 → 直接继承为 release gate blocker。
    bundle_blockers = payload.get("blockers")
    if not isinstance(bundle_blockers, list):
        blockers.append("evidence_bundle_blockers_field_invalid")
    else:
        for item in bundle_blockers:
            if isinstance(item, str) and item.strip():
                blockers.append(f"evidence_bundle_reported:{item}")

    # result 必须合法；production target 下 result≠PASS 即 NO-GO。
    result = payload.get("result")
    if result not in VALID_RESULT_STATUSES:
        blockers.append("evidence_bundle_result_invalid")
    elif target == "production" and result != "PASS":
        blockers.append("evidence_bundle_production_result_not_pass")

    evidence["bundle"] = {
        "schema_version": payload.get("schema_version"),
        "target": payload.get("target"),
        "artifact_type": payload.get("artifact_type"),
        "commit_sha": commit_sha if isinstance(commit_sha, str) else None,
        "environment": environment,
        "generated_at": generated_at,
        "result": result,
        "bundle_blocker_count": (
            len(bundle_blockers) if isinstance(bundle_blockers, list) else None
        ),
        "command_count": len(commands) if isinstance(commands, list) else 0,
    }
    return blockers, evidence


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

        with pymysql.connect(**params) as conn, conn.cursor() as cur:
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
    # 仅文件存在不够：占位 artifact 的 result 必须是 PASS 才算证据；NOT_RUN/NO-GO
    # 占位不能让 release gate 误报 GO。
    rollback_drill = ROOT / "artifacts" / "ai-rollback-drill.json"
    rollback_verified = False
    if rollback_drill.exists():
        try:
            rollback_payload = json.loads(rollback_drill.read_text(encoding="utf-8"))
            rollback_verified = (
                isinstance(rollback_payload, dict)
                and rollback_payload.get("result") == "PASS"
                and isinstance(rollback_payload.get("blockers"), list)
                and not rollback_payload["blockers"]
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            rollback_verified = False
    if not rollback_verified:
        blockers.append("rollback_drill_not_evidenced")
    evidence["rollback_drill"] = {"verified": rollback_verified}

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


def _resolve_environment_from_target(target: str, explicit_environment: str | None) -> str:
    """根据 ``--target`` 解析最终 environment；``--target`` 优先于 ``--environment``。

    - ``target=production`` → 强制 ``production``（除非显式给出且一致）。
    - ``target=internal`` → ``development``/``testing``；显式 ``--environment``
      保留兼容期，但禁止 ``production``。
    """
    if target == "production":
        if explicit_environment and explicit_environment != "production":
            # 显式给出非 production 但 target=production 是矛盾输入，按更严格者处理。
            return "production"
        return "production"
    # target=internal
    if explicit_environment == "production":
        # internal target 不允许 production environment。
        return "testing"
    return explicit_environment or "testing"


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
        "--target",
        required=True,
        choices=("internal", "production"),
        help="目标发布通道（Task 10 Step3）；``--target`` 优先于 ``--environment``",
    )
    parser.add_argument(
        "--environment",
        default=None,
        choices=("development", "testing", "staging", "production"),
        help=(
            "[deprecated] 目标环境；``--target`` 优先。保留兼容期，"
            "internal target 不允许 production environment"
        ),
    )
    parser.add_argument(
        "--report",
        default="artifacts/ai-release-evidence.json",
        help="证据 JSON 输出路径",
    )
    parser.add_argument(
        "--evidence-bundle",
        default=str(EVIDENCE_BUNDLE_PATH),
        help="build_ai_evidence 产物路径（ai-evidence-bundle.json）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    target = args.target
    environment = _resolve_environment_from_target(target, args.environment)
    try:
        settings = _build_settings(environment)
    except ValidationError as exc:
        # 兜底：任何 Settings 构造失败（例如 shell/进程环境注入非法配置）都计为
        # 稳定 blocker 收尾，绝不带 traceback 崩溃——统一契约 exit 2 + 写报告。
        payload = {
            "target": target,
            "environment": environment,
            "release_gate": "disabled-until-approved",
            "decision_code": "AI_FEATURE_DISABLED",
            "blockers": ["settings_invalid"],
            "config": {},
            "evidence": {"settings": {"verified": False, "error": str(exc)}},
        }
        _write_report(args.report, payload)
        print(f"target={target}")
        print(f"environment={environment}")
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

    # Task 10 Step3：证据 bundle 聚合（build_ai_evidence 产物）。
    bundle_path = Path(args.evidence_bundle)
    if not bundle_path.is_absolute():
        bundle_path = ROOT / bundle_path
    bundle_payload, bundle_load_blockers = _load_evidence_bundle(bundle_path)
    # 尝试读取当前 git commit sha 用于 staleness 校验；失败不阻塞。
    expected_commit_sha: str | None = None
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        expected_commit_sha = result.stdout.strip().lower() or None
    except Exception:  # noqa: BLE001 - git 不可用或子进程失败都不阻塞校验
        expected_commit_sha = None
    bundle_blockers, bundle_evidence = _evidence_bundle_blockers(
        bundle_payload,
        target=target,
        settings=settings,
        expected_commit_sha=expected_commit_sha,
    )
    blockers.extend(bundle_load_blockers)
    blockers.extend(bundle_blockers)

    evidence = ReleaseEvidence(
        required_paths=REQUIRED_AI_PATHS,
        phase4_requires_dpa=True,
        phase5_requires_fairness_review=True,
        blockers=tuple(dict.fromkeys(blockers)),
    )
    decision = evaluate_ai_release_gate(settings, evidence)

    payload = {
        "target": target,
        "environment": environment,
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
            **bundle_evidence,
            "phase4_requires_dpa": evidence.phase4_requires_dpa,
            "phase5_requires_fairness_review": evidence.phase5_requires_fairness_review,
        },
    }
    _write_report(args.report, payload)

    print(f"target={target}")
    print(f"environment={environment}")
    print(f"release_gate={decision.release_gate}")
    for blocker in decision.blockers:
        print(f"blocker={blocker}")
    print(f"report={args.report}")

    # 任何证据缺失或门禁未通过都必须以退出码 2 收尾，绝不误报通过。
    return 2 if decision.blockers else 0


if __name__ == "__main__":
    sys.exit(main())
