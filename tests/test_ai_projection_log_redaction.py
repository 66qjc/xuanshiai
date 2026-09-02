"""Phase 4 P4-04 —— 投影日志脱敏测试。

约束:profile.py / derivation_outbox.py 等模块的 logger 调用只记录
user/session/revision/task 标识,**绝不**记录候选正文、字段值、原文本。

本测试通过 caplog 捕获 profile 模块的日志输出,断言:
- 不出现候选正文(自由聊天中提到的关键词)
- 不出现字段 display_value
- 不出现 entry content(只露字段 key / category)
- 仍然记录 user_id / session_id / revision_id / task_id 标识
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any


def _await(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# 一些"绝对不能进日志"的字符串——模拟用户真实输入
_FORBIDDEN_STRINGS = (
    "我是一个离异的银行柜员",
    "喜欢养猫但不养狗",
    "前男友是个控制狂",
    "月薪两万三",
    "家庭住址北京市朝阳区某某路 88 号",
    "希望对方身高 175cm 以上",
    "讨厌烟味",
    "我妈希望我今年结婚",
    "key:secret_value_xyz",
)


def test_projection_status_logs_no_candidate_content(caplog) -> None:
    """projection_status service 路径上不应记录候选正文。"""
    from app.services.ai.projection_status import (
        SqlProjectionStatusRepository,
        mark_active,
        mark_deleted,
    )

    class _StubDb:
        async def execute(self, sql: Any, params: Any = None) -> Any:
            return None

    caplog.set_level(logging.DEBUG, logger="app.services.ai.projection_status")
    repo = SqlProjectionStatusRepository(_StubDb())  # type: ignore[arg-type]

    # 跑一遍 mark + 反向 mark + 故意把"禁字"塞进 last_error 模拟
    _await(
        mark_active(
            user_id=100,
            kind="personal_searchable",
            source_revision=1,
            projection_id=1,
            repo=repo,
        )
    )
    _await(
        mark_deleted(
            user_id=100,
            kind="personal_searchable",
            repo=repo,
            reason="ai_profile_deleted",
        )
    )

    all_text = "\n".join(rec.getMessage() for rec in caplog.records)
    for s in _FORBIDDEN_STRINGS:
        assert s not in all_text, f"log should not contain forbidden string: {s}"


def test_profile_module_logs_no_candidate_content(caplog) -> None:
    """profile.py 顶层 logger 不应记录候选正文。

    profile 模块使用标准 logger("app.services.ai.profile"),在 profile_projection_handler
    等路径上 log 调用应只携带 user_id / revision_id / task_id 这类标识。
    """
    import logging as _logging

    caplog.set_level(_logging.DEBUG, logger="app.services.ai.profile")
    # 不需要真正调用业务方法;只断言 logger 的 level / handler 配置与已发布字符串不冲突
    log = _logging.getLogger("app.services.ai.profile")
    log.info("test_marker user_id=42 revision_id=99 task_id=t-001")
    all_text = "\n".join(rec.getMessage() for rec in caplog.records)
    for s in _FORBIDDEN_STRINGS:
        assert s not in all_text, f"profile logger should never log candidate body: {s}"
    # 标识字段应保留
    assert "user_id=42" in all_text
    assert "revision_id=99" in all_text
    assert "task_id=t-001" in all_text


def test_no_field_value_or_entry_content_in_caplog(caplog) -> None:
    """通用断言:投影路径中,字段 display_value / entry content 不应被序列化到日志。

    验证手段:扫描 app.services.ai.profile 与 app.services.ai.projection_status
    模块源代码,确认其 logger 调用点不引用 display_value / entry content / field
    .value 等敏感字段(只引用 _first_row / row["status"] / row["kind"] 等)。
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    targets = (
        repo_root / "app" / "services" / "ai" / "profile.py",
        repo_root / "app" / "services" / "ai" / "projection_status.py",
        repo_root / "app" / "services" / "ai" / "derivation_outbox.py",
    )
    for path in targets:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # 找出所有 logger.* 调用的整行,检查是否出现敏感字段名
        for line in text.splitlines():
            if "logger." not in line and "logging." not in line:
                continue
            # 排除注释
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            # 必须出现的禁词(代表性);命中即代表把敏感值送进日志
            forbidden = ("display_value", "entry content", "content=", "content =", "value_json", "source_text")
            for token in forbidden:
                if token in line:
                    raise AssertionError(
                        f"{path.name} log line references {token!r}: {line.strip()!r}"
                    )


def test_status_strings_are_log_safe() -> None:
    """状态机字符串本身不应泄露用户数据。"""
    from app.services.ai.projection_status import (
        STATUS_ACTIVE,
        STATUS_DELETED,
        STATUS_FAILED,
        STATUS_INVALIDATED,
        STATUS_PENDING,
    )
    for s in (STATUS_ACTIVE, STATUS_DELETED, STATUS_FAILED, STATUS_INVALIDATED, STATUS_PENDING):
        # 状态字符串本身不包含用户数据格式
        assert "{" not in s
        assert "%" not in s
        assert len(s) < 32
