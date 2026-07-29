from pydantic import BaseModel


class QuotaItem(BaseModel):
    quota_code: str
    daily_limit: int
    daily_used: int
    daily_remaining: int
    extra_remaining: int
    total_remaining: int


class QuotaSummary(BaseModel):
    items: list[QuotaItem]
