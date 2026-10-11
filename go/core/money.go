// Package core implements the deterministic business logic of FinScope in Go.
//
// FinScope's Python service (src/finscope/) is the reference implementation and
// source of truth. This package mirrors the exact semantics of its pure
// calculation layer — money validation, cash-flow goal arithmetic, and
// hard-constraint product matching — using math/big.Rat so that every result is
// exact and reproducible.
//
// Why Go here: these functions are pure, allocation-light, and are the hottest
// part of the request path. They carry no Python-only dependency (no model, no
// vector store). Re-implementing them in Go lets the service keep a small,
// auditable core that can be deployed as a sidecar or embedded in a Go gateway,
// while the Python process stays the oracle for the model / MCP / SQLite layers.
//
// The two implementations are pinned together by golden tests: fixtures derived
// from the shared catalog.json are evaluated by both, and the Go results must
// match the Python outputs field-for-field.
package core

import (
	"fmt"
	"math/big"
	"regexp"
	"strings"
)

// MoneyPattern matches the exact strings the Python money() accepts:
// "0" or a non-negative integer/decimal with at most two fractional digits.
var MoneyPattern = regexp.MustCompile(`^(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,2})?$`)

// ErrInvalidProfile mirrors ProfileValidationError with a machine-readable code.
type ErrInvalidProfile struct {
	Code          string
	Message       string
	MissingFields []string
}

func (e *ErrInvalidProfile) Error() string { return e.Message }

// Money parses a non-negative yuan string into an exact rational, rejecting
// floats and hidden rounding — identical to Python money(). The value is
// quantized to two decimal places.
func Money(value, field string) (*big.Rat, error) {
	if value == "" || !MoneyPattern.MatchString(value) {
		return nil, &ErrInvalidProfile{
			Code:    "invalid_profile",
			Message: fmt.Sprintf("%s 必须为非负人民币金额字符串，最多两位小数。", field),
		}
	}
	r := new(big.Rat)
	if _, ok := r.SetString(value); !ok {
		return nil, &ErrInvalidProfile{
			Code:    "invalid_profile",
			Message: fmt.Sprintf("%s 必须为非负人民币金额字符串，最多两位小数。", field),
		}
	}
	return Quantize(r, 2), nil
}

// Quantize rounds r to the given number of decimal places (round half away from
// zero, matching Python Decimal.quantize default ROUND_HALF_EVEN is NOT used —
// FinScope inputs already have <= 2 digits so this only normalizes).
func Quantize(r *big.Rat, places int) *big.Rat {
	scale := new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(places)), nil)
	num := new(big.Int).Mul(r.Num(), scale)
	q := new(big.Int).Quo(num, r.Denom())
	return new(big.Rat).SetFrac(q, scale)
}

// FormatMoney renders a rational as a fixed two-decimal yuan string, the exact
// shape Python's format(Decimal, ".2f") produces.
func FormatMoney(r *big.Rat) string {
	q := Quantize(r, 2)
	s := q.FloatString(2)
	return s
}

// MoneyFields mirrors calculations.MONEY_FIELDS order.
var MoneyFields = []string{"monthly_income", "essential_expense", "available_balance", "goal_amount"}

// PreferenceFields mirrors calculations.PREFERENCE_FIELDS.
var PreferenceFields = []string{"liquidity_need", "risk_preference", "goal_kind"}

var preferenceOptions = map[string]map[string]bool{
	"liquidity_need":  {"immediate": true, "flexible": true, "locked": true},
	"risk_preference": {"low": true, "medium": true, "high": true},
	"goal_kind":       {"emergency": true, "purchase": true, "long_term": true},
}

// Profile is the normalized, validated input. Money fields are exact rationals;
// horizon and preferences are validated enums/integers.
type Profile struct {
	MonthlyIncome   *big.Rat
	EssentialExpense *big.Rat
	AvailableBalance *big.Rat
	GoalAmount      *big.Rat
	HorizonMonths   int
	LiquidityNeed   string
	RiskPreference  string
	GoalKind        string
	Currency        string
}

// ValidateProfile replicates calculations.validate_profile. When preferences is
// false, only money + horizon fields are required.
func ValidateProfile(raw map[string]string, preferences bool) (*Profile, error) {
	if raw == nil {
		return nil, &ErrInvalidProfile{Code: "invalid_profile", Message: "画像必须为对象。"}
	}
	required := append([]string{}, MoneyFields...)
	required = append(required, "horizon_months")
	if preferences {
		required = append(required, PreferenceFields...)
	}
	var missing []string
	for _, key := range required {
		if raw[key] == "" {
			missing = append(missing, key)
		}
	}
	if len(missing) > 0 {
		return nil, &ErrInvalidProfile{
			Code:          "missing_profile_fields",
			Message:       "请补充并确认字段：" + strings.Join(missing, "、"),
			MissingFields: missing,
		}
	}
	p := &Profile{Currency: "CNY"}
	var err error
	if p.MonthlyIncome, err = Money(raw["monthly_income"], "monthly_income"); err != nil {
		return nil, err
	}
	if p.EssentialExpense, err = Money(raw["essential_expense"], "essential_expense"); err != nil {
		return nil, err
	}
	if p.AvailableBalance, err = Money(raw["available_balance"], "available_balance"); err != nil {
		return nil, err
	}
	if p.GoalAmount, err = Money(raw["goal_amount"], "goal_amount"); err != nil {
		return nil, err
	}
	var horizon int64
	if _, err := fmt.Sscanf(raw["horizon_months"], "%d", &horizon); err != nil || horizon < 1 || horizon > 1200 {
		return nil, &ErrInvalidProfile{Code: "invalid_profile", Message: "horizon_months 必须是 1 至 1200 的整数。"}
	}
	p.HorizonMonths = int(horizon)
	if cur, ok := raw["currency"]; ok && cur != "" && cur != "CNY" {
		return nil, &ErrInvalidProfile{Code: "invalid_profile", Message: "当前计算器仅支持 CNY，不能自动转换币种。"}
	}
	if preferences {
		for _, key := range PreferenceFields {
			if !preferenceOptions[key][raw[key]] {
				return nil, &ErrInvalidProfile{Code: "invalid_profile", Message: key + " 的取值无效。"}
			}
		}
		p.LiquidityNeed = raw["liquidity_need"]
		p.RiskPreference = raw["risk_preference"]
		p.GoalKind = raw["goal_kind"]
	}
	return p, nil
}

// GoalResult mirrors calculate_goal's return dict, field names preserved so the
// JSON serialization matches Python's keys exactly.
type GoalResult struct {
	Operation                 string            `json:"operation"`
	Inputs                    map[string]any    `json:"inputs"`
	MonthlySurplus            string            `json:"monthly_surplus"`
	GoalGap                   string            `json:"goal_gap"`
	MonthsToGoal              *int              `json:"months_to_goal"`
	HorizonMonths             int               `json:"horizon_months"`
	HorizonFeasible           bool              `json:"horizon_feasible"`
	ProjectedBalanceAtHorizon string            `json:"projected_balance_at_horizon"`
	Status                    string            `json:"status"`
	Units                     map[string]string `json:"units"`
	Rounding                  string            `json:"rounding"`
	Assumptions               []string          `json:"assumptions"`
	FinancialAdvice           bool              `json:"financial_advice"`
}

// CalculateGoal replicates calculations.calculate_goal using exact rational
// arithmetic. Month count uses ceiling division on a positive surplus, matching
// Python ROUND_CEILING to_integral_value.
func CalculateGoal(raw map[string]string) (*GoalResult, error) {
	p, err := ValidateProfile(raw, false)
	if err != nil {
		return nil, err
	}
	surplus := new(big.Rat).Sub(p.MonthlyIncome, p.EssentialExpense)
	gap := new(big.Rat).Sub(p.GoalAmount, p.AvailableBalance)
	if gap.Sign() < 0 {
		gap = new(big.Rat)
	}

	var months *int
	status := ""
	switch {
	case gap.Sign() == 0:
		status = "already_achieved"
	case surplus.Sign() <= 0:
		status = "unreachable"
	default:
		m := CeilDiv(gap, surplus)
		months = &m
		status = "reachable"
	}

	horizon := p.HorizonMonths
	horizonFeasible := months != nil && *months <= horizon
	projected := new(big.Rat).Add(p.AvailableBalance, new(big.Rat).Mul(surplus, big.NewRat(int64(horizon), 1)))

	inputs := map[string]any{
		"monthly_income":   FormatMoney(p.MonthlyIncome),
		"essential_expense": FormatMoney(p.EssentialExpense),
		"available_balance": FormatMoney(p.AvailableBalance),
		"goal_amount":      FormatMoney(p.GoalAmount),
		"horizon_months":   horizon,
		"currency":         "CNY",
	}

	return &GoalResult{
		Operation:                 "cashflow_goal_v1",
		Inputs:                    inputs,
		MonthlySurplus:            FormatMoney(surplus),
		GoalGap:                   FormatMoney(gap),
		MonthsToGoal:              months,
		HorizonMonths:             horizon,
		HorizonFeasible:           horizonFeasible,
		ProjectedBalanceAtHorizon: FormatMoney(projected),
		Status:                    status,
		Units:                     map[string]string{"money": "CNY yuan", "duration": "whole months"},
		Rounding:                  "金额输入最多两位小数；达成月数向上取整，不舍入金额。",
		Assumptions: []string{
			"每月收入与必要支出保持不变，每月结余全部用于该单一目标。",
			"忽略利息、投资收益、通胀、税费及收入支出的未来变化。",
			"按完整月份累计；初始可用余额均可用于该目标。",
			"期限末余额为线性收支推算，负值表示现金流缺口，不表示可借款额度。",
		},
		FinancialAdvice: false,
	}, nil
}

// CeilDiv returns ceil(num / denom) for positive num and denom.
func CeilDiv(num, denom *big.Rat) int {
	q := new(big.Rat).Quo(num, denom)
	i := new(big.Int).Quo(q.Num(), q.Denom())
	if new(big.Int).Rem(q.Num(), q.Denom()).Sign() != 0 {
		i.Add(i, big.NewInt(1))
	}
	return int(i.Int64())
}
