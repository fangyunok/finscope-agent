# Build the Go calculation sidecar and MCP server.
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o /finscope-sidecar ./cmd/finscope-sidecar
RUN go build -o /finscope-mcp ./cmd/finscope-mcp

# Minimal runtime; the catalog.json is mounted at /data/catalog.json.
FROM alpine:3.20
RUN adduser -D -u 10001 app
COPY --from=build /finscope-sidecar /usr/local/bin/finscope-sidecar
COPY --from=build /finscope-mcp /usr/local/bin/finscope-mcp
USER app
EXPOSE 8081
ENV FINSCOPE_CATALOG=/data/catalog.json
ENTRYPOINT ["finscope-sidecar"]
