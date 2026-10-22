# TokenGuard v0.4

当前版本说明；项目概览和环境创建步骤见[根 README](../README.md)。

任务级 A/B 执行器已加入：参见 [TASK_EXPERIMENTS.md](TASK_EXPERIMENTS.md)。默认只预览计划；可通过本地 Fixture 子进程验证修复、压缩和独立测试验收，不调用付费模型。另已明确授权完成首轮真实 Claude 任务，两组均通过独立测试，但优化组输出不足阈值，未发生压缩，不能当作压缩收益验证。

支持 `prepare --baseline-mode no_hook` 与默认 `disabled_hook` 两种基线。运行 `python benchmark/baseline_demo.py --rounds 3` 可完成两种基线的本地对照，记录 Hook 进程开销并分别输出报告，不调用模型。

## 关闭与卸载

启动 Claude 前设置 `TOKENGUARD_ENABLED=0` 即可关闭压缩。移除该变量后恢复。

```powershell
$env:TOKENGUARD_ENABLED = '0'
# 卸载只移除本安装器拥有的 v0.4 项目 Hook，保留其他设置。
.\install.ps1 -Project 'C:\work\my-project' -Python $Python -Uninstall
```

已有旧版本 Hook 不会被自动删除。若同项目仍有 v0.3 条目，应先处理重复注册，再做实验。当前版本不改变 Claude 权限、Bypass 或模型配置。

## 策略与保护

至少 3000 字符才尝试压缩，加入原文恢复提示后仍须减少至少 15% 字符，否则透传。这不是 Token/费用收益保证。

|日志|策略与限制|
|---|---|
|pytest -v|命令和完成汇总匹配后聚合 PASSED；保留 FAILED/ERROR/SKIPPED/XFAIL/XPASS、未知行和汇总；失败详情区不匹配成功行|
|unittest -v|需 Ran 汇总及 OK/FAILED；仅聚合标准成功行；常见 unittest 默认写 stderr，Hook 对 stderr 原样保留|
|构建/重复警告|仅折叠相邻完全相同的非严重行；保留首条、次数和错误行|
|pip install|仅聚合明确进度条，保留版本、安装结果、resolver 与未知内容|
|文件搜索|只对明确文件名列表压缩，内容搜索和未知格式不截断|
|Git diff / Traceback|保守全局透传|

Hook 复制工具响应，只修改 stdout，保留 stderr、图像/中断和额外字段。压缩、存储或审计异常时不返回替换 JSON。stdout 以外结构不匹配则透传。

## 离线安装链路自检

```powershell
& $Python .\tg.py doctor --project 'C:\work\my-project'
```

doctor 只读已注册的项目 Hook，验证解释器和脚本路径，在临时目录模拟 PostToolUse，核验响应结构、结束标记、压缩及原文字节一致性，不启动 Claude、不调用模型、不污染真实审计。它不能证明 Claude 环境继承、shell 或替换消费成功。

## 诊断和统计

```powershell
& $Python .\tg.py diagnostics --limit 5
& $Python .\tg.py diagnostics --session 'SESSION_ID' --limit 5
& $Python .\tg.py stats --session 'SESSION_ID' --json
& $Python .\tg.py stats --experiment 'EXPERIMENT_ID' --json
```

将占位符替换为实际 ID。诊断需要调用进程 `TOKENGUARD_DIAGNOSTICS=1`，旧记录不会自动补写新字段。没有新诊断不等于没有压缩，请同时查看审计统计。`below_threshold` 只表示该次 stdout 少于门槛。

`TOKENGUARD_EXPERIMENT_ID` 未被启动进程继承时，新压缩记录的 experiment_id 可为空；按错误实验 ID 查询会得到 0，不能据此判定没有触发。日志默认在 `~/.tokenguard`，可用 `TOKENGUARD_DATA_DIR` 指向专用本地目录。

压缩审计是候选生成时的状态，不代表宿主消费确认。真实接入可在同一模型会话让模型不调用工具、引用前一次 Bash 输出中的 artifact_id 与 compression_id，再与本地审计核对。固定输出脚本为 `live_check.py`；不要对验证命令加 tail/head、重定向或截取。

## 有界原文恢复

```powershell
$Id = 'ARTIFACT_SHA256'
$CompressionId = 'COMPRESSION_ID'
& $Python .\tg.py grep $Id 'FAILED' --context 2 --limit 10 --compression-id $CompressionId
& $Python .\tg.py show $Id --start 30 --end 60 --max-chars 6000 --compression-id $CompressionId
& $Python .\tg.py grep $Id 'FAILED' --start 100000 --end 100200 --context 2 --compression-id $CompressionId
& $Python .\tg.py index $Id
```

关键词为 1..200 字符的大小写不敏感字面值；上下文 0..5 行、命中 1..50、show 1..100 行；单次返回最多 12000 字符，内容扫描最多 8 Mi 字符加探测字符。范围关键词查询的上下文也限制在该区间。超限返回限制标记和续读行号，不能解释为无匹配。

start>1 时按需构建/复用稀疏字节偏移索引，支持 UTF-8、LF/CRLF/CR 及无末尾换行。首次构建需完整顺序读取，成本单独计入 index_build_bytes；随后从目标附近读取。索引损坏/过期重建，不可写时回退，`TOKENGUARD_INDEX_ENABLED=0` 可关闭自动索引。显式 `index --rebuild` 只重建 sidecar，不改原文。单条超长行仍受预算限制，但其后行可直接定位。

ID 严格限制为小写 SHA256，拒绝符号链接/路径逃逸。原文原子写入、保留 UTF-8 字节；不会删除历史 Artifact。数据目录视为可信本地用户目录，不防御可同时篡改源文件和指纹的恶意进程。原日志可能包含私密内容，不要公开上传该目录。

恢复按 compression_id 关联，累计读取量与恢复数可查询。同一个 Artifact 对应多个压缩时，不猜测归属；请带 compression_id。审计字段 compression_ratio 为 optimized/original，报告中的 reduction 为 1-ratio。

## 离线 Benchmark

```powershell
& $Python .\benchmark\run.py --rounds 3
& $Python .\benchmark\usage_demo.py
& $Python .\benchmark\examples.py
```

首个命令对 5 个固定场景运行独立 Mock 进程，复制相同初始快照、比较 Hash、使用相同 Prompt 和模型标签；覆盖 pytest 成功/失败、构建错误、真实小 Python Bug fixture 及大 Artifact 尾部恢复。Bug 修复是固定脚本策略，最终运行 504 项 pytest 并检查测试未改，不代表模型能力。三轮为 30 个 arm，工具文本/耗时/恢复均计入报告。

usage_demo 只演示合成 usage 的离线导入和配对。真实使用、字段口径、完整性与费用证据见 [USAGE.md](USAGE.md)。生成内容保存在 reports，Git 默认忽略；没有付费模型调用。

examples 实际运行同一项目修复前后的 504 项 pytest，另验证混合状态、构建、unittest、pip、搜索及安全透传，共 13 例。8 个压缩案例各检查 Artifact 字节及关键词/行号恢复。运行方法、前后文本和边界见 [EXAMPLES.md](EXAMPLES.md)。CLI 恢复明确输出 UTF-8、保留原换行，避免 Windows CRLF 被重复转换。

中等复杂真实任务的质量对照见 [SWE_BENCH_QUALITY.md](SWE_BENCH_QUALITY.md)：使用公开 SWE-bench Verified Pylint 任务及官方测试指标，先检查原始/参考修复，再单独授权真实 A/B；不把无压缩的试验当作压缩能力验证。

## 开发测试

测试执行凭证与可选 `controlled_initial_log` 协议见 [QUALITY_PROTOCOL.md](QUALITY_PROTOCOL.md)。原生 Hook 暴露与控制器预处理分别计数；不完整测试、无实际压缩或审计／响应不匹配会使比较证据不满足。运行 `python -X utf8 benchmark/protocol_demo.py` 可验证真实本地测试与恢复链路；固定修改及工具消息明确为 Mock，模型调用为 0。

运行 `python -X utf8 -m pytest tests/test_quality_execution.py -q` 可验证执行器原生／受控路径，以及评分异常、损坏流、缺失审计和下一会话阻断等 10 项回归。它使用独立假 CLI 和实际本地 pytest/Hook；捕获到基础设施异常后保存结果、停止后续会话，不自动重试或复用会话。

在本版本目录执行 `python -m pytest -q` 或 `python -m unittest discover -s tests -v`。手动协议验证为 `python verify_hook.py`。这几类验证与真实 Claude 会话、提供商 usage 和账单证据分开记录。
