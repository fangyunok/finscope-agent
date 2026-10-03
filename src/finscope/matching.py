"""Fixed, explainable ranking after hard constraints on synthetic products."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .calculations import calculate_goal, validate_profile
from .catalog import Catalog


RANKING_VERSION = "rule-baseline-v1"
RISK_LEVEL = {"low": 0, "medium": 1, "high": 2}
LIQUIDITY_LEVEL = {"immediate": 0, "flexible": 1, "locked": 2}


def _references(product: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    return [
        {"product_id": product["product_id"], "term_id": clause["term_id"],
         "version": product.get("version"), "effective_from": product.get("effective_from"),
         "effective_to": product.get("effective_to"), "quote": clause["content"], "synthetic": True}
        for clause in product.get("clauses", [])
        if isinstance(clause, dict) and all(isinstance(clause.get(key), str) for key in ("term_id", "content")) and (kind is None or clause.get("kind") == kind)
    ]


def match_products(profile: dict[str, Any], catalog: Catalog, as_of: str = "2026-10-03", excluded_product_ids: list[str] | set[str] | None = None) -> dict[str, Any]:
    values = validate_profile(profile, preferences=True)
    if excluded_product_ids is not None and (not isinstance(excluded_product_ids, (list, set, tuple)) or any(not isinstance(value, str) for value in excluded_product_ids)):
        raise ValueError("excluded_product_ids 必须为产品 ID 列表。")
    excluded_preferences = set(excluded_product_ids or [])
    eligible, excluded = [], []
    available = Decimal(values["available_balance"])
    for product in catalog.products(as_of):
        reasons = []
        def reject(code: str, message: str, kind: str | None = None) -> None:
            reasons.append({"code": code, "message": message, "source_terms": _references(product, kind)})
        errors = product["validation_errors"]
        if errors:
            reject("catalog_incomplete_or_conflicting", "条款结构缺失、无效或有版本冲突：" + ", ".join(errors))
        else:
            lower, upper = (Decimal(product["amount_bounds"][key]) for key in ("min", "max"))
            if not lower <= available <= upper:
                reject("amount_out_of_bounds", f"可用余额 {values['available_balance']} 元超出金额区间 {lower:.2f}–{upper:.2f} 元。", "amount")
            if product["lock_months"] > values["horizon_months"]:
                reject("lock_exceeds_horizon", f"锁定 {product['lock_months']} 个月超过目标期限 {values['horizon_months']} 个月。", "lock")
            if LIQUIDITY_LEVEL[product["liquidity"]] > LIQUIDITY_LEVEL[values["liquidity_need"]]:
                reject("liquidity_mismatch", "产品流动性低于用户明确确认的取用要求。", "liquidity")
            if RISK_LEVEL[product["risk_label"]] > RISK_LEVEL[values["risk_preference"]]:
                reject("risk_exceeds_preference", "模拟风险标签高于用户明确确认的风险上限。", "risk")
            if values["goal_kind"] == "emergency" and product["liquidity"] != "immediate":
                reject("emergency_requires_immediate", "应急目标采用仅允许立即取用的固定硬约束。", "liquidity")
        if product["product_id"] in excluded_preferences:
            reject("explicit_user_exclusion", "用户已确认排除该产品。")
        if reasons:
            excluded.append({"product_id": product["product_id"], "name": product.get("name", product["product_id"]), "reasons": reasons, "validation_errors": errors, "synthetic": True})
            continue
        target_lock = values["horizon_months"] if values["liquidity_need"] == "locked" and values["goal_kind"] != "emergency" else 0
        components = {
            "goal_fit": 40 if values["goal_kind"] in product["supported_goals"] else 10,
            "liquidity_fit": 30 if values["liquidity_need"] == product["liquidity"] else 20,
            "term_fit": max(0, 20 - abs(target_lock - product["lock_months"])),
            "risk_fit": 10 if values["risk_preference"] == product["risk_label"] else 5,
            "fee_penalty": -min(10, product["fees_bps"] // 10),
        }
        eligible.append({
            "product_id": product["product_id"], "name": product["name"], "version": product["version"],
            "score": sum(components.values()), "score_components": components,
            "risk_label": product["risk_label"], "liquidity": product["liquidity"], "lock_months": product["lock_months"],
            "amount_bounds": product["amount_bounds"], "fees_bps": product["fees_bps"], "fees_flat": product["fees_flat"],
            "reason": "满足可用金额、期限、流动性与确认的风险约束；评分按固定规则计算。",
            "source_terms": _references(product), "synthetic": True,
        })
    eligible.sort(key=lambda product: (-product["score"], product["product_id"]))
    return {
        "status": "matched" if eligible else "no_eligible_products", "eligible": eligible, "excluded": excluded,
        "calculation": calculate_goal(values), "profile_inputs": values, "as_of": as_of,
        "catalog_fingerprint": catalog.fingerprint(as_of), "ranking_version": RANKING_VERSION,
        "ranking_rules": {
            "goal_fit": "目标在模拟产品的支持目标中得 40 分，否则 10 分。",
            "liquidity_fit": "完全一致得 30 分；满足更宽松要求得 20 分。",
            "term_fit": "max(0, 20 - |目标锁定月份 - 产品锁定月份|)；立即/灵活取用或应急目标的目标锁定月份为 0。",
            "risk_fit": "模拟风险标签与确认上限一致得 10 分，更低得 5 分。",
            "fee_penalty": "-min(10, 费用基点整除 10)；固定费用不参与评分。",
            "tie_break": "同分按 product_id 升序；模型不能调整权重或改写分数。",
        },
        "assumptions": [
            "金额区间按全部可用余额作为单一产品的模拟匹配金额；未计算分配比例或组合。",
            "风险偏好为用户明确确认的标签上限，不从聊天、点击或兴趣推断。",
            "流动性 immediate 只允许立即取用；flexible 允许立即或灵活取用；locked 允许三类。",
            "应急目标额外限制立即取用，与其他流动性要求同时生效。",
            "硬约束匹配与现金流达成能力分别呈现；有候选不表示目标可按期达成。",
            "分数是版本化设计选择，未由真实行为训练，不能代表收益或投资质量。",
        ],
        "synthetic": True, "financial_advice": False,
    }
