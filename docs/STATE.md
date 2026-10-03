# FinScope 当前状态与续做说明

记录日期：2026-10-03（Asia/Shanghai）。用户要求在额度耗尽前记录进展。

## 交付状态

代码开发与本地验收完成。完整 GitHub 发布及远端 CI 尚未完成。

- 本地目录：`D:\agent开发大师来了\finscope-agent`。
- 仓库：https://github.com/fangyunok/finscope-agent
- 远端 main 当前仅含 LICENSE，初始化提交 `ef6f436239923f582a492fc86e9fa7db4cbb8f56`，tree `464e9776b6d2b6d4f8a2b80102ff58a476d0c01f`。
- 用户已授权开发及发布；继续时无需再次请求仓库或发布确认。

## 已实现

- `schemas.py`：严格画像字段，人民币金额字符串，最多两位小数；拒绝身份、确认权限等额外字段。
- `database.py` / `service.py`：SQLite 事务、服务端身份、待确认画像提议、版本和内容哈希确认、字段来源与历史、用户隔离、明确兴趣反馈。
- 删除清理画像、来源、历史、提议、反馈、个人任务和事件；递增 memory_epoch，阻止旧模型请求写回。
- profile_version 和 feedback_version 绑定分析快照；资料或反馈变更会使执行中的旧任务失效。重复 request_id 只有一个创建者。
- `calculations.py`：Decimal 结余、目标差额、完整月份向上取整、期限是否可达；列明忽略利息等假设。
- `catalog.py`：8 个当前模拟产品，每个 6 条条款，另含一份失效版本。先按日期过滤，再进行关键词检索；全局条款 ID 冲突、缺资料和非 CNY 产品受校验。
- `matching.py`：金额、期限、流动性、明确风险偏好硬约束；固定规则评分、来源和排除原因。收藏不改变风险或评分，排除需要明确确认。
- `tools.py`：真实 MCP SDK Client / Server，身份和记忆代次绑定服务端；没有画像确认工具。
- `model.py` / `agent.py`：fixture 显式 JSON 解析，可选 Qwen / API 受限字段抽取；固定分析编排、工具轨迹、只读条款问答和引用原文核验。
- `webapp.py`：Starlette 中文工作台、签名演示会话、同源写入保护、画像差异确认、来源、匹配、历史、删除及跨页面清理。
- CLI：seed / demo / serve / doctor / evaluate。

## 已验证证据

- 完整 **100 项测试全部通过，20.108 秒**：领域 39、记忆服务 27、Agent 19、ASGI 网页 13、序列评测 2。
- 20 组预先标注的独立模拟跨会话序列 **20 / 20 通过**，报告 `docs/evaluation_report.json`；83 次真实本地 MCP 调用，0 次模型调用。
- 真实 Chrome 页面：填写模拟资料 → 提议 → 勾选确认 → 计算分析 → 期限修改 → 再分析 → 删除 → 旧 run 404，新任务 needs_profile；浏览器异常 0。
- 截图：`docs/assets/workbench.png`，已实际查看。JavaScript 语法检查通过。临时网页服务器和 Chrome 已关闭。
- Alice 标签：月结余 1500.00 元、差额 7000.00 元、5 个月；期限 6 可达、3 不可达；唯一匹配 FS-CASH，规则分 100。
- Bob 具有不同的确认约束与候选，排序有独立预期标签。
- 最终 wheel 在源码目录外全新环境中、仅使用 **38 项锁定依赖** 安装成功，pip check 通过。
- wheel 内 **15 个 Python 模块 + 3 个 JSON** 与最终源码逐字节一致。
- 独立安装验证通过 CLI、MCP demo 13 次工具调用、持久化恢复、期限更新、删除、20 组序列和实际 HTTP 登录 / 条款引用预览。
- 凭据扫描无异常；数据副本一致；不发布 runs、dist、build、虚拟环境、数据库、密钥或真实个人资料。

## 未验证与范围

- 真实 Qwen 端到端调用尚未验证；HTTP 模型测试使用 MockTransport，不能当成模型质量成绩。
- 远端 CI 仅配置，完整代码尚未推送，因此尚无 CI 通过证据。
- 所有产品、用户与费用数据是公开合成数据；无收益率、实际资金操作或训练排序模型。
- 检索是关键词 / 字符基线，没有向量库；只实现确认的结构化 Memory，尚未比较无记忆 / 最近对话历史两种基线。
- 引用检查证明来源 ID 与字面片段合法，不证明全部语义；模型问答标记 pending_review 与 semantic_support_verified=false。
- 删除是应用数据库逻辑范围，不声称清除备份、磁盘取证残留或外部模型服务日志。

## 下次续做

1. 检查本地 Git 提交及工作区，读取 README、EVALUATION 和本文件。
2. 根据 `.gitignore` 生成发布清单；包含源码、tests、三份 data JSON 及包内副本、README、LICENSE、pyproject、lock、.env.example、CI、docs 与截图。
3. 使用 git 已认证推送，或 GitHub connector create_tree → create_commit → update_ref（force=false）发布；保留远端 LICENSE 初始化历史。
4. 发布后验证 main 与所有文件 blob 哈希一致；检查 Windows / Ubuntu × Python 3.11 / 3.12 四组 CI。
5. 根据实际 CI 结果更新 EVALUATION / README / RESUME_ENTRY / 本文件并同步本地 main。

此前用于准备 connector 发布的 `runs/publication-core.json` 包含 33 个公开文本文件及预期 blob 哈希，但未包含最终 docs / README / CI / 截图，也可能因后续改动而过期。继续时重新生成完整清单，不依赖聊天工具 store。

## 运行命令

在本地项目目录使用：

```powershell
.\.venv\Scripts\python.exe -m finscope serve --mode fixture
# 浏览器打开 http://127.0.0.1:7862
.\.venv\Scripts\python.exe -m finscope demo --db runs/demo.sqlite --mode fixture
.\.venv\Scripts\python.exe -m finscope evaluate --output runs/evaluation.json
```

原开发计划在上级目录 `internship-project-plans/03-finance-agent.md`。
