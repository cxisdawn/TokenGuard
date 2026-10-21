# 验证范围

当前版本在 Windows、Python 3.11 上完成 **125 passed、1 skipped**。跳过项为 Windows 符号链接权限不可用。历史版本独立测试：v0.1 4 项、v0.2 6 项、v0.3 14 项通过。

覆盖压缩策略与失败保留、Unicode/换行与索引边界、恢复预算、路径保护、损坏回退、项目安装/重复安装/卸载、Hook 子进程协议、doctor、Mock A/B 契约、usage 缓存口径、重复请求、配对一致性、账单证据与质量回退。

新增任务实验覆盖：相同快照、文件清单和路径限制、AB/BA 顺序、全新会话、配置及运行代码漂移、默认无进程执行、付费开关、重复执行拒绝、独立原始测试、防止候选测试篡改、确切测试数量、进程超时、输出上限和 usage 关联。Claude 适配器只通过了本地进程替身的参数/协议测试，未执行真实付费模型。

本地任务 Fixture 原始 504 项测试中 3 项失败，修复后通过全部 504 项。优化组压缩了 pytest 输出并保留失败详情；基线组同样安装 Hook，但关闭压缩。Mock 不模拟 API Token/费用，没有绑定 usage 的报告明确保留 unavailable。三轮任务实验包含 6 个独立 trial，运行方法见 [任务实验文档](../tokenguard_v04/TASK_EXPERIMENTS.md)。

既有固定日志 Mock 三轮、5 场景、每场景 2 arm，共 30/30 通过，包含选择性恢复。固定手动 Hook 模拟验证响应结构、失败详情、原文字节一致和选择性恢复；它不是 Claude 集成。

用户另行运行的一次真实 Claude 会话中，模型引用了与本地压缩审计精确匹配的专属 Artifact/压缩 ID，支持该次替换内容对模型可见。会话 ID、审计、原日志和机器路径不随公开仓库发布。该次观察不能证明稳定触发率或完整任务费用下降。

usage 示例均为合成数据。导入器处理明确提供的供应商/网关 usage-only 导出，报告注明来源与完整覆盖由调用方声明。缺失数据不以 0 填补，CLI 费用估计不当作账单。尚无完整真实配对 usage/账单，不能宣传经统计验证的稳定 Token 或费用节省率。

GitHub Actions 覆盖 Windows/Linux、Python 3.10/3.12，远端状态以 [实际运行结果](https://github.com/cxisdawn/TokenGuard/actions/workflows/tests.yml) 为准。先前版本的四组矩阵已通过；新提交仍需等待相应运行结果。

先前 CI 发现 Windows Python 3.12 的路径 stat 与句柄 fstat 在 ctime 含义上可能不同。索引现统一持久化/读取句柄指纹，跨 API 比较仅使用文件身份、大小和 mtime，同一 API 仍检查 ctime，并保留修改与文件替换保护。参考 [CPython #157671](https://github.com/python/cpython/issues/157671)。
