# 用公开任务评估压缩是否影响代码能力

后续执行证据与显式受控日志协议见 [QUALITY_PROTOCOL.md](QUALITY_PROTOCOL.md)。旧实验继续按原有协议和预算报告，不追补为新凭证验证成功。新准备默认 `agent_driven`，可显式选择 `--protocol controlled_initial_log`，两种模式分别比较。

此前的日志案例只证明固定输入能压缩和恢复；除法 Fixture 也不足以代表中等复杂工程任务。这里使用公开 SWE-bench Verified 的一个真实任务，重复比较相同模型在 no_hook / TokenGuard 两组的修复质量。

## 任务和指标

- 数据集：[SWE-bench Verified](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified)。固定任务 `pylint-dev__pylint-6528`，基准提交 `273a8b25620467c1e5686aa8d2a1dbb8c02c78d0`。
- 问题：Pylint 递归扫描不遵守 ignore / ignore-patterns / ignore-paths，需要兼顾文件和目录的忽略行为。
- 数据集难度标签：`15 min - 1 hour`。它是人类修复耗时档位，不是对模型难度的独立校准。
- 官方任务定义含 4 个 `FAIL_TO_PASS`、171 个 `PASS_TO_PASS`。使用未修改的上游评分源码；[指标定义](https://www.swebench.com/SWE-bench/api/harness/)要求修复目标测试并维持已有功能。
- 主结果为任务是否 resolved、两类测试的成功/失败列表、保护文件是否被修改。缺失测试不当作通过；P2P 的 skipped/xfail 按固定上游评分器的语义处理，另保存完整状态数量。

这是**单个公开任务的局部对照实验**，不是 500 题官方排行榜得分。三个重复样本仍不足以证明普遍不降低能力。

## 首次真实三轮结果

已按单独授权完成 6 个新 Claude 会话，请求模型 deepseek-flash；每会话传入 max-turns 20，超时 600 秒，所有会话身份均匹配。没有继续消耗该授权以外的会话。

| 轮次 | 基线代码 resolved | 优化代码 resolved | 基线/优化正常完成 | 优化压缩次数 | 匹配压缩标记的宿主工具消息 |
| --- | --- | --- | --- | ---: | ---: |
| 1 | 否，F2P 0/4 | 否，F2P 0/4 | 否 / 否 | 1 | 1 |
| 2 | 否，F2P 0/4 | 是，F2P 4/4 | 否 / 否 | 2 | 2 |
| 3 | 否，F2P 0/4 | 否，F2P 0/4 | 否 / 否 | 0 | 0 |

全部候选的 P2P 均为 171/171 维持。第二轮优化仅修改 pylint/lint/pylinter.py，其独立验收 resolved=true；但六个 Agent 都以 error_max_turns 结束，所以按“正常结束并验收成功”的严格任务标准，两组均为 0/3。代码通过与 Agent 正常完成不能混为一项。

三次压缩均在宿主流式工具消息中找到与本地审计精确匹配的 Artifact/压缩 ID；最终回答未引用匹配标记，不能声称完成了模型主动引用验证。第三轮优化未触发压缩，不是压缩效果样本。恢复次数全部为 0。

**结论：观察到一份经历压缩的候选修复通过官方指标，但本轮无法证明能力无损或改善。** 基线均未修复，且共同轮数限制使完成率出现地板效应。不能把两组正常完成率同为 0 宣传成无质量损失。随后另行授权了下面的 40 轮补充对照；两种预算分开报告。

原生提供商 Usage 与实际账单不可得。CLI 未验证聚合如下，不能据此计算实际费用节省；本轮连这些宿主计数也没有呈现统一下降趋势：

| CLI 原始聚合字段（3 次合计，unverified） | 基线 | 优化 |
| --- | ---: | ---: |
| input_tokens | 139831 | 140334 |
| cache_read_input_tokens | 2339712 | 2507008 |
| cache_creation_input_tokens | 0 | 0 |
| output_tokens | 26724 | 49653 |

所有失败、限额退出、原始测试、候选补丁和逐次 usage 均保留本地。只读分析脚本 `benchmark/quality_analysis.py EXPERIMENT` 生成 Markdown/JSON/CSV，将代码验收、会话结束原因、压缩暴露和宿主计数分别列出。它不启动模型。

## 另行授权的 40 轮补充对照

追加一轮、两个新会话，双方统一 max-turns 40、超时 600 秒，其余显式模型、问题和验收标准保持一致。再次验证原始/gold，使用新的相同快照配对，不复用前一批会话。两个批次共 8 个会话 ID 均唯一、返回身份匹配，所有独立验收有效，没有权限拒绝。

| 组别 | 代码 resolved | F2P | P2P 维持 | 会话正常完成 | 实际压缩 |
| --- | --- | ---: | ---: | --- | ---: |
| 基线 | 是 | 4/4 | 171/171 | 否，error_max_turns | 0 |
| 优化 | 否 | 0/4 | 171/171 | 否，error_max_turns | 0 |

基线只改库源码，候选通过官方指标，但在第 40 轮预算下仍未正常结束。优化组也达限退出，Hook 记录的 15 次调用均为 below_threshold，没有发出压缩替换；另有失败工具调用不能与成功 PostToolUse 的诊断数混同。本次代码结果确实不同，但它不能作为“压缩删掉信息导致能力下降”的证据，也不能作为压缩无损样本。

这两次 CLI 聚合 input/cache-read/output 分别是基线 70419/2464896/32460、优化 53356/2000128/24004；cache creation 都为 0。优化组没有压缩，计数却较低，进一步说明自然工具轨迹、任务进展和重复实验波动足以改变总量，不能把差值直接归功于 TokenGuard。这些仍是 unverified 宿主聚合，实际提供商 Usage 与账单 unavailable。

当前证据不足以给“是否影响代码能力”下普遍结论：20 轮档有真实压缩但存在预算/基线地板效应，40 轮档未发生压缩。不能把不同预算合并成总成功率，也不能隐藏这次优化组失败。下一步应先改进测试命令执行与压缩暴露的可控性，而非继续扩大付费样本。本次两批授权均已用完，没有自动追加其他模型会话。

## 比较方式

复用已有 snapshot、配置哈希、新会话 ID、AB/BA 次序、Claude 启动参数和项目 Hook 安装代码，增加 Docker 独立验收适配器，不替换压缩算法。

每轮基线不注入执行器的 TokenGuard Hook，优化组使用当前默认算法和 3000 字符门槛。原有用户/组织 Hook 和 Claude 权限不改。两组的源码快照、Prompt、模型标签、测试助手、镜像 ID 和工具代码完全相同。三轮按 AB、BA、AB 顺序启动六个会话，不复用或续跑会话。

Agent 看到全部 `pylint/` 库源码、两份原有测试及部分根目录说明/配置。它不是完整仓库检索环境，报告明确记录此源码选择范围。测试助手 `pytest.py` 将候选库源码只读挂载到镜像，在原始测试上运行 pytest -v；命令名让现有 pytest 策略自然识别，不降低阈值、不注入重复填充日志。

新增回归测试和 gold patch 不复制进 Agent 工作目录。独立验收在新容器的原始源码上应用候选修改，再运行数据集提供的测试脚本及隐藏测试补丁。只允许更改已有 pylint/ 文件，候选对测试、助手、配置或其他文件的修改会导致验收失败。共享系统用户下的目录隔离不是安全沙箱；提示禁止检索参考答案、父目录或凭据，但不能提供防恶意窥探认证。

压缩次数为 0 的 trial 不能衡量压缩对能力的影响。报告分别记录压缩暴露、恢复次数、工具调用、耗时，以及模型最终回答中引用了多少组与审计精确匹配的 Artifact/压缩 ID。引用支持相应输出可见性的判断，不自动证明全部工具输出的消费过程。

## 环境与已知偏差

使用官方环境镜像 `swebench/sweb.eval.x86_64.pylint-dev_1776_pylint-6528:latest`，首次拉取约需 GB 量级本地空间。执行前记录官方镜像 ID/仓库 digest、上游源码提交和数据文件 SHA256，再冻结本次所有组使用的镜像 ID。

本机预检发现该镜像停留在环境设置提交，适配器在自己创建的容器中 reset 到任务指定 base commit 后冻结为派生镜像，不修改已有镜像或宿主源码。镜像中的 tomlkit 0.13.2 还会使两个既有 TOML 配置测试失败；可用下面的显式兼容参数固定为源码声明允许的 tomlkit 0.10.1，wheel SHA256 随计划记录，所有组和独立验收使用同一派生环境。这意味着环境不是官方镜像的原封不动执行，报告必须保留该偏差。

官方 eval 脚本在测试结束后尝试 checkout 本来不存在的新测试文件，导致外层退出 1。适配器只识别本任务那个精确的末尾 pathspec 错误：保留错误和退出码，同时仍要求完整测试输出标记、所有官方预期 ID 可见、上游解析器成功且无超时/截断。其他异常退出不按这条规则放行。不会改写官方测试或评分源码。

评分模块通过其原始包路径加载，跳过会额外导入 Docker SDK/datasets 的包初始化；实际 grading/constants/log-parser 源文件保持不变。只需小型可选依赖 unidiff，不安装模型推理框架。

## 先做本地准备与验收

以下从仓库根目录执行。首次使用时下载公开数据、上游评分源码和官方镜像；不上传源码/日志，不调用模型，不读取 API Key。Docker Desktop 需运行 Linux 容器。

```powershell
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$V04 = (Resolve-Path .\tokenguard_v04).Path
& $Python -m pip install unidiff==0.7.5
& $Python -X utf8 "$V04\benchmark\swebench_runner.py" fetch `
  --output "$V04\reports\quality-data\instance.json"

# 若目录已有源码，不重复 clone；使用干净的明确版本。
git clone --depth 1 https://github.com/SWE-bench/SWE-bench.git "$V04\reports\quality-data\harness"
git -C "$V04\reports\quality-data\harness" fetch --depth 1 origin 02e7a74ffd0b707aab73d203fe87bdc7c76afc8e
git -C "$V04\reports\quality-data\harness" checkout --detach 02e7a74ffd0b707aab73d203fe87bdc7c76afc8e

docker pull swebench/sweb.eval.x86_64.pylint-dev_1776_pylint-6528:latest
& $Python -m pip download --no-deps --only-binary=:all: `
  --dest "$V04\reports\quality-data\wheels" tomlkit==0.10.1

$Exp = (& $Python -X utf8 "$V04\benchmark\swebench_runner.py" prepare `
  --instance "$V04\reports\quality-data\instance.json" `
  --harness "$V04\reports\quality-data\harness" `
  --output "$V04\reports\q" --rounds 3 `
  --compat-wheel "$V04\reports\quality-data\wheels\tomlkit-0.10.1-py3-none-any.whl").Trim()
if ($LASTEXITCODE -ne 0) { throw 'Preparation failed' }
& $Python -X utf8 "$V04\benchmark\swebench_runner.py" preflight $Exp
if ($LASTEXITCODE -ne 0) { throw 'Inspect original/gold/visible test evidence; do not start paid sessions' }

# 默认仅预览，不启动 Agent。
& $Python -X utf8 "$V04\benchmark\swebench_runner.py" run $Exp
```

预检必须证明：原始源码在 4 个目标测试全部失败，171 个回归项保持；gold patch 通过官方目标验收；未修改的可见测试可运行且输出确实达到现有压缩条件。这些是独立本地验收，不是模型修复能力证据。

## 真实执行与报告

真实运行需要单独授权及 `--execute --allow-paid --max-paid-sessions 6`。默认每会话传入 max-turns 20，墙钟超时 600 秒。准备新实验时可用 `--max-turns 40` 等明确参数统一改变两组预算；已经启动的实验不得修改预算或复用会话。会话数量限制不是 API 请求数或账单上限；max-turns 的宿主计数也没有独立计费认证。执行过的实验不能重复启动。

授权后的命令形式为：

```powershell
& $Python -X utf8 "$V04\benchmark\swebench_runner.py" run $Exp `
  --execute --allow-paid --max-paid-sessions 6
& $Python -X utf8 "$V04\benchmark\swebench_runner.py" report $Exp
& $Python -X utf8 "$V04\benchmark\quality_analysis.py" $Exp
Get-Content "$Exp\quality-report.md" -Encoding UTF8
```

生成 Markdown/JSON/CSV。未运行项仍在报告中；不能只保留成功样本。完整模型轨迹、候选补丁、独立原始测试日志、压缩/恢复审计留在忽略的 reports 目录，不提交公开仓库。

适配器升级后仍可读取已有验收报告；再次启动或 preflight 则继续要求准备时的运行代码完全一致。实际执行过的适配器/入口源码可单独归档并按计划 SHA256 核对，避免把更新后的实现冒充旧实验执行版本。

Claude 的流式输出用于统计工具调用和校验会话身份；最终 CLI usage 聚合单独标为 unverified。原生供应商 Usage、完整请求覆盖和实际账单不可得时保持 unavailable，不用字符节省或 CLI 费用估计替代。仅在优化组实际压缩且验收完成时，才报告该配对上的质量观察；即使三次都通过，也不能据此宣布普遍能力无损。
