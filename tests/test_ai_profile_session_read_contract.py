"""ProfileSessionRead 必须能回传墨相师 master 会话。"""

from datetime import UTC, datetime

from app.schemas.ai_profile import (
    ProfileProgress,
    ProfileSessionRead,
    ProfileSessionStatus,
    ProfileSubject,
)


def test_profile_session_read_accepts_master_session_kind() -> None:
    payload = ProfileSessionRead(
        session_id="master_contract_001",
        subject=ProfileSubject.PERSONAL,
        status=ProfileSessionStatus.DRAFT,
        session_kind="master",
        progress=ProfileProgress(),
        created_at=datetime.now(UTC),
    )

    assert payload.session_kind == "master"
