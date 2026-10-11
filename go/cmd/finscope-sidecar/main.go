// Command finscope-sidecar exposes the Go deterministic calculation core over a
// minimal HTTP surface, mirroring the two pure endpoints of the Python service:
//
//	POST /calculate   — cash-flow goal arithmetic (calculate_goal)
//	POST /match       — hard-constraint product matching (match_products)
//
// It reads the shared catalog.json so the Go results are computed over the exact
// same product data the Python oracle uses. The Python service remains the source
// of truth for model extraction, MCP tool routing, and SQLite-backed memory; this
// binary exists to run the pure calculation hot-path in Go.
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/fangyunok/finscope-agent/go/core"
)

type service struct {
	catalog *core.CatalogDocument
}

type calculateRequest struct {
	Profile map[string]string `json:"profile"`
}

type matchRequest struct {
	Profile          map[string]string `json:"profile"`
	AsOf             string            `json:"as_of"`
	ExcludedProducts []string          `json:"excluded_product_ids"`
}

func (s *service) handleCalculate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req calculateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "请求体不是有效 JSON。")
		return
	}
	result, err := core.CalculateGoal(req.Profile)
	if err != nil {
		code := "invalid_profile"
		if pe, ok := err.(*core.ErrInvalidProfile); ok {
			code = pe.Code
		}
		writeError(w, http.StatusBadRequest, code, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *service) handleMatch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req matchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "请求体不是有效 JSON。")
		return
	}
	asOf := req.AsOf
	if asOf == "" {
		asOf = "2026-10-03"
	}
	excluded := map[string]bool{}
	for _, id := range req.ExcludedProducts {
		excluded[id] = true
	}
	products := s.catalog.ActiveProducts(asOf)
	result, err := core.MatchProducts(req.Profile, products, asOf, excluded)
	if err != nil {
		code := "invalid_profile"
		if pe, ok := err.(*core.ErrInvalidProfile); ok {
			code = pe.Code
		}
		writeError(w, http.StatusBadRequest, code, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]string{"error": code, "message": message})
}

func main() {
	catalogPath := os.Getenv("FINSCOPE_CATALOG")
	if catalogPath == "" {
		catalogPath = "../src/finscope/data/catalog.json"
	}
	catalog, err := core.LoadCatalog(catalogPath)
	if err != nil {
		log.Fatalf("加载产品目录失败 %s: %v", catalogPath, err)
	}
	s := &service{catalog: catalog}

	mux := http.NewServeMux()
	mux.HandleFunc("/calculate", s.handleCalculate)
	mux.HandleFunc("/match", s.handleMatch)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "service": "finscope-go-sidecar", "version": "0.1.0"})
	})

	addr := os.Getenv("FINSCOPE_ADDR")
	if addr == "" {
		addr = ":8081"
	}
	server := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("finscope-go-sidecar listening on %s", addr)
	if err := server.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
