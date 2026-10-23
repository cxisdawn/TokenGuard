# 跨实验的只读质量汇总

新实验的 `pytest_mode`（compact / indexed / off）也作为分组条件；旧记录缺省按历史 compact 处理，不回写。不同模式不合并为同一质量成功率，详见 [PYTEST_NAVIGATION.md](PYTEST_NAVIGATION.md)。

2026-10-23 新增一对 `controlled_initial_log` 真实实验，作为第三组单独汇总：受控压缩比较证据满足，基线修复、优化组未修复并新增 9 项回归。详见 [QUALITY_PILOT_RESULTS.md](QUALITY_PILOT_RESULTS.md)。下文八个历史会话的结果保持原有分组，不与新协议合并。

`benchmark/quality_compare.py` 汇总已有实验，不启动 Agent、Docker 或提供商请求，也不重跑评分器。它校验原始快照和配置，读取已保存的独立判分、宿主流与压缩审计，然后在新的唯一目录输出 Markdown、JSON 和逐配对 CSV。

## 一条命令查看已有结果

从项目根目录运行，替换下面两个路径为实际实验目录：

```powershell
$Python = 'D:\tokenguard-benchmark\.venv\Scripts\python.exe'
$V04 = 'D:\tokenguard-benchmark\tokenguard_v04'
& $Python -X utf8 "$V04\benchmark\quality_compare.py" `
  "$V04\reports\q\task-experiment-FIRST" `
  "$V04\reports\q\task-experiment-SECOND" `
  --output "$V04\reports\quality-scorecards"
```

输出 `quality-scorecard-UUID/scorecard.md`、`scorecard.json`、`pairs.csv`。每次生成新目录；输入中的旧 `quality-report.*`、`quality-analysis.*`、源码、日志和 Artifact 都不改写。输出目录不能放进任何输入实验目录。

目录只接受明确传入的 1..100 个实验，不递归发现项目或凭据。重复实验 ID、复制目录重复计数、跨实验复用会话、配置／快照变化会报错。报告包含输入文件的 SHA256；读取前后证据变化则中止，待实验停止写入后再运行。超限日志仍显示该配对及证据缺失，标记未取得完整 Hash，不无限读取。

报告保留本地来源路径和实验 ID，便于追溯，但不复制原始工具日志、源码、完整 Prompt、会话 ID 或凭据。报告目录默认在 Git 忽略的 `reports/` 内，不自动上传。

## 如何分组

按任务、提供商／请求模型、协议、预算、基线模式、基础 Prompt Hash、初始快照 Hash、完整配置／工具 Hash、镜像 ID、任务数据及评分源码提交保守分组。隐式旧协议和显式新协议也区分，受控模式的初始观察 Hash 不同时分开；两个预算或不同压缩器版本不合并成一个成功率。

同一个任务的多次重复仍是一个任务，不能冒充多个独立开源任务。模型标签由本地配置声明，不是后端路由认证；同一系统用户下的记录与 Hash 也不是不可篡改证明。

## 两套配对结果同时展示

每组列出所有计划配对的五种结果：仅基线修复、仅优化修复、双方修复、双方未修复、无有效判分／尚不完整。判分无效、测试保护被破坏或未运行不能假装成一份测量到的失败修复。

另列**压缩证据充分的子集**，要求已有执行凭证满足协议，且当前仍能核对到原生 Bash tool_use_id／审计／指引匹配，或受控初始日志的完整预检、同源日志及交付校验。仅存一个“曾经通过”的状态字段不能代替当前可读取的压缩证据。

子集是次级分析，可能受选择偏差影响；其余配对与缺失原因完整保留。不能用双方都未修复证明无损，也不能把未发生压缩的结果差异归因于压缩。质量判分和 Agent 正常完成分开计数：有效补丁可以在达轮数上限时仍通过判分，但严格任务成功仍为否。

## 已有八个真实会话的复核

此次只读复核没有新增模型请求。两档独立显示：

| 预算 | 计划配对 | 基线代码修复 | 优化代码修复 | 优化原生压缩 | 严格任务成功（双方） |
| --- | ---: | ---: | ---: | ---: | --- |
| 20 轮 | 3 | 0/3 | 1/3 | 3 次 | 0/3、0/3 |
| 40 轮 | 1 | 1/1 | 0/1 | 0 次 | 0/1、0/1 |

八次均达到轮数上限。按后来补齐的完整测试执行／tool_use_id 证据要求，两档可认证的压缩比较子集均为 0 对。这不否认旧记录中的 3 次真实压缩及宿主元数据匹配，只表示不能追补成新证据协议验证成功；原观察和原始文件保留。总体能力无损仍未建立。

## 用量、费用与耗时的口径

- 每项测量显示 `known`、`planned`、`complete`、已知值之和／中位数；没有观测时为 null，有缺失时不冒充整批总量。
- 原生压缩／恢复与控制器手动处理分开统计。未运行和故障样本保留，不只统计成功修复。
- CLI input、cache read、cache creation、output 各自单列为 **UNVERIFIED**，不相加宣称整个任务真实 Token，也不计算真实费用节省。
- 墙钟值为 Agent 执行耗时，包含其等待工具的时间；控制器预检及独立判分不在此字段中。逐对的优化减基线耗时差仅为描述性数据。
- 真实提供商 Usage、实际账单缺失时保持 unavailable。重复任务的描述性计数不提供统计非劣效结论、置信度承诺或因果证明。

## 本地回归

```powershell
Set-Location D:\tokenguard-benchmark\tokenguard_v04
& 'D:\tokenguard-benchmark\.venv\Scripts\python.exe' -X utf8 `
  -m pytest tests/test_quality_compare.py -q
```

13 项回归覆盖协议／预算／模型和固定输入分组、配对分母、达限候选与正常结束的区别、测量覆盖、同源日志、重复实验／会话、只读导出、损坏／超限日志、错误判分类型、读取时变化及命令行执行。全部为离线 Fixture，不代表新增真实模型能力测评。
