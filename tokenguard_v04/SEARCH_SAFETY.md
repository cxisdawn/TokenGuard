# 文件搜索压缩的保守边界

修复前，文件搜索策略允许冒号和空格，因而可能把 `WARNING: missing configuration.py` 或 `src/file.py:10: use configuration.py` 视为路径，在第 20 行后隐藏。真实 PowerShell 子进程执行 `rg --files` 后追加提示，已复现提示从压缩结果消失；这是内容保护缺陷，不是模型测评分数。

现在使用独立 `file_search.py` 模块：只对明确的单次文件名列举命令及可识别的物理记录生成摘要。复合命令、管道、重定向、变量/命令替换、控制字符、未知选项和不完整引号触发透传。歧义搜索通过宽松提示路由到安全分支，避免因选项顺序、`.exe` 拼写或引号而落入通用重复压缩。

## 支持与回退

- 支持 `rg --files`、字面 `rg.exe`/可识别的引用路径、PowerShell 开头的单个 `&` 调用符，以及部分明确不改变输出结构的选项，例如 `--hidden`、`--no-config`、`--sort path`、`-g '*.py'`、`--max-depth 3`、`--color never`。
- 简单 POSIX `find ROOT -type f` 及 `-name/-iname/-path/-ipath/-print` 仅按有限语法识别。Windows `find.exe` 是内容搜索工具，不能按 POSIX find 压缩。该 find 语法有单元测试，本轮实际工具评测为 ripgrep。
- 文件记录继续保守要求可识别的扩展名；中文、普通空格、驱动器绝对路径和常见 UNC 拼写可识别。冒号仅接受绝对驱动器前缀，其他冒号及提示前缀不能当作路径。
- 裸 `LICENSE`、裸 `.gitignore`、方括号/emoji 等未覆盖的路径、边缘空白、NUL、ANSI 和内嵌分隔控制符均保留整个输出。合法文件名也可能触发保守回退，不能因此声称它不存在。
- 保留前 20 条物理记录的原始字符和行终止符，准确报告隐藏数量，全文保存至 Artifact，继续提供有界 grep/show。仅 CR/LF/CRLF 划分物理记录。

此模块不执行命令，也不读取所列文件来猜测其真实性；它不是通用 Shell 解析器。识别不确定时牺牲压缩机会。若脚本、shell 函数或 PATH 中的同名包装程序冒充 `rg`，命令文本本身不能认证输出来源；Hook 原始记录始终可恢复。

## 复现实际本地验证

在仓库根目录运行，无付费模型调用：

```powershell
$Python = 'D:\tokenguard-benchmark\.venv\Scripts\python.exe'
& $Python -X utf8 .\tokenguard_v04\benchmark\search_contracts.py
```

需要本机实际 ripgrep，可用 `--rg 'C:\Tools\rg.exe'` 指定；文件名须为 `rg` 或 `rg.exe`，以固定 Shell 样例调用同一工具。未找到或版本证据异常时，保留全部失败检查，退出码为 1，不以 Mock 替代。常规 pytest 中实际工具用例会在缺少 rg 时明确跳过；CI 在临时 runner 准备工具，再强制运行独立评测。安装 CI 工具不修改用户电脑配置。

独立评测包含 11 类 Fixture：普通路径、中文/空格、无扩展名、方括号、NUL、原生 stderr、部分成功加 stderr、空目录、实际 Shell 追加提示，以及两类显式混合的提示/内容位置后缀。10 类乘三种换行和开关共 60 项；原生 stderr 保持原始换行、检查开关共 2 项，总计 **62 项**。换行变体是转换后的重放，不是 62 个独立任务。

评测先核对真实目录的全部文件及内容 Hash，确认 rg 的记录与清单严格一致；随后经过实际手动 Hook 和 CLI 子进程，验证前 20 条、隐藏数量、非 stdout 字段、Artifact 字节、一行 show、一次有界 grep 及审计计数。通用过程复用 `build_pip_contracts.py`；搜索的 Fixture 清单和前缀/计数断言独立于生产正则，报告绑定两份验证器、运行源码及 rg 二进制 Hash。

输出 JSON、CSV、Markdown、原始 stdout/stderr、参数、退出码、Fixture 文件清单及来源。超时、坏 UTF-8、错误退出码、缺文件/多出未知记录、缺工具或实现均不认证成功，失败保留分母。测试另在临时故障副本中注入丢提示与少保留一条路径，验证评测器能拒绝；这些故障副本不等于当前源码存在同样问题。

本轮没有新的真实 Claude 会话、编码能力 A/B、实际 API usage、缓存或账单证据；软件回归与输出契约通过不能代替编码能力无损结论。历史真实质量评测的负面结果保留。
