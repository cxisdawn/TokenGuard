# TokenGuard v0.2 — Claude Code + DeepSeek API

Keeps v0.1's deterministic repeated-line compression; adds **bounded artifact retrieval** (`tg.py show`, `tg.py grep`) and **local audit** (`tg.py stats`). Python 3.10+; no external Python dependencies. Not an MCP server or full conversational compressor.

## Windows PowerShell quick start

```powershell
cd C:\src\TokenGuard\tokenguard_v02
python -m unittest discover -s tests -v
Set-ExecutionPolicy -Scope Process Bypass -Force
.\install.ps1 -Project 'C:\src\TokenGuard\tokenguard_v01\optimized'
Get-Content 'C:\src\TokenGuard\tokenguard_v01\optimized\.claude\settings.local.json'
```

**Important:** for fair comparison, create a *new* optimized fixture from the original `tg-fixture`; do not reuse a previously fixed `optimized` directory. Run Claude from that new fixture. Existing global `~/.claude/settings.json` DeepSeek credentials are unchanged. Close/restart Claude sessions to apply the Hook.

## Artifact lookup

Compressed output includes the 64-character SHA256 artifact ID. CLI commands:

```powershell
python .\tg.py show <artifact_id> --start 1 --count 30
python .\tg.py grep <artifact_id> "FAIL" --context 2 --limit 20
python .\tg.py stats
```

For Windows commands invoked inside Claude, the Hook prints the full absolute Python script path. Queries are case-insensitive **literal strings**, not regex. `show` limits to 100 lines; `grep` limits to 50 matches and five lines of context on either side.

## Important limitations

- Only **Claude Code Bash PostToolUse** `stdout` (dict representation) is processed. Errors on Hook failure cause it to exit without a replacement; the original tool output remains in use.
- `grep` can read large files into memory; avoid multi-GB artifacts in this prototype.
- The optimizer records **characters**, not accurate model tokens, cache costs, or model task success. No quality preservation guarantees. All artifact files contain raw command output; treat them as sensitive data. Do not commit them.
- Replacing the Bash result only affects information provided to the model, not already-run commands or shell exit codes.
- No background service; no server, network, or API key requirement in TokenGuard itself.
