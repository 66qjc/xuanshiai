"""AI 三模块端到端跑分脚本。

在真实 Docker DB（compose.ai-test.yml）上用真实 DeepSeek provider 端到端跑
M04 画像抽取，产出带 commit SHA、可复现的跑分 artifact，证明后端可成立性。

用法:
    python scripts/run_ai_scoring.py --provider deepseek --report-dir artifacts/

不依赖 Worker 异步队列，直接调服务层函数。每个 case 失败不中断整体跑分。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Ensure the backend root (parent of scripts/) is importable regardless of CWD.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ai" / "scoring"


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
        return out
    except Exception:
        return "unknown"


def _git_branch() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def _now() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _pct(numerator: float, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator * 100, 2)


def _profile_metrics(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute precision/recall and supporting metrics from M04 cases.

    Each result carries ``expected`` (ground-truth field keys) and ``predicted``
    (fields the real provider actually produced).
    """
    total = len(results)
    if total == 0:
        return {"total": 0}

    tp = 0  # 正确抽取
    fp = 0  # 误抽取（predicted 非 expected）
    fn = 0  # 漏抽取（expected 未被 predicted）
    latencies: list[float] = []
    successes = 0
    forbidden = 0
    failures: list[dict[str, str]] = []

    for r in results:
        if r.get("error"):
            failures.append({"case_id": r["case_id"], "reason": r["error"]})
            continue
        successes += 1
        predicted = set(r.get("predicted", []))
        expected = set(r.get("expected", []))
        tp += len(predicted & expected)
        fp += len(predicted - expected)
        fn += len(expected - predicted)
        if r.get("latency_ms") is not None:
            latencies.append(r["latency_ms"])
        if r.get("forbidden_published"):
            forbidden += 1

    precision = _pct(tp, tp + fp)
    recall = _pct(tp, tp + fn)
    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0

    return {
        "total": total,
        "succeeded": successes,
        "field_precision": precision,
        "field_recall": recall,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "forbidden_published": forbidden,
        "extract_latency_p50_ms": round(p50, 1),
        "extract_latency_p95_ms": round(p95, 1),
        "publish_success_rate": _pct(successes, total),
        "failures": failures,
    }


def _verdict(metrics: dict[str, Any], thresholds: dict[str, float]) -> str:
    for key, threshold in thresholds.items():
        value = metrics.get(key)
        if value is None:
            return "FAIL"
        if isinstance(value, (int, float)) and value < threshold:
            return "FAIL"
    if metrics.get("forbidden_published", 0) > 0:
        return "FAIL"
    return "PASS"


async def _run_profile_scoring(provider: str, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run real M04 extraction for each case, return per-case results."""
    from app.core.config import settings
    from app.services.ai.providers import get_provider
    from app.services.ai.base import StructuredExtractRequest

    # Force real provider for scoring (override the test conftest if imported).
    settings.ai_provider = provider

    ai_provider = get_provider(provider)
    results: list[dict[str, Any]] = []

    for case in cases:
        case_id = case["case_id"]
        subject = case["subject"]
        source_text = case["source_text"]
        expected = case["expected_fields"]
        t0 = time.monotonic()
        row: dict[str, Any] = {
            "case_id": case_id,
            "subject": subject,
            "expected": expected,
            "predicted": [],
            "latency_ms": None,
            "error": None,
        }
        try:
            req = StructuredExtractRequest(
                subject=subject,
                turn_texts=(source_text,),
                consent_version="profile-text-v1",
                policy_revision="ai-policy-2026-08-20-v2",
            )
            result = await ai_provider.structured_extract(req)
            predicted = [f.field_key for f in result.fields]
            row["predicted"] = predicted
            row["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        except Exception as exc:  # noqa: BLE001 — record failure, continue
            row["error"] = f"{type(exc).__name__}: {exc!s:.200}"
            row["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        results.append(row)
        # Progress to stderr so stdout stays clean for piping.
        status = "OK" if row["error"] is None else "ERR"
        print(f"  [{status}] {case_id}: predicted={row['predicted']}", file=sys.stderr)

    return results


def _search_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute filter precision/recall and condition-count accuracy for M03."""
    total = len(results)
    if total == 0:
        return {"total": 0}

    tp = 0
    fp = 0
    fn = 0
    latencies: list[float] = []
    successes = 0
    count_hits = 0
    failures: list[dict[str, str]] = []

    for r in results:
        if r.get("error"):
            failures.append({"case_id": r["case_id"], "reason": r["error"]})
            continue
        successes += 1
        predicted = set(r.get("predicted_fields", []))
        expected = set(r.get("expected_fields", []))
        tp += len(predicted & expected)
        fp += len(predicted - expected)
        fn += len(expected - predicted)
        if r.get("latency_ms") is not None:
            latencies.append(r["latency_ms"])
        if r.get("conditions_count") == r.get("expected_conditions_count"):
            count_hits += 1

    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0

    return {
        "total": total,
        "succeeded": successes,
        "filter_precision": _pct(tp, tp + fp),
        "filter_recall": _pct(tp, tp + fn),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "conditions_count_match_rate": _pct(count_hits, successes),
        "parse_latency_p50_ms": round(p50, 1),
        "parse_latency_p95_ms": round(p95, 1),
        "parse_success_rate": _pct(successes, total),
        "failures": failures,
    }


async def _run_search_scoring(provider: str, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run real M03 search parse for each case, return per-case results."""
    from app.core.config import settings
    from app.services.ai.providers import get_provider
    from app.services.ai.base import SearchParseRequest

    settings.ai_provider = provider
    ai_provider = get_provider(provider)
    results: list[dict[str, Any]] = []

    for case in cases:
        case_id = case["case_id"]
        query_text = case["query_text"]
        expected_fields = case["expected_filter_fields"]
        expected_count = case["expected_conditions_count"]
        t0 = time.monotonic()
        row: dict[str, Any] = {
            "case_id": case_id,
            "query_text": query_text,
            "expected_fields": expected_fields,
            "expected_conditions_count": expected_count,
            "predicted_fields": [],
            "conditions_count": 0,
            "latency_ms": None,
            "error": None,
        }
        try:
            req = SearchParseRequest(query_text=query_text)
            result = await ai_provider.parse_search_query(req)
            row["predicted_fields"] = [c.field_key for c in result.conditions]
            row["conditions_count"] = len(result.conditions)
            row["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc!s:.200}"
            row["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        results.append(row)
        status = "OK" if row["error"] is None else "ERR"
        print(f"  [{status}] {case_id}: fields={row['predicted_fields']} count={row['conditions_count']}", file=sys.stderr)

    return results


async def _run_module(
    module: str,
    provider: str,
    cases: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, float], str, list[dict[str, Any]]]:
    """Run one scoring module, return (metrics, thresholds, verdict, results)."""
    if module == "profile":
        results = await _run_profile_scoring(provider, cases)
        metrics = _profile_metrics(results)
        thresholds = {"field_precision": 70.0, "field_recall": 70.0}
        verdict = _verdict(metrics, thresholds)
        return metrics, thresholds, verdict, results
    if module == "search":
        results = await _run_search_scoring(provider, cases)
        metrics = _search_metrics(results)
        thresholds = {"filter_precision": 70.0, "filter_recall": 70.0}
        verdict = _verdict(metrics, thresholds)
        return metrics, thresholds, verdict, results
    raise ValueError(f"unknown module: {module}")


_MODULE_CONFIG = {
    "profile": ("profile_cases.json", "ai-profile-scoring.json", "M04-profile"),
    "search": ("search_cases.json", "ai-search-scoring.json", "M03-search"),
}


async def _main_async(provider: str, report_dir: Path, dry_run: bool, module: str | None) -> int:
    modules = [module] if module else list(_MODULE_CONFIG)
    exit_code = 0

    for mod in modules:
        fixture_name, artifact_name, module_label = _MODULE_CONFIG[mod]
        cases = _read_json(FIXTURES / fixture_name)
        print(f"\n{module_label} 跑分: {len(cases)} cases, provider={provider}", file=sys.stderr)

        metrics, thresholds, verdict, _results = await _run_module(mod, provider, cases)

        artifact = {
            "provenance": {
                "commit_sha": _git_sha(),
                "branch": _git_branch(),
                "run_at": _now(),
                "environment": "docker-compose-ai-test",
                "provider": provider,
                "model": "deepseek-v4-flash",
            },
            "module": module_label,
            "case_count": len(cases),
            "metrics": metrics,
            "thresholds": thresholds,
            "verdict": verdict,
        }

        out = json.dumps(artifact, ensure_ascii=False, indent=2)
        if dry_run:
            print(out)
            continue

        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / artifact_name
        path.write_text(out, encoding="utf-8")
        print(f"\n=== {module_label} 跑分结果 ===", file=sys.stderr)
        print(f"verdict: {verdict}", file=sys.stderr)
        print(f"metrics: {json.dumps(metrics, ensure_ascii=False)}", file=sys.stderr)
        print(f"artifact: {path}", file=sys.stderr)
        if verdict == "FAIL":
            exit_code = 1

    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI 三模块端到端跑分")
    parser.add_argument("--provider", default="deepseek", choices=["mock", "deepseek"])
    parser.add_argument("--report-dir", default="artifacts", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--module",
        default=None,
        choices=["profile", "search"],
        help="只跑指定模块；默认跑全部",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args.provider, args.report_dir, args.dry_run, args.module))


if __name__ == "__main__":
    raise SystemExit(main())
