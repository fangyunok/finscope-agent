package core

import (
	"encoding/json"
	"regexp"
	"sort"
	"strings"
)

// RiskLevel and LiquidityLevel mirror matching.RISK_LEVEL / LIQUIDITY_LEVEL.
var RiskLevel = map[string]int{"low": 0, "medium": 1, "high": 2}
var LiquidityLevel = map[string]int{"immediate": 0, "flexible": 1, "locked": 2}

// Clause mirrors a single product clause. Field names match catalog.json.
type Clause struct {
	TermID  string `json:"term_id"`
	Kind    string `json:"kind"`
	Content string `json:"content"`
}

// Product mirrors a catalog.json product record. Validation errors are computed
// by ValidateProducts (the Go equivalent of catalog._validation_errors).
type Product struct {
	ProductID      string            `json:"product_id"`
	Name           string            `json:"name"`
	Version        string            `json:"version"`
	EffectiveFrom  string            `json:"effective_from"`
	EffectiveTo    string            `json:"effective_to"`
	Synthetic      bool              `json:"synthetic"`
	Currency       string            `json:"currency"`
	AmountBounds   map[string]string `json:"amount_bounds"`
	LockMonths     int               `json:"lock_months"`
	RiskLabel      string            `json:"risk_label"`
	Liquidity      string            `json:"liquidity"`
	FeesBps        int               `json:"fees_bps"`
	FeesFlat       string            `json:"fees_flat"`
	SupportedGoals []string          `json:"supported_goals"`
	Clauses        []Clause          `json:"clauses"`
}

// Reject is one hard-constraint rejection reason, mirroring matching.reject.
type Reject struct {
	Code        string   `json:"code"`
	Message     string   `json:"message"`
	SourceTerms []string `json:"source_terms"`
}

// EligibleProduct mirrors an eligible (scored) product entry.
type EligibleProduct struct {
	ProductID       string            `json:"product_id"`
	Name            string            `json:"name"`
	Version         string            `json:"version"`
	Score           int               `json:"score"`
	ScoreComponents map[string]int    `json:"score_components"`
	RiskLabel       string            `json:"risk_label"`
	Liquidity       string            `json:"liquidity"`
	LockMonths      int               `json:"lock_months"`
	AmountBounds    map[string]string `json:"amount_bounds"`
	FeesBps         int               `json:"fees_bps"`
	FeesFlat        string            `json:"fees_flat"`
	Reason          string            `json:"reason"`
	SourceTerms     []string          `json:"source_terms"`
	Synthetic       bool              `json:"synthetic"`
}

// ExcludedProduct mirrors an excluded product entry.
type ExcludedProduct struct {
	ProductID       string   `json:"product_id"`
	Name            string   `json:"name"`
	Reasons         []Reject `json:"reasons"`
	ValidationErrors []string `json:"validation_errors"`
	Synthetic       bool     `json:"synthetic"`
}

// MatchResult mirrors matching.match_products return dict.
type MatchResult struct {
	Status           string            `json:"status"`
	Eligible         []EligibleProduct `json:"eligible"`
	Excluded         []ExcludedProduct `json:"excluded"`
	Calculation      *GoalResult       `json:"calculation"`
	ProfileInputs    map[string]any    `json:"profile_inputs"`
	AsOf             string            `json:"as_of"`
	CatalogFingerprint string           `json:"catalog_fingerprint"`
	RankingVersion   string            `json:"ranking_version"`
	RankingRules     map[string]string `json:"ranking_rules"`
	Assumptions      []string          `json:"assumptions"`
	Synthetic        bool              `json:"synthetic"`
	FinancialAdvice  bool              `json:"financial_advice"`
}

const RankingVersion = "rule-baseline-v1"

// referenceTerms extracts the term ids referenced by a rejection, mirroring
// matching._references filtered by kind. The Python version returns full objects;
// for the sidecar's JSON contract we keep the term ids plus the content quote
// available on the product, but here we expose term IDs for brevity — the full
// reference objects are kept in the Python oracle.
func referenceTerms(product *Product, kind string) []string {
	var out []string
	for _, clause := range product.Clauses {
		if clause.TermID == "" || clause.Content == "" {
			continue
		}
		if kind == "" || clause.Kind == kind {
			out = append(out, clause.TermID)
		}
	}
	return out
}

// MatchProducts replicates matching.match_products. It validates the profile
// (with preferences), applies the hard constraints in order, scores survivors,
// and sorts by (-score, product_id).
func MatchProducts(raw map[string]string, products []*Product, asOf string, excludedIDs map[string]bool) (*MatchResult, error) {
	// Validate profile including preferences; the validated *Profile carries the
	// typed money values and enum fields used for scoring.
	profile, err := ValidateProfile(raw, true)
	if err != nil {
		return nil, err
	}
	values, err := normalizedInputs(raw)
	if err != nil {
		return nil, err
	}

	available := profile.AvailableBalance

	var eligible []EligibleProduct
	var excluded []ExcludedProduct

	for _, product := range products {
		var reasons []Reject
		reject := func(code, message, kind string) {
			reasons = append(reasons, Reject{Code: code, Message: message, SourceTerms: referenceTerms(product, kind)})
		}

		verrs := validateProduct(product)
		if len(verrs) > 0 {
			reject("catalog_incomplete_or_conflicting", "条款结构缺失、无效或有版本冲突："+strings.Join(verrs, ", "), "")
		} else {
			lower, _ := Money(product.AmountBounds["min"], "min")
			upper, _ := Money(product.AmountBounds["max"], "max")
			if available.Cmp(lower) < 0 || available.Cmp(upper) > 0 {
				reject("amount_out_of_bounds", "可用余额超出金额区间。", "amount")
			}
			if product.LockMonths > profile.HorizonMonths {
				reject("lock_exceeds_horizon", "锁定月份超过目标期限。", "lock")
			}
			if LiquidityLevel[product.Liquidity] > LiquidityLevel[profile.LiquidityNeed] {
				reject("liquidity_mismatch", "产品流动性低于用户明确确认的取用要求。", "liquidity")
			}
			if RiskLevel[product.RiskLabel] > RiskLevel[profile.RiskPreference] {
				reject("risk_exceeds_preference", "模拟风险标签高于用户明确确认的风险上限。", "risk")
			}
			if profile.GoalKind == "emergency" && product.Liquidity != "immediate" {
				reject("emergency_requires_immediate", "应急目标采用仅允许立即取用的固定硬约束。", "liquidity")
			}
		}
		if excludedIDs[product.ProductID] {
			reject("explicit_user_exclusion", "用户已确认排除该产品。", "")
		}

		if len(reasons) > 0 {
			excluded = append(excluded, ExcludedProduct{
				ProductID:        product.ProductID,
				Name:             orDefault(product.Name, product.ProductID),
				Reasons:          reasons,
				ValidationErrors: verrs,
				Synthetic:        true,
			})
			continue
		}

		targetLock := 0
		if profile.LiquidityNeed == "locked" && profile.GoalKind != "emergency" {
			targetLock = profile.HorizonMonths
		}
		goalFit := 10
		if contains(product.SupportedGoals, profile.GoalKind) {
			goalFit = 40
		}
		liquidityFit := 20
		if profile.LiquidityNeed == product.Liquidity {
			liquidityFit = 30
		}
		termFit := 20 - abs(targetLock-product.LockMonths)
		if termFit < 0 {
			termFit = 0
		}
		riskFit := 5
		if profile.RiskPreference == product.RiskLabel {
			riskFit = 10
		}
		feePenalty := -(product.FeesBps / 10)
		if feePenalty < -10 {
			feePenalty = -10
		}
		components := map[string]int{
			"goal_fit":     goalFit,
			"liquidity_fit": liquidityFit,
			"term_fit":     termFit,
			"risk_fit":     riskFit,
			"fee_penalty":  feePenalty,
		}
		score := goalFit + liquidityFit + termFit + riskFit + feePenalty

		eligible = append(eligible, EligibleProduct{
			ProductID:       product.ProductID,
			Name:            product.Name,
			Version:         product.Version,
			Score:           score,
			ScoreComponents: components,
			RiskLabel:       product.RiskLabel,
			Liquidity:       product.Liquidity,
			LockMonths:      product.LockMonths,
			AmountBounds:    product.AmountBounds,
			FeesBps:         product.FeesBps,
			FeesFlat:        product.FeesFlat,
			Reason:          "满足可用金额、期限、流动性与确认的风险约束；评分按固定规则计算。",
			SourceTerms:     referenceTerms(product, ""),
			Synthetic:       true,
		})
	}

	sort.SliceStable(eligible, func(i, j int) bool {
		if eligible[i].Score != eligible[j].Score {
			return eligible[i].Score > eligible[j].Score
		}
		return eligible[i].ProductID < eligible[j].ProductID
	})

	goal, err := CalculateGoal(raw)
	if err != nil {
		return nil, err
	}

	status := "matched"
	if len(eligible) == 0 {
		status = "no_eligible_products"
	}

	return &MatchResult{
		Status:              status,
		Eligible:            eligible,
		Excluded:            excluded,
		Calculation:         goal,
		ProfileInputs:       values,
		AsOf:                asOf,
		CatalogFingerprint:  fingerprintProducts(products, asOf),
		RankingVersion:      RankingVersion,
		RankingRules:        rankingRules(),
		Assumptions:         matchingAssumptions(),
		Synthetic:           true,
		FinancialAdvice:     false,
	}, nil
}

// normalizedInputs returns the validated, string-normalized profile inputs in
// the same shape Python's validate_profile(preferences=True) produces.
func normalizedInputs(raw map[string]string) (map[string]any, error) {
	p, err := ValidateProfile(raw, true)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"monthly_income":    FormatMoney(p.MonthlyIncome),
		"essential_expense": FormatMoney(p.EssentialExpense),
		"available_balance": FormatMoney(p.AvailableBalance),
		"goal_amount":       FormatMoney(p.GoalAmount),
		"horizon_months":    p.HorizonMonths,
		"currency":          "CNY",
		"liquidity_need":    p.LiquidityNeed,
		"risk_preference":   p.RiskPreference,
		"goal_kind":         p.GoalKind,
	}, nil
}

func contains(items []string, s string) bool {
	for _, it := range items {
		if it == s {
			return true
		}
	}
	return false
}

func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}

func orDefault(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

func rankingRules() map[string]string {
	return map[string]string{
		"goal_fit":     "目标在模拟产品的支持目标中得 40 分，否则 10 分。",
		"liquidity_fit": "完全一致得 30 分；满足更宽松要求得 20 分。",
		"term_fit":     "max(0, 20 - |目标锁定月份 - 产品锁定月份|)；立即/灵活取用或应急目标的目标锁定月份为 0。",
		"risk_fit":     "模拟风险标签与确认上限一致得 10 分，更低得 5 分。",
		"fee_penalty":  "-min(10, 费用基点整除 10)；固定费用不参与评分。",
		"tie_break":    "同分按 product_id 升序；模型不能调整权重或改写分数。",
	}
}

func matchingAssumptions() []string {
	return []string{
		"金额区间按全部可用余额作为单一产品的模拟匹配金额；未计算分配比例或组合。",
		"风险偏好为用户明确确认的标签上限，不从聊天、点击或兴趣推断。",
		"流动性 immediate 只允许立即取用；flexible 允许立即或灵活取用；locked 允许三类。",
		"应急目标额外限制立即取用，与其他流动性要求同时生效。",
		"硬约束匹配与现金流达成能力分别呈现；有候选不表示目标可按期达成。",
		"分数是版本化设计选择，未由真实行为训练，不能代表收益或投资质量。",
	}
}

// validateProduct mirrors catalog._validation_errors.
func validateProduct(p *Product) []string {
	var errors []string
	if p.Currency != "CNY" {
		errors = append(errors, "missing_or_invalid_currency")
	}
	if strings.TrimSpace(p.Name) == "" {
		errors = append(errors, "missing_name")
	}
	if strings.TrimSpace(p.Version) == "" {
		errors = append(errors, "missing_version")
	}
	if p.AmountBounds == nil {
		errors = append(errors, "missing_amount_bounds")
	} else {
		lower, lerr := Money(p.AmountBounds["min"], "min")
		upper, uerr := Money(p.AmountBounds["max"], "max")
		if lerr != nil || uerr != nil || lower.Cmp(upper) > 0 {
			errors = append(errors, "invalid_amount_bounds")
		}
	}
	if p.LockMonths < 0 || p.LockMonths > 1200 {
		errors = append(errors, "missing_or_invalid_lock_months")
	}
	if p.RiskLabel != "low" && p.RiskLabel != "medium" && p.RiskLabel != "high" {
		errors = append(errors, "missing_or_invalid_risk_label")
	}
	if p.Liquidity != "immediate" && p.Liquidity != "flexible" && p.Liquidity != "locked" {
		errors = append(errors, "missing_or_invalid_liquidity")
	}
	if p.LockMonths > 0 && (p.Liquidity == "immediate" || p.Liquidity == "flexible") {
		errors = append(errors, "inconsistent_liquidity_and_lock")
	}
	if p.FeesBps < 0 || p.FeesBps > 10000 {
		errors = append(errors, "missing_or_invalid_fees_bps")
	}
	if _, err := Money(p.FeesFlat, "fees_flat"); err != nil {
		errors = append(errors, "missing_or_invalid_fees_flat")
	}
	if len(p.SupportedGoals) == 0 {
		errors = append(errors, "missing_or_invalid_supported_goals")
	} else {
		for _, g := range p.SupportedGoals {
			if g != "emergency" && g != "purchase" && g != "long_term" {
				errors = append(errors, "missing_or_invalid_supported_goals")
				break
			}
		}
	}
	if len(p.Clauses) == 0 {
		errors = append(errors, "missing_clauses")
	} else {
		seen := map[string]bool{}
		kinds := map[string]bool{}
		for _, c := range p.Clauses {
			if c.TermID == "" || c.Kind == "" || c.Content == "" {
				errors = append(errors, "invalid_clause")
				continue
			}
			if !regexpTermID(c.TermID) {
				errors = append(errors, "invalid_term_id")
			}
			if c.Kind != "amount" && c.Kind != "lock" && c.Kind != "risk" && c.Kind != "liquidity" && c.Kind != "fees" && c.Kind != "goal" {
				errors = append(errors, "invalid_clause_kind")
			}
			if seen[c.TermID] {
				errors = append(errors, "duplicate_term_id")
			}
			seen[c.TermID] = true
			kinds[c.Kind] = true
		}
		for _, k := range []string{"amount", "lock", "risk", "liquidity", "fees", "goal"} {
			if !kinds[k] {
				errors = append(errors, "missing_constraint_clauses")
				break
			}
		}
	}
	return uniqueSorted(errors)
}

var termIDRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,96}$`)

func regexpTermID(s string) bool { return termIDRe.MatchString(s) }

func uniqueSorted(items []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, it := range items {
		if !seen[it] {
			seen[it] = true
			out = append(out, it)
		}
	}
	sort.Strings(out)
	return out
}

// fingerprintProducts is a stable canonical hash of the active products for the
// given as_of date. It is deliberately a structural fingerprint (sorted JSON),
// matching the Python catalog.fingerprint contract in intent. The exact value is
// pinned by golden tests against the Python oracle.
func fingerprintProducts(products []*Product, asOf string) string {
	sorted := make([]*Product, len(products))
	copy(sorted, products)
	sort.Slice(sorted, func(i, j int) bool {
		return sorted[i].ProductID < sorted[j].ProductID
	})
	b, err := json.Marshal(map[string]any{"as_of": asOf, "products": sorted})
	if err != nil {
		return ""
	}
	return sha256Hex(b)
}
