# unittest 输出保护与真实本地验证

unittest 仅聚合格式明确的 verbose 成功记录。默认报告写入 stderr，Hook 保留 stderr；只有报告实际进入 stdout（例如明确使用 `2>&1`）时才可能压缩。不要把 stderr 原样保留当作压缩失败或节省。

## 识别与回退

- 识别 Python 测试方法与点分身份，兼容 Python 3.10 和后续版本的常见显示格式；自定义显示名称保留。
- 只隐藏完整的 `... ok` 成功记录。跳过、预期失败、意外成功及未知行保持原样。
- 从分隔线、测试计数汇总、最终 OK/FAILED 或失败详情开始，整个报告尾部保持原样，即使后续文字看起来像成功记录。
- 发现未完成的测试标题、文档说明标题、独立状态行或插入实时输出的已知标题，整段透传，诊断状态为 `ambiguous_unittest_output`。
- 原有 Traceback/Git diff、最小字符数和加入恢复元数据后的净字符阈值继续生效；`TOKENGUARD_ENABLED=0` 仍可关闭全部压缩。

真实反例：unittest 在有文档说明的方法中输出两行：

```text
test_documented (test_contract.ContractCase.test_documented)
IMPORTANT_DOC (configuration missing) ... ok
```

旧规则会把第二行当成功记录隐藏。现在识别第一行的未完成标题，透传整段。实时输出可产生类似的假成功记录，也使用同一回退规则。

这里只识别常见标准输出格式，不是任意自定义 runner 的语法解析器，也不是日志来源认证。如果外部输出完全伪装为正常进度且没有可见歧义，无法仅凭文本证明其来源。语义测试不证明真实模型能力无损。

歧义透传会降低某些日志的压缩覆盖率。字符减少仍需计算恢复提示开销；本地检查不能据此推算任务 Token、缓存或实际费用。

## 可重复评测

在仓库根目录执行，使用已有解释器：

```powershell
$Python = 'D:\tokenguard-benchmark\.venv\Scripts\python.exe'
Push-Location .\tokenguard_v04
& $Python -X utf8 -m pytest tests/test_unittest_safety.py tests/test_unittest_contracts.py -q
& $Python -X utf8 .\benchmark\unittest_contracts.py
Pop-Location
```

`benchmark/unittest_contracts.py` 仅使用标准库，实际启动本地 unittest、手动 Hook 和 tg.py 子进程，不启动 Claude、不调用模型、不读取用户历史 Artifact。每次生成独立数据和报告目录，输出 JSON/CSV/Markdown，不覆盖旧报告。可用 `--output` 指定父目录；`--implementation` 只应用于自己确认可信的本地 v0.4 源码，它会执行指定代码，不是恶意代码沙箱。

13 类真实 Fixture 包含普通成功、跳过/预期失败/意外成功、断言失败、setup 错误、subTest 失败、缓冲失败输出、文档说明、stderr/stdout 实时输出、报告尾部、警告及未知诊断。12 类显式合并 stdout/stderr 的场景各验证 LF/CRLF/CR 和开关，共 72 项；默认 stderr 场景保留原始流并验证开关，共 2 项。换行变体是对真实输出的显式转换，不冒充各平台原生输出。

Fixture 核对实际退出码、测试执行数、关键标记和最终汇总。独立 oracle 仅允许删除源码中明确生成的 120 个标准成功方法行，要求其他每条物理行按原顺序和重复次数保留；不借用被测实现的识别正则。压缩案例还核对其他工具响应字段、Artifact 字节及实际 show/grep 的有界恢复与审计。每个压缩案例故意运行两次恢复，不代表真实 Agent 的恢复频率或任务开销。

报告绑定目标全部平级 Python 模块、tg.py、评测器和 Fixture 清单 Hash，前后重枚举源码。超时、坏编码、错误退出码、测试数错误和未完成检查保留完整计划分母，并输出失败结果。没有原生宿主接收确认；实际 API 用量、缓存和账单为 `unavailable`。

## 开发证据

首批 41 项构造检查中 38 失败、3 通过，保留了文档/实时输出、报告边界及未知记录的反例。真实输出首轮 56/74：其中部分检查还暴露评测器自身通过普通 Windows 路径读取长 Artifact 失败；该失败记录保留，不能全部称为语义故障。评测器已改用可访问的扩展路径读取自己的合成 Artifact，未修改系统设置或历史数据。

新增 Fixture 证据反例同时确认：缺失最终结果、错误实际测试数不能被标为有效。后续完整验证使用同一份冻结评测器重新比较当前与历史源码，避免混用不同评测条件。

扩展检查还复现既有 pytest 评测器相同的普通 Windows 路径读取问题：108 项中 26 项在读取 Artifact 时失败。已保留该报告，补上独立扩展路径读取及实际长文件名回归；没有修改压缩语义、系统设置或原 Artifact。13 个本地例子在相同深度目录通过，不扩大修改其逻辑。

另对文档说明、实时 stderr、实时 stdout、报告尾部、未知诊断和警告六类归档日志，向当前与历史 `f7cd0de` 传入逐字节相同的原始 stdout/stderr。当前 6/6 通过；历史六类均未通过，其中五类丢失非进度内容，警告一类仅违反新增的整段透传规则。每对记录核对输入 Hash，源码前后不变。它们是固定软件反例，不是六个模型任务，也不能用来估算真实故障率。

冻结后的完整 unittest 评测：当前 74/74，历史 56/74。18 个失败检查包含 15 个内容丢失和 3 个新增透传规则不满足，不将全部都称为信息丢失。相同验证器/Fixture 清单 Hash 的全评测会重新生成实际输出；逐字节相同输入另由上述六类回放保证。当前完整回归 1125 passed、1 skipped、406 subtests passed；跳过项为本机 Windows 符号链接权限不可用，没有新增覆盖率或模型测量。

完整回归、当前/历史 unittest、恢复 67/67、实际 pytest 108/108、手动 Hook、13 个本地例子和三轮 Mock 30/30 期间，92 个 Python 源码/测试文件前后 Hash 一致。历史源码完整导出到隔离目录，没有覆盖旧版本或历史 Artifact。失败报告与最终报告分开保留，本地 stdout 写出成功仍不代表真实 Claude 已消费。
