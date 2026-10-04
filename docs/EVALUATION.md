# 评测说明

本地 Windows / Python 3.12 完整测试 **100 / 100 通过**，本次耗时 20.108 秒：39 项计算与目录、27 项画像与业务、19 项 Agent / 模型协议、13 项 HTTP、2 项模拟序列与打包数据测试。

本地还已验证 wheel 中 15 个 Python 模块和 3 个 JSON 文件与源码逐字节一致；在源码目录之外创建全新虚拟环境，仅安装 38 项锁定依赖及 wheel，`pip check`、13 次 MCP 工具调用的记忆演示、20 组序列和实际 HTTP 登录 / 条款来源展示均通过。跨平台结果以仓库 Actions 为准。

## 模拟序列

标签保存于 `data/evaluation_cases.json`，与打包副本一致。每组序列使用独立临时数据库，标签在执行前给定，不通过复制被测函数的输出生成。

20 组序列覆盖：

| 方向 | 用例 |
| --- | --- |
| 跨会话与重启 | `baseline_restart`、`horizon_update`、`risk_update`、`liquidity_update`、`emergency_goal_update` |
| 反馈与兴趣信号 | `favorite_not_risk`、`explicit_exclusion` |
| 删除与隔离 | `memory_delete`、`user_isolation` |
| 确认语义 | `unconfirmed_update`、`stale_proposal`、`repeated_confirmation` |
| 计算边界 | `zero_surplus`、`negative_surplus`、`already_achieved`、`decimal_round_up`、`exact_month_boundary` |
| 数据与条款版本 | `missing_fields`、`no_products_for_amount`、`effective_term_version` |

```powershell
.\.venv\Scripts\python.exe -m finscope evaluate --output runs/evaluation.json
```

报告逐案例保存步骤和是否满足预设标签，并记录工具调用、模型调用和合成数据标记。失败时退出码为 1。

本次本地 Windows / Python 3.12 执行 **20 / 20 组通过**；可查看[完整模拟报告](evaluation_report.json)。这份报告不代表其他环境的 CI 状态，跨平台结果以仓库 Actions 为准。

该评测在 `fixture` 模式下执行：显式 JSON 解析、真实 MCP 调用与已确认的结构化记忆可重复验证，因此结果与外部服务状态无关。扩展到真实模型质量、投资收益或排序质量的指标设计见[演进路线](ROADMAP.md)。

## 测试边界

- 确定性计算使用人工预先算出的结余、差额和整月标签，并覆盖单位、精度与边界输入。
- 画像确认、版本和记忆代次通过真实 SQLite 测试；删除后旧记录访问与并发写回分别验证。
- MCP 测试执行实际服务端 / 客户端协议，而不是用字典模拟工具返回。
- 模型 HTTP 协议测试采用 `httpx.MockTransport` 隔离外部依赖，逐项验证非法字段、无依据引用与服务错误路径。
- 网页测试验证同源写入、会话隔离、确认与删除流程。CI 另运行安装后的 live HTTP smoke。

## 后续评测计划

1. **检索质量**：在混合检索落地后输出 Recall@1/3/5、MRR、nDCG、延迟分位数与 bootstrap 置信区间，并按查询类型分组。
2. **记忆方案对照**：比较「无跨会话记忆」「最近若干轮对话历史」「已确认结构化画像」在偏好更新正确率、隔离与删除语义、耗时与 token 用量上的差异。
3. **模型质量**：接入真实服务后建立带人工标签的中文字段抽取集与条款问答集，逐项记录模型名、配置、失败率、延迟与 token 用量。

三项计划的实现路径见[演进路线](ROADMAP.md)。在此之前，仓库不报告字段抽取准确率、回答语义质量、投资收益或排序质量指标。
