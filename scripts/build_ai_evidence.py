"""Build deterministic, commit-bound AI evidence from command result files.

The builder never executes a test command and never accepts source text as proof
that a command ran.  Every input is an ``ai-command-result-v1`` JSON record that
points to captured stdout/stderr files.  The builder validates the command
allowlist, current Git SHA/branch/dirty state, timestamps and exit code, then
hashes every referenced artifact.  Raw output is never copied into the bundle.

An incomplete bundle is still useful for audit, but its result is ``NO-GO`` and
the CLI exits 2.  Only a clean, reviewed bundle with evidence for G0-G7 can be
``PASS``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_SCHEMA_VERSION = "ai-evidence-v1"
COMMAND_RESULT_SCHEMA_VERSION = "ai-command-result-v1"
EVIDENCE_SCHEMA_PATH = ROOT / "artifacts" / "schemas" / "ai-evidence-v1.schema.json"

ALL_GATES = tuple(f"G{index}" for index in range(8))
ARTIFACT_TYPES = frozenset({"command-bundle", "readiness", "rollback", "stability"})
TARGETS = frozenset({"internal", "production"})
ENVIRONMENTS = frozenset({"development", "testing", "staging", "production"})
RESULTS = frozenset({"PASS", "FAIL", "CONDITIONAL", "NOT_RUN", "NO-GO"})
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMAND_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
EVIDENCE_KIND_PATTERN = COMMAND_ID_PATTERN

KNOWN_EVIDENCE_KINDS = frozenset(
    {
        "decision-review",
        "release-guard",
        "consent-migration",
        "outbox-concurrency",
        "worker-finalize",
        "profile-contract",
        "search-contract",
        "compatibility-shadow",
        "static-analysis",
        "unit-tests",
        "integration-tests",
        "migration-matrix",
        "privacy-matrix",
        "deletion-propagation",
        "dual-worker",
        "wechat-acceptance",
        "rollback-drill",
        "stability-observation",
        "release-verification",
        "graph-refresh",
    }
)

_SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|cookie|password|passwd|secret|token|api[_-]?key|"
    r"database[_-]?url|dsn|phone|id[_-]?card|prompt|transcript|raw[_-]?content)"
)
_SENSITIVE_FLAGS = frozenset(
    {
        "--authorization",
        "--cookie",
        "--password",
        "--passwd",
        "--secret",
        "--token",
        "--api-key",
        "--api_key",
        "--database-url",
        "--database_url",
        "--dsn",
    }
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@", re.I)
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")


class EvidenceBuildError(ValueError):
    """Raised when a command record cannot be treated as release evidence."""


@dataclass(frozen=True)
class GitState:
    """The source state to which evidence is bound."""

    commit_sha: str
    branch: str
    dirty: bool


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 digest of ``path`` without loading it whole."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_timestamp(value: Any, field_name: str) -> datetime:
    """Parse an RFC3339 timestamp and require an explicit timezone."""
    if not isinstance(value, str) or not value.strip():
        raise EvidenceBuildError(f"{field_name} must be an RFC3339 timestamp")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvidenceBuildError(f"{field_name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceBuildError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """Return a stable UTC RFC3339 timestamp."""
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def redact_text(value: str) -> str:
    """Redact common credentials and direct identifiers from metadata strings."""
    redacted = _BEARER.sub("Bearer [REDACTED]", value)
    redacted = _URL_CREDENTIALS.sub(r"\g<scheme>[REDACTED]@", redacted)
    redacted = _PHONE.sub("[REDACTED_PHONE]", redacted)
    return _ID_CARD.sub("[REDACTED_ID_CARD]", redacted)


def redact_value(value: Any, key: str | None = None) -> Any:
    """Recursively redact sensitive metadata; hashes and booleans remain intact."""
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_value(item_value, str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_argv(argv: Sequence[str]) -> list[str]:
    """Redact values following sensitive flags and ``--flag=value`` forms."""
    redacted: list[str] = []
    redact_next = False
    for token in argv:
        text = str(token)
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        flag, separator, _ = text.partition("=")
        if flag.lower() in _SENSITIVE_FLAGS:
            redacted.append(f"{flag}=[REDACTED]" if separator else flag)
            redact_next = not separator
            continue
        redacted.append(redact_text(text))
    return redacted


def _executable_name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower()


def command_is_allowed(argv: Sequence[str]) -> bool:
    """Return whether ``argv`` belongs to the reviewed release command allowlist."""
    if not argv or any(not isinstance(token, str) or not token for token in argv):
        return False
    if any(any(operator in token for operator in (";", "|", "`", "\n", "\r")) for token in argv):
        return False

    tokens = list(argv)
    executable = _executable_name(tokens[0])
    if executable in {"uv", "uv.exe"}:
        if len(tokens) < 3 or tokens[1].lower() != "run":
            return False
        tokens = tokens[2:]
        executable = _executable_name(tokens[0])

    if executable in {"python", "python.exe", "py", "py.exe"}:
        if len(tokens) >= 3 and tokens[1] == "-m":
            return tokens[2] in {"pytest", "ruff", "app.workers.ai_worker"}
        if len(tokens) < 2:
            return False
        script = _executable_name(tokens[1])
        return script in {
            "build_ai_evidence.py",
            "capture_ai_evidence.py",
            "evaluate_ai_quality.py",
            "manage_ai_migration.py",
            "verify_ai_release.py",
        }

    if executable in {"pytest", "pytest.exe", "ruff", "ruff.exe"}:
        return True
    if executable in {"node", "node.exe"}:
        return len(tokens) >= 2 and tokens[1].replace("\\", "/").startswith("tests/")
    if executable in {"npm", "npm.cmd", "npm.exe"}:
        return len(tokens) >= 3 and tokens[1] == "run" and (
            tokens[2] == "test" or tokens[2].startswith("verify:")
        )
    if executable in {"docker", "docker.exe"}:
        return len(tokens) >= 2 and tokens[1] == "compose"
    if executable in {"git", "git.exe"}:
        return len(tokens) >= 2 and tokens[1] in {
            "diff",
            "status",
            "rev-parse",
            "show",
            "log",
        }
    if executable in {"graphify", "graphify.exe"}:
        return len(tokens) >= 2 and tokens[1] in {"query", "update"}
    return False


def _run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return result.stdout.strip()


def get_git_state(root: Path = ROOT, ignored_paths: Iterable[Path] = ()) -> GitState:
    """Read Git state, optionally ignoring exact generated evidence paths or dirs."""
    resolved_root = root.resolve()
    ignored: set[str] = set()
    ignored_dirs: set[str] = set()
    for path in ignored_paths:
        try:
            rel = path.resolve().relative_to(resolved_root).as_posix()
        except ValueError:
            continue
        if path.is_dir():
            ignored_dirs.add(rel + "/")
        else:
            ignored.add(rel)

    # Capture raw status without leading/trailing stripping: ``stdout.strip()``
    # would erase the leading space in the first `` M <path>`` porcelain line,
    # shifting the path under ``line[3:]`` by one character.
    status_result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    status_lines = status_result.stdout.splitlines()
    dirty_lines: list[str] = []
    for line in status_lines:
        candidate = line[3:].strip().strip('"').replace("\\", "/") if len(line) > 3 else ""
        if " -> " in candidate:
            candidate = candidate.rsplit(" -> ", 1)[-1]
        if candidate in ignored:
            continue
        if any(candidate.startswith(prefix) for prefix in ignored_dirs):
            continue
        dirty_lines.append(line)
    return GitState(
        commit_sha=_run_git(root, "rev-parse", "HEAD").lower(),
        branch=_run_git(root, "branch", "--show-current") or "DETACHED",
        dirty=bool(dirty_lines),
    )


def _relative_file(path_value: Any, *, record_path: Path, root: Path, field: str) -> Path:
    if not isinstance(path_value, str) or not path_value.strip():
        raise EvidenceBuildError(f"{field} must be a non-empty path")
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = record_path.parent / candidate
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise EvidenceBuildError(f"{field} does not exist: {path_value}")
    return resolved


def relative_label(path: Path, root: Path = ROOT) -> str:
    """Return a stable repository-relative POSIX path."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise EvidenceBuildError(f"artifact is outside repository: {path}") from exc


def evidence_label(path: Path, root: Path = ROOT) -> str:
    """Label an evidence file: repo-relative inside the repo, ``external:`` outside."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return f"external:{resolved.as_posix()}"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceBuildError(f"invalid JSON result file: {path}") from exc
    if not isinstance(value, dict) or not value:
        raise EvidenceBuildError(f"result file must contain a non-empty object: {path}")
    return value


def _require_string(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceBuildError(f"command result missing {key}")
    return value.strip()


def _artifact_hashes(
    values: Any,
    *,
    record_path: Path,
    root: Path,
    field: str,
) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise EvidenceBuildError(f"{field} must be a list of repository paths")
    hashes: dict[str, str] = {}
    for value in sorted(set(values)):
        artifact = _relative_file(value, record_path=record_path, root=root, field=field)
        hashes[evidence_label(artifact, root)] = sha256_file(artifact)
    return hashes


def load_command_evidence(
    result_path: Path,
    *,
    root: Path,
    git_state: GitState,
    environment: str,
) -> dict[str, Any]:
    """Load, validate and hash one ``ai-command-result-v1`` record."""
    result_path = result_path.resolve()
    relative_result = evidence_label(result_path, root)
    record = _read_json_object(result_path)
    if record.get("schema_version") != COMMAND_RESULT_SCHEMA_VERSION:
        raise EvidenceBuildError("command result schema_version must be ai-command-result-v1")

    command_id = _require_string(record, "id")
    if not COMMAND_ID_PATTERN.fullmatch(command_id):
        raise EvidenceBuildError(f"invalid command id: {command_id}")
    gate = _require_string(record, "gate")
    if gate not in ALL_GATES:
        raise EvidenceBuildError(f"invalid gate for {command_id}: {gate}")
    evidence_kind = _require_string(record, "evidence_kind")
    if not EVIDENCE_KIND_PATTERN.fullmatch(evidence_kind):
        raise EvidenceBuildError(f"invalid evidence_kind for {command_id}")
    if evidence_kind not in KNOWN_EVIDENCE_KINDS:
        raise EvidenceBuildError(f"unknown evidence_kind for {command_id}: {evidence_kind}")

    argv = record.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
        raise EvidenceBuildError(f"{command_id} argv must be a non-empty string list")
    if not command_is_allowed(argv):
        raise EvidenceBuildError(f"unknown or unsafe command for {command_id}")

    if _require_string(record, "commit_sha").lower() != git_state.commit_sha:
        raise EvidenceBuildError(f"stale commit_sha for {command_id}")
    if _require_string(record, "branch") != git_state.branch:
        raise EvidenceBuildError(f"branch mismatch for {command_id}")
    if record.get("dirty") is not git_state.dirty:
        raise EvidenceBuildError(f"dirty state mismatch for {command_id}")
    if _require_string(record, "environment") != environment:
        raise EvidenceBuildError(f"environment mismatch for {command_id}")

    started = parse_timestamp(record.get("started_at"), f"{command_id}.started_at")
    finished = parse_timestamp(record.get("finished_at"), f"{command_id}.finished_at")
    if finished < started:
        raise EvidenceBuildError(f"finished_at precedes started_at for {command_id}")
    exit_code = record.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise EvidenceBuildError(f"exit_code must be an integer for {command_id}")
    if exit_code != 0:
        raise EvidenceBuildError(f"non-zero exit_code for {command_id}: {exit_code}")
    if record.get("result") != "PASS":
        raise EvidenceBuildError(f"command result must be PASS for {command_id}")

    stdout = _relative_file(
        record.get("stdout_path"), record_path=result_path, root=root, field="stdout_path"
    )
    stderr = _relative_file(
        record.get("stderr_path"), record_path=result_path, root=root, field="stderr_path"
    )
    input_hashes = _artifact_hashes(
        record.get("input_artifacts"),
        record_path=result_path,
        root=root,
        field="input_artifacts",
    )
    output_hashes = _artifact_hashes(
        record.get("output_artifacts"),
        record_path=result_path,
        root=root,
        field="output_artifacts",
    )
    output_hashes[evidence_label(stdout, root)] = sha256_file(stdout)
    output_hashes[evidence_label(stderr, root)] = sha256_file(stderr)
    redacted_argv = redact_argv(argv)

    return {
        "id": command_id,
        "gate": gate,
        "evidence_kind": evidence_kind,
        "command": subprocess.list2cmdline(redacted_argv),
        "argv": redacted_argv,
        "started_at": format_timestamp(started),
        "finished_at": format_timestamp(finished),
        "exit_code": exit_code,
        "stdout_path": evidence_label(stdout, root),
        "stdout_sha256": sha256_file(stdout),
        "stderr_path": evidence_label(stderr, root),
        "stderr_sha256": sha256_file(stderr),
        "result_file": relative_result,
        "result_file_sha256": sha256_file(result_path),
        "input_hashes": dict(sorted(input_hashes.items())),
        "output_hashes": dict(sorted(output_hashes.items())),
        "result": "PASS",
    }


def validate_evidence_shape(payload: Any) -> list[str]:
    """Return stable structural blockers without relying on jsonschema at runtime."""
    if not isinstance(payload, dict) or not payload:
        return ["evidence_empty_or_not_object"]
    required = {
        "schema_version",
        "artifact_type",
        "target",
        "branch",
        "commit_sha",
        "dirty",
        "environment",
        "generated_at",
        "policy_version",
        "provider",
        "model",
        "schema_versions",
        "commands",
        "exit_codes",
        "input_artifact_hashes",
        "output_artifact_hashes",
        "reviewer",
        "gates",
        "blockers",
        "result",
    }
    blockers = [f"evidence_missing_field:{key}" for key in sorted(required - payload.keys())]
    if blockers:
        return blockers
    if payload["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        blockers.append("evidence_schema_version_invalid")
    if payload["artifact_type"] not in ARTIFACT_TYPES:
        blockers.append("evidence_artifact_type_invalid")
    if payload["target"] not in TARGETS:
        blockers.append("evidence_target_invalid")
    if not isinstance(payload["branch"], str) or not payload["branch"]:
        blockers.append("evidence_branch_invalid")
    if not isinstance(payload["commit_sha"], str) or not SHA_PATTERN.fullmatch(
        payload["commit_sha"]
    ):
        blockers.append("evidence_commit_sha_invalid")
    if not isinstance(payload["dirty"], bool):
        blockers.append("evidence_dirty_invalid")
    if payload["environment"] not in ENVIRONMENTS:
        blockers.append("evidence_environment_invalid")
    try:
        parse_timestamp(payload["generated_at"], "generated_at")
    except EvidenceBuildError:
        blockers.append("evidence_generated_at_invalid")
    if payload["result"] not in RESULTS:
        blockers.append("evidence_result_invalid")

    commands = payload["commands"]
    exit_codes = payload["exit_codes"]
    if not isinstance(commands, list):
        blockers.append("evidence_commands_invalid")
        commands = []
    if not isinstance(exit_codes, dict):
        blockers.append("evidence_exit_codes_invalid")
        exit_codes = {}
    seen_ids: set[str] = set()
    for command in commands:
        if not isinstance(command, dict):
            blockers.append("evidence_command_invalid")
            continue
        command_id = command.get("id")
        if not isinstance(command_id, str) or not COMMAND_ID_PATTERN.fullmatch(command_id):
            blockers.append("evidence_command_id_invalid")
            continue
        if command_id in seen_ids:
            blockers.append(f"evidence_command_duplicate:{command_id}")
        seen_ids.add(command_id)
        if command.get("gate") not in ALL_GATES:
            blockers.append(f"evidence_command_gate_invalid:{command_id}")
        argv = command.get("argv")
        if not isinstance(argv, list) or not command_is_allowed(argv):
            blockers.append(f"evidence_command_unknown:{command_id}")
        exit_code = command.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            blockers.append(f"evidence_command_exit_invalid:{command_id}")
        elif exit_codes.get(command_id) != exit_code:
            blockers.append(f"evidence_exit_code_mismatch:{command_id}")
        for hash_key in (
            "stdout_sha256",
            "stderr_sha256",
            "result_file_sha256",
        ):
            digest = command.get(hash_key)
            if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
                blockers.append(f"evidence_command_hash_invalid:{command_id}:{hash_key}")

    if set(exit_codes) != seen_ids:
        blockers.append("evidence_exit_code_keys_mismatch")
    for map_key in ("input_artifact_hashes", "output_artifact_hashes"):
        hash_map = payload[map_key]
        if not isinstance(hash_map, dict):
            blockers.append(f"evidence_hash_map_invalid:{map_key}")
            continue
        for path, digest in hash_map.items():
            if not isinstance(path, str) or not path:
                blockers.append(f"evidence_hash_path_invalid:{map_key}")
            if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
                blockers.append(f"evidence_hash_invalid:{map_key}:{path}")

    reviewer = payload["reviewer"]
    if not isinstance(reviewer, dict) or not {
        "name",
        "role",
        "status",
    }.issubset(reviewer):
        blockers.append("evidence_reviewer_invalid")
    gates = payload["gates"]
    if not isinstance(gates, dict) or set(gates) != set(ALL_GATES):
        blockers.append("evidence_gates_invalid")
    else:
        for gate, entry in gates.items():
            if not isinstance(entry, dict) or entry.get("status") not in RESULTS:
                blockers.append(f"evidence_gate_status_invalid:{gate}")
            elif not isinstance(entry.get("command_ids"), list):
                blockers.append(f"evidence_gate_commands_invalid:{gate}")
    if not isinstance(payload["blockers"], list):
        blockers.append("evidence_blockers_invalid")
    return list(dict.fromkeys(blockers))


def build_evidence(
    result_files: Sequence[Path],
    *,
    target: str,
    environment: str,
    reviewer: str,
    reviewer_role: str = "independent-evidence-reviewer",
    review_status: str = "PENDING",
    policy_version: str = "NOT_SET",
    provider: str = "mock",
    model: str | None = None,
    artifact_type: str = "command-bundle",
    root: Path = ROOT,
    git_state: GitState | None = None,
) -> dict[str, Any]:
    """Build an evidence object from real command result files."""
    if target not in TARGETS:
        raise EvidenceBuildError(f"unsupported target: {target}")
    if environment not in ENVIRONMENTS:
        raise EvidenceBuildError(f"unsupported environment: {environment}")
    if target == "production" and environment != "production":
        raise EvidenceBuildError("production target requires production environment")
    if target == "internal" and environment == "production":
        raise EvidenceBuildError("internal target cannot use production environment")
    if artifact_type not in ARTIFACT_TYPES:
        raise EvidenceBuildError(f"unsupported artifact_type: {artifact_type}")
    if review_status not in {"REVIEWED", "PENDING", "NOT_RUN"}:
        raise EvidenceBuildError(f"unsupported review_status: {review_status}")
    if not result_files:
        raise EvidenceBuildError("at least one command result file is required")

    state = git_state or get_git_state(root)
    if not SHA_PATTERN.fullmatch(state.commit_sha):
        raise EvidenceBuildError("current Git commit SHA is invalid")

    commands = [
        load_command_evidence(
            path,
            root=root,
            git_state=state,
            environment=environment,
        )
        for path in sorted((Path(path) for path in result_files), key=lambda path: path.as_posix())
    ]
    commands.sort(key=lambda item: item["id"])
    command_ids = [item["id"] for item in commands]
    if len(command_ids) != len(set(command_ids)):
        raise EvidenceBuildError("duplicate command id across result files")

    gates: dict[str, dict[str, Any]] = {}
    for gate in ALL_GATES:
        ids = [item["id"] for item in commands if item["gate"] == gate]
        gates[gate] = {"status": "PASS" if ids else "NOT_RUN", "command_ids": ids}

    input_hashes: dict[str, str] = {}
    output_hashes: dict[str, str] = {}
    for command in commands:
        input_hashes[command["result_file"]] = command["result_file_sha256"]
        input_hashes.update(command["input_hashes"])
        output_hashes.update(command["output_hashes"])

    blockers: list[str] = []
    blockers.extend(f"gate_not_run:{gate}" for gate, entry in gates.items() if entry["status"] != "PASS")
    if state.dirty:
        blockers.append("working_tree_dirty")
    if review_status != "REVIEWED" or reviewer.strip().upper() in {"", "UNASSIGNED", "NOT_RUN"}:
        blockers.append("independent_review_not_complete")
    if not policy_version.strip() or policy_version.strip().upper() in {"NOT_SET", "NOT_RUN"}:
        blockers.append("policy_version_missing")
    if target == "production" and provider.strip().lower() == "mock":
        blockers.append("production_provider_must_not_be_mock")

    latest_finished = max(
        parse_timestamp(item["finished_at"], f"{item['id']}.finished_at") for item in commands
    )
    payload: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "artifact_type": artifact_type,
        "target": target,
        "branch": redact_text(state.branch),
        "commit_sha": state.commit_sha,
        "dirty": state.dirty,
        "environment": environment,
        "generated_at": format_timestamp(latest_finished),
        "policy_version": redact_text(policy_version.strip() or "NOT_SET"),
        "provider": redact_text(provider.strip() or "NOT_SET"),
        "model": redact_text(model) if model else None,
        "schema_versions": {
            "command_result": COMMAND_RESULT_SCHEMA_VERSION,
            "evidence": EVIDENCE_SCHEMA_VERSION,
        },
        "commands": commands,
        "exit_codes": {item["id"]: item["exit_code"] for item in commands},
        "input_artifact_hashes": dict(sorted(input_hashes.items())),
        "output_artifact_hashes": dict(sorted(output_hashes.items())),
        "reviewer": {
            "name": redact_text(reviewer.strip() or "UNASSIGNED"),
            "role": redact_text(reviewer_role.strip() or "independent-evidence-reviewer"),
            "status": review_status,
        },
        "gates": gates,
        "blockers": list(dict.fromkeys(blockers)),
        "result": "PASS" if not blockers else "NO-GO",
        "details": {
            "evidence_kinds": sorted({item["evidence_kind"] for item in commands}),
            "raw_output_embedded": False,
            "source_result_count": len(commands),
        },
    }
    structural_blockers = validate_evidence_shape(payload)
    if structural_blockers:
        raise EvidenceBuildError("invalid generated evidence: " + ",".join(structural_blockers))
    return payload


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write canonical, stable JSON with a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _default_environment(target: str) -> str:
    return "production" if target == "production" else "testing"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build commit-bound AI evidence from captured command result files"
    )
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--environment", choices=sorted(ENVIRONMENTS))
    parser.add_argument(
        "--result-file",
        "--input",
        action="append",
        dest="result_files",
        default=[],
        help="ai-command-result-v1 JSON; repeat for each command",
    )
    parser.add_argument(
        "--report",
        default="artifacts/ai-evidence-bundle.json",
        help="output bundle path",
    )
    parser.add_argument("--artifact-type", default="command-bundle", choices=sorted(ARTIFACT_TYPES))
    parser.add_argument("--reviewer", default=os.getenv("AI_EVIDENCE_REVIEWER", "UNASSIGNED"))
    parser.add_argument("--reviewer-role", default="independent-evidence-reviewer")
    parser.add_argument(
        "--review-status",
        default="PENDING",
        choices=("REVIEWED", "PENDING", "NOT_RUN"),
    )
    parser.add_argument(
        "--policy-version",
        default=os.getenv("AI_RETENTION_POLICY_VERSION", "NOT_SET"),
    )
    parser.add_argument("--provider", default=os.getenv("AI_PROVIDER", "mock"))
    parser.add_argument("--model", default=os.getenv("AI_MODEL") or None)
    parser.add_argument(
        "--ignore-untracked",
        action="store_true",
        help="treat untracked evidence output (artifacts/raw/, the bundle) as non-dirty",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result_files = [Path(path) for path in args.result_files]
    if not result_files:
        result_files = sorted((ROOT / "artifacts" / "raw").glob("*.command-result.json"))
    report = Path(args.report)
    if not report.is_absolute():
        report = ROOT / report
    ignored_paths: list[Path] = []
    if args.ignore_untracked:
        ignored_paths.append(ROOT / "artifacts" / "raw")
        ignored_paths.append(report)
        # Quality artifacts are recomputed by the capture flow and would
        # otherwise mark the tree dirty between capture and build.
        for quality in (ROOT / "artifacts").glob("ai-*-quality.json"):
            ignored_paths.append(quality)
    try:
        payload = build_evidence(
            result_files,
            target=args.target,
            environment=args.environment or _default_environment(args.target),
            reviewer=args.reviewer,
            reviewer_role=args.reviewer_role,
            review_status=args.review_status,
            policy_version=args.policy_version,
            provider=args.provider,
            model=args.model,
            artifact_type=args.artifact_type,
            git_state=get_git_state(ROOT, ignored_paths=ignored_paths),
        )
        relative_label(report, ROOT)
        write_json(report, payload)
    except (EvidenceBuildError, OSError, subprocess.CalledProcessError) as exc:
        print(f"evidence build failed: {exc}", file=sys.stderr)
        return 2

    print(f"result={payload['result']}")
    for blocker in payload["blockers"]:
        print(f"blocker={blocker}")
    print(f"report={relative_label(report, ROOT)}")
    return 0 if payload["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
