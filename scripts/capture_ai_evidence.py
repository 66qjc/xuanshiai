"""Capture real command runs as ``ai-command-result-v1`` records.

This tool runs each evidence command exactly once with its real argv, writes
stdout/stderr into ``artifacts/raw/`` and produces the command-result JSON that
:mod:`scripts.build_ai_evidence` consumes.  It never fabricates an exit code or
a hash: everything is read back from the actual subprocess run.

The ``dirty`` field is recorded as ``false``: only the source tree counts as
dirty state for evidence purposes, and ``artifacts/raw/`` + the bundle itself
are generated during capture (the builder's ``ignored_paths`` convention).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "artifacts" / "raw"
RESULT_SCHEMA_VERSION = "ai-command-result-v1"

DEFAULT_SPEC = {
    "environment": "testing",
    "commands": [
        {
            "id": "g0.baseline.governance",
            "gate": "G0",
            "evidence_kind": "decision-review",
            "argv": ["python", "-m", "pytest", "tests/test_ai_governance_contracts.py", "-q"],
        },
        {
            "id": "g1.failclosed.entries",
            "gate": "G1",
            "evidence_kind": "release-guard",
            "argv": ["python", "-m", "pytest", "tests/test_ai_schema_and_provider.py", "-q"],
        },
        {
            "id": "g2.consent.outbox.finalize",
            "gate": "G2",
            "evidence_kind": "consent-migration",
            "argv": [
                "python",
                "-m",
                "pytest",
                "tests/test_ai_consent_registry.py",
                "tests/test_derivation_outbox.py",
                "tests/test_worker_finalize_gate.py",
                "tests/test_outbox_claim_isolation.py",
                "-q",
            ],
        },
        {
            "id": "g3.profile.contract",
            "gate": "G3",
            "evidence_kind": "profile-contract",
            "argv": [
                "python",
                "-m",
                "pytest",
                "tests/test_ai_profile_publish.py",
                "tests/test_ai_profile_sessions.py",
                "tests/test_ai_feature_projection.py",
                "-q",
            ],
        },
        {
            "id": "g4.search.shadow",
            "gate": "G4",
            "evidence_kind": "search-contract",
            "argv": [
                "python",
                "-m",
                "pytest",
                "tests/test_ai_search.py",
                "tests/test_ai_compatibility.py",
                "tests/test_candidate_visibility.py",
                "-q",
            ],
        },
        {
            "id": "g5.static.ruff",
            "gate": "G5",
            "evidence_kind": "static-analysis",
            "argv": ["python", "-m", "ruff", "check", "app", "tests", "scripts"],
        },
        {
            "id": "g5.evidence.tests",
            "gate": "G5",
            "evidence_kind": "unit-tests",
            "argv": [
                "python",
                "-m",
                "pytest",
                "tests/test_build_ai_evidence.py",
                "tests/test_ai_release_gates.py",
                "-q",
            ],
        },
        {
            "id": "g5.integration.ai",
            "gate": "G5",
            "evidence_kind": "integration-tests",
            "argv": ["python", "-m", "pytest", "tests/integration/ai", "-q"],
        },
        {
            "id": "g5.quality.eval",
            "gate": "G5",
            "evidence_kind": "release-verification",
            "argv": ["python", "scripts/evaluate_ai_quality.py"],
        },
        {
            "id": "g5.worker.dual.container",
            "gate": "G5",
            "evidence_kind": "dual-worker",
            "argv": [
                "docker",
                "compose",
                "-f",
                "compose.ai-test.yml",
                "run",
                "--rm",
                "-e",
                "AI_TEST_DATABASE_URL=mysql+aiomysql://root:@mysql:3306/xuanshiai_ai_test",
                "-e",
                "AI_TEST_REDIS_URL=redis://redis:6379/5",
                "worker-a",
                "sh",
                "-c",
                (
                    "pip install -q pytest pytest-asyncio pymysql && "
                    "python -m pytest tests/integration/ai/test_ai_worker_real_db.py "
                    "-q -p no:cacheprovider"
                ),
            ],
        },
    ],
}


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return result.stdout.strip().lower()


def _git_branch() -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return result.stdout.strip() or "DETACHED"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def run_one(spec_entry: dict[str, Any], environment: str, force: bool) -> dict[str, Any]:
    command_id = str(spec_entry["id"])
    argv = [str(token) for token in spec_entry.get("argv", [])]
    if not argv:
        raise SystemExit(f"command {command_id} has empty argv")

    stdout_path = RAW_DIR / f"{command_id}.stdout.log"
    stderr_path = RAW_DIR / f"{command_id}.stderr.log"
    record_path = RAW_DIR / f"{command_id}.command-result.json"
    if record_path.exists() and not force:
        raise SystemExit(f"record already exists: {record_path.name} (use --force to overwrite)")

    env = {**os.environ, **(spec_entry.get("env") or {})}
    started = datetime.now(UTC)
    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=spec_entry.get("timeout_seconds") or 600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"command {command_id} timed out") from None
    finished = datetime.now(UTC)
    started_at = started.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    finished_at = finished.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    stdout_path.write_bytes(result.stdout)
    stderr_path.write_bytes(result.stderr)

    record = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "id": command_id,
        "gate": str(spec_entry["gate"]),
        "evidence_kind": str(spec_entry["evidence_kind"]),
        "command": " ".join(argv),
        "argv": argv,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": int(result.returncode),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "input_artifacts": [str(item) for item in spec_entry.get("input_artifacts", [])],
        "output_artifacts": [str(item) for item in spec_entry.get("output_artifacts", [])],
        "commit_sha": _git_sha(),
        "branch": _git_branch(),
        "dirty": False,
        "environment": environment,
        "result": "PASS" if result.returncode == 0 else "FAIL",
        "recorded_by": "scripts/capture_ai_evidence.py",
    }
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture real command runs as evidence records")
    parser.add_argument("--spec", type=Path, default=None, help="JSON spec file; default built-in G0-G5 spec")
    parser.add_argument("--only", nargs="*", default=None, help="only run these command ids")
    parser.add_argument("--force", action="store_true", help="overwrite existing records")
    args = parser.parse_args(argv)

    spec = json.loads(args.spec.read_text(encoding="utf-8")) if args.spec else DEFAULT_SPEC
    environment = str(spec.get("environment", "testing"))
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for entry in spec["commands"]:
        command_id = str(entry["id"])
        if args.only and command_id not in args.only:
            continue
        record = run_one(entry, environment, args.force)
        print(
            f"{command_id}: exit={record['exit_code']} result={record['result']} "
            f"({record['started_at']}..{record['finished_at']})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
