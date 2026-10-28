"""Only CR/LF delimit log records; embedded controls are diagnostic content."""
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys

import pytest

from tokenguard.core import compress


# str.splitlines() also recognizes these, unlike artifact recovery and the
# CR/LF log protocol. None may turn an inline fragment into a duplicate row.
INLINE_SEPARATORS = ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


@pytest.mark.parametrize("separator", INLINE_SEPARATORS)
@pytest.mark.parametrize("command", ["python diagnostics.py", "cmake --build out"])
def test_single_physical_record_is_not_fragment_compressed(separator, command):
    raw = ("phase=compile" + separator) * 400 + "detail=missing configuration\n"
    optimized, _ = compress(raw, command=command)
    assert optimized == raw


@pytest.mark.parametrize("separator", INLINE_SEPARATORS)
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r", ""])
def test_whole_diagnostic_survives_compression_of_other_records(separator, ending):
    newline = ending or "\n"
    diagnostic = ("phase=compile" + separator) * 80 + "fatal: missing configuration" + ending
    raw = ("compile step complete" + newline) * 400 + diagnostic
    optimized, strategy = compress(raw, command="cmake --build out")
    assert strategy == "adjacent_duplicates"
    assert len(optimized) < len(raw) * 0.85
    assert optimized.endswith(diagnostic)


@pytest.mark.parametrize("separator", ["\x85", "\u2028", "\u2029"])
def test_single_json_log_remains_parseable_and_equal(separator):
    # Valid JSON strings may contain literal Unicode separators. C0 controls
    # would instead be escaped by the real JSON producer.
    payload = {"diagnostic": ("phase=compile" + separator) * 400, "result": "pending"}
    raw = json.dumps(payload, ensure_ascii=False) + "\n"
    optimized, _ = compress(raw, command="python diagnostics.py")
    assert json.loads(optimized) == payload
    assert optimized == raw


@pytest.mark.parametrize("separator", INLINE_SEPARATORS)
def test_search_with_inline_separators_remains_ambiguous(separator):
    raw = "".join(f"project/module_{n}.py{separator}" for n in range(400)) + "\n"
    optimized, strategy = compress(raw, command="rg --files")
    assert optimized == raw
    assert strategy == "ambiguous_search"


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_actual_duplicate_records_with_inline_content_still_compress(ending):
    raw = ("compile phase\u2028status=pending" + ending) * 400
    optimized, strategy = compress(raw, command="cmake --build out")
    assert strategy == "adjacent_duplicates"
    assert optimized.startswith("compile phase\u2028status=pending" + ending)
    assert "399 identical adjacent non-critical lines hidden" in optimized


def test_seeded_mixed_record_round_trip():
    """Independent expansion oracle checks lossless duplicate counts and bytes."""
    rng = random.Random(20261028)
    marker = re.compile(r"([^\r\n]*(?:\r\n|\r|\n))"
                        r"\[TokenGuard: (\d+) identical adjacent non-critical lines hidden\]\n")
    for case in range(40):
        records = []
        critical = []
        for number in range(20):
            embedded = rng.choice(INLINE_SEPARATORS)
            newline = rng.choice(["\n", "\r\n", "\r"])
            severity = "fatal " if number % 5 == 0 else ""
            record = f"{severity}diagnostic {case}/{number}: " + ("payload" + embedded) * rng.randint(1, 8) + newline
            copies = rng.randint(5, 15)
            records.append(record * copies)
            if severity:
                critical.append(record * copies)
        raw = "".join(records) + "unrepeated tail without terminator"
        optimized, strategy = compress(raw, command="cmake --build out")
        assert strategy == "adjacent_duplicates", case
        assert all(record in optimized for record in critical), case
        expanded = marker.sub(lambda match: match[1] * (int(match[2]) + 1), optimized)
        assert expanded == raw, case


@pytest.mark.parametrize("command,prefix,diagnostic,keyword", [
    ("cmake --build out", "compile step complete\r\n",
     "diagnostic=" + "phase\u2028" * 400 + "fatal: configuration missing\r\n", "configuration"),
    ("python -m pip install example", "  " + "\u2501" * 24 + " 1/2 MB 5 MB/s\r\n",
     "  " + "\u2501" * 24 + " 1/2 MB 5 MB/s\u2028ERROR: checksum mismatch\r\n", "checksum"),
], ids=["build", "pip"])
def test_hook_subprocess_preserves_record_and_selectively_recovers(tmp_path, command, prefix, diagnostic, keyword):
    root = Path(__file__).resolve().parents[1]
    raw = prefix * 400 + diagnostic
    response = {"stdout": raw, "stderr": "compiler warning\r\n", "exit_code": 1, "extra": {"unchanged": True}}
    event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
             "session_id": "manual-physical-records", "tool_input": {"command": command},
             "tool_response": response}
    env = dict(os.environ, TOKENGUARD_DATA_DIR=str(tmp_path), TOKENGUARD_ENABLED="1")
    hook = subprocess.run([sys.executable, "-X", "utf8", str(root / "tokenguard" / "hook.py"),
                           "--tokenguard-v04", "--source", "manual"],
                          input=json.dumps(event).encode("utf-8"), capture_output=True, env=env, timeout=30)
    assert hook.returncode == 0, hook.stderr
    changed = json.loads(hook.stdout)["hookSpecificOutput"]["updatedToolOutput"]
    assert diagnostic in changed["stdout"]
    assert len(changed["stdout"]) < len(raw) * 0.85
    assert {k: v for k, v in changed.items() if k != "stdout"} == {k: v for k, v in response.items() if k != "stdout"}
    with (tmp_path / "audit-v04.jsonl").open(encoding="utf-8") as audit:
        row = next(item for line in audit if (item := json.loads(line))["event"] == "compression")
    artifact = tmp_path / "artifacts" / (row["artifact_id"] + ".txt")
    assert artifact.read_bytes() == raw.encode("utf-8")
    recovered = subprocess.run([sys.executable, "-X", "utf8", str(root / "tg.py"), "show",
                                row["artifact_id"], "--start", "401", "--count", "1",
                                "--compression-id", row["compression_id"]],
                               capture_output=True, env=env, timeout=30)
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout == diagnostic.encode("utf-8")
    matched = subprocess.run([sys.executable, "-X", "utf8", str(root / "tg.py"), "grep",
                              row["artifact_id"], keyword, "--context", "0", "--compression-id", row["compression_id"]],
                             capture_output=True, env=env, timeout=30)
    assert matched.returncode == 0, matched.stderr
    assert matched.stdout == ("401: " + diagnostic).encode("utf-8")
