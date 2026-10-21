# v0.3 审查摘要

历史 v0.3 源码保留，v0.4 在独立目录演进。

- 原有 14 项单元测试通过。没有预设 Hook 故障；先区分调用、压缩条件、处理异常和宿主消费。
- v0.3 已复制响应后只改 stdout，这个方向正确；手动返回 JSON 不能证明真实宿主接受替换。
- pytest 标准 PASSED 聚合保留非成功行，但 ANSI、空白参数和失败详情区域需要补强。
- Traceback/标准 diff 已有保护；v0.4 扩展统一/合并/二进制 diff 和 git diff/show 命令保护。
- show 原先仍遍历全文，grep 原先使用 readlines，且无单行字符上限。v0.4 改为流式有界恢复，后续增加稀疏行索引。
- 原文存储新增原子写入、复用完整性检查、链接/路径保护，保留历史 Artifact。
- 旧审计没有恢复事件、来源和实验标记。v0.4 将候选生成状态与真实模型可见性分开，不猜测多个会话共用 Artifact 的归属。
- v0.3 核心门槛未包含检索提示，已复现加入提示后净字符反增的边界。提示长度依赖安装路径；`python review_v03.py` 可在临时数据目录复现本机结果。v0.4 在完整元数据后再次检查门槛。
- 原安装脚本依赖 PowerShell 7 的 Hashtable JSON 转换；v0.4 使用 Python JSON 安装器和 PowerShell 5.1 薄包装，保留其他 Hook/权限，不改全局配置。

协议参考：[Claude Code PostToolUse decision control](https://code.claude.com/docs/en/hooks#posttooluse-decision-control)。

完整的公开验证范围见[验证说明](../docs/VALIDATION.md)。本仓库不附用户本机会话 ID、原始审计或真实工具日志。
