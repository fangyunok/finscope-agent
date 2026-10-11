package core

import (
	"testing"
)

// These tests pin the Go implementation to the Python reference
// (src/finscope/calculations.py and src/finscope/matching.py). The expected
// values below were produced by running the Python oracle on the shared demo
// profiles, so any divergence here means the two implementations have drifted.

func TestCalculateGoalAlice(t *testing.T) {
	raw := map[string]string{
		"monthly_income":    "8000.00",
		"essential_expense": "6500.00",
		"available_balance": "5000.00",
		"goal_amount":       "12000.00",
		"horizon_months":    "6",
	}
	got, err := CalculateGoal(raw)
	if err != nil {
		t.Fatalf("CalculateGoal(alice) error: %v", err)
	}
	if got.Status != "reachable" {
		t.Errorf("status = %q, want reachable", got.Status)
	}
	if got.MonthlySurplus != "1500.00" {
		t.Errorf("monthly_surplus = %q, want 1500.00", got.MonthlySurplus)
	}
	if got.GoalGap != "7000.00" {
		t.Errorf("goal_gap = %q, want 7000.00", got.GoalGap)
	}
	if got.MonthsToGoal == nil || *got.MonthsToGoal != 5 {
		t.Errorf("months_to_goal = %v, want 5", got.MonthsToGoal)
	}
	if !got.HorizonFeasible {
		t.Errorf("horizon_feasible = false, want true")
	}
	if got.ProjectedBalanceAtHorizon != "14000.00" {
		t.Errorf("projected_balance_at_horizon = %q, want 14000.00", got.ProjectedBalanceAtHorizon)
	}
}

func TestCalculateGoalBob(t *testing.T) {
	raw := map[string]string{
		"monthly_income":    "18000.00",
		"essential_expense": "9000.00",
		"available_balance": "30000.00",
		"goal_amount":       "80000.00",
		"horizon_months":    "12",
	}
	got, err := CalculateGoal(raw)
	if err != nil {
		t.Fatalf("CalculateGoal(bob) error: %v", err)
	}
	if got.Status != "reachable" {
		t.Errorf("status = %q, want reachable", got.Status)
	}
	if got.MonthlySurplus != "9000.00" {
		t.Errorf("monthly_surplus = %q, want 9000.00", got.MonthlySurplus)
	}
	if got.GoalGap != "50000.00" {
		t.Errorf("goal_gap = %q, want 50000.00", got.GoalGap)
	}
	if got.MonthsToGoal == nil || *got.MonthsToGoal != 6 {
		t.Errorf("months_to_goal = %v, want 6", got.MonthsToGoal)
	}
	if got.ProjectedBalanceAtHorizon != "138000.00" {
		t.Errorf("projected_balance_at_horizon = %q, want 138000.00", got.ProjectedBalanceAtHorizon)
	}
}

func TestCalculateGoalCarolUnreachable(t *testing.T) {
	raw := map[string]string{
		"monthly_income":    "7000.00",
		"essential_expense": "7500.00",
		"available_balance": "800.00",
		"goal_amount":       "6000.00",
		"horizon_months":    "4",
	}
	got, err := CalculateGoal(raw)
	if err != nil {
		t.Fatalf("CalculateGoal(carol) error: %v", err)
	}
	if got.Status != "unreachable" {
		t.Errorf("status = %q, want unreachable", got.Status)
	}
	if got.MonthlySurplus != "-500.00" {
		t.Errorf("monthly_surplus = %q, want -500.00", got.MonthlySurplus)
	}
	if got.MonthsToGoal != nil {
		t.Errorf("months_to_goal = %v, want nil", got.MonthsToGoal)
	}
	if got.ProjectedBalanceAtHorizon != "-1200.00" {
		t.Errorf("projected_balance_at_horizon = %q, want -1200.00", got.ProjectedBalanceAtHorizon)
	}
}

func TestMoneyValidation(t *testing.T) {
	cases := []struct {
		in      string
		wantOK  bool
	}{
		{"100.00", true},
		{"0", true},
		{"0.5", true},
		{"123456789012345", true},
		{"100.999", false}, // three decimals
		{"-1.00", false},
		{"abc", false},
		{"1,000.00", false},
		{"", false},
		{"1.2.3", false},
	}
	for _, c := range cases {
		_, err := Money(c.in, "amount")
		if c.wantOK && err != nil {
			t.Errorf("Money(%q) unexpected error: %v", c.in, err)
		}
		if !c.wantOK && err == nil {
			t.Errorf("Money(%q) should have failed", c.in)
		}
	}
}

func TestValidateProfileMissingFields(t *testing.T) {
	raw := map[string]string{
		"monthly_income": "8000.00",
		// essential_expense, available_balance, goal_amount, horizon_months missing
	}
	_, err := ValidateProfile(raw, false)
	if err == nil {
		t.Fatal("expected missing-fields error")
	}
	perr, ok := err.(*ErrInvalidProfile)
	if !ok {
		t.Fatalf("error type = %T, want *ErrInvalidProfile", err)
	}
	if perr.Code != "missing_profile_fields" {
		t.Errorf("code = %q, want missing_profile_fields", perr.Code)
	}
	if len(perr.MissingFields) != 4 {
		t.Errorf("missing_fields = %v, want 4 entries", perr.MissingFields)
	}
}

func TestMatchProductsAlice(t *testing.T) {
	// Alice: emergency / immediate / low / 5000.00 — only FS-CASH is eligible.
	raw := map[string]string{
		"monthly_income":    "8000.00",
		"essential_expense": "6500.00",
		"available_balance": "5000.00",
		"goal_amount":       "12000.00",
		"horizon_months":    "6",
		"liquidity_need":    "immediate",
		"risk_preference":   "low",
		"goal_kind":         "emergency",
	}
	products := []*Product{
		mkProduct("FS-CASH", "0.00", "100000.00", 0, "low", "immediate", 0, []string{"emergency", "purchase"}),
		mkProduct("FS-FLEX", "100.00", "200000.00", 0, "low", "flexible", 10, []string{"purchase", "long_term"}),
	}
	got, err := MatchProducts(raw, products, "2026-10-03", nil)
	if err != nil {
		t.Fatalf("MatchProducts error: %v", err)
	}
	if len(got.Eligible) != 1 {
		t.Fatalf("eligible count = %d, want 1", len(got.Eligible))
	}
	if got.Eligible[0].ProductID != "FS-CASH" {
		t.Errorf("eligible[0] = %q, want FS-CASH", got.Eligible[0].ProductID)
	}
	if len(got.Excluded) != 1 {
		t.Fatalf("excluded count = %d, want 1", len(got.Excluded))
	}
	if got.Status != "matched" {
		t.Errorf("status = %q, want matched", got.Status)
	}
}

func mkProduct(id, min, max string, lock int, risk, liq string, fees int, goals []string) *Product {
	return &Product{
		ProductID:     id,
		Name:          id,
		Version:       "2026.1",
		EffectiveFrom: "2026-01-01",
		EffectiveTo:   "2027-01-01",
		Synthetic:     true,
		Currency:      "CNY",
		AmountBounds:  map[string]string{"min": min, "max": max},
		LockMonths:    lock,
		RiskLabel:     risk,
		Liquidity:     liq,
		FeesBps:       fees,
		FeesFlat:      "0.00",
		SupportedGoals: goals,
		Clauses: []Clause{
			{TermID: id + "-amount", Kind: "amount", Content: "amount"},
			{TermID: id + "-lock", Kind: "lock", Content: "lock"},
			{TermID: id + "-risk", Kind: "risk", Content: "risk"},
			{TermID: id + "-liquidity", Kind: "liquidity", Content: "liquidity"},
			{TermID: id + "-fees", Kind: "fees", Content: "fees"},
			{TermID: id + "-goal", Kind: "goal", Content: "goal"},
		},
	}
}
