# Build the Go calculation sidecar.
FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY . .
RUN go build -o /finscope-sidecar ./cmd/finscope-sidecar

# Minimal runtime; the catalog.json is mounted at /data/catalog.json.
FROM alpine:3.20
RUN adduser -D -u 10001 app
COPY --from=build /finscope-sidecar /usr/local/bin/finscope-sidecar
USER app
EXPOSE 8081
ENV FINSCOPE_CATALOG=/data/catalog.json
ENTRYPOINT ["finscope-sidecar"]
