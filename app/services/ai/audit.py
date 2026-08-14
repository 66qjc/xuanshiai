"""AI-CORE audit and observability: redaction, generation audit and metrics.

Task 12 keeps three hard promises from the unified plan §6.5/§6.6 and the
execution plan:

1. Sensitive values never cross the audit/log boundary.  ``redact_ai_log``
   drops keys from :data:`SENSITIVE_KEYS` (case-insensitive, recursively), the
   same key-allowlist discipline the Gateway already applies to provider
   messages.

2. ``record_generation_audit`` writes a minimal, replayable row into
   ``ai_generation_audit`` — request id, task id, scene, provider/model,
   prompt/schema version, source revisions, policy revision, status, error
   code, usage/cost presence, display eligibility and timestamps.  Raw prompts,
   original answers and raw provider responses never reach the row.  The
   frozen Task 5 table has no dedicated columns for ``status``/
   ``policy_revision``/``display_eligible``, so those controlled fields are
   persisted inside ``safety_result_json`` under an ``audit_meta`` block (the
   column is free-form JSON and the table comment only forbids raw
   prompt/response).  A failed audit write must never block business — it is
   caught and recorded as a local warning.

3. ``emit_ai_metric`` sinks the plan's operational metrics (queue age, lease
   reclaim, retry rate, schema invalid, provider 429/5xx, stale rate, fallback
   rate, deletion propagation, outbox/purge backlog) into an in-process
   registry plus a structured log line; backlog metrics over the configured
   threshold also raise a local warning.  Metric failures never raise.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from app.core.config import settings

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 脱敏（统一方案 §3.2 / §12.1 安全日志；与 gateway._PROVIDER_MESSAGE_SENSITIVE_KEYS
# 同一 key-allowlist 纪律）
# ----------------------------------------------------------------------

SENSITIVE_KEYS = frozenset({
    "prompt",
    "raw_response",
    "phone",
    "id_card",
    "precise_location",
    "raw_ip",
})


def _redact_value(key: str, value: Any) -> Any:
    """Recursively strip sensitive keys from nested mappings and lists."""
    if isinstance(value, Mapping):
        return {
            k: _redact_value(k, v)
            for k, v in value.items()
            if k.lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_redact_value("", item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value("", item) for item in value)
    return value


def redact_ai_log(fields: Mapping[str, object]) -> dict[str, object]:
    """Return a log-safe copy of ``fields`` with sensitive keys removed.

    The brief contract is verbatim: ``prompt``/``phone``/``id_card`` (and any
    other key in :data:`SENSITIVE_KEYS`) are dropped while non-sensitive keys
    such as ``task_id``/``request_id`` survive.  Nested mappings are scrubbed
    recursively with the same key-allowlist rule.
    """
    return {
        key: _redact_value(key, value)
        for key, value in fields.items()
        if key.lower() not in SENSITIVE_KEYS
    }


# ----------------------------------------------------------------------
# Generation audit（统一方案 §6.5，ai_generation_audit 表）
# ----------------------------------------------------------------------

#: Frozen Task 5 columns of ``ai_generation_audit``; the writer only ever
#: touches these columns so no schema migration is introduced.
_AUDIT_TABLE_COLUMNS = (
    "request_id",
    "task_id",
    "scene",
    "provider",
    "model",
    "prompt_version",
    "schema_version",
    "input_revision_json",
    "duration_ms",
    "token_usage_json",
    "cost",
    "safety_result_json",
    "error_code",
)


@dataclass(frozen=True)
class GenerationAuditEvent:
    """Minimal replayable record of one provider generation (Task 12).

    Only controlled metadata; never the prompt, original answers or the raw
    provider response.  ``usage_cost`` carries token/cost presence when the
    provider reports it (phase-1 mock reports none).
    """

    request_id: str
    task_id: str | None
    scene: str
    provider: str
    model: str | None = None
    prompt_version: str | None = None
    schema_version: str | None = None
    input_revision: Mapping[str, int] = field(default_factory=dict)
    policy_revision: str | None = None
    status: str | None = None
    error_code: str | None = None
    usage_cost: Mapping[str, Any] | None = None
    display_eligible: bool = False
    safety_result: Mapping[str, Any] | None = None
    duration_ms: int | None = None
    created_at: datetime | None = None


def _db_connect_params() -> dict[str, Any] | None:
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


def _audit_row(event: GenerationAuditEvent) -> tuple[str, tuple[Any, ...]]:
    """Build the INSERT statement and its bound parameters for one event."""
    usage = event.usage_cost or {}
    safety_meta: dict[str, Any] = {
        "status": event.status,
        "policy_revision": event.policy_revision,
        "display_eligible": bool(event.display_eligible),
        "safety": event.safety_result,
    }
    values = (
        event.request_id,
        event.task_id,
        event.scene,
        event.provider,
        event.model,
        event.prompt_version,
        event.schema_version,
        json.dumps(dict(event.input_revision), ensure_ascii=False, sort_keys=True)
        if event.input_revision
        else None,
        event.duration_ms,
        json.dumps(usage, ensure_ascii=False, sort_keys=True) if usage else None,
        usage.get("cost"),
        json.dumps(safety_meta, ensure_ascii=False, sort_keys=True),
        event.error_code,
    )
    placeholders = ", ".join("%s" for _ in values)
    statement = (
        f"INSERT IGNORE INTO ai_generation_audit ({', '.join(_AUDIT_TABLE_COLUMNS)}) "
        f"VALUES ({placeholders})"
    )
    return statement, values


def _persist_audit_row_sync(event: GenerationAuditEvent) -> None:
    """Best-effort synchronous write; never raises.

    Runs inside a worker thread (via :func:`asyncio.to_thread`) from
    :func:`record_generation_audit` so the blocking ``pymysql.connect`` call
    never stalls the event loop.
    """
    params = _db_connect_params()
    if params is None:
        logger.debug("ai_audit_skip_unparseable_db_url request_id=%s", event.request_id)
        return
    try:
        import pymysql

        statement, values = _audit_row(event)
        with pymysql.connect(**params) as conn:
            with conn.cursor() as cur:
                cur.execute(statement, values)
            conn.commit()
    except Exception:  # noqa: BLE001 - audit must never block business
        logger.warning(
            "ai_audit_write_failed request_id=%s error_code=%s",
            event.request_id,
            event.error_code,
            exc_info=True,
        )


async def record_generation_audit(event: GenerationAuditEvent) -> None:
    """Record one generation audit event without blocking the event loop.

    Always logs the minimal non-sensitive metadata; then best-effort persists a
    row into ``ai_generation_audit``.  The synchronous DB write is offloaded to
    a worker thread via :func:`asyncio.to_thread` so the blocking
    ``pymysql.connect`` call never stalls the event loop.  Raw prompts,
    original answers and raw provider responses are never part of the row.  Any
    write failure is caught and recorded as a local warning so business keeps
    running.
    """
    if not settings.ai_audit_enabled:
        return
    logger.info(
        "ai_audit request_id=%s task_id=%s scene=%s provider=%s model=%s "
        "prompt_version=%s schema_version=%s policy_revision=%s status=%s "
        "error_code=%s usage_reported=%s display_eligible=%s",
        event.request_id,
        event.task_id,
        event.scene,
        event.provider,
        event.model,
        event.prompt_version,
        event.schema_version,
        event.policy_revision,
        event.status,
        event.error_code,
        bool(event.usage_cost),
        event.display_eligible,
    )
    try:
        await asyncio.to_thread(_persist_audit_row_sync, event)
    except Exception:  # noqa: BLE001 - belt and braces around the sink
        logger.warning(
            "ai_audit_persist_unhandled request_id=%s",
            event.request_id,
            exc_info=True,
        )


# ----------------------------------------------------------------------
# 指标（统一方案 §6.5 / §12.2 运行指标；执行计划 §7 审计/发布检查点）
# ----------------------------------------------------------------------

#: 指标至少覆盖：queue age、lease 回收、重试率、schema invalid、Provider
#: 429/5xx、stale rate、fallback rate、撤回传播延迟、outbox 积压和清理积压。
KNOWN_METRICS = frozenset({
    "queue_age",
    "lease_reclaimed",
    "retry_rate",
    "schema_invalid",
    "provider_429",
    "provider_5xx",
    "stale_rate",
    "fallback_rate",
    "deletion_propagation_seconds",
    "outbox_backlog",
    "purge_backlog",
})

#: 积压类指标超过阈值时打印本地告警（queue/backlog 告警语义）。
_BACKLOG_METRICS = frozenset({"outbox_backlog", "purge_backlog"})

#: 单个指标序列保留的最大样本数。超过后丢弃最旧样本，保证注册表在
#: 长生命周期进程内不会无界增长。
_METRIC_MAX_SAMPLES = 10_000

#: 进程内指标注册表：name -> deque[(value, tags), ...]。本地可观测与测试快照
#: 用，生产仍以结构化日志为真实时序出口。每个序列上限为
#: :data:`_METRIC_MAX_SAMPLES`，超过后丢弃最旧样本。
_METRIC_REGISTRY: dict[str, deque[tuple[float, dict[str, str]]]] = defaultdict(
    lambda: deque(maxlen=_METRIC_MAX_SAMPLES)
)


def metric_snapshot() -> dict[str, list[tuple[float, dict[str, str]]]]:
    """Return a copy of the in-process metric registry (tests/ops)."""
    return {name: list(values) for name, values in _METRIC_REGISTRY.items()}


def emit_ai_metric(name: str, value: float, tags: Mapping[str, str] | None = None) -> None:
    """Emit one operational metric without ever blocking the caller.

    ``name`` should be one of :data:`KNOWN_METRICS`; unknown names are still
    recorded but logged as a warning.  Each per-name series is bounded by
    :data:`_METRIC_MAX_SAMPLES`; the oldest sample is dropped once the limit is
    reached, so the registry never grows unbounded.  Backlog metrics above
    ``settings.ai_metrics_backlog_warn_threshold`` raise a local warning.
    """
    try:
        tag_map = dict(tags or {})
        if name not in KNOWN_METRICS:
            logger.warning(
                "ai_metric_unknown name=%s value=%s", name, value
            )
        _METRIC_REGISTRY[name].append((float(value), tag_map))
        if name in _BACKLOG_METRICS and float(value) >= settings.ai_metrics_backlog_warn_threshold:
            logger.warning(
                "ai_metric_backlog_high name=%s value=%s threshold=%s",
                name,
                value,
                settings.ai_metrics_backlog_warn_threshold,
            )
        logger.info(
            "ai_metric name=%s value=%s tags=%s",
            name,
            value,
            json.dumps(tag_map, ensure_ascii=False, sort_keys=True),
        )
    except Exception:  # noqa: BLE001 - metrics must never block business
        logger.warning("ai_metric_emit_failed name=%s", name, exc_info=True)
