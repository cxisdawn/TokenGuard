# 任务级 A/B 执行器

该执行器把“能压缩日志”与“能完成相同编程任务”分开验证。现有 `benchmark/run.py` 的固定日志实验继续保留。新增 `benchmark/task_runner.py` 支持真实 Python 项目快照、本地 Fixture Agent 和 Claude CLI 适配器。

**本版本完成了本地 Mock、CLI 协议替身、无模型预检及一轮明确授权的真实 Claude A/B。** 真实两组均通过任务验收，但优化组输出不足阈值，压缩次数为 0；不能据此宣称 TokenGuard 节省 Token 或费用。

## 本地完整流程（PowerShell，不调用模型）

从仓库根目录执行，使用已安装 pytest 的 Python：

```powershell
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
Push-Location .\tokenguard_v04

$Experiment = (& $Python .\benchmark\task_runner.py prepare `
  --project .\benchmark\fixture `
  --task .\benchmark\task_fixture.json `
  --output .\reports\tasks --rounds 3).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Experiment preparation failed' }

# 默认只展示计划：不会启动 Agent、pytest 或 --version 进程。
& $Python .\benchmark\task_runner.py run $Experiment --driver mock

# 本地 Fixture Agent 在独立子进程修复 Bug，实际运行 pytest 和 Hook。
& $Python .\benchmark\task_runner.py run $Experiment --driver mock --execute
if ($LASTEXITCODE -ne 0) { throw 'Inspect experiment results: not all trials succeeded' }

& $Python .\benchmark\task_runner.py report $Experiment
Get-Content (Join-Path $Experiment 'report.md') -Encoding UTF8
Pop-Location
```

每轮包含 baseline/optimized 两组，三轮共 6 次 Fixture 任务。原始项目有 504 项测试，其中 3 项失败；Fixture 只将 `a // b` 改成 `a / b`。Agent 的运行时间和 Python/Hook 开销是真实本地测量，Agent 本身是固定程序，不代表模型推理。Mock 不伪造 API usage 或账单。

`run` 的退出码：0 表示预览成功或所有任务验收成功；1 表示执行完成但存在失败任务；2 表示参数、状态、文件或运行前检查拒绝。失败记录及独立测试输出位于各 trial 目录。首次执行会写入 `started.json`；执行中断也不能复用旧会话，需重新 prepare。

Linux/macOS 使用 `.venv/bin/python` 和对应 shell 路径，子命令相同。

## 任务定义与可比条件

参考 [task_fixture.json](benchmark/task_fixture.json)。必填字段包括任务 ID、provider、model、prompt、完整 `files` 清单、允许修改的 `editable_files` 和可信 `test_files`。支持平面或嵌套 Python 源码文件；测试环境依赖需事先在运行 Python 中安装。

- 仅复制明确列出的文本文件；拒绝 `.env`、凭据目录、日志、二进制文件、链接、路径穿越和不兼容 Windows 的文件名。单文件最多 1 MiB、总计 16 MiB。这不保证任意源码内没有硬编码秘密，运行前需确认所选文件适合提供给模型。
- 输出目录必须在初始项目外。原始项目不会运行测试或接受修改。
- 相同快照和 Prompt、相同模型/MCP 参数及代码版本哈希；实验设计固定干预模式与配置哈希，每个 trial 使用新 UUID、独立工作目录、独立 Artifact/审计目录。
- 默认 `--baseline-mode disabled_hook`：两组都加载同一 TokenGuard Hook，基线设 `TOKENGUARD_ENABLED=0`，优化组设为 1。基线仍承担 Hook 启动和事件处理开销。
- 可选 `--baseline-mode no_hook`：基线使用空的实验配置，不注入本执行器的 TokenGuard Hook；优化组照常启用。Mock 中基线不创建 Hook 子进程；真实 Claude 中用户/组织已有 Hook 仍可能加载，不修改它们或任何权限设置。
- 每次实验只采用一种基线，每轮仍是 2 个全新会话。报告明确列出 `baseline_mode` 和每组 `hook_mode`；不同基线的 Usage 不允许配对，也不会汇总到同一 cohort。没有模式信息的通用旧 Usage 标为 `unspecified`，不擅自猜测。
- 各轮交替 AB/BA 顺序。禁止重复执行、恢复或继续旧会话；执行前检测初始快照、候选目录、配置和运行代码漂移。
- 每组先验证原始失败可复现，再运行 Agent，最后把候选可编辑源码覆盖到新的可信快照，独立运行原始测试。JUnit 数量、失败、错误和跳过状态参与验收；`expected_tests` 可要求确切测试数。
- 候选修改测试、删除文件、创建额外文件或更改其他受保护源码都会导致任务失败。最终工作目录保留，便于查看代码差异；压缩不会替代最终测试验收。

目录隔离不是操作系统沙箱。候选代码和测试是本机代码，不应将此框架用于不可信项目。可信测试/配置存储于工作目录之外，但同一系统用户的恶意进程仍可能访问它们；本地记录不提供防篡改认证。进程输出通过临时文件缓冲，单流读取上限 16 MiB，超过则验收失败；临时文件在超时前仍可能占用较大磁盘空间。超时会尝试终止自己启动的进程树。

独立验收的工作目录使用系统临时目录，仅日志写回实验目录，避免将嵌套报告路径继续加长而触发 Windows 路径限制。不修改注册表或系统长路径设置。

## 比较两种基线（完全本地）

```powershell
# 在 tokenguard_v04 目录执行；脚本只支持 Mock，不提供付费调用参数。
& $Python .\benchmark\baseline_demo.py --rounds 3
```

脚本分别为 `disabled_hook` 和 `no_hook` 准备独立配对实验：每种 3 轮、每轮 2 组，共 12 次 Fixture 任务。每个实验保持 AB/BA 轮次顺序，汇总报告与两个实验目录并列放在输出位置，避免额外的 Windows 深路径层级。默认输出 `reports/baseline-modes/`，可用 `--output` 指定较短目录。

输出 Markdown/JSON/CSV，记录任务成功数、Hook 子进程次数/累计耗时、Agent 墙钟耗时中位数及压缩量。汇总保留失败任务，缺失测量用 unavailable；不能只展示成功样本的速度。单次实验 JSON 还包含 `local_wall_clock_pairs` 的逐轮差值。

Hook 子进程耗时包含启动、Python 导入、压缩和本地读写，不是纯启动时间。两种基线按批次执行，时间受缓存和机器负载影响；这些是描述性本地数据，不证明显著性能提升或真实任务节省。真实 Claude 的 Hook 子进程耗时没有可靠测量时仍为 unavailable，不能把审计里的压缩函数耗时代替它。

只准备无 Hook 基线的普通配对实验时，在 `prepare` 命令追加 `--baseline-mode no_hook`，其他预览、预检、执行和 Usage 绑定命令相同。旧任务实验缺少模式字段时继续按其已知旧协议 `disabled_hook` 读取；旧记录没有测过的 Hook 耗时不会补成 0。

## Claude CLI：先预览，显式开启付费执行

先重新 prepare 一轮（2 个全新会话），再运行本地预检；已经完成 Mock 的目录不能用于真实试验：

```powershell
$LiveExperiment = (& $Python .\benchmark\task_runner.py prepare `
  --project .\benchmark\fixture --task .\benchmark\task_fixture.json `
  --output .\reports\live --rounds 1).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Preparation failed' }
& $Python .\benchmark\task_runner.py preflight $LiveExperiment
```

`preflight` 只执行本机 `--version`、`--help`（包括隐藏 `--max-turns` 的 help 解析探针）、实验自有配置中的 Hook 子进程及可信快照的初始 pytest。不启动 Agent、不发起模型请求，不读取用户配置或凭据，不写 `started.json`。原始任务预期失败，因此 `initial_validation.passed=false`、3 个失败与 `checks.initial_task=true` 可以同时成立；后者表示原始失败已按预期复现。

检查项还包括未使用的会话、快照/配置/运行代码一致性和明确禁用 Hook 的环境开关。成功时 `local_checks_passed=true`，退出码为 0；检查未通过时为 1，输入/状态拒绝为 2。每次结果保存在该目录下独立的 `preflight-*.json`。更新源码后需要重新 prepare，不能继续使用旧代码哈希的计划。

预检通过只证明本地条件满足：认证、DeepSeek 网关实际路由、模型可用性和宿主对替换输出的接受仍是 unverified。隐藏参数的 help 解析也不等于真实任务中已验证其执行行为。

```powershell
& $Python .\benchmark\task_runner.py run $LiveExperiment --driver claude
```

准备的轮数决定启动数量。真实运行必须同时显式提供 `--execute --allow-paid`；默认 `--max-paid-sessions 2` 只允许一轮配对，三轮需要显式提高到 6。该数量限制是 CLI 启动数，不是 API 请求数或账单上限。未经授权不要执行付费命令。

真实执行的形式为 `run EXPERIMENT --driver claude --execute --allow-paid --max-paid-sessions N`，可用 `--claude PATH` 指定可执行文件。Windows 直接执行原生 `claude.exe`；若 PATH 命中 npm 的 `claude.cmd`/`claude.ps1`/`claude`，会定位同目录 `node_modules/@anthropic-ai/claude-code/bin/claude.exe` 并直接启动。不会读取或运行包装器，也不经 cmd/PowerShell 解释 Prompt 或路径。纯 JavaScript 的旧 npm 安装不做此自动回退，报告 `npm_native_binary_missing_or_linked`；可通过 `--claude` 指定现有原生程序。框架不会安装或升级 Claude。原生程序定位或版本检查失败时，尚未写入 `started.json`，可修复路径后重新检查。

适配器采用官方 [Claude CLI 参数](https://code.claude.com/docs/en/cli-reference)：`--print --output-format json --session-id UUID --model MODEL --max-turns N`，Prompt 经标准输入传递。使用显式 Hook `--settings`、`--setting-sources user` 和 `--strict-mcp-config` 的空 MCP 配置。CLI 沿用用户层配置及权限，执行器不解析其配置文件内容，不修改全局配置，也不添加权限或 Bypass 参数。源项目设置文件没有复制，项目专属权限/Hook 没有迁移；用户层 Hook、组织策略、CLAUDE.md、自动记忆和其他宿主定制仍可能影响实验，尚未得到完整隔离认证。CLI 版本不兼容、权限拒绝或非交互审批失败均计入失败，不自动绕过。

认证/模型网关需由现有 Claude 用户配置或启动环境预先配置。执行器不查找、解析、复制或记录 API Key；CLI 自行按其现有用户配置认证，并继承启动环境。请求模型名会记录，但别名解析、网关真实路由及付费模型成功执行仍需单独验证。

CLI JSON 中的 session_id 必须与计划一致；非零退出、错误状态或超时不算成功。CLI 提供的 `total_cost_usd` 和聚合 `usage` 不自动当作原生供应商 usage 或账单导入。真实 CLI 的所有工具调用总数当前保持 unavailable；报告另列 Hook 观察到的 Bash 调用数。Hook 发出压缩候选也不自动证明宿主替换已对模型生效。

## 显式关联离线 Usage

首轮真实任务的 CLI 输出确实包含 usage 汇总，但它没有自动导入为原生供应商响应。任务验收与 Hook 诊断可直接核实，原生请求完整覆盖、后端身份及账单仍缺少独立证据。该轮本地 pilot-report 将 CLI 汇总单独标为 unverified，不与 Mock 数值或供应商账单混合。

每个 trial 执行后可绑定**仅包含 usage 的供应商响应导出**，无需日志正文：

```powershell
& $Python .\benchmark\task_runner.py attach-usage $Experiment `
  --trial '001-baseline' --source 'C:\exports\baseline-usage.json' `
  --format deepseek --evidence gateway_export `
  --coverage complete_task --request-count 12
```

上述路径与数量需要换成实际导出数据。原生 DeepSeek 响应可用 `provider_export`；网关转换的 DeepSeek 格式应使用 `gateway_export`。Claude CLI 的最终汇总 JSON 不是此导入器接受的原生响应格式。Mock trial 只接受 `synthetic`，不能冒充真实使用。账单可额外用 `--billing FILE` 绑定，规则见 [USAGE.md](USAGE.md)。

manifest 的会话、模型、Prompt/快照/配置哈希和验收结果来自该 trial，不能通过另一个用户手写 manifest 覆盖。usage 文件与会话的关联、完整覆盖声明和导出真实性仍由提供者保证，框架无法独立认证。重用请求 ID、跨组不一致和失败质量会在配对报告中标记/排除；不允许静默覆盖已绑定 measurement。

再次运行 `report` 后生成 `report.json`、`report.csv`、`report.md`。两组都有 usage 时另生成配对比较报告。字段区分：

报告重新核对当前候选和实际受测白名单文件的 Hash，以及当前审计和执行状态。旧运行缺少受测文件绑定或当前文件已变化时，保留 `reported_task_success`，当前 `task_success` 为 null，不将它纳入质量通过的配对；不能通过补写旧字段升级证据。详见 [报告证据校验](REPORT_RELIABILITY.md)。

|指标|来源和边界|
|---|---|
|任务成功、测试通过、受保护文件未改|本机独立验收|
|elapsed_ms|Agent 进程墙钟时间，包含其工具调用，不含控制器验收|
|total_execution_ms|整组流程墙钟时间，含前后独立验收|
|hook_process_count / hook_process_elapsed_ms|Mock 的 Hook 子进程次数与墙钟总耗时，含启动/处理/I/O；真实宿主没有此测量时 unavailable|
|original_stdout_chars / visible_stdout_chars|Mock 中两次工具 stdout 的原始/最终可见字符数，包含恢复提示，不等于任务 Token|
|字符节省、压缩次数、恢复次数|该 trial 的 TokenGuard 审计|
|task tokens、缓存读取/写入、缓存占比|显式绑定的完整 usage；缺失或部分覆盖为 unavailable|
|reported billing / actual billed cost|符合证据规则的独立账单；CLI 估价不算账单，导出仍未经独立认证|

字符压缩率、本地估算 Token、实际任务 Token 和账单必须分别解读。一次或少量配对不构成稳定节省率。

生成的实验、原文、会话记录和报告不随源码发布；`.gitignore` 已忽略 `reports/` 以及 `task-experiment-*/`。不自动上传日志，不自动清理旧 Artifact。
