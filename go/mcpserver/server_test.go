package mcpserver

import (
	"context"
	"strings"
	"testing"

	"github.com/mark3labs/mcp-go/mcp"
)

// TestCalculateGoalHandlerRejectsMissingProfile exercises the handler directly
// (no catalog needed for calculate_financial_goal), so it is hermetic.
func TestCalculateGoalHandlerRejectsMissingProfile(t *testing.T) {
	res, err := calculateGoalHandler(context.Background(), mcp.CallToolRequest{
		Params: mcp.CallToolParams{Arguments: map[string]any{}},
	})
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	if !res.IsError {
		t.Fatal("expected tool error for missing profile")
	}
	txt, ok := res.Content[0].(mcp.TextContent)
	if !ok {
		t.Fatalf("content type = %T, want TextContent", res.Content[0])
	}
	if !strings.Contains(txt.Text, "invalid_tool_input") {
		t.Errorf("error text = %q, want invalid_tool_input", txt.Text)
	}
}

func TestProfileArgNormalizesNumbers(t *testing.T) {
	req := mcp.CallToolRequest{
		Params: mcp.CallToolParams{Arguments: map[string]any{
			"profile": map[string]any{
				"monthly_income":    "8000.00",
				"essential_expense": "6500.00",
				"available_balance": "5000.00",
				"goal_amount":       "12000.00",
				"horizon_months":    float64(6),
			},
		}},
	}
	profile, err := profileArg(req)
	if err != nil {
		t.Fatalf("profileArg error: %v", err)
	}
	if profile["horizon_months"] != "6" {
		t.Errorf("horizon_months = %q, want 6 (normalized from float)", profile["horizon_months"])
	}
}

func TestProfileArgRejectsNonObject(t *testing.T) {
	req := mcp.CallToolRequest{
		Params: mcp.CallToolParams{Arguments: map[string]any{
			"profile": "not-an-object",
		}},
	}
	_, err := profileArg(req)
	if err == nil {
		t.Fatal("expected error for non-object profile")
	}
}
