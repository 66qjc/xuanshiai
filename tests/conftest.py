"""Top-level test configuration.

Unit tests must never call a real LLM provider and must not depend on local
``.env`` overrides.  When the local ``.env`` sets ``AI_PROVIDER=deepseek`` and
``AI_MASTER_ENABLED=true`` (for development/联调), unit tests that read the
``settings`` singleton would otherwise route through the real DeepSeek API and
see feature gates as enabled, making them slow, flaky, non-deterministic, and
dependent on a network key.  ``settings`` is an ``lru_cache`` singleton
initialised at import time, so setting env vars at fixture time is too late;
we mutate the attributes directly to the declared defaults.

Integration tests under ``tests/integration/`` have their own conftest and may
opt into a real provider / enabled features explicitly when needed.
"""

from __future__ import annotations

import pytest

from app.core.config import settings

# Defaults declared in app/core/config.py — tests assume these, regardless of .env.
_DEFAULT_FEATURE_FLAGS = {
    "ai_master_enabled": False,
    "ai_profile_enabled": False,
    "ai_search_enabled": False,
    "ai_compatibility_shadow_enabled": False,
    "ai_provider": "mock",
}


@pytest.fixture(autouse=True)
def _hermetic_ai_settings() -> None:
    """Reset AI provider and feature flags to declared defaults for unit tests.

    Tests that genuinely need enabled features or a real provider should
    override the relevant ``settings`` attribute in the test itself (many
    already do via ``_enable_search_feature`` and similar helpers).
    """
    for attr, default in _DEFAULT_FEATURE_FLAGS.items():
        setattr(settings, attr, default)
