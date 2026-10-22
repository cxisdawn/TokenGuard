# 用具体例子验证压缩与恢复

运行这些例子不调用模型，不产生模型 API 请求，不启动 Claude。脚本实际执行 Hook 和 `tg.py` 子进程，检查前后文本、响应字段、Artifact 和恢复审计；这属于本地协议验证，不能代替真实 Claude 接入或完整任务费用实验。

## 运行

在仓库根目录的 PowerShell 中，使用已安装开发依赖的 Python：

```powershell
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $Python .\tokenguard_v04\benchmark\examples.py
```

如果已经位于 `tokenguard_v04`，执行 `& $Python .\benchmark\examples.py`。

每次生成独立 `reports/examples/examples-.../`，最后打印 `report.md` 路径，同时生成 JSON/CSV。原文和审计保存在本次报告内的 `tg-data`，不改真实用户审计、原 Fixture 或现有 Artifact。生成内容被 Git 忽略，不随源码发布。任一检查失败即以非零状态退出。

## 实际验证了哪些例子

下表是一轮 Windows 本地结果；Python、pytest 版本、临时路径和恢复指引中的脚本路径会影响字符数，重跑应以新报告为准。输出字符包含 Artifact/恢复指引，不能换算成真实任务 Token 或账单节省。

| 例子 | 数据来源 | 原 stdout 字符 | 最终 stdout 字符 | 验证结果 |
| --- | --- | ---: | ---: | --- |
| pytest 修复前 | 实际运行 504 项测试 | 42807 | 2750 | 3 failed、501 passed；完整失败段落保留 |
| pytest 修复后 | 实际运行同一组 504 项测试 | 41287 | 922 | 504 passed，测试文件未改 |
| stderr Traceback | 实际 Python 异常 | 18200 | 481 | 压缩重复 stdout；异常 stderr 和退出码 1 原样保留 |
| pytest 混合状态 | 固定 Fixture | 21243 | 812 | FAILED、ERROR、SKIPPED、未知插件提示及失败详情保留 |
| 构建警告与错误 | 固定 Fixture | 41172 | 668 | 合并 1000 行重复警告，3 行相同 error 全保留 |
| unittest 失败 | 固定 Fixture | 15132 | 609 | 合并 400 条 ok，FAIL、断言和最终汇总保留 |
| pip 进度 | 固定 Fixture，无安装/联网 | 20592 | 550 | 合并进度，依赖版本、安装结果、警告保留 |
| 文件名搜索 | 固定 Fixture，无扫描用户文件 | 19000 | 832 | 保留前 20 条；隐藏路径可按关键词恢复 |
| Git diff | 固定 Fixture | 15079 | 15079 | 逐字符相等，安全透传 |
| stdout Traceback | 固定 Fixture | 9246 | 9246 | 逐字符相等，安全透传 |
| 短 pytest 汇总 | 固定 Fixture | 18 | 18 | below_threshold，安全透传 |
| 无最终汇总的 pytest | 固定 Fixture | 20890 | 20890 | incomplete_pytest，安全透传 |
| 不重复的未知格式 | 固定 Fixture | 23500 | 23500 | insufficient_saving，安全透传 |

pytest 案例先复制公开的 `benchmark/fixture` 到短临时目录，再运行 `pytest -v`。修复只把副本中的 `a // b` 换成 `a / b`；通过 JUnit 检查每次都执行 504 项、没有错误或跳过，修复前恰有 3 项失败、修复后没有失败。原始源码和测试按字节核对未改。

## 例一：压缩成功记录，保留定位 Bug 的信息

原日志含 501 条 PASSED，以及 3 个小数除法失败。压缩结果的相关片段如下，省略号仅用于此文档展示；完整失败段落在实际输出中逐字符核对保留：

```text
[TokenGuard: 500 pytest PASSED lines hidden; original retrievable]
test_arithmetic.py::test_fraction[3-2-1.5] FAILED ...
test_arithmetic.py::test_fraction[-3-2--1.5] FAILED ...
test_arithmetic.py::test_fraction[1-4-0.25] FAILED ...
[TokenGuard: 1 pytest PASSED lines hidden; original retrievable]
...
>       assert divide(a,b) == expected
E       assert 1 == 1.5
E        +  where 1 = divide(3, 2)
...
3 failed, 501 passed in ...s
```

脚本比较从 `FAILURES` 标题到最后汇总的整段原文，避免只检查 FAILED 关键词而漏掉断言、参数、文件位置。修复后另运行一遍，确认 504 项全部通过。

## 例二：相同的错误也不能当重复噪声删掉

构建 Fixture 的输出包含 1000 行相同 warning、3 行相同 error 和未知提示。压缩后主体为：

```text
src/legacy.c:12: warning: deprecated API
[TokenGuard: 999 identical adjacent non-critical lines hidden]
src/main.c:99: error: TG_UNDEFINED_SYMBOL
src/main.c:99: error: TG_UNDEFINED_SYMBOL
src/main.c:99: error: TG_UNDEFINED_SYMBOL
BUILD FAILED: exit 1
UNKNOWN_BUILD_NOTE: keep
```

脚本检查 error 精确出现 3 次、失败结束标记与未知提示仍在，不把重复错误误压缩。

## 例三：恢复被隐藏的那一行

文件名搜索隐藏了后面的 980 条路径。对其 Artifact 搜索 `src/module_0777.py`，限定 `--context 0 --limit 1 --max-chars 500`，实际返回：

```text
778: src/module_0777.py
```

随后用 `show --start 778 --count 1 --max-chars 500` 核对同一行原文。8 个压缩案例各执行一次 grep 和一次 show，共 16 次恢复，本轮总返回 772 字符。每个 Artifact 都与原 stdout 的 UTF-8 字节比较相等，恢复数、返回量及压缩归属也与审计核对。

选择性恢复限制的是返回给 Agent 的文本；行索引首次构建可能完整扫描本地文件，扫描量和 index_build_bytes 单独审计，不应宣传成磁盘从未读取完整文件。

## Windows 修复与边界

这组实际例子复现了 CLI 换行问题：Windows 的默认 stdout 翻译会把已存储的 CRLF 输出为 CRCRLF，审计返回字符与实际输出不一致。`tg.py` 现在明确使用 UTF-8 并关闭换行翻译。新增子进程测试覆盖 LF、CRLF、CR、无末尾换行、混合换行以及中文/重音字符/emoji；即使继承 `PYTHONIOENCODING=ascii`，恢复字节仍符合 UTF-8 契约。

所有例子都检查 `updatedToolOutput` 中 stdout 以外字段完全相等，包括 stderr、退出码和未知嵌套字段。Traceback 位于 stdout 时整体透传；位于 stderr 时保留 stderr，仅对可安全处理的 stdout 运行策略。

这些例子证明上述固定输入上的行为和可恢复性。它们不能证明任意格式都安全，也不能证明真实宿主消费了替换、长期任务质量不降或实际 API 成本下降。真实付费 A/B 仍以 [TASK_EXPERIMENTS.md](TASK_EXPERIMENTS.md) 的证据和授权边界为准。
