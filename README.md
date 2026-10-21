# TokenGuard

[![tests](https://github.com/cxisdawn/TokenGuard/actions/workflows/tests.yml/badge.svg)](https://github.com/cxisdawn/TokenGuard/actions/workflows/tests.yml)

面向编程 Agent 的保守工具输出压缩器：减少重复日志占用，保留失败信息，并提供本地原文恢复与可审计的实验流程。

当前主版本为 **v0.4**，优先支持 Claude Code 的 Bash `PostToolUse`。Python 3.10+，运行时仅使用标准库。

## 功能

- pytest/unittest 成功记录聚合、构建重复日志、pip 进度、重复警告及文件名搜索压缩。
- Traceback、Git diff 和重要失败内容保护；不确定或异常时透传原始输出。
- SHA256 Artifact、本地关键词/行范围恢复、有界返回及稀疏行索引。
- session/experiment 审计，记录净字符节省、恢复量和本地处理开销。
- 固定快照、独立进程、多轮离线 A/B，生成 Markdown/JSON/CSV。
- DeepSeek/Anthropic usage-only 导出的离线归一与配对分析；缓存、账单、任务质量分列。

## 快速验证（Windows PowerShell）

克隆本仓库后，在仓库根目录执行：

```powershell
python -m venv .venv
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $Python -m pip install -r requirements-dev.txt

Push-Location .\tokenguard_v04
& $Python -m pytest -q
& $Python .\verify_hook.py
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

如本机策略不允许执行 PowerShell 脚本，可直接运行等效 Python 安装器：

```powershell
& $Python .\tokenguard_v04\installer.py --project $Project --python $Python
```

在启动新的 Claude 会话前可设置 `TOKENGUARD_DIAGNOSTICS=1` 和 `TOKENGUARD_EXPERIMENT_ID`。实际模型会话可能产生提供商费用，离线验证与真实接入需分别评估。

## 当前证据边界

本地当前版本回归为 101 项通过、1 项因 Windows 符号链接权限跳过；历史 v0.1/v0.2/v0.3 测试分别为 4/6/14 项通过。CI 配置覆盖 Windows/Linux 与 Python 3.10/3.12，远端执行状态请以 Actions 为准。

已有一次用户运行的真实 Claude 会话证据：模型引用了与本地审计精确匹配的替换元数据。这证明该次模型看到了替换内容，不能证明稳定触发率，也不能证明原文在所有上下文中完全不存在。

**字符下降、本地估算 Token 下降、整个任务实际 Token 与费用下降是不同指标。** 尚无完整真实 A/B usage/账单，因此没有稳定费用节省率结论。所有示例 usage 和费用 Fixture 均为 synthetic，不能用于宣传真实收益。

## 文档

- [v0.4 接入与恢复说明](tokenguard_v04/README.md)
- [离线 usage 与账单配对](tokenguard_v04/USAGE.md)
- [v0.3 审查要点](tokenguard_v04/AUDIT_V03.md)
- [验证范围与限制](docs/VALIDATION.md)
- [变更记录](CHANGELOG.md)

v0.1、v0.2、v0.3 保留历史源码与测试；请在各自目录独立运行测试，避免同名包导入冲突。v0.4 包含故意含 Bug 的 Benchmark 初始快照，常规回归只收集 `tests/`。

生成的原始日志、Artifact、真实会话 ID/审计、本机配置、虚拟环境和压缩包不属于此仓库的发布内容。
