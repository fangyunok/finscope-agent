"""Local demos and distribution checks without hidden environment loading."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from .resources import DEFAULT_DB_PATH, data_path


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def _doctor(mode: str, timeout: float) -> tuple[dict, int]:
    if mode == "fixture":
        return {"mode": mode, "available": True, "model_used": False, "check": "Explicit JSON fixture mode needs no model service"}, 0
    import httpx
    from .model import HttpExtractor

    try:
        extractor = HttpExtractor(mode=mode, timeout=timeout)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            response = await client.get(extractor.base_url + "/models", headers={"Authorization": "Bearer " + extractor.api_key} if extractor.api_key else {})
            response.raise_for_status()
            payload = response.json()
        models = {record["id"] for record in payload["data"] if isinstance(record, dict) and isinstance(record.get("id"), str)}
        available = extractor.model in models
        return {"mode": mode, "available": available, "requested_model": extractor.model, "model_used": False,
                "check": "Model catalog only; no generation or quality validation"}, 0 if available else 1
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {"mode": mode, "available": False, "model_used": False, "check": "Model catalog unavailable or invalid; provider details omitted"}, 1


async def _demo(database_path: Path, mode: str) -> dict:
    from .agent import AgentRunner
    from .service import DomainError, FinanceService

    service = FinanceService(database_path)
    service.seed_demo()
    principal = service.authenticate_demo("alice")
    users = json.loads(data_path("demo_users.json").read_text(encoding="utf-8"))["users"]
    patch = next(user["demo_profile"] for user in users if user["user_id"] == "alice")
    profile_before = service.get_profile(principal)
    proposal = service.propose_profile_update(principal, patch, source="manual", expected_version=profile_before["version"])
    confirmed = service.confirm_profile_update(principal, proposal["proposal_id"], proposal["base_version"], proposal["content_hash"], confirmed=True)
    first = await AgentRunner(service, mode).run(principal, "请根据我已经确认的资料分析当前目标")
    if first["status"] != "completed":
        raise DomainError("demo_failed", "首次分析未完成，请检查模型服务或使用 fixture 模式。")
    calculation = first["result"]["calculation"]
    if (calculation["monthly_surplus"], calculation["goal_gap"], calculation["months_to_goal"]) != ("1500.00", "7000.00", 5):
        raise DomainError("demo_failed", "演示计算未满足预设标签。")

    # A fresh service opens the same SQLite file; no in-memory conversation retained.
    service = FinanceService(database_path)
    principal = service.authenticate_demo("alice")
    restored = service.get_profile(principal)
    if restored["data"] != confirmed["data"] or restored["version"] != confirmed["version"]:
        raise DomainError("demo_failed", "持久化画像未在新服务实例中恢复。")
    second = await AgentRunner(service, mode).run(principal, "请使用当前确认资料再次分析")
    if second["status"] != "completed":
        raise DomainError("demo_failed", "跨会话分析没有完成。")

    update = service.propose_profile_update(principal, {"horizon_months": 3}, source="manual", expected_version=restored["version"])
    updated = service.confirm_profile_update(principal, update["proposal_id"], update["base_version"], update["content_hash"], confirmed=True)
    third = await AgentRunner(service, mode).run(principal, "请使用当前确认资料重新分析")
    if third["status"] != "completed" or third["result"]["calculation"]["horizon_feasible"] is not False:
        raise DomainError("demo_failed", "确认期限更新后的分析没有使用新约束。")
    deletion = service.delete_memory(principal, confirmed=True)
    service = FinanceService(database_path)
    principal = service.authenticate_demo("alice")
    after_delete = await AgentRunner(service, "fixture").run(principal, "根据当前资料分析")
    old_run_inaccessible = False
    try:
        service.get_run(principal, first["run_id"])
    except DomainError as exc:
        old_run_inaccessible = exc.status_code == 404
    if not old_run_inaccessible or after_delete["status"] != "needs_profile":
        raise DomainError("demo_failed", "删除后旧记忆仍可访问。")
    return {
        "synthetic": True, "mode": mode, "model_used": first["result"]["model_used"],
        "monthly_surplus": calculation["monthly_surplus"], "goal_gap": calculation["goal_gap"], "months_to_goal": calculation["months_to_goal"],
        "profile_restored": True, "initial_profile_version": confirmed["version"], "updated_profile_version": updated["version"],
        "updated_horizon_months": 3, "updated_horizon_feasible": False,
        "eligible_product_ids": [product["product_id"] for product in first["result"]["matching"]["eligible"]],
        "tool_calls": sum(len(run["result"].get("tool_events", [])) for run in (first, second, third, after_delete)),
        "memory_deleted": deletion["deleted"], "old_run_inaccessible": old_run_inaccessible, "final_status": after_delete["status"],
        "financial_advice": False, "money_operation": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="finscope", description="FinScope: confirmed memory and synthetic financial requirement analysis")
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="Import synthetic users; never automatically confirm profiles")
    seed.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    demo = commands.add_parser("demo", help="Run confirmed memory, restart, horizon update and deletion demo")
    demo.add_argument("--db", type=Path, default=Path("runs/demo.sqlite"))
    demo.add_argument("--mode", choices=("fixture", "qwen", "api"), default="fixture")
    serve = commands.add_parser("serve", help="Start local workbench")
    serve.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    serve.add_argument("--mode", choices=("fixture", "qwen", "api"), default="fixture")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7862)
    doctor = commands.add_parser("doctor", help="Check fixture availability or model catalog; no generation")
    doctor.add_argument("--mode", choices=("fixture", "qwen", "api"), default="fixture")
    doctor.add_argument("--timeout", type=float, default=5)
    evaluate = commands.add_parser("evaluate", help="Run 20 labelled synthetic sequences in independent temporary databases")
    evaluate.add_argument("--output", type=Path, default=Path("runs/evaluation.json"))
    options = parser.parse_args(argv)
    from .service import DomainError

    try:
        if options.command == "seed":
            from .service import FinanceService
            _emit(FinanceService(options.db).seed_demo())
        elif options.command == "demo":
            _emit(asyncio.run(_demo(options.db, options.mode)))
        elif options.command == "doctor":
            report, code = asyncio.run(_doctor(options.mode, options.timeout))
            _emit(report)
            return code
        elif options.command == "evaluate":
            from .evaluation import evaluate_cases
            report = asyncio.run(evaluate_cases(options.output))
            _emit(report)
            return 0 if report["passed"] else 1
        elif options.command == "serve":
            import uvicorn
            from .webapp import create_app
            uvicorn.run(create_app(database_path=options.db, model_mode=options.mode), host=options.host, port=options.port)
    except DomainError as exc:
        _emit({"error": {"code": exc.code, "message": str(exc)}})
        return 1
    except (OSError, ValueError):
        _emit({"error": {"code": "configuration_error", "message": "Unable to initialize configured database or model settings"}})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
