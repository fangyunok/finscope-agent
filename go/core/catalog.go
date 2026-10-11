package core

import (
	"encoding/json"
	"os"
	"sort"
)

// CatalogDocument is the shape of src/finscope/data/catalog.json. The Go
// sidecar reads the exact same fixture file the Python oracle reads, so the two
// implementations share a single source of truth for product data.
type CatalogDocument struct {
	SchemaVersion int       `json:"schema_version"`
	Synthetic     bool      `json:"synthetic"`
	Description   string    `json:"description"`
	Products      []Product `json:"products"`
}

// LoadCatalog reads and parses a catalog.json file.
func LoadCatalog(path string) (*CatalogDocument, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var doc CatalogDocument
	if err := json.Unmarshal(b, &doc); err != nil {
		return nil, err
	}
	return &doc, nil
}

// ActiveProducts returns products active on the given as_of date
// (effective_from <= as_of < effective_to), mirroring catalog._active.
func (d *CatalogDocument) ActiveProducts(asOf string) []*Product {
	var out []*Product
	for i := range d.Products {
		p := &d.Products[i]
		if dateLE(p.EffectiveFrom, asOf) && dateLT(asOf, p.EffectiveTo) {
			out = append(out, p)
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		return out[i].ProductID < out[j].ProductID
	})
	return out
}

// dateLE / dateLT compare YYYY-MM-DD strings lexicographically, which is valid
// for zero-padded ISO dates.
func dateLE(a, b string) bool { return a <= b }
func dateLT(a, b string) bool { return a < b }
