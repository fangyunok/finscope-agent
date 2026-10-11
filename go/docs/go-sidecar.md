# Go 计算侧车（finscope-go）

FinScope 的 Python 服务（`src/finscope/`）是参考实现与事实来源。本目录提供一套**语义对等的 Go 实现**，覆盖其中**纯确定性、无 Python 专属依赖**的计算热路径：

- 金额校验（`money`）：正则 + 精确有理数，与 `calculations.py` 的 `Decimal` 语义一致；
- 现金流目标计算（`calculate_goal`）：向上取整月份、线性期末余额；
- 产品硬约束匹配与规则打分（`match_products`）：金额区间 / 锁定月份 / 流动性 / 风险 / 应急目标约束，以及 `goal_fit/liquidity_fit/term_fit/risk_fit/fee_penalty` 打分与 `(-score, product_id)` 排序。

## 为什么要做这一层

FinScope 的计算核心是**纯函数、无 I/O、无模型依赖**，是请求路径上最热、也最适合用 Go 实现的部分。把它独立成 Go 模块后：

1. 计算逻辑可以内嵌进 Go 网关或作为 sidecar 独立部署，Python 进程继续承担模型抽取、MCP 工具路由、SQLite 记忆等它擅长的部分；
2. `math/big.Rat` 提供精确有理数运算，天然避免浮点误差，与 Python `Decimal` 的"精确、不丢精度"设计目标一致；
3. 两个实现用**黄金测试对拍**锁定，任何一边改动导致结果漂移都会在 CI 里暴露。

## 目录结构

```
go/
├── go.mod
├── Dockerfile
├── .github/workflows/go.yml      # go vet / go test / go build
├── core/                         # 确定性计算核心
│   ├── money.go                  # 金额校验 + calculate_goal
│   ├── matching.go               # match_products 硬约束与打分
│   ├── catalog.go                # 读取共享 catalog.json
│   ├── hash.go                   # sha256 摘要
│   └── matching_test.go          # 黄金对拍测试
└── cmd/finscope-sidecar/main.go  # HTTP 侧车服务
```

## 对拍测试

`core/matching_test.go` 中的期望值全部来自**用 Python oracle 在共享 demo 画像上跑出的结果**（alice / bob / carol）。任何一处 Go 结果与 Python 不一致，测试都会失败。

```
cd go && go test ./...
```

## 侧车服务

```
cd go && go run ./cmd/finscope-sidecar
```

端点：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/calculate` | 现金流目标计算，body 为 `{"profile": {...}}` |
| POST | `/match` | 产品匹配，body 为 `{"profile": {...}, "as_of": "2026-10-03", "excluded_product_ids": [...]}` |
| GET | `/healthz` | 存活探针 |

环境变量：

- `FINSCOPE_CATALOG`：catalog.json 路径，默认 `../src/finscope/data/catalog.json`；
- `FINSCOPE_ADDR`：监听地址，默认 `:8081`。

## 边界（重要）

这一层**只实现计算**。以下能力仍由 Python 服务负责，Go 侧不重复实现：

- 模型字段抽取（Qwen / API 模式）；
- MCP 工具网关与工具调用路由；
- SQLite 事务、乐观锁（`expected_version` / `expected_hash`）、内存 epoch、幂等；
- 条款检索（`catalog.search`）与条款问答（`TermQA`）。

Go 与 Python 通过 HTTP（sidecar）或直接内嵌（Go 网关）衔接，Python 始终是这些业务语义的**事实来源（oracle）**。
