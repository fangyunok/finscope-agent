"""Actual in-process MCP tools bound to trusted server identity and memory epoch."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from mcp import Client
from mcp.server import MCPServer
from pydantic import ValidationError

from .model import validated_patch
from .service import DomainError, FinanceService

TOOL_NAMES = {"get_my_profile", "search_financial_terms", "calculate_financial_goal", "match_products", "propose_profile_update"}


def create_tool_server(service: FinanceService, principal, *, memory_epoch: int | None = None, source: str = "model") -> MCPServer:
    server = MCPServer("FinScope authorized financial tools", version="0.1.0")

    def invoke(function, *args, **kwargs):
        try:
            return {"ok": True, "result": function(principal, *args, **kwargs)}
        except DomainError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc), "status_code": exc.status_code}}
        except (ValidationError, ValueError, TypeError):
            return {"ok": False, "error": {"code": "invalid_tool_input", "message": "Tool input is invalid", "status_code": 400}}

    @server.tool()
    def get_my_profile() -> dict[str, Any]:
        """Read only this authenticated user's confirmed profile."""
        return invoke(service.get_profile)

    @server.tool()
    def search_financial_terms(query: str = "", as_of: str = "2026-10-03", product_id: str | None = None) -> dict[str, Any]:
        """Read effective synthetic product clauses and their source metadata."""
        return invoke(service.search_terms, query=query, as_of=as_of, product_id=product_id)

    @server.tool()
    def calculate_financial_goal() -> dict[str, Any]:
        """Calculate from the confirmed profile with deterministic decimal arithmetic."""
        return invoke(service.calculate)

    @server.tool()
    def match_products(as_of: str = "2026-10-03") -> dict[str, Any]:
        """Filter and rank synthetic products using confirmed explicit constraints."""
        return invoke(service.match, as_of=as_of)

    @server.tool()
    def propose_profile_update(patch: dict[str, Any], expected_version: int) -> dict[str, Any]:
        """Create a pending proposal only; human confirmation is a separate API."""
        try:
            parsed = validated_patch(patch)
        except (ValueError, TypeError):
            return {"ok": False, "error": {"code": "invalid_tool_input", "message": "Invalid profile fields", "status_code": 400}}
        return invoke(service.propose_profile_update, parsed, source=source, expected_version=expected_version,
                      expected_memory_epoch=memory_epoch)

    return server


class ToolGateway:
    def __init__(self, service: FinanceService, principal, *, memory_epoch: int | None = None, source: str = "model", timeout: float = 15):
        self.server = create_tool_server(service, principal, memory_epoch=memory_epoch, source=source)
        self.timeout = timeout

    async def call(self, name: str, arguments: dict | None = None) -> tuple[Any, dict]:
        if name not in TOOL_NAMES:
            raise DomainError("unknown_tool", "Tool is not in the business allowlist")
        allowed = {"get_my_profile": set(), "calculate_financial_goal": set(),
                   "match_products": {"as_of"}, "search_financial_terms": {"query", "as_of", "product_id"},
                   "propose_profile_update": {"patch", "expected_version"}}[name]
        if arguments is not None and (not isinstance(arguments, dict) or set(arguments) - allowed):
            raise DomainError("invalid_tool_input", "Unsupported tool arguments; identity is server-bound")
        if name == "propose_profile_update" and (not arguments or type(arguments.get("expected_version")) is not int):
            raise DomainError("invalid_tool_input", "An exact profile version is required")
        started = time.perf_counter()

        async def perform():
            async with Client(self.server) as client:
                response = await client.call_tool(name, arguments or {})
                envelope = response.structured_content
                if envelope is None:
                    for block in response.content:
                        if getattr(block, "type", None) == "text":
                            try:
                                envelope = json.loads(block.text)
                            except (ValueError, TypeError):
                                continue
                            break
            if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
                raise DomainError("tool_protocol_error", "Invalid MCP response", 502)
            if not envelope["ok"]:
                error = envelope.get("error")
                if not isinstance(error, dict):
                    raise DomainError("tool_protocol_error", "Invalid MCP error", 502)
                raise DomainError(error.get("code", "tool_error"), error.get("message", "Business tool failed"), error.get("status_code", 400))
            return envelope.get("result")
        try:
            result = await asyncio.wait_for(perform(), timeout=self.timeout)
        except TimeoutError:
            raise DomainError("tool_timeout", "Business tool timed out", 504) from None
        except DomainError:
            raise
        except Exception:
            raise DomainError("tool_transport_error", "MCP tool input or transport failed", 502) from None
        return result, {"tool": name, "duration_ms": round((time.perf_counter() - started) * 1000), "ok": True}
