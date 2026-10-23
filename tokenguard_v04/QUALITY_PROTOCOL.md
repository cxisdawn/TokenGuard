# 质量测评的执行证据与受控日志模式

2026-10-23 已单独授权并执行一对受控日志真实会话：基线通过，优化组未修复且引入回归。结果和限制见 [QUALITY_PILOT_RESULTS.md](QUALITY_PILOT_RESULTS.md)。下文关于“未追加调用”的叙述为执行器开发和本地准备阶段的历史记录，不代表此后未运行真实实验。

此前 8 个付费会话已经用完授权：有的未发生压缩，全部触及轮数上限。它们不足以回答压缩是否影响代码能力。此次改进执行器和测量方式，**不追加模型调用，不修改压缩阈值、全局设置或 Claude 权限**。

## 两种协议分别回答什么

| 协议 | 初始日志从哪里来 | 如何确认压缩暴露 | 能证明的范围 |
| --- | --- | --- | --- |
| `agent_driven`（默认） | Agent 按 Prompt 自行运行测试，经原生工具响应返回 | 同一会话、实验、Bash tool_use_id 的压缩审计与工具响应中的 Artifact/压缩 ID 匹配 | 原生 Hook 路径上的一次可观察暴露；仍不能证明普遍无损 |
| `controlled_initial_log`（显式选择） | 控制器实际跑一次初始可见测试；全部轮次共享这一份原文 | 优化组经真实本地 Hook 子进程压缩，原文 Artifact 字节核对；启动前再次核对交付文件 Hash | 相同初始测试观察经过压缩后的对照；**不是原生工具响应集成测试** |

两组共享源码、模型、基础任务指令、测试助手、预算和镜像。受控模式中，实际发送的 Prompt 包含初始测试观察，因此两组的序列化 Prompt 不同：基线包含原日志，优化组包含压缩日志和恢复指引。这是明确记录的干预，不宣称二者 Prompt 字节相同。后续工具响应仍分别使用 no_hook / TokenGuard。

不同协议、任务、模型、预算不能合并成一个质量成功率。所有失败、达限和未运行项保留；两组都失败不能支持“能力无损”。达到轮数上限也不会使一份通过独立测试的候选补丁变成正常完成的 Agent 会话，报告分别显示代码 resolved 与严格任务成功。

## 测试凭证

生成的受保护 `pytest.py` 助手仍把候选 `pylint/` 只读挂载到固定 Docker 镜像，容器关闭网络，测试来自镜像，不挂载候选测试文件。

每次真实测试记录独立 JSON、stdout 和 stderr，包含：实验／会话身份、来源（Agent 或控制器）、镜像 ID、参数、测试前后源码 Hash、退出码、超时／截断标记、耗时，以及日志字节数和 SHA256。日志尾部有唯一 `test_run` 标记，用来关联宿主工具响应；两组均支付这段少量元数据的上下文开销，不能将它排除后夸大压缩收益。

“提到了测试命令”与“完成了测试”分开计数。Agent 自行执行协议要求：

1. 两次不同的测试执行，初始测试先于最终测试；一张凭证不能复用两次。
2. 参数为规定的完整可见测试，存在完整 pytest 汇总，本任务为 169 项（包含 xfail），未超时／截断。
3. 测试期间源码 Hash 稳定，分别匹配初始快照和最终候选源码。
4. 测试标记出现在对应 Bash 响应中，工具调用使用规定命令。

退出 1 的完整测试仍算“执行过测试”，不算测试通过。导入错误、缺失日志、错误身份、日志修改、定向 `-k`、不完整汇总、Hash 不匹配等不能满足完整测试证据。路径链接在打开源码前拒绝，单文件／总量受限。

受控模式的初始观察由控制器强制执行，最终候选仍由独立官方隐藏测试验收；Agent 是否按要求执行最终可见测试另行记录。缺少 Agent 测试执行证据不会被伪装成完成执行。报告中的 `comparison_evidence_satisfied` 表示该协议的可比较证据条件，不表示统计显著、因果归因已成立或模型能力无损。

这些是**同一 OS 用户下的本地执行凭证，不是防篡改证明或操作系统安全沙箱**。同用户恶意改写控制文件、模型训练污染及提供商后端路由不在认证范围内。

## 原生压缩与初始观察分开统计

压缩审计增加 `tool_use_id`，不改变 Hook 替换结构。原生计数只匹配 `source=claude_hook`；控制器预处理使用 `source=manual`。初始日志的 Artifact 和压缩归属进入该 trial 的数据目录，使原有 `tg.py grep/show --compression-id ...` 可直接选择性恢复。手动预处理的压缩／恢复次数单列，不重复记为真实 Claude Hook 成功。

`quality-report` 与 `quality-analysis` 均保留每对的证据条件和缺失原因，例如 `compression_not_observed`、`baseline_test_protocol_not_verified`、`pair_incomplete`。旧会话没有新凭证／tool_use_id 时保持未验证，不追补成新协议成功；旧日志和结果不删除。

预检完成后保存其 Hash 和计划 Hash。真实启动前复核原始／gold 判分、可见测试凭证、输入和 Artifact；把 `passed` 手动改成 true、修改日志或交付 Prompt 均不能替代这些检查。代码或配置变化后需要重新 prepare；不续跑已消耗的会话。

## 零模型调用演示

从源码仓库根目录执行（Python 环境需安装 `requirements-dev.txt`）：

```powershell
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $Python -X utf8 .\tokenguard_v04\benchmark\protocol_demo.py
```

演示真实运行 3 次本地 pytest：初始 504 项、3 失败；两组应用相同的固定修改后各 504 项通过。它使用真实 Hook 子进程处理相同初始日志，检查失败信息保留、原文字节、选择性恢复及归属。预期为优化组手动压缩 1 次、恢复 1 次，原生 Hook 压缩 0 次；受控协议证据通过，原生协议证据拒绝。

**修改决策和宿主工具消息是确定性 Mock；pytest、Hook、存储和恢复是真实本地进程。** 这验证测量流程，不是 SWE-bench 得分或模型代码能力证据。报告为独立 `reports/protocol/.../protocol-demo.{json,md,csv}`，不上传。

## 公开中等任务的本地准备

前提环境及固定上游依赖见 [SWE_BENCH_QUALITY.md](SWE_BENCH_QUALITY.md)。已有数据、镜像和评分源码时，不必再次下载。下面只做本地准备、原始／gold 验证和预览，不启动模型：

```powershell
$Python = 'D:\tokenguard-benchmark\.venv\Scripts\python.exe'
$V04 = 'D:\tokenguard-benchmark\tokenguard_v04'
$Exp = (& $Python -X utf8 "$V04\benchmark\swebench_runner.py" prepare `
  --instance "$V04\reports\quality-data\instance.json" `
  --harness "$V04\reports\quality-data\harness" `
  --output "$V04\reports\q" --rounds 1 --max-turns 40 `
  --protocol controlled_initial_log `
  --compat-wheel "$V04\reports\quality-data\wheels\tomlkit-0.10.1-py3-none-any.whl").Trim()
if ($LASTEXITCODE -ne 0) { throw 'Preparation failed' }
& $Python -X utf8 "$V04\benchmark\swebench_runner.py" preflight $Exp
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed; do not launch sessions' }
& $Python -X utf8 "$V04\benchmark\swebench_runner.py" run $Exp
```

原生路径用 `--protocol agent_driven` 单独准备。受控模式若实际没有压缩，预检直接失败；不能通过降阈值或追加重复填充来掩盖。由于原始／gold／可见测试需要本地 Docker，CI 的默认检查只运行不依赖 Docker 的离线测试和演示。

真实执行仍需一份新的、明确的付费范围授权及原有 CLI 双重开关；本轮没有这样的授权，也没有实际启动新 Claude 会话。此前授权不能重复使用。实际提供商 Usage、缓存命中和账单缺失时维持 `unavailable`；字符、局部估算 Token 和 CLI 未验证聚合均不替代整个任务账单。

## 执行中异常保留与停止

批次启动时及**每个会话启动前**复核预检文件、输入、工具代码 Hash 和该会话的初始工作目录。前一会话之后证据变化，会将下一项记为 `blocked_before_launch`，不启动该项，也不从报告分母中删掉它。

每次启动前保存 `agent-started.json`，它只表示一次启动尝试，不能证明提供商请求数或已收费。`started.json` 的 sessions/planned_sessions 表示本批次计划数，不能作为实际计费会话数。工具执行结束后，流式 stdout/stderr 及 `quality-result.json` 留在独立 trial 目录。

控制器捕获到进程、流协议、源码校验、独立评分、审计或凭证异常时，记录 `stage_errors` 的阶段与异常类型，保存能获得的结果，并写入 `execution-stopped.json`，停止后续会话。异常消息不进入摘要，避免把任意日志内容带到公开报告。不会自动重试或复用会话 ID；可先用 `report` 和 `quality_analysis.py` 只读核对，之后另行 prepare 新实验。

损坏的 CLI 流仍可对已生成的合法候选执行独立验收；代码 resolved 与会话正常完成分开记录。若评分器自身失败，则 `evaluation_valid=false`，不能把它当作代码确实失败或通过的有效测量。可选 `usage`、`modelUsage`、`permission_denials` 的类型异常单列 `cli_metadata_warnings`，不使完成的会话记录丢失；缺失权限拒绝计数为 null，不假定为 0。

审计不可读取时，逐次压缩／恢复计数为 null，`data_available=false`。汇总仅累加已有观测，并标记 `compression_data_complete=false`；不能把汇总的已知 0 次解释为整批确定没有压缩。Markdown、JSON 和 CSV 都保留停止原因与缺失证据。分析器遇到损坏日志行会保留独立判分，标记 `analysis_evidence_errors`，不认证该对的压缩影响比较；手动指引的工具回显另列 `manual_guides_echoed_in_tool_results`。

运行下列端到端回归，不需要 Docker、Claude、网络或模型授权：

```powershell
Set-Location D:\tokenguard-benchmark\tokenguard_v04
& 'D:\tokenguard-benchmark\.venv\Scripts\python.exe' -X utf8 `
  -m pytest tests/test_quality_execution.py -q
```

10 项回归使用独立 Python 假 CLI 走实际执行器，运行真实 pytest、Hook、凭证和日志交付流程；独立验收使用受保护的本地 504 项 Fixture 测试，替代官方 Docker 评分器。覆盖原生／受控路径、评分和凭证异常、审计不可用、源码拒绝、进程启动失败、无效流、输入变化阻断下一会话及畸形可选 usage。固定修复与工具消息属于 Mock，**不是真实 Claude 集成或新增 SWE-bench 能力样本**。

多个已有实验的只读分组与配对结果见 [QUALITY_SCORECARD.md](QUALITY_SCORECARD.md)。新命令不改写旧分析文件，不合并不同预算／协议，也不把证据子集当作全部样本。
