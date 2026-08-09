import pytest

from app.services.candidate_visibility import (
    CandidateVisibilityService,
    CandidateContext,
    ViewerContext,
    VisibilityScene,
    evaluate_visibility,
)


@pytest.mark.parametrize(
    ("who_can_see_me", "realname_status", "is_vip", "allowed"),
    [
        (1, 0, False, True),
        (2, 0, True, False),
        (2, 2, False, True),
        (3, 2, False, False),
        (3, 0, True, True),
        (4, 2, True, False),
    ],
)
def test_visibility_truth_table(
    who_can_see_me: int, realname_status: int, is_vip: bool, allowed: bool
) -> None:
    viewer = ViewerContext(
        user_id=1,
        realname_status=realname_status,
        is_vip=is_vip,
    )
    candidate = CandidateContext.visible(user_id=2, who_can_see_me=who_can_see_me)

    assert evaluate_visibility(viewer, candidate).allowed is allowed


def test_block_in_either_direction_is_fail_closed() -> None:
    viewer = ViewerContext(user_id=1, realname_status=2, is_vip=True)
    candidate = CandidateContext.visible(user_id=2, who_can_see_me=1, blocked=True)

    decision = evaluate_visibility(viewer, candidate)

    assert decision.allowed is False
    assert decision.denial_code == "BLOCKED_RELATIONSHIP"


@pytest.mark.parametrize(
    ("overrides", "denial_code"),
    [
        ({"account_active": False}, "ACCOUNT_INACTIVE"),
        ({"profile_visible": False}, "CANDIDATE_NOT_VISIBLE"),
        ({"match_active": False}, "MATCH_DISABLED"),
        ({"not_restricted": False}, "ACCOUNT_RESTRICTED"),
        ({"profile_complete": False}, "PROFILE_INCOMPLETE"),
        ({"media_approved": False}, "MEDIA_REVIEW_PENDING"),
    ],
)
def test_visibility_denies_inactive_or_unreviewed_candidates(
    overrides: dict[str, bool], denial_code: str
) -> None:
    viewer = ViewerContext(user_id=1, realname_status=2, is_vip=True)
    candidate = CandidateContext.visible(
        user_id=2,
        who_can_see_me=1,
        **overrides,
    )

    decision = evaluate_visibility(viewer, candidate)

    assert decision.allowed is False
    assert decision.denial_code == denial_code


def test_unknown_visibility_policy_is_fail_closed() -> None:
    viewer = ViewerContext(user_id=1, realname_status=None, is_vip=True)
    candidate = CandidateContext.visible(user_id=2, who_can_see_me=99)

    decision = evaluate_visibility(viewer, candidate)

    assert decision.allowed is False
    assert decision.denial_code == "UNKNOWN_VISIBILITY_POLICY"


def test_sql_predicate_rejects_unknown_visibility_policies() -> None:
    predicate = CandidateVisibilityService().predicate(
        ViewerContext(user_id=1, realname_status=2, is_vip=True),
        VisibilityScene.DISCOVERY,
    )

    assert "COALESCE(pr.who_can_see_me, 1) IN (1, 2, 3)" in predicate.clause


@pytest.mark.parametrize("candidate_alias", ["candidate.id", "1candidate", "u; DROP TABLE users"])
def test_sql_predicate_rejects_non_identifier_aliases(candidate_alias: str) -> None:
    with pytest.raises(ValueError, match="invalid SQL alias"):
        CandidateVisibilityService().predicate(
            ViewerContext(user_id=1, realname_status=2, is_vip=True),
            VisibilityScene.DISCOVERY,
            candidate_alias=candidate_alias,
        )


@pytest.mark.asyncio
async def test_decide_loads_current_candidate_facts_from_the_database() -> None:
    class MappingResult:
        def mappings(self) -> "MappingResult":
            return self

        def first(self) -> dict[str, int]:
            return {
                "candidate_id": 2,
                "viewer_realname_status": 2,
                "viewer_is_vip": 0,
                "who_can_see_me": 2,
                "account_active": 1,
                "profile_visible": 1,
                "match_active": 1,
                "not_restricted": 1,
                "profile_complete": 1,
                "media_approved": 1,
                "blocked": 0,
            }

    class RecordingDb:
        statement: object | None = None
        params: dict[str, int] | None = None

        async def execute(
            self, statement: object, params: dict[str, int]
        ) -> MappingResult:
            self.statement = statement
            self.params = params
            return MappingResult()

    db = RecordingDb()

    decision = await CandidateVisibilityService().decide(
        db,
        viewer_id=1,
        candidate_id=2,
        scene=VisibilityScene.PROFILE,
    )

    assert decision.allowed is True
    assert db.params == {
        "visibility_viewer_id": 1,
        "candidate_id": 2,
    }
    assert db.statement is not None
    assert "FROM users candidate" in str(db.statement)


@pytest.mark.asyncio
async def test_decide_denies_a_candidate_with_an_active_total_ban() -> None:
    class MappingResult:
        def mappings(self) -> "MappingResult":
            return self

        def first(self) -> dict[str, int]:
            return {
                "candidate_id": 2,
                "viewer_realname_status": 2,
                "viewer_is_vip": 1,
                "who_can_see_me": 1,
                "account_active": 1,
                "profile_visible": 1,
                "match_active": 1,
                "not_restricted": 0,
                "profile_complete": 1,
                "media_approved": 1,
                "blocked": 0,
            }

    class RecordingDb:
        async def execute(
            self, _: object, __: dict[str, int]
        ) -> MappingResult:
            return MappingResult()

    decision = await CandidateVisibilityService().decide(
        RecordingDb(),
        viewer_id=1,
        candidate_id=2,
        scene=VisibilityScene.PROFILE,
    )

    assert decision.allowed is False
    assert decision.denial_code == "ACCOUNT_RESTRICTED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "allowed", "denial_code"),
    [
        ({}, True, None),
        ({"who_can_see_me": 2, "viewer_realname_status": 0}, False, "REALNAME_REQUIRED"),
        ({"who_can_see_me": 2, "viewer_realname_status": 1}, False, "REALNAME_REQUIRED"),
        ({"who_can_see_me": 2, "viewer_realname_status": 2}, True, None),
        ({"who_can_see_me": 3, "viewer_is_vip": 0}, False, "VIP_REQUIRED"),
        ({"who_can_see_me": 3, "viewer_is_vip": 1}, True, None),
        ({"who_can_see_me": 4}, False, "PRIVATE_PROFILE"),
        ({"who_can_see_me": 99}, False, "UNKNOWN_VISIBILITY_POLICY"),
        ({"blocked": 1}, False, "BLOCKED_RELATIONSHIP"),
        ({"account_active": 0}, False, "ACCOUNT_INACTIVE"),
        ({"not_restricted": 0}, False, "ACCOUNT_RESTRICTED"),
        ({"profile_visible": 0}, False, "CANDIDATE_NOT_VISIBLE"),
        ({"match_active": 0}, False, "MATCH_DISABLED"),
        ({"profile_complete": 0}, False, "PROFILE_INCOMPLETE"),
        ({"media_approved": 0}, False, "MEDIA_REVIEW_PENDING"),
    ],
)
async def test_decide_applies_the_full_database_fact_matrix(
    overrides: dict[str, int],
    allowed: bool,
    denial_code: str | None,
) -> None:
    class MappingResult:
        def mappings(self) -> "MappingResult":
            return self

        def first(self) -> dict[str, int]:
            return {
                "candidate_id": 2,
                "viewer_realname_status": 0,
                "viewer_is_vip": 0,
                "who_can_see_me": 1,
                "account_active": 1,
                "profile_visible": 1,
                "match_active": 1,
                "not_restricted": 1,
                "profile_complete": 1,
                "media_approved": 1,
                "blocked": 0,
                **overrides,
            }

    class RecordingDb:
        async def execute(
            self, _: object, __: dict[str, int]
        ) -> MappingResult:
            return MappingResult()

    decision = await CandidateVisibilityService().decide(
        RecordingDb(),
        viewer_id=1,
        candidate_id=2,
        scene=VisibilityScene.INTERACTION,
    )

    assert decision.allowed is allowed
    assert decision.denial_code == denial_code
    assert decision.scene is VisibilityScene.INTERACTION
