# 构建与 pip 的本地输出验证

`benchmark/build_pip_contracts.py` 执行本机 Python 编译、离线 pip 安装，以及已安装 pip 的下载进度渲染器。输出经过实际 Hook 和 `tg.py` 子进程核对，不启动模型，不改 Claude 配置。

在仓库根目录的 PowerShell 中执行：

```powershell
$Python = 'D:\tokenguard-benchmark\.venv\Scripts\python.exe'
& $Python -X utf8 .\tokenguard_v04\benchmark\build_pip_contracts.py
```

命令返回报告目录及通过数；失败时退出码为 1。报告包含 JSON、CSV、Markdown、原始进程 stdout/stderr、调用参数、退出码、工具版本和 Hash。不要提交本机生成的 `reports/`。`--implementation` 可指定另一份可信的本地 v0.4 源码；该选项会执行指定代码。

## 验证范围

| 样例 | 输出来源 | 预期行为 |
| --- | --- | --- |
| Python 编译成功 | `python -m compileall -f` 编译 90 个临时文件，核验实际 `.pyc` | 无相邻重复，原样透传 |
| Python 编译失败 | 90 个有效文件加一个语法错误文件 | 完整保留编译位置、源码、错误及其他输出 |
| 混合编译失败 | 上述真实失败日志前加 200 条明确标记的合成重复警告 | 折叠重复警告；独立展开后等于原文，编译诊断保留 |
| 彩色重复错误 | 从真实编译日志取出 SyntaxError 记录，在 Error 词内部插入 SGR 颜色并重复 200 次；属于明确标记的合成变换 | 每条严重记录原样保留，不折叠次数 |
| 警告加彩色错误 | 200 条合成警告、3 条上述彩色错误及真实编译日志 | 只折叠警告，错误次数和字节保留 |
| pip 安装成功 | 标准库生成合法纯 Python wheel，真实安装到临时 target 并核对文件 | 安装结果原样透传 |
| pip 安装失败 | `--no-index` 请求一个不存在的包，默认 stderr 与显式合并分别检查 | 错误原样保留 |
| pip 进度 | 已安装 pip 的真实 renderer 重复渲染 100 个完成帧，输入为内存字节 | 聚合已识别的完整进度记录 |
| 进度加安装错误 | renderer 输出与真实离线安装错误组合，分别放入 stdout/stderr | 聚合纯进度，保留错误及响应其他字段 |
| 同一行进度加错误 | 把真实错误拼接到各进度行，明确属于混合样例 | 原样透传，不能按进度前缀删掉诊断 |

共 12 类：11 类乘三种换行和开关为 66 项；默认 stderr 场景保留原始换行，检查开关共 2 项，总计 **68 项**。LF/CRLF/CR 是对归档 stdout 的显式转换；stderr 保持原值。68 项不是 68 个独立编码任务，也不是 68 次独立真实下载。彩色错误并非 compileall 原生输出，也不表示已验证某个实际编译器的配色行为。

pip 使用 `--isolated --disable-pip-version-check --no-index --no-deps --no-compile --no-cache-dir --target TEMP`，不查询索引、不下载依赖、不写入当前虚拟环境的包目录。wheel 用标准库 `zipfile` 生成，包含 METADATA/WHEEL/RECORD 及记录 Hash，无需安装 wheel/setuptools。真实 renderer 测试没有执行网络下载，也不代表终端实时刷新、所有 pip 版本或所有平台进度格式已验证。不支持的 renderer 版本会明确失败，不能跳过后计为通过。

每个压缩样例验证：净字符阈值包含恢复提示，非 stdout 字段不变，Artifact 字节相等，一行 show 与一次有界 grep 成功，会话审计的压缩/恢复/返回字符数匹配。固定执行的恢复用于验证可取回性，不测量 Agent 的恢复决策或效率。

评测不导入生产压缩器的识别正则。pip 可删除行仅来自专门的 renderer 捕获；其他物理记录按原序列和出现次数核对。构建重复记录按公开 RLE 标记独立展开。超时、坏 UTF-8、错误退出码、缺少实际产物或实现源码均保留完整计划分母并判失败，零样例不能通过。运行前后核对被执行源码及评测器自身 Hash。

回归在独立临时副本中注入四种缺陷：把全行匹配改成前缀匹配、清空 stderr、损坏审计字符数，以及恢复旧版未剥除颜色的严重词识别。评测器必须拒绝这些副本；关闭压缩的分支仍保留原输出。前三种是主动注入的负对照；第四种重现已修复的颜色识别问题。

本轮另修正 `compileall` / `py_compile` 命令的审计分类：原先记录为 generic，现在记录为 build；二者的重复压缩行为不变。

## 颜色与异常边界

例如 `ER<SGR颜色码>ROR` 在终端可显示为 ERROR。旧重复策略直接匹配含控制码的原文，会保留第一条并把其余严重记录错误标记为“非关键重复行”；原始 Artifact 仍完整。现仅在识别严重词时移除已有 ANSI CSI 匹配器识别的序列，决定保留后仍输出原记录，颜色、Unicode 和换行不变。普通重复警告仍可压缩，generic 命令的 `repeated_warnings` 标签也使用同一去码后的文本。

这不是完整终端模拟器：没有新增 OSC 超链接、光标覆盖或退格渲染验证，也没有证明所有彩色日志语义都可识别。测试覆盖九类严重词/堆栈位置、两类命令和三种物理换行，以及普通输出与严重记录混合的边界。

`tests/test_hook_fallbacks.py` 额外验证七类合成可压缩日志，每类分别启动真正的手动 Hook 子进程，检查启用、关闭、data 路径为文件、artifacts 路径为文件、audit 路径为目录，共 35 项。启用分支证明样例本来满足压缩条件，避免把“不够长”误当关闭或故障回退成功。关闭不生成 Artifact/压缩审计；诊断显式开启时仍记录 disabled。文件系统故障时 Hook stdout 为空、进程正常退出、stderr 仅含异常类型，既有哨兵文件保持原字节。审计失败可能留下完整但未交付的 Artifact，不自动删除。

这些子进程是本地协议验证；空 stdout 表示 Hook 不请求替换，未独立观察真实 Claude 宿主在故障时的行为。没有修改开关契约：全局关闭值仍为 `TOKENGUARD_ENABLED=0`。

## 结论限制

本评测覆盖 Python 字节编译，没有验证 GCC、Clang、MSBuild 或任意构建工具；没有新的真实 Claude 集成、编码能力 A/B 或统计显著性证据。实际 API usage、缓存和账单仍是 unavailable。历史受控编码实验的负面结果保留，软件回归通过不能替代能力无损验证。
