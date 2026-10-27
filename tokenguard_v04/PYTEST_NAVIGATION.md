# pytest 测试名称与选择性恢复

真实质量实验出现了基线成功、优化组失败的负面结果，见 [QUALITY_PILOT_RESULTS.md](QUALITY_PILOT_RESULTS.md)。失败原因尚未确认为压缩，因此这里先增加可选择的信息保留方式及离线对照，不宣称已经改善编码能力，也不替换旧实验结果。

## 三种模式

| 模式 | 行为 | 当前用途 |
| --- | --- | --- |
| `compact` | 原有 PASSED 行聚合 | 默认，保持已有行为 |
| `indexed` | 聚合 PASSED 参数用例，保留测试函数名称、数量和原文行号范围；超预算时整段透传 | 实验性，需要显式选择 |
| `off` | 对识别为 pytest 的命令透传 stdout | 对照或关闭 pytest 压缩；其他策略仍可工作 |

`indexed` 最多保留 32 个不同的测试节点前缀，索引最多 2,400 字符，包含索引标题和说明。先识别 `.py` 文件和合法 Python 节点组件，再去掉参数后缀；参数中的 `::` 不当作节点分隔符，无法确认的自定义节点保留原文。参数 ID 仍在原文中。名称使用 JSON 字符串形式显示，避免控制字符破坏索引行。

索引必须覆盖所有被隐藏的节点前缀。节点数、名称长度或最终索引总长度超限时，返回 `pytest_index_budget_exceeded`，保留原文，不生成残缺索引。每条索引的行号是原文起止范围，**范围中可能包含警告或其他状态**，不是可以整段删除的通过记录。

索引只来自报告段之前的已识别 PASSED 进度行；进入警告、捕获输出、插件报告或汇总后停止聚合。FAILED、ERROR、SKIPPED、XFAIL、XPASS、未知行和失败详情保持原有保护。显式 `-s` / `--capture=no` / `tee-sys` 或可见的进度交错歧义触发整段透传。Git diff、Traceback、未完成输出及收益不足仍透传。索引开销及恢复指引都计入压缩后的字符数；加入元数据后无法满足现有收益门槛也不替换输出。实际 pytest 生成的反例与验证方式见 [语义验证](SAFETY_VALIDATION.md)。

例如 240 条参数化记录可以显示为：

```text
"tests/test_paths.py::TestPaths::test_ignore_path": 240 passed; raw lines 243-482
```

需要具体参数时，可使用已有 `tg.py show ID --start 243 --count 1 --compression-id CID`，或 `tg.py grep ID "test_ignore_path[case 239]" --context 0 --limit 1 --compression-id CID`。不要求恢复整份日志。`grep` 本身返回原文行与行号，不需要再运行一次 `show` 才算恢复。

## Windows 安装与回退

```powershell
$Python = 'D:\tokenguard-benchmark\.venv\Scripts\python.exe'
$V04 = 'D:\tokenguard-benchmark\tokenguard_v04'
Set-Location $V04

# 按需选择实验性索引；只修改指定项目的本地 Hook 设置
.\install.ps1 -Project 'D:\tokenguard-benchmark' -Python $Python -PytestMode indexed

# 恢复现有默认行为
.\install.ps1 -Project 'D:\tokenguard-benchmark' -Python $Python -PytestMode compact

# 或仅关闭 pytest 压缩
.\install.ps1 -Project 'D:\tokenguard-benchmark' -Python $Python -PytestMode off
```

选择其中一种即可；安装后重启 Claude Code 使设置生效。不带 `-PytestMode` 时使用 `compact`。本次开发未替用户切换已安装的模式，也未修改全局 Claude 配置或权限。全部优化的原有总开关 `TOKENGUARD_ENABLED=0` 仍然有效。

Python/Linux 安装器支持相同的 `--pytest-mode` 参数。Hook 子进程本身也支持 `--pytest-mode`；没有新增隐式环境变量覆盖。压缩审计和诊断记录包含 `pytest_mode`，只表示选择了哪种模式，不能证明宿主实际接受替换。

## 可重复的本地比较

```powershell
& $Python -X utf8 "$V04\benchmark\pytest_navigation.py"
```

七类固定日志分别运行三种模式，共 21 项：参数化成功记录、混合失败与其他状态、CRLF/ANSI/中文、节点超限、名称超长、Traceback、未完成输出。Hook 和恢复 CLI 是实际本地子进程；日志、查询词与查找步骤为固定 Fixture。每项核对响应结构、重要信息保留、原文 Artifact 字节和必要的单行恢复。

输出到新的 `reports/navigation/pytest-navigation-.../`，包含 Markdown / JSON / CSV 与前后文本。报告记录交付字符、恢复次数、返回及扫描字符、首次行偏移索引构建字节。这里的行偏移索引是已有的 Artifact 检索功能，与摘要中的测试名称索引分开计费。

初始索引功能的一次 Windows 本地运行中，参数化 Fixture 原文为 34,894 字符：

| 模式 | 交付字符，含指引 | 恢复前目标名称可见 | 必要恢复次数 | 恢复返回字符 | 首次偏移索引构建字节 |
| --- | ---: | --- | ---: | ---: | ---: |
| off | 34,894 | 是 | 0 | 0 | 0 |
| compact | 533 | 否 | 1 次 grep | 76 | 0 |
| indexed | 860 | 是 | 1 次 show | 71 | 34,894 |

索引提供了导航信息，但本例交付与恢复合计文本更多，首次 I/O 也更多。不能只比较 76 与 71，或故意让 compact 多执行一次 show 来夸大收益。工具路径、换行和平台可能使字符数变化；这些不是实际 API Token 或费用。

2026-10-27 修复 grep 匹配上限的续取提示后，同一 Fixture 的本地复核为 compact 交付 545、恢复返回 155 字符，indexed 交付 872、恢复返回 71 字符；两者仍各恢复一次。grep 的 155 包含原始行号、完整原文行和续取提示，不能把提示开销排除后比较。偏移索引首次构建仍为 indexed 的 34,894 字节。完整原文关闭模式不变，21 项检查通过；这些测量不代表真实 Agent 查找策略。

另外在本地重放了上一对真实实验的初始日志：compact 仍为 16,661 → 1,769 字符；indexed 因索引预算超限透传全部 16,661 字符。旧实验、判分与 Artifact 未改动，没有重跑模型或重新判断任务成功。它验证了保守透传，而没有证明新模式能让该任务修复成功。

## 实验配置必须显式固定

`benchmark/task_runner.py prepare` 和 `benchmark/swebench_runner.py prepare` 新增 `--pytest-mode`，默认为 compact。配置、安装的 Hook 命令、Mock 驱动和受控初始日志使用同一选择；模式进入配置 Hash，并在质量汇总中分组。旧记录没有该字段时按历史 compact 解释，不改写旧文件。源码改变后，旧已准备计划的运行校验可能拒绝继续，必须重新 prepare，不能手改 Hash 或复用已启动会话。

受控质量协议仍要求初始日志实际发生压缩。若 indexed 选择整段透传，预检会阻止受控付费执行，不能通过降低门槛或追加重复内容绕过。特别是上述历史 Pylint 日志，当前 indexed 不适合作为“已发生压缩”的受控实验输入。

可仅用 Mock 验证配置传播和固定修复流程：

```powershell
$Exp = (& $Python -X utf8 "$V04\benchmark\task_runner.py" prepare `
  --project "$V04\benchmark\fixture" --task "$V04\benchmark\task_fixture.json" `
  --output "$V04\reports\navigation-tasks" --rounds 1 `
  --baseline-mode no_hook --pytest-mode indexed).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Preparation failed' }
& $Python -X utf8 "$V04\benchmark\task_runner.py" run $Exp --driver mock --execute
```

该本地流程已验证两组均通过独立测试，但修复决策是固定脚本，不是模型能力结果。新增边界与子进程测试覆盖索引完整性、超限透传、换行/颜色/Unicode、重要状态、恢复归属、异常回退、安装切换及模式固定。模型 API 调用为 0，真实 API 用量和账单仍为 `unavailable`。再次验证真实模型能力需新的独立实验与明确的付费范围。

初始索引功能发布时完整回归为 **221 passed、1 skipped、93 subtests passed**，其中新增 15 项测试；手动 Hook 子进程验证的失败信息、原文 Artifact 字节及恢复检查也通过。后续安全修复与当前验收见 [语义验证](SAFETY_VALIDATION.md)。上述结果均为本地软件验证，不作为新模式真实编码能力的证据。
