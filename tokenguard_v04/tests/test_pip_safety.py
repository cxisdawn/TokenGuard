"""A progress-looking prefix must never erase diagnostic content."""
import pytest

from tokenguard.core import compress


BAR = "  " + "\u2501" * 24
PROGRESS = BAR + " 1/2 MB 5 MB/s"


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r", ""])
@pytest.mark.parametrize("suffix", [
    " ERROR: checksum mismatch", " WARNING: resolver conflict",
    " FAILED: dependency conflict", " detail=unrecognized resolver state",
    "\x85ERROR: checksum mismatch", "\u2028detail: unknown",
    " \x1b[31mERROR: checksum mismatch\x1b[0m",
])
def test_progress_prefix_with_diagnostic_tail_is_kept(suffix, ending):
    diagnostic = PROGRESS + suffix + ending
    raw = "Collecting example==1.0\n" + (PROGRESS + "\n") * 400 + diagnostic
    optimized, strategy = compress(raw, command="python -m pip install example")
    assert strategy == "pip_progress"
    assert len(optimized) < len(raw) * 0.85
    assert optimized.endswith(diagnostic)


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("progress", [
    PROGRESS,
    BAR + " 1.0/2.0 MB 5.1 MB/s eta 0:00:01",
    BAR + " 100/200 bytes 50 bytes/s",
    BAR + " 1.0 GB 10.5 MB/s 0:00:01",
    # Observed from installed pip's Rich progress renderer, fed local bytes;
    # '?' denotes unavailable speed. No package installation or network call.
    "   ---------------------------------------- 2.0/2.0 MB ? eta 0:00:00",
    BAR + " 1.0/2.0 MB ? eta -:--:--",
    "\x1b[32m" + PROGRESS + "\x1b[0m",
])
def test_known_whole_progress_records_can_be_aggregated(progress, ending):
    footer = "Successfully installed example-1.0" + ending
    raw = "Collecting example==1.0" + ending + (progress + ending) * 400 + footer
    optimized, strategy = compress(raw, command="python -m pip install example")
    assert strategy == "pip_progress"
    assert "400 pip progress lines hidden" in optimized
    assert optimized.endswith(footer)


@pytest.mark.parametrize("malformed", [
    BAR + " unknown download data 5 MB/s",
    BAR + " 1/2 MB arbitrary notes 5 MB/s",
    BAR + " 1/2 MB 5 MB/s eta unknown diagnostic",
    BAR + " 1/2 MB 5 MB/s\u2028" + PROGRESS,
])
def test_unknown_progress_shaped_records_are_not_deleted(malformed):
    raw = (malformed + "\n") * 400
    optimized, _ = compress(raw, command="python -m pip install example")
    assert optimized == raw
