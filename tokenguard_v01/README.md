# TokenGuard v0.1 (experimental)

This is a **research prototype**. It compresses only adjacent identical non-critical lines in successful Bash outputs larger than 3,000 characters. Other content passes through. It does not prove quality equivalence or savings of billable tokens.

## Windows PowerShell

1. `python -m unittest discover -s tests -v` from the extracted folder.
2. `powershell -ExecutionPolicy Bypass -File .\make_fixture.ps1 -Target .\tg-fixture`
3. For a **new fixed benchmark**, keep the original fixture untouched and make independent copies for baseline and optimized run.
4. `powershell -ExecutionPolicy Bypass -File .\install.ps1 -Project C:\path\to\your\optimized-copy`
5. `cd C:\path\to\your\optimized-copy; claude`

Keep your existing user-level DeepSeek settings intact. The installer uses project-level `.claude/settings.local.json` and backs up existing local settings.

When the Hook successfully compresses a Bash stdout, it stores original output locally under `%USERPROFILE%\.tokenguard\artifacts` and puts the original path in the result. The hook makes no model/API calls. **Do not install this Hook globally until tested.**

On older Windows PowerShell 5.1, `ConvertFrom-Json -AsHashtable` may be unavailable; run installer in PowerShell 7 (`pwsh`) or add settings manually.

### Test plan
Run equal prompt, equal model, equal fixture on two independent directories and fresh sessions. Record actual DeepSeek billing, elapsed time, cache read, un-cached input, output, passes/failures. Ask Agent to run `python noisy_check.py`, then `python -m pytest -v`, fix the failing code, and rerun tests. The repeated HEARTBEAT output exercises the safe compressor. The sample fixture has a deliberate bug; avoid changing hidden tests or deleting tests to 'fix' the task.

## Security
Treat Bash stdout as sensitive. Artifact storage uses local disk without encryption. Do not run against sensitive repositories without implementing permissions, TTL and redaction. No API keys are required for TokenGuard itself.
