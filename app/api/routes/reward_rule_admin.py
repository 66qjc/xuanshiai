"""Configurable reward tasks for the independent matchmaker back office."""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.matchmaker_admin import RewardRule, RewardRuleCreate, RewardRuleDeleteResponse, RewardRuleUpdate
from app.services.reward_rule_admin import create_rule, delete_rule, get_rule, list_rules, update_rule

router = APIRouter(prefix="/admin/reward-rules")


@router.get("", response_model=list[RewardRule], summary="查询奖励任务配置")
async def reward_rules(active_only: bool = Query(False), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[RewardRule]:
    return await list_rules(db, active_only)


@router.post("", response_model=RewardRule, status_code=201, summary="新增奖励任务配置")
async def create_reward_rule(body: RewardRuleCreate, current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> RewardRule:
    return await create_rule(db, current.account.id, body)


@router.get("/{task_code}", response_model=RewardRule, summary="查询单个奖励任务配置")
async def reward_rule_detail(task_code: str = Path(..., min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> RewardRule:
    return await get_rule(db, task_code)


@router.patch("/{task_code}", response_model=RewardRule, summary="修改奖励任务配置")
async def update_reward_rule(task_code: str = Path(..., min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"), body: RewardRuleUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> RewardRule:
    return await update_rule(db, current.account.id, task_code, body)


@router.delete("/{task_code}", response_model=RewardRuleDeleteResponse, summary="删除奖励任务配置")
async def delete_reward_rule(task_code: str = Path(..., min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> RewardRuleDeleteResponse:
    return await delete_rule(db, current.account.id, task_code)
