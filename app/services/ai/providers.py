"""Deterministic mock provider and the provider registry.

``MockAIProvider`` is the only runnable provider in phase 1: inputs and outputs
are deterministic fixtures, and it supports injecting timeout / 429 / 5xx /
schema-invalid / policy-blocked scenarios so the Gateway error classification
can be exercised without a real model.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.services.ai.base import (
    ExtractedField,
    ModerationRequest,
    ModerationResult,
    ProviderError,
    ProviderErrorKind,
    SearchCondition,
    SearchParseRequest,
    SearchParseResult,
    StructuredExtractRequest,
    StructuredExtractResult,
)

# Deterministic fixture field values for profile extraction. Keys are the
# frozen allowlist field names; values are (value, source_quote, confidence).
_PROFILE_FIXTURE_FIELDS: dict[str, tuple[Any, str | None, float]] = {
    "interest_tags": (["旅行", "看展"], "周末喜欢旅行和看展", 0.91),
    "city_code": ("330100", "住在杭州", 0.95),
    "marriage_status": ("single", "未婚", 0.90),
    "education_level": (4, "本科", 0.93),
    "height_cm": (172, "身高172cm", 0.97),
    "income_band": (2, "月收入一档到二档", 0.72),
    "occupation_group": ("技术", "互联网做技术", 0.85),
    "lifestyle_tags": (["户外"], "周末愿意户外", 0.78),
    "relationship_goal": ("marriage", "想认真奔着结婚", 0.88),
}

_SEARCH_FIXTURE_CONDITIONS: tuple[SearchCondition, ...] = (
    SearchCondition(
        field_key="age",
        operator="between",
        value={"min": 26, "max": 32},
        kind="hard",
        confidence=0.99,
        source_span="26到32岁",
    ),
    SearchCondition(
        field_key="city_code",
        operator="eq",
        value="330100",
        kind="hard",
        confidence=0.95,
        source_span="住杭州",
    ),
    SearchCondition(
        field_key="education_level",
        operator="gte",
        value=4,
        kind="hard",
        confidence=0.90,
        source_span="本科以上",
    ),
    SearchCondition(
        field_key="interest_tags",
        operator="contains",
        value="户外",
        kind="soft",
        confidence=0.78,
        source_span="周末愿意户外",
    ),
)


class MockAIProvider:
    """Deterministic fixture provider with failure injection.

    ``failures`` accepts any of: ``timeout``, ``http_429``, ``http_500``,
    ``schema_invalid``, ``policy_blocked``.  A ``"<method>:<name>"`` entry
    scopes the failure to one method (``structured_extract``,
    ``parse_search_query``, ``moderate_text``).
    """

    def __init__(
        self,
        failures: Iterable[str] = (),
        response_delay_seconds: float = 0.0,
    ) -> None:
        self._failures = set(failures)
        self._response_delay_seconds = response_delay_seconds

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------
    async def structured_extract(
        self, request: StructuredExtractRequest
    ) -> StructuredExtractResult:
        self._check_failure("structured_extract")
        fixture = "profile-personal-v1"
        if "schema_invalid" in self._failures or "structured_extract:schema_invalid" in self._failures:
            fixture = "profile-schema-invalid"
        return await self.structured_extract_fixture(fixture, request=request)

    async def parse_search_query(
        self, request: SearchParseRequest
    ) -> SearchParseResult:
        self._check_failure("parse_search_query")
        conditions = tuple(
            condition
            for condition in _SEARCH_FIXTURE_CONDITIONS
            if condition.field_key in request.allowlist
        )
        unknown: tuple[str, ...] = ()
        if "pure_free" in request.query_text:
            unknown = ("pure_free",)
        return SearchParseResult(
            schema_version="search-condition-v1",
            conditions=conditions,
            unknown=unknown,
        )

    async def moderate_text(
        self, request: ModerationRequest
    ) -> ModerationResult:
        self._check_failure("moderate_text")
        blocked = any(
            token in request.text
            for token in ("联系方式", "加微信", "敏感", "绝对保证")
        )
        if blocked:
            return ModerationResult(allowed=False, action="reject", reason_code="SENSITIVE_TEXT")
        return ModerationResult(allowed=True, action="allow")

    # ------------------------------------------------------------------
    # Deterministic fixture accessor (used by the acceptance test)
    # ------------------------------------------------------------------
    async def structured_extract_fixture(
        self,
        fixture_name: str,
        request: StructuredExtractRequest | None = None,
    ) -> StructuredExtractResult:
        """Return a deterministic typed extraction fixture.

        ``profile-interest-v1`` is the canonical acceptance fixture: its first
        field is ``interest_tags`` with ``confirmation_status == "suggested"``.
        """
        self._check_failure("structured_extract")
        allowlist = request.allowlist if request is not None else None
        if fixture_name == "profile-interest-v1":
            fields = self._fields_for(("interest_tags",), allowlist)
        elif fixture_name == "profile-schema-invalid":
            fields = (
                ExtractedField(
                    field_key="interest_tags",
                    subject="personal",
                    value=["旅行"],
                    confidence=1.7,  # schema violation: confidence outside 0..1
                ),
            )
        elif fixture_name == "profile-ideal-partner-v1":
            fields = self._fields_for(
                ("city_code", "education_level", "height_cm"), allowlist
            )
        else:
            # profile-personal-v1 and the default fallback.
            fields = self._fields_for(
                (
                    "interest_tags",
                    "city_code",
                    "marriage_status",
                    "education_level",
                    "height_cm",
                    "income_band",
                    "occupation_group",
                    "lifestyle_tags",
                    "relationship_goal",
                ),
                allowlist,
            )
        return StructuredExtractResult(
            schema_version="profile-extract-v1", fields=fields
        )

    # ------------------------------------------------------------------
    # Failure injection helpers
    # ------------------------------------------------------------------
    def _check_failure(self, method: str) -> None:
        """Raise the configured failure for the method, if any."""
        failure_map: dict[str, tuple[str, ProviderErrorKind, int]] = {
            "timeout": ("AI_TEMPORARILY_UNAVAILABLE", ProviderErrorKind.RETRYABLE, 0),
            "http_429": ("AI_QUOTA_EXCEEDED", ProviderErrorKind.RETRYABLE, 2000),
            "http_500": ("AI_TEMPORARILY_UNAVAILABLE", ProviderErrorKind.RETRYABLE, 1000),
            "policy_blocked": ("AI_POLICY_DENIED", ProviderErrorKind.NON_RETRYABLE, 0),
        }
        for name, (code, kind, retry_after_ms) in failure_map.items():
            if name in self._failures or f"{method}:{name}" in self._failures:
                raise ProviderError(
                    code=code,
                    message=f"mock provider injected failure: {name}",
                    kind=kind,
                    retry_after_ms=retry_after_ms,
                )
        if "schema_invalid" in self._failures or f"{method}:schema_invalid" in self._failures:
            # schema_invalid does not raise; it returns an invalid result that
            # the Gateway must reject as a non-retryable schema violation.
            return

    def _fields_for(
        self, keys: tuple[str, ...], allowlist: frozenset[str] | None
    ) -> tuple[ExtractedField, ...]:
        fields: list[ExtractedField] = []
        for key in keys:
            if allowlist is not None and key not in allowlist:
                continue
            value, source_quote, confidence = _PROFILE_FIXTURE_FIELDS[key]
            fields.append(
                ExtractedField(
                    field_key=key,
                    subject="personal",
                    value=value,
                    source_quote=source_quote,
                    confidence=confidence,
                    needs_confirmation=True,
                    confirmation_status="suggested",
                )
            )
        return tuple(fields)


class AIProviderRegistry:
    """Provider registry keyed by configuration name."""

    def __init__(self) -> None:
        self._factories: dict[str, type[MockAIProvider]] = {
            "mock": MockAIProvider,
        }

    def register(self, name: str, factory: type[MockAIProvider]) -> None:
        self._factories[name] = factory

    def create(self, name: str = "mock", **kwargs: Any) -> MockAIProvider:
        if name not in self._factories:
            raise KeyError(f"未知 AI provider: {name}")
        return self._factories[name](**kwargs)


_provider_registry = AIProviderRegistry()


def get_provider(name: str = "mock", **kwargs: Any) -> MockAIProvider:
    """Return a provider instance from the shared registry."""
    return _provider_registry.create(name, **kwargs)
