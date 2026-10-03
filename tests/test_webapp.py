"""ASGI tests cover real signed demo sessions, memory writes and MCP analysis."""
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from finscope.webapp import _COOKIE, create_app


class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "web.sqlite"
        self.app = create_app(self.path)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def login(self, user="alice"):
        result = await self.client.post("/api/login", json={"user_id": user})
        self.assertEqual(200, result.status_code)
        return result

    async def establish_profile(self, user="alice"):
        await self.login(user)
        fixture = (await self.client.get("/api/demo-profile")).json()
        profile = (await self.client.get("/api/profile")).json()
        proposal = (await self.client.post("/api/profile-proposals", json={"patch": fixture["data"], "expected_version": profile["version"]})).json()
        result = await self.client.post("/api/profile-proposals/" + proposal["proposal_id"] + "/confirm", json={"confirmed": True, "expected_version": proposal["base_version"], "expected_hash": proposal["content_hash"]})
        self.assertEqual(200, result.status_code)
        return result.json()

    async def test_home_health_and_demo_users_are_truthfully_labeled(self):
        home = await self.client.get("/")
        self.assertEqual(200, home.status_code)
        self.assertIn("FinScope", home.text)
        self.assertIn("不调用大模型", home.text)
        self.assertIn("不是生产登录系统", home.text)
        self.assertEqual("no-store", home.headers["cache-control"])
        health = (await self.client.get("/health")).json()
        self.assertFalse(health["model_validated"])
        self.assertTrue(health["synthetic"])
        self.assertEqual(20, len((await self.client.get("/api/demo-users")).json()))

    async def test_private_reads_require_signed_session_and_logout_removes_it(self):
        for route in ("/api/me", "/api/profile", "/api/runs", "/api/feedback", "/api/profile-proposals", "/api/demo-profile"):
            self.assertEqual(401, (await self.client.get(route)).status_code)
        result = await self.login()
        cookie = result.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertEqual("alice", (await self.client.get("/api/me")).json()["user_id"])
        self.assertEqual(200, (await self.client.post("/api/logout", json={})).status_code)
        self.assertEqual(401, (await self.client.get("/api/profile")).status_code)

    async def test_forged_cookie_is_rejected(self):
        await self.login()
        token = self.client.cookies.get(_COOKIE)
        self.client.cookies.clear()
        self.client.cookies.set(_COOKIE, token[:-1] + ("0" if token[-1] != "0" else "1"))
        self.assertEqual(401, (await self.client.get("/api/profile")).status_code)

    async def test_cross_origin_writes_are_rejected_and_same_origin_passes(self):
        for headers in ({"origin": "https://other.example"}, {"origin": "null"}, {"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"}):
            response = await self.client.post("/api/login", json={"user_id": "alice"}, headers=headers)
            self.assertEqual(403, response.status_code)
        response = await self.client.post("/api/login", json={"user_id": "alice"}, headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"})
        self.assertEqual(200, response.status_code)

    async def test_body_limits_json_type_and_identity_injection(self):
        self.assertEqual(413, (await self.client.post("/api/login", content="x" * 40000, headers={"content-type": "application/json"})).status_code)
        self.assertEqual(415, (await self.client.post("/api/login", content='{"user_id":"alice"}')).status_code)
        self.assertEqual(400, (await self.client.post("/api/login", json=["alice"])).status_code)
        self.assertEqual(400, (await self.client.post("/api/login", json={"user_id": "alice", "tenant_id": "beta"})).status_code)
        await self.login()
        response = await self.client.post("/api/profile-proposals", json={"patch": {"user_id": "bob", "monthly_income": "8000"}, "expected_version": 0})
        self.assertEqual(400, response.status_code)
        self.assertEqual({}, (await self.client.get("/api/profile")).json()["data"])

    async def test_demo_form_fill_never_confirms_memory(self):
        await self.login()
        fixture = (await self.client.get("/api/demo-profile")).json()
        self.assertEqual("8000.00", fixture["data"]["monthly_income"])
        self.assertFalse(fixture["confirmed"])
        self.assertEqual({}, (await self.client.get("/api/profile")).json()["data"])
        self.assertEqual([], (await self.client.get("/api/profile-proposals")).json())

    async def test_proposal_confirmation_requires_exact_version_hash_and_literal_true(self):
        await self.login()
        proposal = (await self.client.post("/api/profile-proposals", json={"patch": {"horizon_months": 6}, "expected_version": 0})).json()
        endpoint = "/api/profile-proposals/" + proposal["proposal_id"] + "/confirm"
        body = {"expected_version": 0, "expected_hash": proposal["content_hash"], "confirmed": True}
        for override in ({"confirmed": False}, {"confirmed": 1}, {"expected_version": False}, {"expected_hash": "bad"}, {"expected_hash": "0" * 64}):
            response = await self.client.post(endpoint, json={**body, **override})
            self.assertIn(response.status_code, {400, 409})
        self.assertEqual({}, (await self.client.get("/api/profile")).json()["data"])
        self.assertEqual(200, (await self.client.post(endpoint, json=body)).status_code)
        profile = (await self.client.get("/api/profile")).json()
        self.assertEqual(1, profile["version"])
        self.assertEqual(6, profile["data"]["horizon_months"])

    async def test_changed_profile_rejects_stale_confirmation(self):
        original = await self.establish_profile()
        payload = {"patch": {"horizon_months": 3}, "expected_version": original["version"]}
        old = (await self.client.post("/api/profile-proposals", json=payload)).json()
        new = (await self.client.post("/api/profile-proposals", json={**payload, "patch": {"horizon_months": 12}})).json()
        confirm = lambda value: {"confirmed": True, "expected_version": value["base_version"], "expected_hash": value["content_hash"]}
        self.assertEqual(200, (await self.client.post("/api/profile-proposals/" + new["proposal_id"] + "/confirm", json=confirm(new))).status_code)
        self.assertEqual(409, (await self.client.post("/api/profile-proposals/" + old["proposal_id"] + "/confirm", json=confirm(old))).status_code)

    async def test_actual_mcp_analysis_replay_and_owner_isolation(self):
        await self.establish_profile()
        body = {"message": "使用当前确认画像分析", "request_id": "web-task-one"}
        response = await self.client.post("/api/tasks", json=body)
        self.assertEqual(201, response.status_code)
        run = response.json()
        self.assertEqual("completed", run["status"])
        self.assertEqual(5, run["result"]["calculation"]["months_to_goal"])
        self.assertEqual(4, len(run["result"]["tool_events"]))
        replay = (await self.client.post("/api/tasks", json=body)).json()
        self.assertEqual(run["run_id"], replay["run_id"])
        await self.login("bob")
        self.assertEqual(404, (await self.client.get("/api/runs/" + run["run_id"])).status_code)
        self.assertEqual([], (await self.client.get("/api/runs")).json())
        self.assertEqual({}, (await self.client.get("/api/profile")).json()["data"])

    async def test_agent_fields_only_create_pending_proposal(self):
        await self.login()
        response = await self.client.post("/api/tasks", json={"message": '{"horizon_months":3}'})
        self.assertEqual(201, response.status_code)
        self.assertEqual("pending_confirmation", response.json()["status"])
        self.assertEqual({}, (await self.client.get("/api/profile")).json()["data"])
        proposal = response.json()["result"]["proposal"]
        await self.login("bob")
        self.assertEqual(404, (await self.client.get("/api/profile-proposals/" + proposal["proposal_id"])).status_code)

    async def test_feedback_is_explicit_and_never_updates_risk(self):
        profile = await self.establish_profile()
        body = {"product_id": "FS-CASH", "action": "exclude", "confirmed": True}
        self.assertEqual(400, (await self.client.post("/api/feedback", json={**body, "confirmed": "true"})).status_code)
        self.assertEqual(200, (await self.client.post("/api/feedback", json=body)).status_code)
        self.assertEqual(profile["data"]["risk_preference"], (await self.client.get("/api/profile")).json()["data"]["risk_preference"])
        run = (await self.client.post("/api/tasks", json={"message": "分析"})).json()
        self.assertNotIn("FS-CASH", {p["product_id"] for p in run["result"]["matching"]["eligible"]})
        self.assertEqual(200, (await self.client.post("/api/feedback", json={**body, "action": "clear"})).status_code)
        self.assertEqual([], (await self.client.get("/api/feedback")).json())

    async def test_delete_wipes_own_records_and_reopened_app_does_not_restore_seed(self):
        await self.establish_profile()
        run = (await self.client.post("/api/tasks", json={"message": "分析"})).json()
        self.assertEqual(400, (await self.client.request("DELETE", "/api/memory", json={"confirmed": False})).status_code)
        self.assertEqual(200, (await self.client.request("DELETE", "/api/memory", json={"confirmed": True})).status_code)
        self.assertEqual(404, (await self.client.get("/api/runs/" + run["run_id"])).status_code)
        self.assertEqual([], (await self.client.get("/api/profile-proposals")).json())
        self.assertEqual([], (await self.client.get("/api/runs")).json())
        self.assertEqual({}, (await self.client.get("/api/profile")).json()["data"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.path)), base_url="http://testserver") as new_client:
            await new_client.post("/api/login", json={"user_id": "alice"})
            task = (await new_client.post("/api/tasks", json={"message": "分析"})).json()
            self.assertEqual("needs_profile", task["status"])
            self.assertEqual({}, task["result"]["profile"]["data"])

    async def test_terms_are_read_only_effective_source_preview(self):
        await self.login()
        response = await self.client.post("/api/term-answers", json={"question": "费用", "product_id": "FS-CASH", "as_of": "2026-10-03"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("source_preview", response.json()["status"])
        self.assertTrue(all(s["product_id"] == "FS-CASH" for s in response.json()["sources"]))
        missing = (await self.client.post("/api/term-answers", json={"question": "费用", "product_id": "FS-CASH", "as_of": "2030-01-01"})).json()
        self.assertEqual("insufficient_evidence", missing["status"])
        self.assertEqual([], (await self.client.get("/api/runs")).json())
        self.assertEqual(400, (await self.client.get("/api/terms?as_of=bad")).status_code)


if __name__ == "__main__":
    unittest.main()
