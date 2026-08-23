"""Contracts for the relationship advisor MVP."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AdvisorType = Literal["relationship"]
AdvisorScenario = Literal[
    "opening",
    "reply",
    "topic_extension",
    "rescue",
    "care",
    "compliment",
    "values",
    "intimacy",
    "closing",
    "analyze",
]
AdvisorTone = Literal["natural", "warm", "humorous", "mature"]
AdvisorRiskLevel = Literal["none", "low", "medium", "high"]
AdvisorFeedbackType = Literal["copied", "used", "not_useful", "reported"]


class AdvisorSessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    advisor_type: AdvisorType = "relationship"
    chat_session_id: int | None = Field(default=None, ge=1)


class AdvisorSessionResponse(BaseModel):
    id: int
    advisor_type: AdvisorType
    chat_session_id: int | None
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class AdvisorSessionPage(BaseModel):
    items: list[AdvisorSessionResponse]
    page: int
    page_size: int
    total: int
    has_more: bool


class AdvisorAdviceRequest(BaseModel):
    scenario: AdvisorScenario
    goal: str | None = Field(default=None, max_length=300)
    incoming_message: str = Field(min_length=1, max_length=2000)
    tone: AdvisorTone = "natural"
    chat_session_id: int | None = Field(default=None, ge=1)
    include_history: bool = False
    max_suggestions: int = Field(default=3, ge=1, le=3)


class AdvisorSuggestion(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    style: AdvisorTone
    reason: str = Field(min_length=1, max_length=300)


class AdvisorAdviceResponse(BaseModel):
    id: int
    session_id: int
    scenario: AdvisorScenario
    analysis: str
    suggestions: list[AdvisorSuggestion]
    risk_level: AdvisorRiskLevel
    risk_notice: str | None
    next_step: str | None
    disclaimer: str
    created_at: datetime


class AdvisorFeedbackRequest(BaseModel):
    feedback_type: AdvisorFeedbackType


class AdvisorFeedbackResponse(BaseModel):
    message_id: int
    feedback_type: AdvisorFeedbackType
    recorded: bool
