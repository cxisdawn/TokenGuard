# 报告证据校验与失败保留

报告不能仅凭缓存中的 `success=true` 宣布成功，也不能把缺失测量写成零。本阶段检查从任务验收、压缩审计到 usage/账单比较的计算链路；使用故障注入、固定 Fixture 和实际本地子进程，不启动新模型会话。

## 任务结果绑定

通用任务验收保存实际复制到隔离测试目录的白名单文件 SHA256。报告核对当前候选文件、保护文件与验收文件哈希，并检查进程结束、超时、截断和阶段错误。已测得失败保持 false；缺少可信证据为 null。原先记录的结果另存 reported_task_success，不回写历史试验。

SWE-bench 分析把当前候选重建的补丁与已保存的 evaluation/candidate.patch 对照，检查归档 grade.json 和试验内评分的一致性及字段类型。文件缺失、内容变化或布尔值被整数冒充时，不能认证当前代码成绩。reported_code_resolved 保留原声明；独立有效的代码成绩与 Agent 严格完成分开。评分文件和候选文件加入跨实验比较前后 Hash 检查。

这些检查防止误用不匹配的本地记录，不是数字签名，也不会重新运行官方评分器。若第三方协调改写所有证据，哈希一致不能认证来源。旧通用任务缺少受测文件绑定时需明确显示未知；不要补写旧 Hash 将它伪装成新格式运行。

## 审计与计量

分析重新读取实际审计，并核对缓存中的压缩、恢复、字符及 Token 估算计量。异常、缺失或冲突的证据不能变成正常收益。独立代码验收事实可以保留，严格完成与压缩比较资格另行降级。

## 离线 Mock 与基线研究

- 固定日志 Mock 提前保存完整计划，按奇数轮 AB、偶数轮 BA 执行，每项复制相同初始 Python 文件并使用新进程/会话 ID。
- 超时、非零退出、截断、非法结果或审计不完整均保存结果并停止后续启动。未执行项保持 not_run，基础设施失败与任务实际失败分开。
- planned_trials、attempted_trials、completed_trials 分开。全部计划配对通过任务、失败信息、文件及证据检查，才提供整个场景的字符变化；不挑成功子集冒充全实验结果。大 Artifact 案例还必须明确通过尾部恢复检查。
- 基线模式研究在准备、执行或报告异常时仍导出完整计划分母及错误类型；缺失计量保持 null/CSV 与 Markdown 中的 unavailable，并列出测量覆盖数。
- 工具级估算是各 arm 内原始/可见文本的启发式对照，恢复输出同时进入两边；不是任务 Token 或 A/B Token 结论。配对字符变化另外比较两组实际可见总字符，包括恢复。

本地 Mock 的修复决策是固定脚本。直接调用 handle 的 Fixture 不测量原生 Hook 管道交付，也不证明宿主消费。子进程输出有读取上限，超限保留失败状态；报告落盘失败、进程被外部强制结束或磁盘耗尽仍可能阻止最终导出，已写计划可用于追溯。

## Usage 与账单

流式 usage=null 仍建立已观察请求；完整任务必须把每个已观察请求对齐到最终 usage。partial 保留已知请求小计与未终结 ID，任务总量未知。规范化账单保存并重新验证会话及精确请求集合，不能把另一 arm 的金额直接移植。旧无绑定账单费用未知，旧流式证据缺失时要求重新导入原文件。详细口径及兼容规则见 [USAGE.md](USAGE.md)。

导入器无法发现从导出文件完全遗漏的请求，完整覆盖及来源仍由调用方声明。实际 API Token、缓存及账单不能从字符减少反推。本轮不改变 [真实编码实验的负面结果](QUALITY_PILOT_RESULTS.md)。

## 本地复现

```powershell
Set-Location C:\src\TokenGuard\tokenguard_v04
$Python = 'C:\src\TokenGuard\.venv\Scripts\python.exe'
& $Python -X utf8 -m pytest tests/test_report_integrity.py tests/test_usage_integrity.py tests/test_benchmark_evidence.py -q
& $Python -X utf8 .\benchmark\run.py --rounds 3
& $Python -X utf8 .\benchmark\baseline_demo.py --rounds 1
& $Python -X utf8 .\benchmark\usage_demo.py
```

路径按实际 checkout 调整。报告写入新的、Git 忽略的目录；不覆盖历史结果，不调用付费模型。完整回归及跨平台验证记录见 [验证范围](../docs/VALIDATION.md)。
