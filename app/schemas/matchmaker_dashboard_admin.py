"""Dashboard contracts for the matchmaker back office."""

from pydantic import BaseModel


class MatchmakerDashboardStats(BaseModel):
    member_count: int
    vip_count: int
    matchmaker_count: int
    pending_service_count: int
    active_service_count: int
    pending_certification_count: int
    today_new_member_count: int

