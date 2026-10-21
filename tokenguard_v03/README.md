# TokenGuard v0.3 — structured log optimizer

Python 3.10+. Local Claude Code `PostToolUse` Hook; supports DeepSeek via the existing Claude Code configuration, without accessing API credentials.

## Changes from v0.2

- Preserve v0.2 adjacent-line compression and artifact `grep/show` plus JSONL audit.
- Detect canonical `pytest -v` passing lines **when Bash command contains `pytest` and output includes pytest summary**; replace groups of PASSED entries with a count while keeping non-PASSED lines, failures and summary text.
- For commands indicating build/compile, fold adjacent identical warnings (retain first occurrence/count). Other warning lines remain protected from generic folding.
- Fail-safe: if text looks like `git diff` patches or contains Python traceback markers, leave stdout unmodified. No file/command modifications.
- Compression requires `original_chars >= 3000` and at least 15% reduction in core text. Hook appendage (artifact retrieval hints) can offset the actual savings; inspect `tg.py stats`.

## Windows quick start (PowerShell 7 recommended)

```powershell
cd C:\src\TokenGuard\tokenguard_v03
python -m unittest discover -s tests -v
Set-ExecutionPolicy -Scope Process Bypass -Force
.\install.ps1 -Project "$PWD\optimized"
Get-Content .\optimized\.claude\settings.local.json
cd .\optimized
claude
```

Use fresh optimized fixture (not already fixed from earlier experiments). To create it if your existing v0.1 fixture lives nearby:

```powershell
Copy-Item C:\src\TokenGuard\tokenguard_v01\tg-fixture C:\src\TokenGuard\tokenguard_v03\optimized -Recurse
```

`install.ps1` installs a project-local hook only. Existing global `~/.claude/settings.json` (including DeepSeek credentials) is not modified. Do not put API keys in this repository.

## Try it

In Claude Code: `请运行 python -m pytest -v，分析失败测试并修复 test_calc.py，最后再运行 pytest。不要删测试。`

For a no-edit demonstration: `请运行 python noisy_check.py 并汇报结果；如果有压缩，仅按需检索原文。不要修改文件。`

Audit:

```powershell
python .\tg.py stats
Get-Content "$env:USERPROFILE\.tokenguard\audit.jsonl" -Tail 5
```

Artifact retrieval (`<id>` is a 64-character SHA256 hash):

```powershell
python .\tg.py grep <id> "FAILED" --context 2 --limit 10
python .\tg.py show <id> --start 1 --count 30
```

## Warnings / scope

- This is **not** MCP, nor full conversational compression; only Claude Code Bash `stdout` is eligible.
- These are *character counts*, not actual billable tokens. `PostToolUse` executes after commands run; it does not alter or undo command side effects.
- No semantic preservation guarantee. For example, collapsing PASSED rows loses their names within the immediate context, even though they can be restored by ID. For tasks requiring individual passing test names, use the original artifact.
- Does not attempt to intelligently compress `git diff` or Tracebacks; conservative pass-through instead.
- Full stdout may contain secrets. Artifacts saved to `~/.tokenguard/artifacts`; protect this folder. Do not share audit logs/raw artifacts publicly.
- Run baseline/optimized trials from identical initial fixtures and fresh agent sessions. Check actual DeepSeek billing and repeat tests to assess quality and cost.
