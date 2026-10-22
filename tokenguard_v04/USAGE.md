# 离线 usage 导入与任务配对测量

本模块补齐“真实 usage 数据如何进入实验报告”的流程，不采集 API Key、不联网、不启动模型，也不读取用户会话目录。输入必须是用户明确指定的本地 usage-only 导出文件。

当前已完成实际 Token 的**离线导入与比较能力**。首轮经过授权的真实任务 A/B 已执行，CLI 提供了未独立验证的 usage 汇总；尚无对应的供应商原生完整请求 Usage 和独立账单。随代码提供的全部数值 Fixture 仍为 synthetic，不代表 DeepSeek 实际费用、价格或 Token 节省。

## 为什么缓存口径必须分开

- DeepSeek：`prompt_tokens` 已含缓存命中部分；总 Token 为 prompt + completion。`prompt_cache_hit_tokens` 和 `prompt_tokens_details.cached_tokens` 是同一指标的不同字段，不能重复相加。cache miss 不等于 cache write。
- Anthropic：总输入为 `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`。缺失缓存字段时不擅自按 0 补齐，因此总输入可能 unavailable。
- 推理 Token 若作为 completion 的子项报告，不再次加到输出/总量中。

依据：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)；[Anthropic Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。这些是适配器的统计口径；不代表某个第三方网关一定遵守相同语义。经网关翻译的数据应声明 gateway_export，不能当成原生供应商证据。

## 可重复的本地演示

```powershell
Set-Location C:\src\TokenGuard\tokenguard_v04
$Python = 'C:\src\TokenGuard\.venv\Scripts\python.exe'
& $Python .\benchmark\usage_demo.py
```

这仅解析本地 Fixture 并生成 JSON/CSV/Markdown，不调用模型。它特意构造“总 Token 从 3300 降到 2700、缓存命中率从 90% 降到 0%、虚构账单金额从 0.0100 升至 0.0200”的反例。金额为任意合成测试值，不能作为价格或成本模型。报告 status=synthetic_only，actual_billed_cost=unavailable。

## 支持的源格式

`--format deepseek`：最终 Chat Completions 响应对象，或这些对象组成的 JSON 数组/JSONL。读取 id、model 和 usage 允许列表；不保存 choices、消息正文、未知字段或原始响应副本。

可接收已转成 JSONL 的流式 chunk：usage=null 的非最终 chunk 被跳过，带 usage 的 chunk 必须有 finish_reason。相同请求 ID 的相同 usage 只计一次；同 ID 不同 usage 会拒绝，不能把部分流式累计快照相加。

`--format anthropic`：最终原生 message 响应（非空 stop_reason），有 id、model、usage。暂不合并 message_start/message_delta，不导入完整 Claude transcript，也不支持 SSE `data:` 文本。需要先由可信采集方导出最终 usage；TokenGuard 不猜测如何拼接。

文件上限 32 MiB；JSONL 单行上限 2 MiB。拒绝非整数/负数/布尔 Token 计数、冲突总量、重复 JSON key 和 NaN/Infinity。缺失可选字段为 unavailable，而不是零。声明完整任务时，去重后的请求数必须等于 expected_request_count；但完整覆盖仍是调用方声明，工具不能独立确认是否漏了整个请求。

## 每个 arm 的实验清单

`--manifest` 是版本 1 的 JSON，字段可参考 `benchmark/usage_fixtures/baseline-manifest.json`。真实实验应重新准备，不得仅把 synthetic 改名而沿用合成数值。

|字段|含义|
|---|---|
|schema_version|整数 1|
|experiment_id / task_id / pair_id|实验、任务及配对轮次；同一 pair 恰好两个 arm|
|arm|baseline 或 optimized|
|baseline_mode|disabled_hook 或 no_hook；通用旧导出缺失时为 unspecified。两组必须一致，不同模式分别汇总|
|session_id|每个 arm 独立、全新会话 ID|
|provider / model / agent_version|实际提供商、模型与 Agent 版本|
|prompt_sha256|同一任务 Prompt 的 SHA256|
|initial_snapshot_sha256|同一初始项目快照的 SHA256|
|config_sha256|共享实验设计/运行配置的 SHA256；可包含基线模式及两组干预配置文件哈希，同一配对必须一致。不要把密钥纳入配置导出|
|fresh_session|是否确认为全新会话，不可用 resume 冒充|
|task_success / tests_passed / tests_unchanged|最终代码与测试独立验收结果；任何一项失败都不纳入成功节省汇总|
|coverage|complete_task 或 partial|
|expected_request_count|完整任务的预期独立请求数；partial 可为 null|
|evidence_kind|synthetic / provider_export / gateway_export / manual|
|elapsed_ms / tool_calls / recovery_count|任务级测量，非负整数；未知用 null，不用 0 冒充|

这些 Hash 和验收字段目前由实验采集方提供，导入器校验格式和配对相等，不会独立重跑用户工程或认证其来源。当前 Mock runner 的快照/pytest 自动验收与本模块是不同证据，不会自动把 Mock 结果提升成真实 usage。

## 导入命令

```powershell
# 路径示例需要替换为你明确准备的 usage-only 导出文件及对应清单。
& $Python .\tg.py usage import `
  --source .\exports\baseline-responses.jsonl `
  --manifest .\exports\baseline-manifest.json `
  --format deepseek --output .\reports\usage
```

标准输出为新生成的 `usage-UUID.json` 路径，唯一名称，不覆盖已有结果。输出只保存标准化指标、允许列表清单、来源文件 SHA256 和声明的证据类型，不复制原文。无凭据发现、远程请求、全局 Claude 设置修改或自动扫描。

导入 JSON 中：

- imported_request_totals：文件中完整请求的指标之和，可能只是任务的一部分。
- complete_task_totals：只有 coverage=complete_task 且独立请求数吻合才提供；依然注明完整性由调用方声明。
- 缓存比例：cache read / 全部输入，不是“请求命中次数”。任何一项未知或分母为 0 都 unavailable。
- 所有标准化值代表导出文件所报告的 usage，不是字符启发式估算；原始响应/覆盖率真实性没有被独立认证。

## 真实费用证据独立导入

默认账单费用 unavailable，不读取或采用 Claude `total_cost_usd`、未知模型价格估算，也不根据 Token 自动乘价格。

可额外指定 `--billing`，内容为经过调用方从供应商账单整理的 session_total JSON。必须含 schema_version=1、scope=session_total、匹配的 session_id、完整且精确匹配的 request_ids、currency（USD/CNY 等三位大写代码）、amount（非负十进制字符串）、source、evidence_reference。

source 只接受 provider_billing_export 或 invoice；synthetic 清单只接受 synthetic_fixture，并永远不会提供 actual_billed_cost。source=claude_cli_estimate 等会被拒绝。参考字段只保留 SHA256，不将任意引用正文复制到报告。

只有 evidence_kind=provider_export 且提供了对应供应商/发票账单记录时，才提供声明来源的 actual_billed_cost 对象，仍标记 caller_supplied / not independently verified。网关 usage 的来源无法据此被认证，因此 actual_billed_cost 继续 unavailable；有对应账单时仍可查看 reported_billing。工具不会访问账单网站或验证发票真伪。

## 配对报告

```powershell
# 将两个路径替换为导入命令返回的规范化文件。
& $Python .\tg.py usage compare `
  .\reports\usage\usage-BASELINE.json `
  .\reports\usage\usage-OPTIMIZED.json `
  --output .\reports\usage-comparisons
```

可一次传入多轮的全部规范化文件。按 experiment_id/task_id/pair_id 分组，检查同模型、提供商、Agent 版本、Prompt、初始快照、共享配置、原生 usage 格式和证据类型；拒绝复用会话或跨 arm 共用请求 ID。比较时从逐请求记录重新校验总量，不信任被改写的汇总数字。

缺 arm/重复 arm/配置不匹配为 invalid_pair；任务 usage 不完整为 partial_usage；质量下降为 quality_regression。报告保留问题，不将这些 pair 纳入成功任务汇总。不同币种不计算费用比例；零基线不输出除零结果。所有排除原因明确展示。

报告同时提供输入、输出、缓存读/写、推理子项、总量、缓存比例百分点变化、耗时、工具调用、恢复次数和账单金额差异。reduction_ratio=(baseline-optimized)/baseline，负数代表增加。optimized_minus_baseline 保留原始绝对差。费用使用 Decimal 计算，不按二进制浮点累计金额。

多轮聚合只纳入满足配对、完整覆盖与双方质量验收的 pair；按实验/提供商/模型/版本/配置/usage 格式/证据类型分组，合成数据与真实声明数据不会混合。字段有未知值时不输出掩盖缺失的部分合计。当前没有统计显著性或因果节省结论。

比较会写新目录的 Markdown/JSON/CSV，成功纳入全部 pair 时退出 0；有 pair 被排除时仍保留可审查报告并退出 1；输入文件本身非法则退出 2。CSV 防止以公式开头的文本标签被直接解释为电子表格公式。

## 当前真实验证进度

已有一次真实 Claude 会话的 Hook 输出替换可见性证据，也完成了一轮真实任务 A/B（两组代码通过验收，优化组输出较短，实际压缩 0 次），但没有供应商原生的完整请求 Usage 和独立账单。`tg.py stats` 仍只报告 Hook 字符/本地估算与恢复统计，不会自动加载离线 usage 示例，也不会将其数值混入真实会话。后续真实 A/B 需另外授权、相同初始快照和新会话，以及可信的 usage/账单采集来源。
