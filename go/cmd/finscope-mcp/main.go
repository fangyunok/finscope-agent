// Command finscope-mcp runs the FinScope calculation tools as a stdio MCP server.
//
// It is the Go counterpart to the Python service's MCP tool gateway: a real MCP
// server that any host (Claude Desktop, an Eino graph, an IDE) can connect to
// over stdio. Only the stateless calculation/retrieval tools are exposed here;
// the stateful memory tools remain in the Python service.
//
// Usage:
//
//	FINSCOPE_CATALOG=/path/to/catalog.json finscope-mcp
package main

import (
	"log"
	"os"

	"github.com/fangyunok/finscope-agent/go/mcpserver"
	"github.com/mark3labs/mcp-go/server"
)

func main() {
	catalogPath := os.Getenv("FINSCOPE_CATALOG")
	if catalogPath == "" {
		catalogPath = "../src/finscope/data/catalog.json"
	}
	s, err := mcpserver.Server(catalogPath)
	if err != nil {
		log.Fatalf("构建 MCP server 失败: %v", err)
	}
	if err := server.ServeStdio(s); err != nil {
		log.Fatalf("MCP server 运行失败: %v", err)
	}
}
