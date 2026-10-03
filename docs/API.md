# HTTP 与 MCP 接口

网页与 CLI 复用同一业务服务。HTTP 接口使用本机演示会话；登录成功后由服务端签发 HMAC Cookie（HttpOnly、SameSite=Strict），每次请求重新核查用户。服务重启后需要重新选择演示身份。

写接口接受 JSON，正文上限 32 KiB，并核查浏览器同源标头。会话身份控制个人记录，请求体中的额外身份字段会被拒绝。普通本机脚本可以使用会话 Cookie 调用接口。

## HTTP

| 方法与路径 | 内容 |
| --- | --- |
| `GET /health` | 服务状态与模型模式 |
| `GET /api/demo-users` | 合成演示账号目录 |
| `POST /api/login` | `{ "user_id": "alice" }`，建立演示会话 |
| `POST /api/logout` | 清除当前演示会话 |
| `GET /api/me` | 当前服务端身份 |
| `GET /api/demo-profile` | 当前演示账号的公开合成示例；仅用于表单填充 |
| `GET /api/profile` | 已确认画像、版本、代次、缺失字段与来源 |
| `POST /api/profile-proposals` | 提议资料修改：`patch` 与 `expected_version` |
| `GET /api/profile-proposals` | 本人的当前代次提议 |
| `POST /api/profile-proposals/{id}/confirm` | 确认特定版本及哈希，`confirmed: true` |
| `DELETE /api/memory` | `{ "confirmed": true }`，删除本人记忆和个人运行记录 |
| `GET /api/feedback` | 本人的显式兴趣反馈 |
| `POST /api/feedback` | `product_id`、`action: favorite / exclude / clear`、`confirmed: true` |
| `POST /api/tasks` | `message` 与可选 `request_id`，返回分析记录或待确认提议 |
| `GET /api/runs` | 本人当前代次分析记录 |
| `GET /api/runs/{id}` | 分析结果、画像版本、工具调用与来源 |
| `GET /api/terms` | `query`、`as_of`、可选 `product_id`，检索生效模拟条款 |
| `POST /api/term-answers` | `question`、`as_of`、可选 `product_id`，条款来源展示或模型引用回答 |

## 资料提议与确认

金额字段使用人民币金额字符串，最多两位小数。期限为 1–1200 的整数；不接受布尔值替代整数。`risk_preference` 是用户明确提供的标签上限。

```json
{
  "patch": {
    "monthly_income": "8000.00",
    "essential_expense": "6500.00",
    "available_balance": "5000.00",
    "goal_amount": "12000.00",
    "horizon_months": 6,
    "liquidity_need": "immediate",
    "risk_preference": "low",
    "goal_kind": "emergency",
    "currency": "CNY"
  },
  "expected_version": 0
}
```

提议返回 `proposal_id`、`base_version`、`content_hash`、`changes`、`diff` 与 `status`，当前画像保持原值。用户核对后，确认路径使用返回的 ID，正文使用返回的版本和哈希：

```json
{
  "expected_version": 0,
  "expected_hash": "用该提议返回的64位content_hash替换",
  "confirmed": true
}
```

`liquidity_need` 取 `immediate / flexible / locked`；`risk_preference` 取 `low / medium / high`；`goal_kind` 取 `emergency / purchase / long_term`。

## 分析任务

```json
{
  "message": "请根据当前确认资料分析我的目标",
  "request_id": "demo-analysis-001"
}
```

返回的运行记录包含 `run_id`、`status`、`profile_version`、`memory_epoch`、`feedback_version` 与 `result`。已确认完整画像产生 `completed`，缺资料产生 `needs_profile`，有新字段产生 `pending_confirmation`。同一请求 ID 与同一输入、模式、画像版本、代次和反馈版本可复用同一运行；重复 ID 用于变化后的输入或状态返回冲突，需要创建新 ID。

离线模式的普通文字使用现有确认画像。需要演示抽取时，将 `message` 写成显式画像补丁 JSON 字符串，例如 `"{\"horizon_months\":3}"`；解析结果只产生提议，仍需人工确认。

## MCP 工具

| 工具 | 作用与约束 |
| --- | --- |
| `get_my_profile` | 读取当前登录用户确认的画像 |
| `calculate_financial_goal` | Decimal 计算结余、差额、整月与假设 |
| `match_products` | 硬约束过滤、规则排序、排除原因及条款来源 |
| `search_financial_terms` | 日期有效性优先的词项检索 |
| `propose_profile_update` | 生成待确认提议，不直接覆盖确认资料 |

工具服务绑定可信用户及运行的记忆代次。固定编排调用实际 MCP 客户端与服务端，不允许模型提供其他用户身份，也不把确认接口作为模型工具。

## 错误

错误响应包含可读 `message` 与机器可读错误标识。缺少登录通常返回 401，跨用户或删除后的资源返回 404，过期版本、哈希或请求冲突返回 409。条款问答模型不可用返回 503；非法引用或答复结构返回 502。服务不会把模型提供商响应正文或密钥放进公开错误。
