from datetime import datetime, timedelta

import pytest

from app.services.candidate_query import (
    CandidateCursor,
    CandidateQueryService,
    CandidateQuerySnapshot,
    CandidateRankRow,
    InvalidCandidateCursor,
    SORT_VERSION,
    sort_candidate_rows,
)


def test_feed_rank_baseline_has_a_user_id_tie_breaker() -> None:
    same_time = datetime(2026, 8, 7, 8, 0, 0)
    rows = [
        CandidateRankRow(user_id=2, is_boosted=False, last_active_at=same_time),
        CandidateRankRow(user_id=9, is_boosted=False, last_active_at=same_time),
        CandidateRankRow(user_id=5, is_boosted=True, last_active_at=same_time),
    ]

    assert [row.user_id for row in sort_candidate_rows(rows)] == [5, 9, 2]


def test_cursor_round_trip_is_signed_and_bound_to_the_query() -> None:
    cursor = CandidateCursor(
        sort_version=SORT_VERSION,
        query_fingerprint="query-a",
        is_boosted=True,
        last_active_at=datetime(2026, 8, 7, 8, 0, 0),
        user_id=9,
    )
    service = CandidateQueryService(secret_key="test-secret")

    token = service.encode_cursor(cursor)

    assert service.decode_cursor(token, expected_fingerprint="query-a") == cursor
    with pytest.raises(InvalidCandidateCursor):
        service.decode_cursor(token[:-1] + ("A" if token[-1] != "A" else "B"))
    with pytest.raises(InvalidCandidateCursor):
        service.decode_cursor(token, expected_fingerprint="query-b")


def test_non_ascii_cursor_payload_is_rejected_as_a_bad_cursor() -> None:
    with pytest.raises(InvalidCandidateCursor):
        CandidateQueryService(secret_key="test-secret").decode_cursor("非ascii.signature")


class _ScalarResult:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class _RowsResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> "_RowsResult":
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows


class _CandidateQueryDb:
    def __init__(self, rows: list[dict[str, object]], total: int) -> None:
        self.rows = rows
        self.total = total
        self.statements: list[str] = []
        self.params: list[dict[str, object]] = []

    async def execute(
        self, statement: object, params: dict[str, object]
    ) -> _ScalarResult | _RowsResult:
        sql = str(statement)
        self.statements.append(sql)
        self.params.append(params)
        if "COUNT(*)" in sql:
            return _ScalarResult(self.total)
        return _RowsResult(self.rows)


class _CursorPagingDb:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    async def execute(
        self, statement: object, params: dict[str, object]
    ) -> _ScalarResult | _RowsResult:
        if "COUNT(*)" in str(statement):
            return _ScalarResult(len(self.rows))
        rows = sorted(
            self.rows,
            key=lambda row: (
                bool(row["is_boosted"]),
                row["last_active_at"] or datetime.min,
                int(row["user_id"]),
            ),
            reverse=True,
        )
        if "cursor_user_id" in params:
            cursor_key = (
                bool(params["cursor_is_boosted"]),
                params.get("cursor_last_active_at") or datetime.min,
                int(params["cursor_user_id"]),
            )
            rows = [
                row
                for row in rows
                if (
                    bool(row["is_boosted"]),
                    row["last_active_at"] or datetime.min,
                    int(row["user_id"]),
                )
                < cursor_key
            ]
        else:
            rows = rows[int(params["candidate_query_offset"]) :]
        return _RowsResult(rows[: int(params["candidate_query_limit"])])


def _snapshot(*, page: int = 1) -> CandidateQuerySnapshot:
    return CandidateQuerySnapshot(
        select_sql="SELECT user_id, is_boosted, last_active_at FROM candidate_rows",
        count_sql="SELECT COUNT(*) FROM candidate_rows",
        where_sql="candidate_rows.visible = 1",
        params={},
        query_fingerprint="fixture-1001",
        page=page,
    )


def test_snapshot_rejects_an_unsupported_sort_version() -> None:
    with pytest.raises(ValueError, match="unsupported candidate sort version"):
        CandidateQuerySnapshot(
            select_sql="SELECT user_id FROM candidate_rows",
            count_sql="SELECT COUNT(*) FROM candidate_rows",
            where_sql="candidate_rows.visible = 1",
            params={},
            query_fingerprint="fixture",
            page=1,
            sort_version="unknown-sort-version",
        )


def test_search_compiled_snapshot_reuses_candidate_query_contract() -> None:
    """M03 search reuses the exact-count / signed-cursor candidate primitives.

    The snapshot returned by the search service is a plain
    ``CandidateQuerySnapshot``: same sort version, exact ``COUNT(DISTINCT u.id)``
    and the same cursor fingerprint binding, so the discovery total matches the
    manual discovery filters exactly and no candidate is duplicated or skipped
    across cursor pages.
    """
    from app.schemas.ai_search import SearchCondition
    from app.services.ai.search import (
        build_search_query_snapshot,
        compile_search_conditions,
    )

    compiled = compile_search_conditions(
        [
            SearchCondition(
                field_key="age",
                operator="between",
                value={"min": 26, "max": 32},
                kind="hard",
                user_action="confirmed",
            ),
            SearchCondition(
                field_key="city_code",
                operator="eq",
                value="330100",
                kind="hard",
                user_action="confirmed",
            ),
            SearchCondition(
                field_key="interest_tags",
                operator="contains",
                value="户外",
                kind="soft",
                user_action="confirmed",
            ),
        ]
    )
    snapshot = build_search_query_snapshot(
        viewer_id=10,
        viewer={"gender": 1, "realname_status": 2},
        viewer_is_vip=True,
        compiled=compiled,
        page=1,
    )

    assert isinstance(snapshot, CandidateQuerySnapshot)
    assert snapshot.sort_version == SORT_VERSION
    assert "COUNT(DISTINCT u.id)" in snapshot.count_sql
    assert snapshot.query_fingerprint
    # 模型输出只以参数出现，绝不成为 SQL 文本。
    assert "户外" not in snapshot.where_sql
    assert snapshot.params["filter_city_code"] == "330100"
    assert "visibility_viewer_id" in snapshot.params


@pytest.mark.asyncio
async def test_total_is_exact_and_not_page_sample_size() -> None:
    rows = [
        {
            "user_id": user_id,
            "is_boosted": user_id == 1001,
            "last_active_at": datetime(2026, 8, 7, 8, 0, 0),
        }
        for user_id in range(1, 21)
    ]
    db = _CandidateQueryDb(rows, total=1001)

    page = await CandidateQueryService(secret_key="test-secret").fetch_page(
        db, _snapshot(), cursor=None, page_size=20
    )

    assert len(page.items) == 20
    assert page.total == 1001
    assert page.total_is_estimate is False
    assert page.has_more is True
    assert page.next_cursor is not None
    assert any("ORDER BY candidate_rows.is_boosted DESC" in sql for sql in db.statements)


@pytest.mark.asyncio
async def test_cursor_page_adds_a_lexicographic_boundary() -> None:
    cursor = CandidateCursor(
        sort_version=SORT_VERSION,
        query_fingerprint="fixture-1001",
        is_boosted=True,
        last_active_at=datetime(2026, 8, 7, 8, 0, 0),
        user_id=20,
    )
    service = CandidateQueryService(secret_key="test-secret")
    db = _CandidateQueryDb([], total=1001)

    await service.fetch_page(
        db,
        _snapshot(),
        cursor=service.encode_cursor(cursor),
        page_size=20,
    )

    select_sql = next(sql for sql in db.statements if "COUNT(*)" not in sql)
    assert "candidate_rows.is_boosted < :cursor_is_boosted" in select_sql
    assert "candidate_rows.last_active_at < :cursor_last_active_at" in select_sql
    assert "candidate_rows.user_id < :cursor_user_id" in select_sql


@pytest.mark.asyncio
async def test_invalid_cursor_is_rejected_before_count_query() -> None:
    service = CandidateQueryService(secret_key="test-secret")
    db = _CandidateQueryDb([], total=1001)

    with pytest.raises(InvalidCandidateCursor):
        await service.fetch_page(
            db,
            _snapshot(),
            cursor="malformed-cursor",
            page_size=20,
        )

    assert db.statements == []


@pytest.mark.asyncio
async def test_null_activity_cursor_only_advances_within_the_null_tie_breaker() -> None:
    cursor = CandidateCursor(
        sort_version=SORT_VERSION,
        query_fingerprint="fixture-1001",
        is_boosted=False,
        last_active_at=None,
        user_id=20,
    )
    service = CandidateQueryService(secret_key="test-secret")
    db = _CandidateQueryDb([], total=1001)

    await service.fetch_page(
        db,
        _snapshot(),
        cursor=service.encode_cursor(cursor),
        page_size=20,
    )

    select_sql = next(sql for sql in db.statements if "COUNT(*)" not in sql)
    assert "candidate_rows.last_active_at IS NULL" in select_sql
    assert "cursor_last_active_at" not in db.params[-1]


@pytest.mark.asyncio
async def test_cursor_pages_cover_a_1001_candidate_fixture_without_duplicates() -> None:
    base = datetime(2026, 8, 7, 8, 0, 0)
    rows = [
        {
            "user_id": user_id,
            "is_boosted": user_id % 10 == 0,
            "last_active_at": (
                None if user_id % 37 == 0 else base - timedelta(hours=user_id % 13)
            ),
        }
        for user_id in range(1, 1002)
    ]
    db = _CursorPagingDb(rows)
    service = CandidateQueryService(secret_key="test-secret")
    cursor: str | None = None
    observed_ids: list[int] = []

    while True:
        page = await service.fetch_page(
            db,
            _snapshot(),
            cursor=cursor,
            page_size=20,
        )
        observed_ids.extend(int(row["user_id"]) for row in page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor
        assert cursor is not None

    expected_ids = [
        row.user_id
        for row in sort_candidate_rows(
            [
                CandidateRankRow(
                    user_id=int(row["user_id"]),
                    is_boosted=bool(row["is_boosted"]),
                    last_active_at=row["last_active_at"],
                )
                for row in rows
            ]
        )
    ]
    assert observed_ids == expected_ids
    assert len(observed_ids) == 1001
