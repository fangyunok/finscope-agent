"""Real MCP orchestration with HTTP protocol fixtures, not real model evaluation."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp import Client

from finscope.agent import AgentRunner, TermQA
from finscope.model import FixtureExtractor, HttpExtractor, ModelError
from finscope.resources import data_path
from finscope.service import DomainError, FinanceService
from finscope.tools import ToolGateway, create_tool_server


def model_reply(value, usage=None):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}], "usage": usage or {"prompt_tokens": 22, "completion_tokens": 11}})


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "finscope.sqlite"
        self.service = FinanceService(self.path)
        self.service.seed_demo()
        self.alice = self.service.authenticate_demo("alice")
        self.bob = self.service.authenticate_demo("bob")
        users = json.loads(data_path("demo_users.json").read_text(encoding="utf-8"))["users"]
        self.profiles = {u["user_id"]: u["demo_profile"] for u in users}

    def tearDown(self):
        self.tmp.cleanup()

    def confirm(self, principal=None, change=None):
        principal = principal or self.alice
        proposal = self.service.propose_profile_update(principal, change or self.profiles[principal.user_id])
        return self.service.confirm_profile_update(principal, proposal["proposal_id"], proposal["base_version"], proposal["content_hash"])

    async def test_incomplete_profile_asks_before_calculation(self):
        run = await AgentRunner(self.service).run(self.alice, "分析我的目标")
        self.assertEqual("needs_profile", run["status"])
        self.assertEqual(["get_my_profile"], [e["tool"] for e in run["result"]["tool_events"]])
        self.assertEqual(0, run["result"]["usage"]["model_calls"])
        self.assertNotIn("calculation", run["result"])

    async def test_explicit_patch_is_pending_then_confirmed_in_another_session(self):
        run = await AgentRunner(self.service).run(self.alice, json.dumps(self.profiles["alice"]))
        self.assertEqual("pending_confirmation", run["status"])
        self.assertEqual({}, self.service.get_profile(self.alice)["data"])
        proposal = run["result"]["proposal"]
        self.assertEqual("fixture", proposal["source"])
        self.service.confirm_profile_update(self.alice, proposal["proposal_id"], proposal["base_version"], proposal["content_hash"])
        reopened = FinanceService(self.path)
        result = await AgentRunner(reopened).run(reopened.authenticate_demo("alice"), "分析当前确认画像")
        self.assertEqual("completed", result["status"])
        self.assertEqual("1500.00", result["result"]["calculation"]["monthly_surplus"])
        self.assertEqual(5, result["result"]["calculation"]["months_to_goal"])
        self.assertFalse(result["result"]["model_used"])
        self.assertEqual(4, len(result["result"]["tool_events"]))

    async def test_user_constraints_change_candidates_and_confirmed_versions(self):
        self.confirm()
        self.confirm(self.bob)
        first = await AgentRunner(self.service).run(self.alice, "分析")
        other = await AgentRunner(self.service).run(self.bob, "分析")
        ids = lambda record: {p["product_id"] for p in record["result"]["matching"]["eligible"]}
        self.assertNotEqual(ids(first), ids(other))
        for product in first["result"]["matching"]["eligible"]:
            self.assertEqual("low", product["risk_label"])
            self.assertEqual("immediate", product["liquidity"])
        self.confirm(change={"horizon_months": 3})
        updated = await AgentRunner(self.service).run(self.alice, "分析")
        self.assertFalse(updated["result"]["calculation"]["horizon_feasible"])
        self.assertEqual(first["profile_version"] + 1, updated["profile_version"])

    async def test_delete_removes_old_run_and_next_analysis_has_no_profile(self):
        self.confirm()
        old = await AgentRunner(self.service).run(self.alice, "私有画像分析")
        self.service.delete_memory(self.alice)
        with self.assertRaises(DomainError) as caught:
            self.service.get_run(self.alice, old["run_id"])
        self.assertEqual("not_found", caught.exception.code)
        new = await AgentRunner(FinanceService(self.path)).run(self.alice, "分析")
        self.assertEqual("needs_profile", new["status"])
        self.assertEqual({}, new["result"]["profile"]["data"])
        self.assertNotIn("5000.00", json.dumps(new["result"]))

    async def test_actual_mcp_schema_has_no_identity_or_confirmation_tools(self):
        server = create_tool_server(self.service, self.alice)
        async with Client(server) as client:
            definitions = await client.list_tools()
            names = {tool.name for tool in definitions.tools}
            self.assertEqual({"get_my_profile", "search_financial_terms", "calculate_financial_goal", "match_products", "propose_profile_update"}, names)
            for tool in definitions.tools:
                properties = tool.input_schema.get("properties", {})
                self.assertFalse({"user_id", "tenant_id", "confirmed", "memory_epoch"} & set(properties))
            response = await client.call_tool("get_my_profile", {})
            self.assertTrue(response.structured_content["ok"])
            self.assertEqual({}, response.structured_content["result"]["data"])
        with self.assertRaises(DomainError) as caught:
            await ToolGateway(self.service, self.alice).call("get_my_profile", {"user_id": "bob"})
        self.assertEqual("invalid_tool_input", caught.exception.code)

    async def test_gateway_cannot_confirm_or_approve_memory(self):
        with self.assertRaises(DomainError) as caught:
            await ToolGateway(self.service, self.alice).call("confirm_profile_update", {"confirmed": True})
        self.assertEqual("unknown_tool", caught.exception.code)
        bad = await AgentRunner(self.service).run(self.alice, '{"user_id":"bob","monthly_income":"9000"}')
        self.assertEqual("failed", bad["status"])
        self.assertEqual({}, self.service.get_profile(self.alice)["data"])
        self.assertEqual([], self.service.list_proposals(self.alice))

    async def test_qwen_protocol_empty_patch_uses_existing_profile(self):
        self.confirm()
        received = []
        def handler(request):
            received.append(json.loads(request.content))
            return model_reply({"profile_patch": {}})
        result = await AgentRunner(self.service, "qwen", transport=httpx.MockTransport(handler)).run(self.alice, "根据已确认画像分析")
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["result"]["model_used"])
        self.assertEqual(1, result["result"]["usage"]["model_calls"])
        self.assertNotIn("tools", received[0])
        self.assertEqual(1, self.service.get_profile(self.alice)["version"])

    async def test_model_output_cannot_supply_identity_or_numeric_float(self):
        for value in ({"profile_patch": {"user_id": "bob"}}, {"profile_patch": {"monthly_income": 8000}}, {"profile_patch": {}, "confirm": True}):
            with self.subTest(value=value):
                result = await AgentRunner(self.service, "qwen", transport=httpx.MockTransport(lambda r: model_reply(value))).run(self.alice, "修改画像")
                self.assertEqual("failed", result["status"])
        self.assertEqual([], self.service.list_proposals(self.alice))

    async def test_behavior_cannot_infer_risk_and_explicit_risk_still_needs_confirmation(self):
        transport = httpx.MockTransport(lambda r: model_reply({"profile_patch": {"risk_preference": "high"}}))
        rejected = await AgentRunner(self.service, "qwen", transport=transport).run(self.alice, "我收藏了一个高风险产品，请分析")
        self.assertEqual("failed", rejected["status"])
        accepted = await AgentRunner(self.service, "qwen", transport=transport).run(self.alice, "风险偏好为高")
        self.assertEqual("pending_confirmation", accepted["status"])
        self.assertEqual("model", accepted["result"]["proposal"]["source"])
        self.assertEqual({}, self.service.get_profile(self.alice)["data"])

    async def test_provider_failure_is_safe_and_attempt_count_truthful(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(401, text="private-key-provider-body"))
        result = await AgentRunner(self.service, "qwen", transport=transport).run(self.alice, "分析")
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, result["result"]["usage"]["model_calls"])
        self.assertFalse(result["result"]["model_used"])
        self.assertNotIn("private-key", json.dumps(result))
        self.assertEqual([], self.service.list_proposals(self.alice))

    async def test_invalid_api_configuration_is_safe_failed_record(self):
        with patch.dict(os.environ, {"FINSCOPE_API_BASE": "https://user:secret@example.test/v1", "FINSCOPE_API_MODEL": "test"}):
            run = await AgentRunner(self.service, "api").run(self.alice, "分析")
            self.assertEqual("invalid_model_config", run["result"]["error"]["code"])
            self.assertNotIn("secret", json.dumps(run))
            with self.assertRaises(DomainError) as caught:
                await TermQA(self.service, "api").answer(self.alice, "费用")
            self.assertEqual("invalid_model_config", caught.exception.code)

    async def test_delete_during_model_request_prevents_late_proposal(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            started.set()
            await release.wait()
            return model_reply({"profile_patch": {"horizon_months": 3}})
        task = asyncio.create_task(AgentRunner(self.service, "qwen", transport=httpx.MockTransport(handler)).run(self.alice, "目标期限改为3个月"))
        await started.wait()
        self.service.delete_memory(self.alice)
        release.set()
        with self.assertRaises(DomainError) as caught:
            await task
        self.assertEqual("memory_deleted", caught.exception.code)
        self.assertEqual([], self.service.list_runs(self.alice))
        self.assertEqual([], self.service.list_proposals(self.alice))

    async def test_profile_update_during_model_request_prevents_stale_proposal(self):
        self.confirm()
        started, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            started.set()
            await release.wait()
            return model_reply({"profile_patch": {"horizon_months": 3}})
        task = asyncio.create_task(AgentRunner(self.service, "qwen", transport=httpx.MockTransport(handler)).run(self.alice, "目标期限改为3个月"))
        await started.wait()
        self.confirm(change={"horizon_months": 12})
        release.set()
        with self.assertRaises(DomainError) as caught:
            await task
        self.assertEqual("stale_profile", caught.exception.code)
        self.assertEqual(12, self.service.get_profile(self.alice)["data"]["horizon_months"])
        self.assertFalse(any(p["status"] == "pending" for p in self.service.list_proposals(self.alice)))

    async def test_duplicate_running_request_only_calls_model_once_then_replays(self):
        self.confirm()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler(request):
            calls.append(request)
            started.set()
            await release.wait()
            return model_reply({"profile_patch": {}})
        runner = AgentRunner(self.service, "qwen", transport=httpx.MockTransport(handler))
        task = asyncio.create_task(runner.run(self.alice, "分析", request_id="shared-request"))
        await started.wait()
        with self.assertRaises(DomainError) as caught:
            await AgentRunner(self.service, "qwen", transport=httpx.MockTransport(handler)).run(self.alice, "分析", request_id="shared-request")
        self.assertEqual("run_in_progress", caught.exception.code)
        release.set()
        original = await task
        replay = await runner.run(self.alice, "分析", request_id="shared-request")
        self.assertEqual(original["run_id"], replay["run_id"])
        self.assertEqual(1, len(calls))

    async def test_cancellable_http_stores_safe_cancelled_record(self):
        started = asyncio.Event()
        async def handler(request):
            started.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(AgentRunner(self.service, "qwen", transport=httpx.MockTransport(handler)).run(self.alice, "分析"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual("cancelled", self.service.list_runs(self.alice)[0]["result"]["error"]["code"])
        self.assertEqual({}, self.service.get_profile(self.alice)["data"])

    async def test_term_fixture_is_source_preview_and_has_no_business_effect(self):
        report = await TermQA(self.service).answer(self.alice, "费用", product_id="FS-CASH")
        self.assertEqual("source_preview", report["status"])
        self.assertFalse(report["model_used"])
        self.assertTrue(report["citations"])
        self.assertTrue(all(s["product_id"] == "FS-CASH" for s in report["sources"]))
        self.assertEqual([], self.service.list_runs(self.alice))
        self.assertEqual([], self.service.list_proposals(self.alice))

    async def test_term_qwen_uses_only_effective_sources_and_exact_citations(self):
        sources = self.service.search_terms(self.alice, "费用", product_id="FS-CASH")
        source = sources[0]
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            return model_reply({"answer": source["content"], "citations": [{"term_id": source["term_id"], "answer_quote": source["content"], "source_quote": source["content"]}]})
        report = await TermQA(self.service, "qwen", transport=httpx.MockTransport(handler)).answer(self.alice, "费用", product_id="FS-CASH")
        prompt = json.loads(captured[0]["messages"][1]["content"])
        self.assertTrue(all(s["product_id"] == "FS-CASH" for s in prompt["sources"]))
        self.assertEqual(source["version"], report["citations"][0]["version"])
        self.assertFalse(report["semantic_support_verified"])
        self.assertEqual("pending_review", report["status"])

    async def test_term_answer_rejects_unknown_ids_or_fabricated_quotes(self):
        source = self.service.search_terms(self.alice, "费用", product_id="FS-CASH")[0]
        for change in ({"term_id": "foreign-term"}, {"source_quote": "保证收益"}, {"answer_quote": "不存在的回答片段"}):
            citation = {"term_id": source["term_id"], "answer_quote": source["content"], "source_quote": source["content"], **change}
            with self.subTest(change=change), self.assertRaises(DomainError) as caught:
                await TermQA(self.service, "qwen", transport=httpx.MockTransport(lambda r: model_reply({"answer": source["content"], "citations": [citation]}))).answer(self.alice, "费用", product_id="FS-CASH")
            self.assertEqual("invalid_model_answer", caught.exception.code)

    async def test_no_terms_does_not_call_model(self):
        calls = []
        def handler(request):
            calls.append(request)
            return model_reply({})
        result = await TermQA(self.service, "qwen", transport=httpx.MockTransport(handler)).answer(self.alice, "费用", as_of="2030-01-01", product_id="FS-CASH")
        self.assertEqual("insufficient_evidence", result["status"])
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
