"""Conservative PostToolUse Bash output compressor, fail-open."""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

PROTECTED = re.compile(r"error|fail|exception|traceback|warning|assert|fatal|denied|critical|^\s*at\s+|^\s*File\s+\"", re.I)


def compress(text: str):
    """Fold only adjacent identical non-critical lines, never truncate."""
    if len(text) < 3000:
        return text
    lines = text.splitlines(keepends=True)
    output = []
    prior = None
    repeats = 0
    def flush():
        nonlocal repeats
        if repeats:
            output.append(f"[TokenGuard: {repeats} identical adjacent non-critical lines hidden]\n")
            repeats = 0
    for line in lines:
        if line == prior and not PROTECTED.search(line):
            repeats += 1
            continue
        flush()
        output.append(line)
        prior = line
    flush()
    result = "".join(output)
    # requires sufficient reduction to offset tokenized metadata
    return result if len(result) < len(text) * .85 else text


def main():
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return
        response = event.get("tool_response")
        if not isinstance(response, dict) or response.get("interrupted") or response.get("isImage"):
            return
        stdout = response.get("stdout")
        if not isinstance(stdout, str):
            return
        out = compress(stdout)
        if out == stdout:
            return
        root = Path(os.environ.get("TOKENGUARD_DATA_DIR", str(Path.home() / ".tokenguard"))) / "artifacts"
        root.mkdir(parents=True, exist_ok=True)
        artifact_id = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
        target = root / (artifact_id + ".txt")
        if not target.exists():
            target.write_text(stdout, encoding="utf-8")
        response_copy = dict(response)
        response_copy["stdout"] = (out + "\n[TokenGuard: full original is available at " + str(target) + "]\n")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": response_copy}}, ensure_ascii=False))
    except Exception as err:
        print(f"TokenGuard skipped (original output unchanged): {err}", file=sys.stderr)

if __name__ == "__main__":
    main()
