# Changelog

## 2026-10-28 — 恢复预算与完成状态

- 完整结果恰好装满返回预算时，识别请求结束行或 EOF，避免虚假截断及多余续读。
- 未搜索尾部保持截断状态；有界预读计入扫描和完整性核验，不把未知结果当作完成。
- 拒绝只能容纳部分状态提示的预算：CLI 返回 2、stdout 为空、不写成功恢复审计。
- 新增精确预算、Unicode/混合换行、独立行窗口 oracle、扫描边界、上下文和 CLI 回归；无新增模型或账单测量。
- 首轮 CI 暴露旧块校验测试混用 stat/fstat ctime；确定性复现后修正缓存身份模拟，保留块拒绝断言，最终本地 741 项通过。

## 2026-10-28 — Artifact 完整性与并发保存

- 修复稳定改写的 Artifact 仍可能被当作原文恢复的问题；首次流式 SHA256，后续按 64 KiB 块核验，损坏内容拒绝返回并保留。
- 缓存不可写或超预算时完整核验；全量构建与块重读分别计量，旧审计未知测量保持 unavailable。
- 以不覆盖目标的原子硬链接发布替换覆盖写入；竞争者完整核验已发布内容，元信息漂移限次重验。
- 拒绝 Artifact / 定位索引的 Windows junction 和链接祖先，保留 8.3 兼容并规范化扩展路径前缀。
- 恢复、索引及审计严格验证计量与游标；CLI / Mock 报告显示完整性读取开销。
- 未新增模型调用、能力或费用节省结论。见 [完整性与复现](tokenguard_v04/ARTIFACT_INTEGRITY.md)。

## v0.4

- Canonicalize snapshot comparison roots after checking real links/reparse attributes; preserve valid Windows 8.3 aliases and reject linked roots.

- Bind task validation to the files actually tested; recheck current candidate patches and archived grades before certifying report outcomes.
- Reconcile observed streaming request identities with final usage; retain unfinished requests and prevent incomplete coverage from becoming task totals.
- Preserve and revalidate session/request billing bindings; mark legacy unbound costs unavailable.
- Export complete planned Mock/baseline study denominators after execution failures; distinguish unknown evidence, measured failure and eligible comparisons.
- Revalidate cached audit measurements and keep missing counts unavailable with explicit coverage.

- Derive missing legacy recovery context from a unique valid compression ID in memory, keeping scoped counts consistent without rewriting history or overriding explicit conflicts.

- Validate audit rows before arithmetic or attribution; retain corrupt history, expose incomplete/unknown measurements and bound CLI record output.
- Serialize appenders with timed OS locks, preserve truncated tails with a new boundary, and deduplicate only explicit event identities.
- Separate prepared compression candidates from successful stdout writes and unknown host acceptance; handle strict JSON, short writes and broken pipes.
- Preserve independent code grades while stopping subsequent trials after incomplete audit evidence; revalidate historical audit instead of trusting cached completeness.

- Replace duplicate managed v0.4 hooks when reinstalling from a different checkout; preserve byte-identical configuration backups and unrelated settings.
- Match complete installer command serialization and arguments before migrating/uninstalling; preserve unknown arguments, shell expressions and other hook types/versions.
- Add 19 installation migration and command-ownership regression cases, including Windows/POSIX paths and missing old checkout files.

- Preserve pytest warning, captured-output and plugin report sections; reject live-output ambiguity and parse parameter IDs containing `::` conservatively.
- Preserve CR/LF/CRLF report terminators when appending Hook recovery metadata.
- Add a 108-check local contract benchmark using actual pytest fixtures, Hook/recovery subprocesses, immutable source hashes and negative-control tests; no model calls.
- Recover long Unicode lines with paired line/character cursors and sparse intra-line checkpoints; retain complete grep matches and report omitted context explicitly.
- Revalidate scorecard evidence instead of trusting cached success fields; distinguish invalid/missing evidence from measured failure and preserve planned denominators.
- Document the negative controlled coding pilot prominently; software checks do not establish coding-quality or billing savings.

- Read-only multi-experiment scorecards with conservative cohorts, all planned pair outcomes and a separate compression-evidence subset.
- Descriptive latency, recovery and unverified CLI usage coverage; unavailable provider billing never replaced by a character/token proxy.
- Reject duplicate experiments/session reuse and observed input changes; preserve existing reports and export fresh Markdown/JSON/CSV.
- Recompute native Bash/audit metadata matches during analysis and reject malformed boolean grading fields.

- Preserve trial results on local grading, audit, source, receipt, process and stream failures; stop subsequent sessions without automatic retries.
- Recheck bound evidence, runtime hashes and pristine source before each session; retain blocked trials in report denominators.
- Tolerate malformed optional CLI usage/model metadata, with unavailable values kept separate from measured zeroes.
- Fault-tolerant analysis with explicit evidence gaps and manual guide echoes excluded from native Hook matches.
- Ten full-runner regressions using an offline child CLI and actual local pytest/Hook processes; no provider calls or Docker dependency.

- Actual visible-test receipts bind source/log hashes, exit status, complete test counts and matching Bash responses; command mentions alone do not count.
- Separate agent-driven/native Hook and opt-in controller-seeded initial-log protocols, with explicit per-pair missing-evidence reasons.
- Hash-bound preflight/input verification, byte-identical initial artifacts, and separately attributed manual/native compression and recovery counts.
- Zero-model-call protocol demo using real local pytest and Hook processes with disclosed deterministic mock edits/transcripts.
- Windows short-path aliases accepted through actual link/reparse checks, with real 8.3-alias receipt regression coverage.

- Opt-in SWE-bench Verified Pylint quality pilot using official test metrics and independently graded candidates.
- Three authorized real A/B rounds retained including all max-turn exits; patch correctness and agent completion are reported separately.
- Compression exposure and exact host-transcript metadata matches, with unverified CLI usage separated from provider billing.
- Thirteen repeatable local hook/recovery examples, including actual 504-test pytest runs before/after a copied-fixture fix.
- UTF-8 recovery CLI output without Windows newline translation; subprocess tests cover exact bytes and audit character counts.
- Explicit disabled-hook and no-hook baselines with separate usage cohorts and backwards-compatible task records.
- Measured local Hook process counts/time, stdout sizes and repeated offline baseline study reports.
- Short validation temporary paths and flatter study output for Windows path limits.
- Local-only task preflight with help/version checks, owned Hook probes and expected initial-test validation.
- Direct native binary discovery beside Windows npm Claude shims; launcher/version failures leave sessions unused.
- Paired project task runner with explicit snapshots, fresh sessions, AB/BA rounds, dry-run defaults and paid-session gates.
- Independent pristine-test validation, protected-file checks, timeouts and bounded output reads.
- Local bug-fix fixture agent, Claude process-contract tests and explicitly attached per-trial usage reports.
- Conservative structured stdout strategies and fail-open Claude Code adapter.
- Net character saving checks after recovery metadata.
- Atomic, content-addressed artifacts; bounded selective recovery and sparse line indexes.
- Session/experiment audit and isolated installed-hook diagnostics.
- Reproducible offline A/B fixtures with independent processes, snapshot and test checks.
- Offline DeepSeek/Anthropic usage normalization, independent billing evidence and paired quality-aware reports.
- Compatible Windows stat/fstat comparisons while retaining mutation and replacement checks.
- Source-only repository publication and cross-platform CI configuration.

## Historical versions

- v0.3: pytest summaries, traceback/diff protection and build warning handling.
- v0.2: artifact recovery and JSONL audit.
- v0.1: adjacent duplicate Bash log compression via Claude Code PostToolUse.
