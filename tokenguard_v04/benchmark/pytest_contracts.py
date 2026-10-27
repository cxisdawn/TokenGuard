"""Real local pytest, Hook and recovery contracts; no model or billing claims."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

BASE = Path(__file__).resolve().parents[1]
MODES = ("off", "compact", "indexed")
PREFIX = '''import pytest
import warnings
import sys

@pytest.mark.parametrize("case", range(120), ids=lambda value: f"case::{value}")
def test_parameter(case):
    assert case >= 0

'''


@dataclass(frozen=True)
class Case:
    name: str
    source: str
    expected_exit: int
    markers: tuple[str, ...] = ()
    conftest: str = ""
    args: tuple[str, ...] = ()
    ini_addopts: str = ""
    passthrough: bool = False


def cases() -> tuple[Case, ...]:
    regular = (
        Case("parameters", PREFIX, 0),
        Case("warning", PREFIX + '''def test_warning():
    warnings.warn("WARN::contract PASSED", UserWarning)
''', 0, ("WARN::contract PASSED",)),
        Case("failure", PREFIX + '''def test_failure():
    print("CAPTURED::stdout PASSED")
    print("CAPTURED::stderr PASSED", file=sys.stderr)
    assert 1 == 2, "ASSERT::detail PASSED"
''', 1, ("CAPTURED::stdout PASSED", "CAPTURED::stderr PASSED", "ASSERT::detail PASSED")),
        Case("setup_error", PREFIX + '''@pytest.fixture
def broken():
    raise ValueError("SETUP::detail PASSED")

def test_setup(broken):
    pass
''', 1, ("SETUP::detail PASSED",)),
        Case("other_statuses", PREFIX + '''@pytest.mark.skip(reason="SKIP::reason PASSED")
def test_skip():
    pass

@pytest.mark.xfail(reason="XFAIL::reason PASSED")
def test_xfail():
    assert False

@pytest.mark.xfail(reason="XPASS::reason PASSED")
def test_xpass():
    pass
''', 0, ("SKIP::reason PASSED", "XFAIL::reason PASSED", "XPASS::reason PASSED")),
        Case("unknown_section", PREFIX, 0, ("test_contract.py::unknown PASSED",),
             conftest='''def pytest_terminal_summary(terminalreporter):
    terminalreporter.section("external plugin diagnostics")
    terminalreporter.write_line("test_contract.py::unknown PASSED")
'''),
        Case("capture_success", PREFIX + '''def test_capture():
    print("test_contract.py::captured PASSED")
''', 0, ("test_contract.py::captured PASSED",), args=("-rP",)),
    )
    live = PREFIX + '''def test_live():
    print("\\ntest_contract.py::diagnostic_payload PASSED")
'''
    return regular + tuple(
        Case(name, live, 0, ("test_contract.py::diagnostic_payload PASSED",),
             args=args, ini_addopts=ini, passthrough=True)
        for name, args, ini in (
            ("uncaptured_s", ("-s",), ""),
            ("uncaptured_no", ("--capture=no",), ""),
            ("uncaptured_tee", ("--capture=tee-sys",), ""),
            ("configured_s", (), "-s"),
            ("configured_tee", (), "--capture=tee-sys"),
        ))


def environment(data: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("TOKENGUARD_") or key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
            env.pop(key)
    env.update(TOKENGUARD_DATA_DIR=str(data), TOKENGUARD_ENABLED="1",
               TOKENGUARD_SOURCE="manual", TOKENGUARD_DIAGNOSTICS="1",
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONIOENCODING="utf-8")
    return env


def execute(argv: list[str], cwd: Path, env: dict[str, str],
            stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=cwd, env=env, timeout=60)


def cli(arguments: list[str], cwd: Path, env: dict[str, str], implementation: Path) -> bytes:
    result = execute([sys.executable, "-B", "-X", "utf8", str(implementation / "tg.py"), *arguments], cwd, env)
    if result.returncode:
        raise RuntimeError(f"recovery/statistics CLI exit code: {result.returncode}")
    return result.stdout


def physical_lines(text: str) -> list[str]:
    return list(io.StringIO(text, newline=""))


def contract_errors(raw: str, delivered: str, markers: tuple[str, ...]) -> list[str]:
    """Check complete diagnostic lines with multiplicity, independent of parser."""
    errors = []
    raw_lines = physical_lines(raw)
    delivered_sequence = physical_lines(delivered)
    delivered_lines = Counter(delivered_sequence)
    for marker in markers:
        matching = Counter(line for line in raw_lines if marker in line)
        if not matching:
            errors.append(f"fixture missing marker: {marker}")
        elif any(delivered_lines[line] < count for line, count in matching.items()):
            errors.append(f"diagnostic line lost or changed: {marker}")
    statuses = Counter(line for line in raw_lines if re.search(
        r"::.*\s(?:FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)", line))
    if any(delivered_lines[line] < count for line, count in statuses.items()):
        errors.append("important status line lost or changed")
    summaries = Counter(line for line in raw_lines if re.search(
        r"\b\d+ (?:passed|failed|errors?|skipped).* in \d+", line))
    if not summaries:
        errors.append("fixture missing final summary")
    elif any(delivered_lines[line] < count for line, count in summaries.items()):
        errors.append("summary lost or changed")
    # Preserve everything from the first report banner onward, including
    # unanticipated traceback locations, explanations, warnings and plugin text.
    for index, line in enumerate(raw_lines):
        if line.startswith("===") and "test session starts" not in line:
            report_lines = raw_lines[index:]
            if not any(delivered_sequence[start:start + len(report_lines)] == report_lines
                       for start, candidate in enumerate(delivered_sequence) if candidate == line):
                errors.append("complete report section lost or changed")
            break
    return errors


def check_hook(case: Case, raw: str, stderr: str, mode: str, folder: Path,
               experiment: str, implementation: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "original.txt").write_bytes(raw.encode("utf-8"))
    env = environment(folder / "data")
    env["TOKENGUARD_EXPERIMENT_ID"] = experiment
    session = "manual-" + uuid.uuid4().hex
    command = "python -m pytest -vv -ra " + " ".join(case.args)
    response = {"stdout": raw, "stderr": stderr, "exit_code": case.expected_exit,
                "interrupted": False, "extra": {"preserve": [1, None, "中文"]}}
    event = {"hook_event_name": "PostToolUse", "session_id": session,
             "tool_use_id": "local-test", "tool_name": "Bash",
             "tool_input": {"command": command}, "tool_response": response}
    process = execute([sys.executable, "-B", "-X", "utf8", str(implementation / "tokenguard/hook.py"),
                       "--source", "manual", "--pytest-mode", mode], folder, env,
                      json.dumps(event).encode("utf-8"))
    errors = []
    delivered = raw
    compressed = bool(process.stdout.strip())
    if process.returncode or process.stderr:
        errors.append("hook exit/stderr indicates an error")
    if compressed:
        replacement = json.loads(process.stdout)["hookSpecificOutput"]
        if replacement.get("hookEventName") != "PostToolUse":
            errors.append("replacement event changed")
        changed = replacement["updatedToolOutput"]
        if {k: v for k, v in changed.items() if k != "stdout"} != {
                k: v for k, v in response.items() if k != "stdout"}:
            errors.append("response structure changed")
        delivered = changed["stdout"]
    (folder / "delivered.txt").write_bytes(delivered.encode("utf-8"))
    errors.extend(contract_errors(raw, delivered, case.markers))
    if mode == "off" and (compressed or delivered != raw):
        errors.append("off mode changed output")
    if case.passthrough and (compressed or delivered != raw):
        errors.append("ambiguous live output must pass through unchanged")
    stats = json.loads(cli(["stats", "--session", session, "--json"], folder, env, implementation))
    expected_recoveries = returned = 0
    artifact_exact = None
    if compressed:
        if stats["compressions"] != 1 or len(delivered) >= len(raw) * .85:
            errors.append("net saving or compression audit violated")
        record = stats["records"][0]
        identifier, cid = record["artifact_id"], record["compression_id"]
        artifact_exact = (folder / "data/artifacts" / (identifier + ".txt")).read_bytes() == raw.encode("utf-8")
        if not artifact_exact:
            errors.append("artifact bytes differ")
        if record.get("source") != "manual":
            errors.append("manual check mislabeled as live integration")
        # Retrieve one omitted parameter line, using both public CLI interfaces.
        number, original = next((n, line) for n, line in enumerate(physical_lines(raw), 1)
                                if "::test_parameter[case::80] PASSED" in line)
        owner = ["--compression-id", cid]
        shown = cli(["show", identifier, "--start", str(number), "--count", "1", *owner], folder, env, implementation)
        found = cli(["grep", identifier, "::test_parameter[case::80] PASSED",
                     "--context", "0", "--limit", "1", *owner], folder, env, implementation)
        if shown != original.encode("utf-8"):
            errors.append("line recovery differs from original bytes")
        expected_match = str(number) + ": " + original
        continuation = (f'\n[TokenGuard: retrieval limit reached; next_start_line={number + 1}; '
                        'next_start_char=0]\n')
        # Older implementations omit this marker. In either case the complete
        # original line must be exact, and all delivered metadata is counted.
        if found not in (expected_match.encode("utf-8"),
                         (expected_match + continuation).encode("utf-8")):
            errors.append("keyword recovery differs from original line")
        expected_recoveries = 2
        returned = len(shown.decode("utf-8")) + len(found.decode("utf-8"))
    final = json.loads(cli(["stats", "--session", session, "--json"], folder, env, implementation))
    if final["compressions"] != int(compressed):
        errors.append("compression audit differs from emitted replacement")
    if final["recovery_count"] != expected_recoveries or final["returned_chars"] != returned:
        errors.append("recovery audit counters differ from actual CLI output")
    diagnostics = json.loads(cli(["diagnostics", "--session", session], folder, env, implementation))
    invocations = diagnostics["invocations"]
    if len(invocations) != 1 or invocations[0].get("source") != "manual":
        errors.append("manual invocation diagnostic count or origin differs")
    status = invocations[-1]["status"]
    if not compressed and not case.passthrough and mode != "off":
        errors.append("expected compression did not occur")
    return {"case": case.name, "mode": mode, "status": status, "passed": not errors,
            "compression_count": final["compressions"],
            "original_chars": len(raw), "delivered_chars": len(delivered),
            "artifact_byte_identical": artifact_exact, "recovery_count": final["recovery_count"],
            "expected_recovery_count": expected_recoveries,
            "returned_chars": final["returned_chars"], "cli_returned_chars": returned,
            "scanned_chars": final["scanned_chars"],
            "index_build_bytes": final["index_build_bytes"], "errors": errors}


def compare(output: Path, implementation: Path = BASE) -> Path:
    implementation = implementation.resolve()
    folder = output.resolve() / ("pytest-contracts-" + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    sources = ["tokenguard/core.py", "tokenguard/pytest_index.py", "tokenguard/hook.py",
               "tokenguard/storage.py", "tokenguard/line_index.py", "tokenguard/audit.py",
               "tokenguard/tokens.py", "tokenguard/__init__.py", "tg.py"]
    input_errors = []
    try:
        hashes = {name: hashlib.sha256((implementation / name).read_bytes()).hexdigest() for name in sources}
        harness_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError as exc:
        hashes, harness_hash = {}, None
        input_errors.append("initial source unavailable: " + type(exc).__name__)
    rows = []
    fixture_results = []
    for case in cases() if not input_errors else ():
        workspace = folder / "fixtures" / case.name
        workspace.mkdir(parents=True)
        (workspace / "test_contract.py").write_text(case.source, encoding="utf-8")
        (workspace / "pytest.ini").write_text("[pytest]\naddopts = " + case.ini_addopts + "\n", encoding="utf-8")
        if case.conftest:
            (workspace / "conftest.py").write_text(case.conftest, encoding="utf-8")
        try:
            proc = execute([sys.executable, "-X", "utf8", "-m", "pytest", "-vv", "-ra",
                            "--color=no", "-p", "no:cacheprovider", "-c", "pytest.ini", *case.args],
                           workspace, environment(workspace / "data"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            fixture_results.append({"case": case.name, "valid": False,
                                    "error": type(exc).__name__})
            continue
        (workspace / "stdout.txt").write_bytes(proc.stdout)
        (workspace / "stderr.txt").write_bytes(proc.stderr)
        valid_fixture = proc.returncode == case.expected_exit
        fixture_results.append({"case": case.name, "exit_code": proc.returncode,
                                "expected_exit_code": case.expected_exit, "valid": valid_fixture,
                                "stdout_sha256": hashlib.sha256(proc.stdout).hexdigest()})
        if not valid_fixture:
            continue
        try:
            raw = proc.stdout.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            stderr = proc.stderr.decode("utf-8")
        except UnicodeDecodeError as exc:
            fixture_results[-1].update(valid=False, error=type(exc).__name__)
            continue
        for ending, newline in (("lf", "\n"), ("crlf", "\r\n"), ("cr", "\r")):
            for mode in MODES:
                target = folder / "checks" / case.name / ending / mode
                try:
                    row = check_hook(case, raw.replace("\n", newline), stderr,
                                     mode, target, folder.name, implementation)
                except Exception as exc:
                    row = {"case": case.name, "mode": mode, "passed": False,
                           "errors": ["check exception: " + type(exc).__name__ + ": " + str(exc)]}
                rows.append({**row, "newline": ending})
    try:
        source_unchanged = (hashes == {name: hashlib.sha256((implementation / name).read_bytes()).hexdigest()
                                     for name in sources} and
                            harness_hash == hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    except OSError:
        source_unchanged = False
    try:
        pytest_version = version("pytest")
    except PackageNotFoundError:
        pytest_version = "unavailable"
    expected = len(cases()) * len(MODES) * 3
    result = {"schema": "tokenguard.pytest-contracts/v1", "expected_checks": expected,
              "timestamp": datetime.now(timezone.utc).isoformat(),
              "python_version": sys.version.split()[0], "pytest_version": pytest_version,
              "passed": source_unchanged and len(rows) == expected and all(row["passed"] for row in rows),
              "source_unchanged_during_run": source_unchanged, "source_sha256": hashes,
              "verification_source_sha256": harness_hash,
              "input_errors": input_errors,
              "model_api_calls": 0, "coding_ability_evaluated": False, "live_claude_integration": False,
              "actual_provider_usage": "unavailable", "actual_billed_cost": "unavailable",
              "fixture_source": "actual local pytest on generated source; newline variants are transformed",
              "fixtures": fixture_results, "rows": rows}
    (folder / "report.json").write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    fields = sorted(set().union(*(row.keys() for row in rows))) if rows else ["case", "passed"]
    with (folder / "report.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# Actual pytest output contracts", "",
             f"Passed: {result['passed']}; checks: {len(rows)}/{expected}; unchanged source: {source_unchanged}.",
             "No model calls. Local software contracts only; coding ability and live integration are untested.",
             "Each compressed result intentionally exercises both show and grep; this is not a recovery-efficiency benchmark.", "",
             "| Case | Newline | Mode | Passed | Status/errors |", "| --- | --- | --- | --- | --- |"]
    for row in rows:
        detail = row.get("status", "") + " " + "; ".join(row["errors"])
        lines.append(f"| {row['case']} | {row['newline']} | {row['mode']} | {row['passed']} | {detail.replace('|', '/')} |")
    for fixture in fixture_results:
        if not fixture["valid"]:
            lines.append(f"\nFixture {fixture['case']} failed before Hook checks: {fixture.get('error', 'unexpected exit code')}.")
    lines.extend("\nInput error: " + message for message in input_errors)
    lines += ["", "Actual provider usage and billed cost: unavailable.", ""]
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BASE / "reports/contracts")
    parser.add_argument("--implementation", type=Path, default=BASE,
                        help="v0.4 source directory to verify; uses isolated generated data")
    args = parser.parse_args()
    folder = compare(args.output, args.implementation)
    result = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    print(json.dumps({"report": str(folder), "passed": result["passed"],
                      "checks": len(result["rows"]), "expected_checks": result["expected_checks"]}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
