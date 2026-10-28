# TokenGuard

[![tests](https://github.com/cxisdawn/TokenGuard/actions/workflows/tests.yml/badge.svg)](https://github.com/cxisdawn/TokenGuard/actions/workflows/tests.yml)

面向编程 Agent 的保守工具输出压缩器：减少重复日志占用，保留失败信息，并提供本地原文恢复与可审计的实验流程。

当前主版本为 **v0.4**，优先支持 Claude Code 的 Bash `PostToolUse`。Python 3.10+，运行时仅使用标准库。

## 功能

- pytest/unittest 成功记录聚合、构建重复日志、pip 进度、重复警告及文件名搜索压缩。
- Traceback、Git diff 和重要失败内容保护；不确定或异常时透传原始输出。
- SHA256 Artifact、本地关键词/行范围/长行片段恢复、有界返回及稀疏字符定位索引。
- pytest 可选测试名称索引；实时输出歧义、报告段和未知格式采用保守保护。
- session/experiment 审计，分列候选字符减少、stdout 写出状态、恢复量和证据完整性。
- 固定快照、独立进程、多轮离线 A/B，生成 Markdown/JSON/CSV。
- DeepSeek/Anthropic usage-only 导出的离线归一与配对分析；缓存、账单、任务质量分列。
- 任务级 A/B 执行器：相同源码快照、全新会话、独立测试验收、默认 dry-run；本地 Mock 不调用模型。

## 快速验证（Windows PowerShell）

克隆本仓库后，在仓库根目录执行：

```powershell
python -m venv .venv
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $Python -m pip install -r requirements-dev.txt

Push-Location .\tokenguard_v04
& $Python -m pytest -q
& $Python .\verify_hook.py
& $Python -X utf8 .\benchmark\pytest_contracts.py
& $Python .\benchmark\examples.py
& $Python .\benchmark\usage_demo.py
Pop-Location
```

这些验证不调用付费模型。Linux/macOS 可使用 `.venv/bin/python`，在相应版本目录运行同样的 Python 命令。

## 接入 Claude Code

```powershell
$Project = 'C:\work\my-project'
.\tokenguard_v04\install.ps1 -Project $Project -Python $Python
```

安装器只更新目标项目的 `.claude/settings.local.json`，备份原文件并保留其他 Hook/权限，不修改全局配置。代码保留 `install.ps1`/`installer.py` 作为源码接入脚本；仓库不提供二进制安装包或 Release 附件。

从新目录重新安装时，标准 v0.4 Bash Hook 会迁移到新路径并去重；其他版本及无法确认归属的手写命令保留。安装后退出并重新启动目标项目中的 Claude Code，再通过 `/hooks` 检查配置。

如本机策略不允许执行 PowerShell 脚本，可直接运行等效 Python 安装器：

```powershell
& $Python .\tokenguard_v04\installer.py --project $Project --python $Python
```

在启动新的 Claude 会话前可设置 `TOKENGUARD_DIAGNOSTICS=1` 和 `TOKENGUARD_EXPERIMENT_ID`。实际模型会话可能产生提供商费用，离线验证与真实接入需分别评估。

## 当前证据边界

2026-10-28 本地当前版本回归 **704 passed、1 skipped、406 subtests passed**；新增 Artifact 内容身份校验、并发发布与读取成本审计。Windows 线程/进程回归及 Linux 20 组并发压力验证通过。108 项实际 pytest 输出契约、13 个本地例子、三轮 Mock 30/30 及历史 v0.1/v0.2/v0.3 的 4/6/14 项测试通过。首次恢复完整核验的读取开销明确列出，旧测量缺失保持 unavailable。本轮未调用模型；软件测试不能证明编码能力无损或真实费用节省。详细范围及失败复现见 [验证记录](docs/VALIDATION.md)。CI 覆盖 Windows/Linux 与 Python 3.10/3.12，远端状态以 Actions 为准。

已有一次用户运行的真实 Claude 会话证据：模型引用了与本地审计精确匹配的替换元数据。这证明该次模型看到了替换内容，不能证明稳定触发率，也不能证明原文在所有上下文中完全不存在。

另已明确授权完成一轮真实任务 A/B：两组均修复 Bug 并通过独立的 504 项测试。优化组 Hook 自动触发 3 次，但收到的 stdout 均不足 3000 字符，实际压缩为 0 次。这验证了本轮任务执行和 Hook 透传分支，不能把 CLI 汇总数字差异解释为 TokenGuard 节省。

后续公开 SWE-bench Verified Pylint 任务三轮对照中，基线候选修复通过 0/3、优化组 1/3；六次会话都触及 20 轮上限，严格正常完成率均为 0/3。优化组实际压缩 3 次，宿主工具消息中均有匹配标记。这是受预算限制的初步观察，不能据此证明能力无损或提升，详见 [质量实验](tokenguard_v04/SWE_BENCH_QUALITY.md)。

另行授权的 40 轮补充对照中，基线代码通过、优化组未通过，双方仍触及轮数上限；优化组实际压缩 0 次，因此这次差异不能归因于压缩信息损失。两个预算条件分别报告，不混算成功率，也不隐去优化失败样本。

最近一次采用受控初始日志的真实对照中，基线 F2P 4/4、P2P 171/171，优化组 F2P 0/4、P2P 162/171，且达到轮数上限。这是需要保留的负面结果；单对实验无法确定压缩的因果影响，当前也没有编码能力无损结论，见 [完整结果](tokenguard_v04/QUALITY_PILOT_RESULTS.md)。后续语义、恢复与证据修复均通过离线验证，不把软件回归通过当作重新测得的模型能力。

**字符下降、本地估算 Token 下降、整个任务实际 Token 与费用下降是不同指标。** 尚无完整真实 A/B usage/账单，因此没有稳定费用节省率结论。所有示例 usage 和费用 Fixture 均为 synthetic，不能用于宣传真实收益。

现已加入测试执行凭证及显式受控初始日志协议：关联实际测试、源码 Hash 和 Bash 响应，缺少执行／暴露证据时不报告质量比较成立。`benchmark/protocol_demo.py` 以真实本地 pytest、Hook 和恢复验证流程，固定修改和工具消息为 Mock，模型调用为 0。另已通过 Pylint 原始／gold 本地预检，不追加付费会话；详见[执行证据与协议](tokenguard_v04/QUALITY_PROTOCOL.md)。

执行器现逐会话重查输入与源码，评分／审计／凭证等异常保留结果并停止后续会话；缺失统计保持 unavailable/null。10 项新增回归通过独立假 CLI 和真实本地测试验证执行链路，不启动模型或 Docker，不作为真实编码能力样本。

`benchmark/quality_compare.py` 可只读汇总多个已有实验，按固定条件分组，保留所有配对及证据不足原因；原生／受控模式和不同预算不混算。仅输出描述性计数与有覆盖标记的测量，不宣称统计无损或真实费用节省。

## 文档

- [Artifact 完整性、并发保存与恢复读取开销](tokenguard_v04/ARTIFACT_INTEGRITY.md)

- [v0.4 接入与恢复说明](tokenguard_v04/README.md)
- [审计完整性、Hook 输出与故障复现](tokenguard_v04/AUDIT_RELIABILITY.md)
- [任务报告、流式 Usage 与账单证据校验](tokenguard_v04/REPORT_RELIABILITY.md)
- [语义、长行恢复与评测证据验证](tokenguard_v04/SAFETY_VALIDATION.md)
- [pytest 测试名称索引及保守透传](tokenguard_v04/PYTEST_NAVIGATION.md)
- [受控初始日志真实 A/B 的负面结果](tokenguard_v04/QUALITY_PILOT_RESULTS.md)
- [13 个可重复的本地验证例子](tokenguard_v04/EXAMPLES.md)
- [SWE-bench 单任务质量对照](tokenguard_v04/SWE_BENCH_QUALITY.md)
- [测试执行凭证与受控初始日志](tokenguard_v04/QUALITY_PROTOCOL.md)
- [跨实验的只读质量汇总](tokenguard_v04/QUALITY_SCORECARD.md)
- [离线 usage 与账单配对](tokenguard_v04/USAGE.md)
- [任务级 A/B 执行、独立验收与付费调用边界](tokenguard_v04/TASK_EXPERIMENTS.md)
- [v0.3 审查要点](tokenguard_v04/AUDIT_V03.md)
- [验证范围与限制](docs/VALIDATION.md)
- [变更记录](CHANGELOG.md)

v0.1、v0.2、v0.3 保留历史源码与测试；请在各自目录独立运行测试，避免同名包导入冲突。v0.4 包含故意含 Bug 的 Benchmark 初始快照，常规回归只收集 `tests/`。

生成的原始日志、Artifact、真实会话 ID/审计、本机配置、虚拟环境和压缩包不属于此仓库的发布内容。
