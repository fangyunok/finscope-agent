package core

import (
	"regexp"
	"sort"
	"strings"
)

// tokenRe mirrors catalog.py's retrieval tokenizer: ASCII runs of
// [a-z0-9_-]+ OR individual CJK characters ([\u4e00-\u9fff]).
var tokenRe = regexp.MustCompile(`[a-z0-9_-]+|[\x{4e00}-\x{9fff}]`)

// SearchHit mirrors a single catalog.search result row.
type SearchHit struct {
	ProductID              string   `json:"product_id"`
	ProductName            string   `json:"product_name"`
	TermID                 string   `json:"term_id"`
	Kind                   string   `json:"kind"`
	Version                string   `json:"version"`
	EffectiveFrom          string   `json:"effective_from"`
	EffectiveTo            string   `json:"effective_to"`
	AsOf                   string   `json:"as_of"`
	Content                string   `json:"content"`
	Quote                  string   `json:"quote"`
	Synthetic              bool     `json:"synthetic"`
	CatalogValidationErrors []string `json:"catalog_validation_errors"`
	RetrievalScore         int      `json:"retrieval_score"`
	RetrievalMethod        string   `json:"retrieval_method"`
}

// SearchTerms replicates catalog.search: tokenized keyword overlap scoring plus
// an exact-substring bonus, sorted by (-score, product_id, term_id), limited to
// limit results. It returns hits for the products active on as_of.
func (d *CatalogDocument) SearchTerms(query, asOf string, productID string, limit int) ([]SearchHit, error) {
	if query == "" || len(query) > 1000 {
		return nil, &ErrInvalidProfile{Code: "invalid_query", Message: "query 必须为不超过 1000 字的字符串。"}
	}
	if productID != "" && len(productID) > 64 {
		return nil, &ErrInvalidProfile{Code: "invalid_query", Message: "product_id 无效。"}
	}
	if limit < 1 || limit > 100 {
		return nil, &ErrInvalidProfile{Code: "invalid_query", Message: "limit 必须为 1 至 100 的整数。"}
	}

	stripped := strings.TrimSpace(strings.ToLower(query))
	queryTokens := tokenSet(stripped)

	var hits []SearchHit
	for _, product := range d.ActiveProducts(asOf) {
		verrs := validateProduct(product)
		excluded := false
		for _, e := range verrs {
			if e == "version_conflict" || e == "ambiguous_term_id" {
				excluded = true
				break
			}
		}
		if excluded || (productID != "" && product.ProductID != productID) {
			continue
		}
		for _, clause := range product.Clauses {
			if clause.TermID == "" || clause.Content == "" || !regexpTermID(clause.TermID) {
				continue
			}
			document := strings.ToLower(strings.Join([]string{
				product.ProductID, product.Name, clause.Kind, clause.Content,
			}, " "))
			tokens := tokenSet(document)
			score := intersectSize(queryTokens, tokens)
			if stripped != "" && strings.Contains(document, stripped) {
				score += 20
			}
			if stripped != "" && score == 0 {
				continue
			}
			hits = append(hits, SearchHit{
				ProductID:              product.ProductID,
				ProductName:            orDefault(product.Name, product.ProductID),
				TermID:                 clause.TermID,
				Kind:                   clause.Kind,
				Version:                product.Version,
				EffectiveFrom:          product.EffectiveFrom,
				EffectiveTo:            product.EffectiveTo,
				AsOf:                   asOf,
				Content:                clause.Content,
				Quote:                  clause.Content,
				Synthetic:              true,
				CatalogValidationErrors: verrs,
				RetrievalScore:         score,
				RetrievalMethod:        "keyword_character_overlap_v1",
			})
		}
	}

	sort.SliceStable(hits, func(i, j int) bool {
		if hits[i].RetrievalScore != hits[j].RetrievalScore {
			return hits[i].RetrievalScore > hits[j].RetrievalScore
		}
		if hits[i].ProductID != hits[j].ProductID {
			return hits[i].ProductID < hits[j].ProductID
		}
		return hits[i].TermID < hits[j].TermID
	})

	if len(hits) > limit {
		hits = hits[:limit]
	}
	return hits, nil
}

// tokenSet lowercases and tokenizes input into a set, mirroring the Python
// tokenizer's set semantics.
func tokenSet(s string) map[string]bool {
	lower := strings.ToLower(s)
	out := map[string]bool{}
	for _, m := range tokenRe.FindAllString(lower, -1) {
		out[m] = true
	}
	return out
}

// intersectSize returns the count of keys shared by both sets.
func intersectSize(a, b map[string]bool) int {
	n := 0
	for k := range a {
		if b[k] {
			n++
		}
	}
	return n
}
