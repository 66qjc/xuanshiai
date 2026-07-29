from app.core.config import Settings
import pytest


def test_membership_price_override_uses_environment_value() -> None:
    configured = Settings(_env_file=None, membership_monthly_price=88.0)

    assert configured.membership_price_override("monthly", "price", 99.0) == 88.0
    assert configured.membership_price_override("quarterly", "price", 269.0) == 269.0


def test_point_cost_override_is_per_product() -> None:
    configured = Settings(_env_file=None, point_cost_extra_apply=20)

    assert configured.point_cost_override("extra_apply", 999) == 20
    assert configured.point_cost_override("paper_plane_unlock", 30) == 30


def test_mock_wechat_payment_is_rejected_outside_test_environments() -> None:
    with pytest.raises(ValueError, match="微信支付 Mock"):
        Settings(_env_file=None, environment="production", auto_init_db=False, wechat_payment_mode="mock")


def test_real_wechat_payment_mode_is_available_for_production() -> None:
    configured = Settings(_env_file=None, environment="production", auto_init_db=False, wechat_payment_mode="real")

    assert configured.wechat_payment_mode == "real"
