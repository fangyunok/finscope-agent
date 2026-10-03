"""Independent synthetic labels for cross-session confirmed-memory behavior."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time

from .agent import AgentRunner
from .resources import data_path
from .service import DomainError, FinanceService


def _check(value, expected):
    for path, target in expected.items():
        if path == "eligible_ids":
            actual = sorted(item["product_id"] for item in value["result"]["matching"]["eligible"])
            target = sorted(target)
        else:
            actual = value
            for part in path.split("."):
                actual = actual[part]
        if actual != target:
            raise AssertionError(f"{path}: expected {target!r}, got {actual!r}")


async def evaluate_cases(output_path: str | Path | None = None) -> dict:
    """Execute labelled fixture sequences; never report model accuracy."""
    fixture = json.loads(data_path("evaluation_cases.json").read_text(encoding="utf-8"))
    if fixture.get("synthetic") is not True or len(fixture["cases"]) != 20:
        raise ValueError("Evaluation requires exactly 20 explicitly synthetic cases")
    started = time.perf_counter()
    results, tool_calls = [], 0
    for case in fixture["cases"]:
        steps = []
        try:
            with tempfile.TemporaryDirectory(prefix="finscope-evaluation-") as folder:
                database = Path(folder) / "business.sqlite"
                service = FinanceService(database)
                service.seed_demo()
                principal = service.authenticate_demo("alice")
                proposals, runs = {}, {}
                for index, action in enumerate(case["actions"]):
                    operation = action["operation"]
                    actor = service.authenticate_demo(action["user_id"]) if action.get("user_id") else principal
                    if operation in {"confirm_profile", "propose"}:
                        patch = {**fixture["base_profile"], **action.get("patch", {})} if action.get("full") else action["patch"]
                        profile = service.get_profile(actor)
                        value = service.propose_profile_update(actor, patch, source="manual", expected_version=profile["version"])
                        proposals[action.get("name", "latest")] = value
                        if operation == "confirm_profile":
                            value = service.confirm_profile_update(actor, value["proposal_id"], value["base_version"], value["content_hash"], confirmed=True)
                    elif operation == "confirm":
                        proposal = proposals[action["name"]]
                        try:
                            value = service.confirm_profile_update(actor, proposal["proposal_id"], proposal["base_version"], proposal["content_hash"], confirmed=True)
                        except DomainError as exc:
                            if exc.code != action.get("error_code"):
                                raise
                            value = {"error_code": exc.code}
                        else:
                            if "error_code" in action:
                                raise AssertionError("Expected confirmation rejection")
                    elif operation == "reopen":
                        service = FinanceService(database)
                        principal = service.authenticate_demo("alice")
                        value = {"reopened": True}
                    elif operation == "analyze":
                        value = await AgentRunner(service, "fixture").run(actor, "根据当前确认资料分析我的目标", request_id=action.get("request_id"))
                        runs[action.get("name", "latest")] = value
                        tool_calls += len(value["result"].get("tool_events", []))
                        if value["result"].get("model_used") is not False:
                            raise AssertionError("Fixture evaluation must not call a model")
                    elif operation == "profile":
                        value = service.get_profile(actor)
                    elif operation == "feedback":
                        value = service.record_feedback(actor, action["product_id"], action["action"], confirmed=True)
                    elif operation == "delete":
                        value = service.delete_memory(actor, confirmed=True)
                    elif operation == "missing_run":
                        try:
                            service.get_run(actor, runs[action["name"]]["run_id"])
                        except DomainError as exc:
                            if exc.status_code != 404:
                                raise
                            value = {"not_found": True}
                        else:
                            raise AssertionError("Deleted or other-user run remained accessible")
                    elif operation == "terms":
                        terms = service.search_terms(actor, action["query"], as_of=action["as_of"], product_id=action["product_id"])
                        value = {"versions": sorted({term["version"] for term in terms}), "has_sources": bool(terms)}
                    else:
                        raise ValueError("Unknown evaluation operation: " + operation)
                    _check(value, action.get("expected", {}))
                    steps.append({"step": index + 1, "operation": operation, "passed": True})
            results.append({"case_id": case["case_id"], "description": case["description"], "passed": True, "steps": steps})
        except (DomainError, AssertionError, KeyError, TypeError, ValueError) as exc:
            results.append({"case_id": case["case_id"], "description": case["description"], "passed": False, "steps": steps, "error": str(exc)})
    report = {
        "evaluation_version": fixture["evaluation_version"], "mode": "fixture", "synthetic": True,
        "model_used": False, "model_calls": 0, "tool_calls": tool_calls,
        "cases_total": len(results), "cases_passed": sum(item["passed"] for item in results),
        "duration_ms": round((time.perf_counter() - started) * 1000), "cases": results,
        "claims": ["labelled synthetic cross-session behavior", "deterministic calculations", "confirmed memory and user isolation"],
        "not_evaluated": ["real model accuracy", "semantic citation support", "real investment returns", "learned ranking quality"],
    }
    report["passed"] = report["cases_passed"] == report["cases_total"]
    if output_path is not None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
