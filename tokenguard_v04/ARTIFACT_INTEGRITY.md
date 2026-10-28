# Artifact 完整性与恢复开销

本轮修复本地存储与恢复可靠性，未运行新的付费编码任务。字符下降、本地 Token 估算、真实任务用量及账单仍分别报告；没有新的编码能力无损结论。

## 保存与并发

Artifact ID 是原始 stdout 的 UTF-8 字节 SHA256，文件位于数据根的 `artifacts/<ID>.txt`。先写同目录临时文件、flush/fsync，再通过硬链接原子发布完整文件，不覆盖已有目标。

并发发布者发现目标已存在时，必须完整核对 SHA256 与读取前后状态。删除临时链接可能只改变 POSIX ctime；遇到状态变化最多重验三次，每次重新完整核对内容。内容冲突立即失败，持续变化最终失败。

支持硬链接的 NTFS / Linux 文件系统已验证。文件系统不支持硬链接或禁止该操作时，保存失败，Hook 不发出替换，宿主继续使用原输出。中断可能遗留临时文件；本次不自动删除历史文件。

## 按范围校验

1. 首次恢复、缓存缺失或损坏、源文件状态变化时，流式读取整个 Artifact，核对 SHA256 与 ID。
2. 核验成功后，可在 `integrity-v04/<ID>.sha256` 保存 64 KiB 块哈希。单个缓存最多 16 MiB，只包含元信息与哈希。
3. 后续恢复验证缓存布局及校验和，再重读本次扫描涉及的完整块，包含 Python 二进制预读。
4. 缓存无法写入、不可访问或超过预算时，继续完整流式核验原文件。
5. 源文件内容不符合 ID 时，`show` / `grep` 拒绝返回，不修改或删除源文件，不追加成功恢复记录。CLI 返回 2，stdout 为空。
6. `tg.py index` 在建立或复用定位索引前核验整个源文件；底层定位索引 API 本身不构成内容认证。

旧版本曾允许改写内容后沿用旧 Artifact ID 继续恢复，现在明确拒绝这一行为。合法旧 Artifact 首次使用时自动核验，无需搬迁或改名；历史审计不回写。

## 返回预算与续读

`max_chars` 包含返回正文、行号及续读标记，按 Unicode 字符计量。完整结果恰好装满预算时，若已到请求的结束行或 EOF，直接返回完整结果，不添加虚假的续读标记。没有明确结束行时，最多多读一个字符判断是否还有未搜索的内容；这次预读计入扫描量并纳入完整性校验。

达到预算但仍有未搜索内容时，仍报告截断并提供续读游标。尤其是 grep，即使后续实际上没有匹配，只要尚未搜索，也不能声称搜索完成。完整状态提示放不进预算时，抛出 `RecoveryLimitError`，CLI 返回 2、stdout 为空，不写成功恢复记录；不再把 `(no matches)\n` 截成 `(` 后当作成功。

固定本地样例：100 字符的单行原文、返回预算 100 字符。旧版本需要两次恢复、累计返回 178 字符；修复后一次返回 100 字符，两者均完整重建原文。这是单个确定性样例，不是整个任务 Token 或费用节省率。

针对性复现：在 `tokenguard_v04` 执行 `python -X utf8 -m pytest tests/test_recovery_bounds.py -q`。测试覆盖精确预算、CR/LF/CRLF、Unicode、上下文、多匹配、请求结束行、扫描边界、未搜索尾部和真实 CLI stdout。

## 开销计量

恢复成功审计、`tg.py stats`、离线 Mock JSON/CSV/Markdown 提供：

| 字段 | 含义 |
| --- | --- |
| `returned_chars` | 返回的字符数 |
| `scanned_chars` | 恢复扫描的 Unicode 字符数 |
| `index_build_bytes` | 建立定位索引读取的源字节数 |
| `integrity_build_bytes` | 全量内容核验读取的源字节数 |
| `integrity_checked_bytes` | 再次验证所选完整块读取的源字节数 |
| `integrity_status` | `built`、`reused` 或 `uncached` |
| `recovery_latency_ms` | 成功恢复耗时，包含完整性核验 |

首次读取可能同时包含索引建立、全量哈希与块重读，不能把 scanned_chars 宣称为总读取量。上述字节数是应用层源文件读取计数，不是物理磁盘 I/O；不含 sidecar 读写、OS 缓存、重复保存的 SHA 检查或失败请求消耗。失败请求不生成成功恢复记录，当前也不汇总失败读取成本。字符数与字节数不可相加。

旧记录缺少新字段时，汇总为 null / unavailable，并附测量覆盖数；没有恢复请求时才为 0。

## 路径与信任范围

Artifact、定位索引、完整性缓存及其祖先均拒绝符号链接和 Windows reparse point / junction。接受合法 8.3 别名，并规范化 Windows 偶发保留的 `\\?\` / `\\?\UNC\` 路径前缀。

### Windows 长路径

规范路径用于身份/归属比较；实际文件 I/O 在 Windows 路径达到 248 个 UTF-16 代码单元时使用扩展路径拼写。覆盖原文发布/读取、定位与完整性 sidecar、审计 JSONL 和锁文件；相对数据根先计算绝对位置，非 BMP 字符按两个 UTF-16 单元计量。Linux 路径不作转换。无需修改注册表、系统长路径设置或 Claude 权限。

路径检查也必须使用可实际访问的拼写：原先普通长路径的 lstat 返回 FileNotFoundError，可能漏过真实 junction。现先检查实际路径及各祖先的链接/reparse 属性，再解析规范身份；访问拒绝继续报错，不当作不存在。扩展前缀不免除这些检查。接受驱动器和 UNC 文件系统路径，拒绝父目录穿越、设备和 GLOBALROOT 等不支持的命名空间。

本地反例最初 13/13 失败，含三个实际长路径 junction 漏检。修复后覆盖 183/270/350 UTF-16 单元的数据根、Unicode/emoji、相同内容并发发布、已有冲突内容拒绝、缓存建立/复用、审计文件与锁各自跨界，以及 Hook 和 show 子进程。对照操作还确认本机 262 字符普通路径的 link/stat/read 失败，扩展拼写成功；这个数值描述本机反例，不是所有 Windows 环境的统一上限。

本轮不保证任意长路径或任意文件系统：单个目录/文件名仍受文件系统组件长度限制，硬链接、权限、磁盘和 Win32 总路径限制仍可能使操作失败。UNC 只验证转换规则，没有访问真实网络共享或证明其硬链接支持。项目安装、任意 Benchmark 项目路径及外部工具的长路径能力不包含在 Artifact 数据目录修复范围；失败时仍优先保留原输出。

```powershell
# 在 tokenguard_v04 中执行；使用已有解释器。
& $Python -X utf8 -m pytest tests/test_long_paths.py tests/test_path_spelling.py -q
```

这是损坏检测机制，不是对同一 OS 用户的认证边界。能够同时重写源文件、缓存、审计或操纵文件系统的主体不在保证范围；缓存校验和不是签名。复用时只重新核验本次涉及的块；未选中块被改写且元信息也被伪装时，不保证每次立即发现。路径检查不能完全消除同用户在检查后替换目录的竞态。

所有数据保留本地。本轮测试只使用临时数据根，不扫描用户历史 Artifact。

## Windows PowerShell 复现

在源码仓库根目录执行，使用已有虚拟环境：

```powershell
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
Push-Location .\tokenguard_v04
& $Python -X utf8 -m pytest tests/test_storage_integrity.py tests/test_artifact_integrity.py tests/test_recovery_measurements.py -q
& $Python -X utf8 .\verify_hook.py
& $Python -X utf8 .\benchmark\run.py --rounds 3
Pop-Location
```

完整回归：在 tokenguard_v04 运行 `python -X utf8 -m pytest -q`。测试包含稳定改写、缓存损坏/不可写、读中变化、整数参数、真实线程/进程并发及 Windows junction / 8.3 路径。Mock 和手动 Hook 不代表真实 Claude 消费或模型编码能力验证。

## 更新项目接入

```powershell
$Project = 'D:\tokenguard-benchmark'
.\tokenguard_v04\install.ps1 -Project $Project -Python $Python
& $Python -X utf8 .\tokenguard_v04\tg.py doctor --project $Project
```

安装只更新目标项目本地配置。在正在运行的 Claude Code 输入 `/exit`，回到 PowerShell 后 `cd $Project`，运行 `claude` 启动新会话，再输入 `/hooks` 检查 TokenGuard PostToolUse 条目。doctor 是本地模拟，不能证明新会话已消费替换。本轮未修改现有接入配置。
