"""Back-office management for configurable user reward rules."""

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.matchmaker_admin import RewardRule, RewardRuleCreate, RewardRuleDeleteResponse, RewardRuleUpdate

_SELECT = """SELECT id, task_code, task_name, task_type, reward_type, reward_value,
    daily_limit, is_active, sort, created_at, updated_at FROM config_reward_rule"""


def _rule(row: Any) -> RewardRule:
    return RewardRule(**dict(row))


async def list_rules(db: AsyncSession, active_only: bool = False) -> list[RewardRule]:
    suffix = " WHERE is_active = 1" if active_only else ""
    rows = await db.execute(text(f"{_SELECT}{suffix} ORDER BY sort, id"))
    return [_rule(row) for row in rows.mappings().all()]


async def get_rule(db: AsyncSession, task_code: str) -> RewardRule:
    row = (await db.execute(text(f"{_SELECT} WHERE task_code = :task_code"), {"task_code": task_code})).mappings().first()
    if not row:
        raise HTTPException(404, detail="任务规则不存在")
    return _rule(row)


async def create_rule(db: AsyncSession, admin_id: int, request: RewardRuleCreate) -> RewardRule:
    exists = await db.execute(text("SELECT 1 FROM config_reward_rule WHERE task_code = :task_code"), {"task_code": request.task_code})
    if exists.scalar():
        raise HTTPException(409, detail="任务编码已存在")
    result = await db.execute(text("""INSERT INTO config_reward_rule
        (task_code, task_name, task_type, reward_type, reward_value, daily_limit, is_active, sort)
        VALUES (:task_code, :task_name, :task_type, :reward_type, :reward_value, :daily_limit, :is_active, :sort)"""), request.model_dump())
    rule_id = int(result.lastrowid)
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, after_json)
        VALUES (:actor, 'reward_rule.create', 'config_reward_rule', :id, :after_json)"""), {
        "actor": admin_id, "id": rule_id, "after_json": json.dumps(request.model_dump(), ensure_ascii=False),
    })
    await db.commit()
    return await get_rule(db, request.task_code)


async def update_rule(db: AsyncSession, admin_id: int, task_code: str, request: RewardRuleUpdate) -> RewardRule:
    await get_rule(db, task_code)
    values = request.model_dump(exclude_unset=True, exclude_none=True)
    if not values:
        return await get_rule(db, task_code)
    assignments = ", ".join(f"{key} = :{key}" for key in values)
    await db.execute(text(f"UPDATE config_reward_rule SET {assignments}, updated_at = UTC_TIMESTAMP() WHERE task_code = :task_code"), {**values, "task_code": task_code})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, after_json)
        SELECT :actor, 'reward_rule.update', 'config_reward_rule', id, :after_json
        FROM config_reward_rule WHERE task_code = :task_code"""), {
        "actor": admin_id, "task_code": task_code, "after_json": json.dumps(values, ensure_ascii=False),
    })
    await db.commit()
    return await get_rule(db, task_code)


async def delete_rule(db: AsyncSession, admin_id: int, task_code: str) -> RewardRuleDeleteResponse:
    rule = await get_rule(db, task_code)
    await db.execute(text("DELETE FROM config_reward_rule WHERE task_code = :task_code"), {"task_code": task_code})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor, 'reward_rule.delete', 'config_reward_rule', :id, :reason)"""), {
        "actor": admin_id, "id": rule.id, "reason": f"删除任务规则 {task_code}",
    })
    await db.commit()
    return RewardRuleDeleteResponse(task_code=task_code, deleted=True)
