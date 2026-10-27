"""Recovery metadata must not rewrite the final original line terminator."""
import io

import pytest

from tokenguard.hook import handle


@pytest.mark.parametrize("mode", ["compact", "indexed"])
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r", ""])
def test_metadata_keeps_summary_line_boundary(tmp_path, monkeypatch, mode, ending):
    monkeypatch.setenv("TOKENGUARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TOKENGUARD_ENABLED", "1")
    monkeypatch.setenv("TOKENGUARD_SOURCE", "manual")
    newline = ending or "\n"
    summary = "=== 120 passed in 0.5s ==="
    raw = "".join(f"test_x.py::test_value[{n}] PASSED [100%]{newline}" for n in range(120))
    raw += summary + ending
    result = handle({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "pytest -v"}, "session_id": "manual-boundaries",
                     "tool_response": {"stdout": raw, "stderr": "", "exit_code": 0}}, pytest_mode=mode)
    assert result is not None
    delivered = result["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
    lines = list(io.StringIO(delivered, newline=""))
    assert summary + newline in lines
    assert sum(line.startswith("[TokenGuard artifact=") for line in lines) == 1
