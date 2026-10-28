# Changelog

## 2026-10-28 — 彩色严重记录与关闭/回退验证

- 修复 ANSI CSI 颜色码插入 ERROR、fatal 等词内部时，严重重复记录被错误折叠为“非关键重复行”的问题；只规范化识别文本，原始颜色、Unicode、换行和 Artifact 字节不变。
- 普通重复警告的策略标签使用同一去码后的文本；没有改变全局关闭值 `TOKENGUARD_ENABLED=0`。
- 63 项颜色反例全部先失败；新增 35 项真实手动 Hook 子进程检查，覆盖七类日志的启用、关闭及三类存储故障。旧文件哨兵不变，故障不请求替换，错误信息不泄露正文。
- 扩展构建/pip 独立评测到 12 类、68 项；明确标记真实编译错误记录的合成配色/重复，新增恢复旧识别缺陷的负对照。不是原生编译器配色或真实 Agent 能力实验。
- 新增回归合计 99 项；完整回归 1346 passed、1 skipped、406 subtests passed。独立评测新版 68/68、旧发布 bd600af 为 62/68；8 次相同字节回放核对严重记录和 Artifact。范围与限制见 [输出验证](tokenguard_v04/BUILD_PIP_CONTRACTS.md)。

## 2026-10-28 — 文件搜索诊断保护

- 复现并修复路径形状的提示/内容位置行被前 20 条采样隐藏的问题；实际 PowerShell + rg 输出保存旧版失败证据。
- 独立模块收紧文件名列举命令与物理记录识别；复合/未知命令、NUL、提示前缀及非驱动器冒号透传。歧义搜索不回落到通用重复压缩。
- 新增真实 rg 的 11 类、62 项输出/Artifact/恢复/审计检查，绑定真实文件清单、工具二进制、源码和复用的两份验证器 Hash；加入 CI。
- 负对照拒绝丢失提示与错误前缀采样；缺工具和无效 Fixture 保留失败分母。未新增模型调用，范围见 [搜索保护](tokenguard_v04/SEARCH_SAFETY.md)。
- 新增 97 项回归，完整结果 1247 passed、1 skipped、406 subtests passed。搜索评测当前 62/62、历史 236779c 为 53/62；九项失败均涉及三类诊断的启用/换行变体，不是九个独立任务。

## 2026-10-28 — 构建与 pip 实际本地输出验证

- 新增 10 类、56 项离线输出检查，区分真实 compileall、临时 target 的 pip 安装/错误、真实 pip renderer 和明确标记的混合日志；不执行下载或模型调用。
- 核对编译/安装产物、响应结构、诊断序列、重复记录可展开性、原文字节、选择性恢复及审计计数；输出 JSON/CSV/Markdown，失败保留完整分母。
- 新增评测器负对照，主动注入前缀删诊断、清空 stderr 和损坏审计，防止仅验证成功分支。CI 四组矩阵加入独立评测命令。
- 将 Python compileall/py_compile 命令审计分类修正为 build；重复压缩行为不变。范围与复现见 [本地构建/pip 验证](tokenguard_v04/BUILD_PIP_CONTRACTS.md)。
- 新增 25 项回归，完整结果 1150 passed、1 skipped、406 subtests passed；56/56 输出检查通过，94 个 Python 文件验证前后 Hash 一致。没有新增真实编码质量或费用结论。

## 2026-10-28 — unittest 输出保护

- 用独立解析模块替换宽松成功行匹配，保留未知记录、重要状态及整个报告尾部。
- 文档说明、未完成测试标题和独立状态行触发整段透传，避免实时输出中的假成功记录被隐藏。
- 新增 13 类真实 unittest Fixture 的 74 项 Hook/CLI 检查，分列默认 stderr、显式合并流、开关及换行转换；校验实际测试数、响应字段、原文字节及恢复审计。
- CI 加入真实 unittest 输出评测；超时、坏编码、测试数错误等保留完整分母。没有新增付费模型调用。见 [输出保护与验证](tokenguard_v04/UNITTEST_SAFETY.md)。
- 修复新 unittest 和既有 pytest 评测器自身读取长 Artifact 路径失败；两者使用独立的本地路径读取，不借用被测实现的恢复结果替代原文字节核对。
- 新增 64 项回归；完整回归 1125 passed、1 skipped、406 subtests passed。真实 unittest 74/74；相同原文的六类旧/新回放保留五类内容丢失及一类新增保守规则的差异。

## 2026-10-28 — 独立原文恢复评测

- 新增标准库独立评测器：67 项固定/固定种子样例，核对 show 逐字符重建、grep 命中行可达、预算、游标、审计及原文字节，输出 JSON/CSV/Markdown。
- 分列 2 项实际 CLI 与 65 项 API、生产参数与缩小扫描边界的压力参数；绑定源码、验证器及样例 Hash，缺失或无效 worker 证据不能通过。
- 修复小预算上下文分页可能返回部分行号标签的问题；6 项反例先失败后通过，新增测试共 29 项。
- 当前 67/67；同一验证器对历史 d048c89 检出 6 项失败。完整回归 1061 passed、1 skipped、406 subtests passed；没有新增模型或账单测量。
- CI 矩阵加入独立恢复评测。复现与口径见 [原文恢复评测](tokenguard_v04/RECOVERY_CONTRACTS.md)。

## 2026-10-28 — 上下文分页命中保护

- 修复达到 grep 命中上限后，尾部上下文关键词被续读标记切断、下一页从词中间开始而永久漏检的问题；不延长请求的上下文范围。
- 统一预留已选记录的游标元数据，覆盖行号和字符偏移位数变化，修复窗口选择与最终分页之间的预算不一致。
- 新增 64 项测试，含默认 12000 字符实际 CLI、Unicode/混合换行、24 组高密度分页独立核对及 9/10、99/100、999/1000 行边界。
- 旧版对照保留两类漏检和一类预算错误；不回写历史 Artifact/报告，不新增付费模型调用。

## 2026-10-28 — 扫描完成状态与命中窗口

- 扫描预算恰好耗尽时探测实际 EOF，避免无匹配结果误报截断或被不必要的续读元数据挤出预算；预读计入扫描与完整性核验。
- 命中行本身能放下、但后续上下文使续读标记成为必需时，重新选择保留完整关键词的字符窗口，避免本可恢复的内容被错误拒绝。
- 新增 69 项回归，包含实际默认 8 MiB CLI、Unicode/混合换行、16 组连续分页独立 oracle；保留首批 29 项失败和后续 4 组分页失败证据。
- 没有新增模型调用、真实编码质量或账单测量。

## 2026-10-28 — Windows Artifact 长路径

- 分离规范身份路径与实际 I/O 拼写；Windows 按 UTF-16 长度使用扩展驱动器/UNC 路径，覆盖 Artifact、定位索引、完整性缓存及审计/锁文件。
- 修复长路径无法 lstat 时漏检 junction 的问题：扩展路径检查每个祖先，再规范化身份；拒绝路径穿越及不支持的设备命名空间。
- 新增 40 项路径用例，包括真实长目录、非 BMP 字符、并发、缓存复用、损坏拒绝、三类 junction、权限失败和 Hook/恢复 CLI。
- 保留原深目录失败报告，不修改系统设置、用户配置、历史 Artifact 或付费实验。

## 2026-10-28 — 完整诊断记录与 pip 进度保护

- 重复日志按 CR/LF/CRLF 记录折叠，避免 Unicode 分隔符和 C0 控制字符把单条诊断拆成可隐藏片段、破坏合法 JSON 或绕过整行严重信息保护。
- pip 仅聚合格式完整的已知进度记录；同一行内的错误、警告和未知尾部保留。支持 Windows 横线进度条、未知速度和 ETA 占位值。
- 新增 118 项针对性用例，含 40 组固定种子逐字符展开校验，以及实际 Hook / show / grep 子进程；不调用模型、不变更用户配置。

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
