"""Behavioral tests for confirmed memory and deletion boundaries."""
import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from finscope.schemas import Principal
from finscope.service import DomainError, FinanceService


ALICE = {"monthly_income": "8000.00", "essential_expense": "6500.00", "available_balance": "5000.00", "goal_amount": "12000.00", "horizon_months": 6, "liquidity_need": "immediate", "risk_preference": "low", "goal_kind": "emergency", "currency": "CNY"}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "memory.sqlite"
        self.service = FinanceService(self.path)
        with self.service.database.transaction(write=True) as connection:
            connection.executemany("INSERT INTO users(user_id,tenant_id,display_name) VALUES(?,?,?)", [("alice", "alpha", "Alice"), ("bob", "alpha", "Bob"), ("carol", "beta", "Carol")])
        self.alice = self.service.authenticate_demo("alice")
        self.bob = self.service.authenticate_demo("bob")
        self.carol = self.service.authenticate_demo("carol")

    def tearDown(self):
        self.temporary.cleanup()

    def confirm(self, patch=ALICE, principal=None):
        principal = principal or self.alice
        proposal = self.service.propose_profile_update(principal, patch)
        return self.service.confirm_profile_update(principal, proposal["proposal_id"], proposal["base_version"], proposal["content_hash"], confirmed=True)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(DomainError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_proposal_does_not_change_confirmed_memory(self):
        proposal = self.service.propose_profile_update(self.alice, ALICE, source="model")
        self.assertEqual({}, self.service.get_profile(self.alice)["data"])
        self.assertEqual("pending", proposal["status"])
        self.assertEqual({"before": None, "after": "8000.00"}, proposal["diff"]["monthly_income"])

    def test_confirmation_records_source_version_and_is_idempotent(self):
        proposal = self.service.propose_profile_update(self.alice, ALICE, source="fixture")
        args = (self.alice, proposal["proposal_id"], 0, proposal["content_hash"])
        first = self.service.confirm_profile_update(*args)
        again = self.service.confirm_profile_update(*args)
        self.assertEqual(first, again)
        self.assertEqual(1, first["version"])
        self.assertTrue(first["complete"])
        self.assertEqual(9, len(first["field_sources"]))
        self.assertTrue(all(field["source"] == "fixture" and field["confirmed_version"] == 1 for field in first["field_sources"]))

    def test_false_or_truthy_confirmation_is_rejected(self):
        proposal = self.service.propose_profile_update(self.alice, ALICE)
        for truthy in (False, 1, "true", None):
            self.error("confirmation_required", self.service.confirm_profile_update, self.alice, proposal["proposal_id"], 0, proposal["content_hash"], confirmed=truthy)
        self.assertEqual({}, self.service.get_profile(self.alice)["data"])

    def test_identity_and_numeric_schema_injection_rejected(self):
        for patch in ({"user_id": "bob", "monthly_income": "9000"}, {"risk_preference": "medium", "confirmed": True}, {"monthly_income": 8000}, {"monthly_income": "NaN"}, {"monthly_income": "1.001"}, {"goal_amount": "-1"}, {"monthly_income": "1000000001"}, {"horizon_months": True}, {"risk_preference": None}, {}):
            with self.subTest(patch=patch):
                self.error("invalid_profile", self.service.propose_profile_update, self.alice, patch)

    def test_owner_and_tenant_isolation(self):
        proposal = self.service.propose_profile_update(self.alice, ALICE)
        for other in (self.bob, self.carol):
            self.error("not_found", self.service.get_proposal, other, proposal["proposal_id"])
            self.error("not_found", self.service.confirm_profile_update, other, proposal["proposal_id"], 0, proposal["content_hash"])
            self.assertEqual([], self.service.list_proposals(other))
        forged = Principal(user_id="alice", tenant_id="beta", display_name="Alice")
        self.error("invalid_identity", self.service.get_profile, forged)

    def test_changed_profile_rejects_old_pending_proposal(self):
        self.confirm()
        old = self.service.propose_profile_update(self.alice, {"horizon_months": 3})
        self.confirm({"horizon_months": 12})
        self.error("stale_proposal", self.service.confirm_profile_update, self.alice, old["proposal_id"], 1, old["content_hash"])
        self.assertEqual(12, self.service.get_profile(self.alice)["data"]["horizon_months"])
        self.assertEqual({"before": 6, "after": 3}, self.service.get_proposal(self.alice, old["proposal_id"])["diff"]["horizon_months"])

    def test_partial_update_preserves_unchanged_field_origins(self):
        self.confirm()
        updated = self.confirm({"horizon_months": 3})
        by_field = {field["field"]: field for field in updated["field_sources"]}
        self.assertEqual(1, by_field["risk_preference"]["confirmed_version"])
        self.assertEqual(2, by_field["horizon_months"]["confirmed_version"])
        self.assertEqual("8000.00", updated["data"]["monthly_income"])

    def test_hash_and_version_confirmation_are_strict(self):
        proposal = self.service.propose_profile_update(self.alice, ALICE)
        self.error("stale_proposal", self.service.confirm_profile_update, self.alice, proposal["proposal_id"], 0, "0" * 64)
        self.error("invalid_hash", self.service.confirm_profile_update, self.alice, proposal["proposal_id"], 0, "not-a-hash")
        self.error("invalid_version", self.service.confirm_profile_update, self.alice, proposal["proposal_id"], False, proposal["content_hash"])

    def test_memory_persists_in_new_service_instance(self):
        confirmed = self.confirm()
        reopened = FinanceService(self.path)
        self.assertEqual(confirmed, reopened.get_profile(reopened.authenticate_demo("alice")))

    def test_delete_wipes_all_personal_tables_and_other_users_remain(self):
        self.confirm()
        self.confirm(principal=self.bob)
        self.service.propose_profile_update(self.alice, {"horizon_months": 3})
        run = self.service.create_run(self.alice, "private message", "fixture")
        self.service.complete_run(self.alice, run["run_id"], {"status": "completed", "private_snapshot": ALICE})
        with self.service.database.transaction(write=True) as connection:
            connection.execute("INSERT INTO feedback VALUES(?,?,?,?,?)", ("alice", "demo-product", "favorite", 0, "2026-10-03"))
        report = self.service.delete_memory(self.alice)
        self.assertEqual(1, report["memory_epoch"])
        with self.service.database.transaction() as connection:
            for table in ("profiles", "field_sources", "profile_history", "proposals", "feedback", "runs", "events"):
                self.assertEqual(0, connection.execute(f"SELECT COUNT(*) FROM {table} WHERE owner_id='alice'").fetchone()[0], table)
        self.assertTrue(self.service.get_profile(self.bob)["complete"])
        self.assertEqual({}, FinanceService(self.path).get_profile(self.alice)["data"])
        self.error("not_found", self.service.get_run, self.alice, run["run_id"])

    def test_deletion_prevents_inflight_model_proposal_recreation(self):
        run = self.service.create_run(self.alice, "extract later", "fixture")
        self.service.delete_memory(self.alice)
        self.error("memory_deleted", self.service.propose_profile_update, self.alice, ALICE, source="model", expected_memory_epoch=run["memory_epoch"])
        self.assertEqual([], self.service.list_proposals(self.alice))

    def test_deletion_prevents_inflight_result_return_or_persist(self):
        run = self.service.create_run(self.alice, "analyze later", "fixture")
        self.service.delete_memory(self.alice)
        self.error("not_found", self.service.complete_run, self.alice, run["run_id"], {"status": "completed", "snapshot": ALICE})
        self.assertEqual([], self.service.list_runs(self.alice))

    def test_profile_change_during_run_requires_new_analysis(self):
        self.confirm()
        run = self.service.create_run(self.alice, "analyze", "fixture")
        self.confirm({"horizon_months": 3})
        self.error("stale_profile", self.service.complete_run, self.alice, run["run_id"], {"status": "completed"})

    def test_request_id_scoped_by_owner_input_and_profile_version(self):
        one = self.service.create_run(self.alice, "analyze", "fixture", "client-1")
        repeat = self.service.create_run(self.alice, "analyze", "fixture", "client-1")
        other = self.service.create_run(self.bob, "analyze", "fixture", "client-1")
        self.assertEqual(one["run_id"], repeat["run_id"])
        self.assertNotEqual(one["run_id"], other["run_id"])
        self.error("idempotency_conflict", self.service.create_run, self.alice, "different", "fixture", "client-1")
        self.confirm()
        self.error("idempotency_conflict", self.service.create_run, self.alice, "analyze", "fixture", "client-1")

    def test_run_read_and_completion_owner_checks(self):
        run = self.service.create_run(self.alice, "analyze", "fixture")
        self.error("not_found", self.service.get_run, self.bob, run["run_id"])
        self.error("not_found", self.service.complete_run, self.carol, run["run_id"], {"status": "completed"})
        self.assertEqual([], self.service.list_runs(self.bob))

    def test_concurrent_confirmation_produces_one_version(self):
        proposal = self.service.propose_profile_update(self.alice, ALICE)
        def submit(_):
            return self.service.confirm_profile_update(self.alice, proposal["proposal_id"], 0, proposal["content_hash"])["version"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual([1] * 12, list(pool.map(submit, range(12))))
        with self.service.database.transaction() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM profile_history").fetchone()[0])

    def test_post_delete_new_confirmation_has_monotonic_version(self):
        self.confirm()
        self.service.delete_memory(self.alice)
        new = self.confirm({"monthly_income": "9000"})
        self.assertEqual(3, new["version"])
        self.assertEqual(1, new["memory_epoch"])
        self.assertEqual({"monthly_income": "9000.00"}, new["data"])

    def test_same_completed_result_replay_is_safe_but_overwrite_rejected(self):
        run = self.service.create_run(self.alice, "analyze", "fixture")
        result = {"status": "needs_profile", "missing_fields": ["monthly_income"]}
        completed = self.service.complete_run(self.alice, run["run_id"], result)
        self.assertEqual(completed, self.service.complete_run(self.alice, run["run_id"], result))
        self.error("run_conflict", self.service.complete_run, self.alice, run["run_id"], {"status": "completed"})

    def test_financial_example_uses_only_confirmed_memory(self):
        self.service.propose_profile_update(self.alice, ALICE)
        self.error("incomplete_profile", self.service.calculate, self.alice)
        self.confirm()
        result = self.service.calculate(self.alice)
        self.assertEqual("1500.00", result["monthly_surplus"])
        self.assertEqual("7000.00", result["goal_gap"])
        self.assertEqual(5, result["months_to_goal"])
        self.assertTrue(result["horizon_feasible"])
        self.confirm({"horizon_months": 3})
        self.assertFalse(self.service.calculate(self.alice)["horizon_feasible"])

    def test_favorite_never_changes_risk_preference(self):
        self.confirm()
        product_id = self.service.catalog.products()[0]["product_id"]
        self.error("confirmation_required", self.service.record_feedback, self.alice, product_id, "favorite", confirmed=1)
        self.service.record_feedback(self.alice, product_id, "favorite")
        self.assertEqual("low", self.service.get_profile(self.alice)["data"]["risk_preference"])
        self.assertEqual(1, self.service.get_profile(self.alice)["version"])
        self.assertEqual([], self.service.list_feedback(self.bob))

    def test_explicit_exclusion_filters_candidates_and_clear_restores(self):
        self.confirm()
        initial = self.service.match(self.alice)
        self.assertEqual(["FS-CASH"], [entry["product_id"] for entry in initial["eligible"]])
        self.service.record_feedback(self.alice, "FS-CASH", "exclude")
        excluded = self.service.match(self.alice)
        self.assertEqual([], excluded["eligible"])
        cash = next(entry for entry in excluded["excluded"] if entry["product_id"] == "FS-CASH")
        self.assertIn("explicit_user_exclusion", [reason["code"] for reason in cash["reasons"]])
        self.service.record_feedback(self.alice, "FS-CASH", "clear")
        self.assertEqual(["FS-CASH"], [entry["product_id"] for entry in self.service.match(self.alice)["eligible"]])

    def test_invalid_feedback_and_search_dates_have_safe_errors(self):
        self.error("invalid_product", self.service.record_feedback, self.alice, "unknown-product")
        self.error("invalid_feedback", self.service.record_feedback, self.alice, "FS-CASH", "increase-risk")
        self.error("invalid_catalog_query", self.service.search_terms, self.alice, "fees", "bad-date")
        self.assertTrue(all(term["synthetic"] for term in self.service.search_terms(self.alice, "费用", product_id="FS-CASH")))

    def test_reseeding_demo_users_does_not_restore_deleted_profile(self):
        self.service.seed_demo()
        self.confirm()
        self.service.delete_memory(self.alice)
        self.service.seed_demo()
        self.assertEqual({}, self.service.get_profile(self.alice)["data"])
        self.assertEqual(20, len(self.service.list_demo_users()))

    def test_concurrent_request_id_marks_exactly_one_creator(self):
        def create(_):
            return self.service.create_run(self.alice, "analyze", "fixture", "one-request")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            runs = list(pool.map(create, range(12)))
        self.assertEqual(1, sum(run["_created"] for run in runs))
        self.assertEqual(1, len({run["run_id"] for run in runs}))

    def test_profile_confirmation_invalidates_inflight_run_and_late_proposal(self):
        run = self.service.create_run(self.alice, "model still extracting", "qwen")
        self.confirm()
        after = self.service.get_run(self.alice, run["run_id"])
        self.assertEqual("failed", after["status"])
        self.assertEqual("stale_profile", after["result"]["error"]["code"])
        self.error("stale_profile", self.service.propose_profile_update, self.alice, {"risk_preference": "high"}, source="model", expected_version=run["profile_version"], expected_memory_epoch=run["memory_epoch"])
        self.assertEqual(1, len(self.service.list_proposals(self.alice)))

    def test_exclusion_change_invalidates_inflight_snapshot_and_old_request_id(self):
        self.confirm()
        run = self.service.create_run(self.alice, "analyze", "fixture", "request-before-feedback")
        old_match = self.service.match(self.alice)
        self.service.record_feedback(self.alice, "FS-CASH", "exclude")
        self.assertEqual(1, self.service.get_profile(self.alice)["version"])
        self.assertEqual("low", self.service.get_profile(self.alice)["data"]["risk_preference"])
        self.error("stale_feedback", self.service.complete_run, self.alice, run["run_id"], {"status": "completed", "matching": old_match})
        self.error("idempotency_conflict", self.service.create_run, self.alice, "analyze", "fixture", "request-before-feedback")
        self.assertEqual("stale_feedback", self.service.get_run(self.alice, run["run_id"])["result"]["error"]["code"])
        self.assertEqual([], self.service.match(self.alice)["eligible"])

    def test_repeated_identical_feedback_does_not_invalidate_new_run(self):
        self.confirm()
        self.service.record_feedback(self.alice, "FS-CASH", "favorite")
        run = self.service.create_run(self.alice, "analyze", "fixture")
        self.service.record_feedback(self.alice, "FS-CASH", "favorite")
        completed = self.service.complete_run(self.alice, run["run_id"], {"status": "completed"})
        self.assertEqual("completed", completed["status"])
        self.assertEqual(1, completed["feedback_version"])


if __name__ == "__main__":
    unittest.main()
