// Package mcpserver exposes the pure calculation/retrieval tools of FinScope as
// a real Model Context Protocol (MCP) server over stdio, built on
// mark3labs/mcp-go.
//
// It mirrors the tool-allowlist and envelope semantics of the Python service's
// in-process ToolGateway (src/finscope/tools.py), but only for the stateless
// tools whose business logic lives in package core:
//
//   - calculate_financial_goal   -> core.CalculateGoal
//   - match_products             -> core.MatchProducts
//   - search_financial_terms     -> core.SearchTerms
//
// The stateful tools (get_my_profile, propose_profile_update) touch SQLite
// confirmed memory and the optimistic-lock confirmation transaction, so they
// intentionally stay in the Python service. This server is the Go piece of a
// split that keeps the Python process as the memory/transaction oracle.
package mcpserver

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/fangyunok/finscope-agent/go/core"
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

// Server wires the FinScope catalog into a stdio MCP server exposing the three
// stateless tools. catalogPath points at the shared src/finscope/data/catalog.json.
func Server(catalogPath string) (*server.MCPServer, error) {
	doc, err := core.LoadCatalog(catalogPath)
	if err != nil {
		return nil, fmt.Errorf("load catalog: %w", err)
	}

	s := server.NewMCPServer(
		"FinScope calculation tools",
		"0.2.0",
		server.WithToolCapabilities(false),
	)

	s.AddTool(calculateGoalTool(), calculateGoalHandler)
	s.AddTool(matchProductsTool(), matchProductsHandler(doc))
	s.AddTool(searchTermsTool(), searchTermsHandler(doc))

	return s, nil
}

// --- Tool definitions -------------------------------------------------------

func calculateGoalTool() mcp.Tool {
	return mcp.NewTool("calculate_financial_goal",
		mcp.WithDescription("从已确认画像计算现金流目标：月结余、目标差额、达成月数（向上取整）与期限可行性。金额为人民币字符串，最多两位小数。"),
		mcp.WithObject("profile",
			mcp.Required(),
			mcp.Description("已确认画像字段：monthly_income/essential_expense/available_balance/goal_amount 为非负人民币字符串，horizon_months 为 1–1200 整数"),
		),
	)
}

func matchProductsTool() mcp.Tool {
	return mcp.NewTool("match_products",
		mcp.WithDescription("按金额区间、锁定期、流动性、风险与应急目标硬约束过滤模拟产品，并按固定规则打分排序。"),
		mcp.WithObject("profile",
			mcp.Required(),
			mcp.Description("已确认画像，含 liquidity_need/risk_preference/goal_kind 三项偏好"),
		),
		mcp.WithString("as_of",
			mcp.Description("生效日期 YYYY-MM-DD，默认 2026-10-03"),
		),
	)
}

func searchTermsTool() mcp.Tool {
	return mcp.NewTool("search_financial_terms",
		mcp.WithDescription("按关键词检索生效模拟产品条款，返回带来源元数据与检索得分的命中。"),
		mcp.WithString("query",
			mcp.Description("检索关键词，不超过 1000 字"),
		),
		mcp.WithString("as_of",
			mcp.Description("生效日期 YYYY-MM-DD，默认 2026-10-03"),
		),
		mcp.WithString("product_id",
			mcp.Description("可选，限定到单个产品"),
		),
	)
}

// --- Handlers ---------------------------------------------------------------

func calculateGoalHandler(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	profile, err := profileArg(req)
	if err != nil {
		return mcp.NewToolResultError(err.Error()), nil
	}
	result, err := core.CalculateGoal(profile)
	if err != nil {
		return toolError(err), nil
	}
	return mcp.NewToolResultJSON(result)
}

func matchProductsHandler(doc *core.CatalogDocument) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		profile, err := profileArg(req)
		if err != nil {
			return mcp.NewToolResultError(err.Error()), nil
		}
		asOf := req.GetString("as_of", "2026-10-03")
		products := doc.ActiveProducts(asOf)
		result, err := core.MatchProducts(profile, products, asOf, nil)
		if err != nil {
			return toolError(err), nil
		}
		return mcp.NewToolResultJSON(result)
	}
}

func searchTermsHandler(doc *core.CatalogDocument) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		query := req.GetString("query", "")
		asOf := req.GetString("as_of", "2026-10-03")
		productID := req.GetString("product_id", "")
		hits, err := doc.SearchTerms(query, asOf, productID, 12)
		if err != nil {
			return toolError(err), nil
		}
		return mcp.NewToolResultJSON(map[string]any{"items": hits, "count": len(hits)})
	}
}

// --- Helpers ----------------------------------------------------------------

// profileArg extracts and validates the profile object argument. It mirrors the
// Python gateway's parameter whitelist: only a single "profile" object is
// accepted for the calculation/matching tools, and it must be a map of strings.
func profileArg(req mcp.CallToolRequest) (map[string]string, error) {
	args := req.GetArguments()
	raw, ok := args["profile"]
	if !ok || raw == nil {
		return nil, fmt.Errorf("invalid_tool_input: 需要 profile 对象")
	}
	m, ok := raw.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("invalid_tool_input: profile 必须为对象")
	}
	out := make(map[string]string, len(m))
	for k, v := range m {
		s, ok := v.(string)
		if !ok {
			// horizon_months may arrive as a JSON number; accept and normalize.
			switch n := v.(type) {
			case float64:
				s = fmt.Sprintf("%.0f", n)
			case int:
				s = fmt.Sprintf("%d", n)
			default:
				return nil, fmt.Errorf("invalid_tool_input: 字段 %s 类型无效", k)
			}
		}
		out[k] = s
	}
	return out, nil
}

// toolError converts a core error into the same envelope the Python gateway
// returns for business failures: {"ok": false, "error": {"code", "message",
// "status_code"}}.
func toolError(err error) *mcp.CallToolResult {
	code := "invalid_profile"
	status := 400
	if pe, ok := err.(*core.ErrInvalidProfile); ok {
		if pe.Code == "invalid_query" {
			code = "invalid_query"
		} else {
			code = pe.Code
		}
	}
	envelope := map[string]any{
		"ok":    false,
		"error": map[string]any{"code": code, "message": err.Error(), "status_code": status},
	}
	b, _ := json.Marshal(envelope)
	return mcp.NewToolResultError(string(b))
}
