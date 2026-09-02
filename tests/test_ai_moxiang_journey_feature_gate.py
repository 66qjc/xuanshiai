"""Phase 1 moxiang_journey feature gate + config registration tests.

Contract v1.1 §10:
- ``app.core.config.Settings`` exposes ``ai_moxiang_journey_enabled`` (default
  ``False``).
- The new switch participates in ``_validate_ai_feature_gates`` so production
  fail-closed checks cover it.
- ``tests/conftest.py`` registers the new attribute with a test default so
  existing suites keep running without the env var being set explicitly.

These are pure-source / ``Settings(...)`` construction tests; they do not
require a real database.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_ROOT / "app" / "core" / "config.py"
CONFTEST_FILE = REPO_ROOT / "tests" / "conftest.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_config_exposes_moxiang_journey_switch() -> None:
    """``Settings`` must declare ``ai_moxiang_journey_enabled``."""
    source = _read(CONFIG_FILE)
    assert "ai_moxiang_journey_enabled" in source, (
        "config.py must declare ai_moxiang_journey_enabled"
    )


def test_config_switch_defaults_to_false() -> None:
    """The new switch must default to ``False`` (Contract v1.1 §10)."""
    from app.core.config import Settings

    settings = Settings()
    assert settings.ai_moxiang_journey_enabled is False


def test_config_switch_is_registered_in_feature_gates() -> None:
    """The new switch must participate in the fail-closed feature gate check."""
    source = _read(CONFIG_FILE)
    # The body of ``_validate_ai_feature_gates`` must mention the new switch.
    body = source.split("def _validate_ai_feature_gates", 1)[1]
    body = body.split("\n    def ", 1)[0]
    assert "ai_moxiang_journey_enabled" in body, (
        "_validate_ai_feature_gates must include ai_moxiang_journey_enabled"
    )


def test_conftest_registers_test_default_for_moxiang_journey() -> None:
    """``tests/conftest.py`` must register the test default for the new switch."""
    source = _read(CONFTEST_FILE)
    assert "ai_moxiang_journey_enabled" in source, (
        "tests/conftest.py must set a test default for ai_moxiang_journey_enabled"
    )


def test_settings_construction_does_not_require_new_env_var() -> None:
    """Existing tests / dev mode must keep working without setting the new env var."""
    from app.core.config import Settings

    # Force a non-production environment to skip the production fail-closed path.
    settings = Settings(environment="development")
    assert hasattr(settings, "ai_moxiang_journey_enabled")
    assert settings.ai_moxiang_journey_enabled is False
