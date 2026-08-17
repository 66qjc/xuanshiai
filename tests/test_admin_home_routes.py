from datetime import date
from decimal import Decimal

from app.main import app
from app.schemas.admin_home import AdminDashboard, DailyTrend, DashboardMetrics
from app.api.routes.admin_home import _legacy_page, _order_records


def test_admin_home_routes_are_registered() -> None:
    def walk(routes: object, prefix: str = "") -> set[str]:
        paths: set[str] = set()
        for route in routes or []:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(f"{prefix}{path}")
            if hasattr(route, "original_router"):
                context = getattr(route, "include_context", None)
                paths.update(walk(route.original_router.routes, f"{prefix}{getattr(context, 'prefix', '')}"))
            else:
                paths.update(walk(getattr(route, "routes", None), prefix))
        return paths

    paths = walk(app.routes)
    assert "/api/v1/admin/bootstrap" in paths
    assert "/api/v1/admin/dashboard" in paths
    assert "/api/v1/admin/announcements" in paths
    assert "/api/v1/commonadmin/api/system/getTenantData" in paths
    assert "/api/v1/commonadmin/api/finOrder/getOrderStatics" in paths
    assert "/api/v1/loveadmin/api/loveUser/getAdminIndexStatistic" in paths
    assert "/commonadmin/api/system/getTenantData" in paths
    assert "/loveadmin/api/loveUser/getAdminIndexStatistic" in paths


def test_order_statistics_aggregate_month_and_paginate() -> None:
    report = AdminDashboard(
        from_date=date(2026, 8, 30),
        to_date=date(2026, 9, 1),
        metrics=DashboardMetrics(),
        trends=[
            DailyTrend(date=date(2026, 8, 30), paid_count=1, paid_amount=Decimal("100.00"), net_amount=Decimal("100.00")),
            DailyTrend(date=date(2026, 8, 31), completed_refund_count=1, completed_refund_amount=Decimal("100.00"), net_amount=Decimal("-100.00")),
            DailyTrend(date=date(2026, 9, 1), paid_count=1, paid_amount=Decimal("50.00"), net_amount=Decimal("50.00")),
        ],
    )

    records = _order_records(report, "Month")

    assert records[0]["totalAmount"] == 100.0
    assert records[0]["refundedAmount"] == 100.0
    assert records[0]["netAmount"] == 0.0
    assert _legacy_page(records, page=2, limit=1)["records"] == [records[1]]
