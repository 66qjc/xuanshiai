"""Paper-plane bilateral contact-exchange contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


ContactKind = Literal["wechat", "phone"]
ContactExchangeDecision = Literal["accept", "reject", "withdraw"]


class PaperPlaneContactExchangeCreate(BaseModel):
    kind: ContactKind


class PaperPlaneContactExchangeRespond(BaseModel):
    decision: ContactExchangeDecision


class PaperPlaneContactExchangeResponse(BaseModel):
    id: int
    conversation_id: int
    kind: ContactKind
    requester_user_id: int
    target_user_id: int
    status: Literal["PENDING", "APPROVED", "REJECTED", "REVOKED"]
    requester_consented_at: datetime | None = None
    target_consented_at: datetime | None = None
    responded_at: datetime | None = None
    created_at: datetime
