"""Negative controls for the independent real-output verification oracle."""
from pathlib import Path
import json
import runpy
import subprocess
from unittest.mock import patch

API = runpy.run_path(str(Path(__file__).resolve().parents[1] / "benchmark/pytest_contracts.py"))
CHECK = API["contract_errors"]


def test_oracle_rejects_changed_warning_even_when_marker_stays_visible():
    raw = "  test_x.py:5: UserWarning: critical::contract PASSED\n1 passed in 0.5s\n"
    changed = "critical::contract PASSED\n1 passed in 0.5s\n"
    assert CHECK(raw, changed, ("critical::contract PASSED",)) == [
        "diagnostic line lost or changed: critical::contract PASSED"]


def test_oracle_rejects_lost_duplicate_diagnostics():
    line = "test_x.py::payload PASSED\r\n"
    raw = line * 2 + "2 passed in 0.5s\r\n"
    assert CHECK(raw, line + "2 passed in 0.5s\r\n", ("::payload",))


def test_oracle_rejects_missing_status_and_missing_summary():
    raw = "test_x.py::test_bug FAILED\n1 failed in 0.5s\n"
    errors = CHECK(raw, "", ())
    assert "important status line lost or changed" in errors
    assert "summary lost or changed" in errors


def test_oracle_rejects_invalid_fixture_without_expected_marker():
    assert CHECK("1 passed in 0.5s\n", "1 passed in 0.5s\n", ("absent",)) == [
        "fixture missing marker: absent"]


def test_oracle_accepts_exact_important_lines_with_all_physical_newlines():
    for newline in ("\n", "\r\n", "\r"):
        raw = newline.join(("test_x.py::test_skip SKIPPED (中文)", "1 skipped in 0.5s", ""))
        assert CHECK(raw, raw, ("中文",)) == []


def test_oracle_rejects_summary_embedded_in_a_changed_line():
    assert "summary lost or changed" in CHECK(
        "1 passed in 0.5s\n", "CORRUPTED: 1 passed in 0.5s\n", ())


def test_oracle_rejects_loss_of_unmarked_failure_detail():
    raw = ("test_x.py::test_failure FAILED\n=== FAILURES ===\n"
           "E assert 1 == 2\ntest_x.py:17: AssertionError\n=== 1 failed in 0.5s ===\n")
    delivered = raw.replace("test_x.py:17: AssertionError\n", "")
    assert "complete report section lost or changed" in CHECK(raw, delivered, ("E assert 1 == 2",))


def test_oracle_rejects_report_banner_embedded_in_a_changed_line():
    raw = "=== FAILURES ===\nE assert 1 == 2\n=== 1 failed in 0.5s ===\n"
    assert "complete report section lost or changed" in CHECK(raw, "MALFORMED_PREFIX" + raw, ())


def test_invalid_utf8_fixture_creates_failed_report_and_preserves_bytes(tmp_path):
    compare = API["compare"]
    with patch.dict(compare.__globals__, {
        "cases": lambda: (API["cases"]()[0],),
        "execute": lambda *args, **kwargs: subprocess.CompletedProcess("pytest", 0, b"\xff", b""),
    }):
        folder = compare(tmp_path)
    result = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert result["passed"] is False
    assert result["rows"] == []
    assert result["fixtures"][0]["error"] == "UnicodeDecodeError"
    assert (folder / "fixtures/parameters/stdout.txt").read_bytes() == b"\xff"


def test_fixture_timeout_creates_failed_reports_instead_of_losing_trial(tmp_path):
    compare = API["compare"]
    first = API["cases"]()[0]
    with patch.dict(compare.__globals__, {
        "cases": lambda: (first,),
        "execute": lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("pytest", 60)),
    }):
        folder = compare(tmp_path)
    result = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert result["passed"] is False
    assert result["expected_checks"] == 9
    assert result["rows"] == []
    assert result["fixtures"][0]["error"] == "TimeoutExpired"
    assert (folder / "report.csv").is_file()
    assert "TimeoutExpired" in (folder / "report.md").read_text(encoding="utf-8")


def test_missing_implementation_is_saved_as_failed_evidence(tmp_path):
    folder = API["compare"](tmp_path / "output", tmp_path / "missing-implementation")
    result = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert result["passed"] is False
    assert result["source_unchanged_during_run"] is False
    assert result["input_errors"] == ["initial source unavailable: FileNotFoundError"]
    assert result["rows"] == []
