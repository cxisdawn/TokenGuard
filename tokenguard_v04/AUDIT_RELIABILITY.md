# 审计完整性与 Hook 输出验证

本轮只运行本地进程和固定 Fixture，不新增付费模型会话。目标是让压缩、恢复和评测报告准确表达已经观察到的事实；这些修复不证明编码能力无损，也不提供实际费用节省率。

## 三种证据不能混用

| 状态 | 已验证的事实 | 不能据此推断 |
| --- | --- | --- |
| `compression` / `prepared_candidate_not_host_confirmed` | 原文及候选审计已写入，替换 JSON 可序列化 | stdout 写出成功 |
| `replacement_delivery` / `stdout_write_succeeded` | Hook 的 stdout 写入及 flush 返回成功 | Claude 已接收、使用或只保留替换内容 |
| 宿主会话中的精确 Artifact/压缩 ID 匹配 | 对应宿主记录包含该次替换标记 | 所有会话稳定触发、编码质量无损或真实费用下降 |

`stdout_write_failed` 是独立失败记录。旧版 `emitted_candidate_not_host_confirmed` 不会追认为写出成功。直接调用 Python `handle()` 仅准备返回对象，不执行 stdout 写入，因此没有 delivery receipt；受控初始日志的手动种子还需由自己的交付凭证验证。

为保持已有分析接口，`compressions` 仍等于候选数量。新增 `prepared_candidates`、`confirmed_stdout_deliveries`、`delivery_failures` 和 `delivery_unknown` 明确区分状态。`saved_chars` 是候选文本的字符差，不是已经进入模型的净节省量。缺少 receipt 保留 unknown；`delivery_data_available` 与审计记录完整性 `data_available` 分列。

Hook 在保存 Artifact 和候选审计之前序列化完整响应，拒绝 NaN/Infinity 和非 JSON 额外字段；身份字段只接受不含 ASCII 控制符的 1..256 字符字符串，缺失身份仍可为空。诊断不复制无效身份对象，不打印私密异常消息。输入、压缩或写出前存储失败时不输出替换 JSON。

stdout 写出后发生错误有物理限制：已经进入管道的部分 JSON 无法撤回。代码显式检查短写、flush 失败和关闭管道，尽力记录失败并避免解释器退出时再次报错。写出成功但 receipt 写入失败时，也无法撤回已经写出的候选；此时统计为交付未知，不能标成成功或假装从未写出。进程被强制终止、主机掉电或磁盘损坏仍可能留下未知状态。

## 损坏审计的处理

JSONL 逐行以 UTF-8 解码，只在文件头接受 BOM；每行最多 256 KiB，超长行有界丢弃读取并计为坏行。原文件的损坏字节不删除、不重写。追加时使用有超时的系统文件锁，在缺少末尾换行时补充边界，随后写入新事件。锁由操作系统释放；锁文件保留。该锁协调 TokenGuard 写入者，不保证其他未遵守锁的程序并发改写安全，也没有断电持久化承诺。

在统计及原文归属前验证 schema。负数、布尔值、非有限数、超出有符号 64 位范围的计量和无效字段不参与算术。完全重复的候选 ID 去重并报告重复；同 ID 内容冲突时隔离整组，不随便选择一条。新恢复事件带独立 event_id；没有事件 ID 的历史恢复即使内容相同，也保留为多次读取。

缺少压缩 ID 的恢复不会通过 `None == None` 关联。明确给出 session、experiment、artifact 时必须与候选一致。同一 Artifact 无归属参数的读取始终记为未归属，不猜测调用者；需要统计到具体会话时传 `--compression-id`。损坏审计不阻断完好原文的无归属读取；显式指定无效或冲突的压缩身份会失败，避免错误归属。

旧恢复记录如果有唯一合法压缩 ID，却缺少上述上下文字段，则只在内存统计视图中从该候选补齐缺失字段，并标记 `derived_context_fields`；显式提供的不一致值不会被覆盖。全局、会话、实验及 source 过滤应得到一致归属，原始 JSONL 和输入对象保持不变。

报告分别给出 `malformed_rows`、`invalid_rows`、`duplicate_rows`、`conflicting_rows`。发现这些问题时 `data_available=false`，仍展示有效观察，但这些值不代表完整总量。无法识别归属的坏行可能来自任何会话，因此完整性告警是整份审计文件级别，不会被 session/source 过滤隐藏。

Token 估算缺失显示 null/unavailable，并提供覆盖计数；历史恢复缺少扫描量或耗时，也不补成零。`stats --json` 默认只返回最近 20 条候选，`--limit-records 0..100` 控制返回条数，所有范围内的有效记录仍参与汇总。该限制控制输出大小，不限制历史文件扫描总量或内存；目前没有审计轮转和历史索引功能。

## 评测证据

质量执行器及通用任务执行器遇到不完整审计，会保留已经取得的独立代码验收结果，记录审计阶段错误，并停止后续会话。没有执行的试验仍保留在计划分母中。只读质量分析复核当前审计，不依赖旧缓存中的“完整”标记。审计缺失不能解释为模型失败，也不能当成完整的零压缩测量。

当前真实编码实验仍以 [已有负面结果](QUALITY_PILOT_RESULTS.md) 为准；本轮没有重新测量 Claude 的工具输出消费、DeepSeek API Usage 或账单。

## 本地复现

在仓库根目录设置已有 Python 环境，然后运行：

```powershell
$Python = 'D:\tokenguard-benchmark\.venv\Scripts\python.exe'
Push-Location .\tokenguard_v04
& $Python -X utf8 -m pytest -q tests/test_audit_integrity.py tests/test_hook_delivery.py tests/test_audit_cli.py
& $Python -X utf8 -m pytest -q
& $Python -X utf8 .\verify_hook.py
& $Python -X utf8 .\benchmark\pytest_contracts.py
& $Python -X utf8 .\benchmark\protocol_demo.py
Pop-Location
```

以上故障注入使用临时目录：坏 UTF-8、截断尾行、缺失/错误类型、重复/冲突身份、跨会话错误归属、多进程写入和锁超时，以及真实关闭 stdout 管道、短写、部分写入、flush 与二次异常。实际 pytest 契约额外验证失败内容、原文字节和恢复结果。完整结果见 [验证范围](../docs/VALIDATION.md)。

日常查询：

```powershell
& $Python .\tokenguard_v04\tg.py stats --session '实际会话ID'
& $Python .\tokenguard_v04\tg.py stats --experiment '实际实验ID' --source claude_hook --limit-records 5 --json
& $Python .\tokenguard_v04\tg.py diagnostics --session '实际会话ID' --limit 5
```

这些查询只读取本地记录，不启动模型。`data_available=false` 时先保留原文件和报告排查，不清空历史来制造健康统计。
