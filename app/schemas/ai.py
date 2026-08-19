"""Contracts for the phase-one AI features."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AIAssistantSessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=80)


class AIAssistantSessionResponse(BaseModel):
    id: int
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class AIAssistantMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class AIAssistantMessageResponse(BaseModel):
    id: int
    session_id: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class AIAssistantSessionPage(BaseModel):
    items: list[AIAssistantSessionResponse]
    page: int
    page_size: int
    total: int
    has_more: bool


class AIProfilePolishRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    style: Literal["natural", "warm", "humorous", "mature", "concise"] = "natural"
    max_length: int = Field(default=300, ge=50, le=2000)


class AIProfilePolishResponse(BaseModel):
    original: str
    polished: str
    style: str
    changed_points: list[str]


class AISearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    page: int = Field(default=1, ge=1, le=1000)
    page_size: int = Field(default=20, ge=1, le=20)


class AISearchResponse(BaseModel):
    query: str
    normalized_query: str
    filters: dict[str, object]
    unresolved: list[str]
    results: object


class AIMatchItem(BaseModel):
    user_id: int
    nickname: str | None
    avatar: str | None
    match_type: Literal["who_likes_me", "i_like", "material", "soul"]
    match_score: float = Field(ge=0, le=100)
    score_breakdown: dict[str, float]
    match_reason: str
    suggestions: list[str]


class AIMatchPage(BaseModel):
    match_type: Literal["who_likes_me", "i_like", "material", "soul"]
    items: list[AIMatchItem]
    page: int
    page_size: int
    total: int
    has_more: bool

