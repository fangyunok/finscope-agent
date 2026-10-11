package core

import (
	"testing"
)

// TestSearchTermsTokenization pins the tokenizer semantics to the Python
// catalog.search: ASCII runs + single CJK characters, overlap scoring plus a +20
// exact-substring bonus.
func TestSearchTermsTokenization(t *testing.T) {
	doc := &CatalogDocument{
		Synthetic: true,
		Products: []Product{
			{
				ProductID:      "FS-TERM-3",
				Name:           "三月固定计划",
				Version:        "2026.1",
				EffectiveFrom:  "2026-01-01",
				EffectiveTo:    "2027-01-01",
				Synthetic:      true,
				Currency:       "CNY",
				AmountBounds:   map[string]string{"min": "1000.00", "max": "200000.00"},
				LockMonths:     3,
				RiskLabel:      "low",
				Liquidity:      "locked",
				FeesBps:        0,
				FeesFlat:       "0.00",
				SupportedGoals: []string{"purchase", "long_term"},
				Clauses: []Clause{
					{TermID: "FS-TERM-3-amount", Kind: "amount", Content: "模拟匹配金额区间为 1000.00 至 200000.00 元人民币。"},
					{TermID: "FS-TERM-3-lock", Kind: "lock", Content: "模拟锁定期为 3 个完整月。"},
					{TermID: "FS-TERM-3-risk", Kind: "risk", Content: "本演示目录的显式风险标签为低。"},
					{TermID: "FS-TERM-3-liquidity", Kind: "liquidity", Content: "模拟流动性为锁定期内不可取用。"},
					{TermID: "FS-TERM-3-fees", Kind: "fees", Content: "模拟一次性按匹配金额计费 0 个基点。"},
					{TermID: "FS-TERM-3-goal", Kind: "goal", Content: "模拟支持目标为计划购买、长期目标。"},
				},
			},
		},
	}

	// query "锁定" -> tokens {锁,定}. The lock clause content contains both
	// "锁" and "定", so its overlap is 2; no +20 substring bonus for "锁定"
	// (the clause does not contain the exact bigram "锁定").
	hits, err := doc.SearchTerms("锁定", "2026-10-03", "", 12)
	if err != nil {
		t.Fatalf("SearchTerms error: %v", err)
	}
	if len(hits) == 0 {
		t.Fatal("expected at least one hit")
	}
	top := hits[0]
	if top.RetrievalScore < 2 {
		t.Errorf("top score = %d, want >= 2 (锁+定 overlap)", top.RetrievalScore)
	}
	if top.RetrievalMethod != "keyword_character_overlap_v1" {
		t.Errorf("retrieval_method = %q", top.RetrievalMethod)
	}
	if !top.Synthetic {
		t.Errorf("synthetic should be true")
	}
}

func TestSearchTermsExactSubstringBonus(t *testing.T) {
	doc := &CatalogDocument{
		Synthetic: true,
		Products: []Product{
			{
				ProductID:      "FS-CASH",
				Name:           "即时备用账户",
				Version:        "2026.1",
				EffectiveFrom:  "2026-01-01",
				EffectiveTo:    "2027-01-01",
				Synthetic:      true,
				Currency:       "CNY",
				AmountBounds:   map[string]string{"min": "0.00", "max": "100000.00"},
				LockMonths:     0,
				RiskLabel:      "low",
				Liquidity:      "immediate",
				FeesBps:        0,
				FeesFlat:       "0.00",
				SupportedGoals: []string{"emergency", "purchase"},
				Clauses: []Clause{
					{TermID: "FS-CASH-amount", Kind: "amount", Content: "模拟匹配金额区间为 0.00 至 100000.00 元人民币。"},
					{TermID: "FS-CASH-lock", Kind: "lock", Content: "模拟锁定期为 0 个完整月。"},
					{TermID: "FS-CASH-risk", Kind: "risk", Content: "本演示目录的显式风险标签为低。"},
					{TermID: "FS-CASH-liquidity", Kind: "liquidity", Content: "模拟流动性为立即取用。"},
					{TermID: "FS-CASH-fees", Kind: "fees", Content: "模拟一次性按匹配金额计费 0 个基点。"},
					{TermID: "FS-CASH-goal", Kind: "goal", Content: "模拟支持目标为应急备用、计划购买。"},
				},
			},
		},
	}

	// "立即取用" appears verbatim in the liquidity clause -> +20 bonus on top
	// of the single-char overlap.
	hits, err := doc.SearchTerms("立即取用", "2026-10-03", "", 12)
	if err != nil {
		t.Fatalf("SearchTerms error: %v", err)
	}
	if len(hits) == 0 {
		t.Fatal("expected hits")
	}
	top := hits[0]
	if top.TermID != "FS-CASH-liquidity" {
		t.Errorf("top term = %q, want FS-CASH-liquidity", top.TermID)
	}
	if top.RetrievalScore < 20 {
		t.Errorf("score = %d, want >= 20 (substring bonus)", top.RetrievalScore)
	}
}

func TestSearchTermsEmptyQuery(t *testing.T) {
	doc := &CatalogDocument{Synthetic: true}
	_, err := doc.SearchTerms("", "2026-10-03", "", 12)
	if err == nil {
		t.Fatal("expected error for empty query")
	}
	if pe, ok := err.(*ErrInvalidProfile); !ok || pe.Code != "invalid_query" {
		t.Errorf("error = %v, want invalid_query", err)
	}
}
