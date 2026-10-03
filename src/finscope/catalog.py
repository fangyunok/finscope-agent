"""Versioned synthetic product terms and deterministic keyword retrieval."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .calculations import ProfileValidationError, money


class CatalogError(ValueError):
    def __init__(self, message: str, code: str = "invalid_catalog"):
        super().__init__(message)
        self.code = code


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise CatalogError(f"{field} 必须为 YYYY-MM-DD 日期。", "invalid_date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogError(f"{field} 日期无效。", "invalid_date") from exc


def _validation_errors(product: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if product.get("currency") != "CNY":
        errors.append("missing_or_invalid_currency")
    for key in ("name", "version"):
        if not isinstance(product.get(key), str) or not product[key].strip():
            errors.append(f"missing_{key}")
    bounds = product.get("amount_bounds")
    if not isinstance(bounds, dict):
        errors.append("missing_amount_bounds")
    else:
        try:
            lower, upper = money(bounds.get("min"), "min"), money(bounds.get("max"), "max")
            if lower > upper:
                errors.append("invalid_amount_bounds")
        except ProfileValidationError:
            errors.append("invalid_amount_bounds")
    lock = product.get("lock_months")
    if type(lock) is not int or not 0 <= lock <= 1200:
        errors.append("missing_or_invalid_lock_months")
    if product.get("risk_label") not in ("low", "medium", "high"):
        errors.append("missing_or_invalid_risk_label")
    if product.get("liquidity") not in ("immediate", "flexible", "locked"):
        errors.append("missing_or_invalid_liquidity")
    if type(lock) is int and lock > 0 and product.get("liquidity") in ("immediate", "flexible"):
        errors.append("inconsistent_liquidity_and_lock")
    fees = product.get("fees_bps")
    if type(fees) is not int or not 0 <= fees <= 10000:
        errors.append("missing_or_invalid_fees_bps")
    try:
        money(product.get("fees_flat"), "fees_flat")
    except ProfileValidationError:
        errors.append("missing_or_invalid_fees_flat")
    goals = product.get("supported_goals")
    if not isinstance(goals, list) or not goals or any(goal not in ("emergency", "purchase", "long_term") for goal in goals):
        errors.append("missing_or_invalid_supported_goals")
    clauses = product.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        errors.append("missing_clauses")
    else:
        seen: set[str] = set()
        for clause in clauses:
            if not isinstance(clause, dict) or not all(isinstance(clause.get(key), str) and clause[key].strip() for key in ("term_id", "kind", "content")):
                errors.append("invalid_clause")
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", clause["term_id"]):
                errors.append("invalid_term_id")
            if clause["kind"] not in ("amount", "lock", "risk", "liquidity", "fees", "goal"):
                errors.append("invalid_clause_kind")
            if clause["term_id"] in seen:
                errors.append("duplicate_term_id")
            seen.add(clause["term_id"])
        if not {"amount", "lock", "risk", "liquidity", "fees", "goal"}.issubset({item.get("kind") for item in clauses if isinstance(item, dict) and isinstance(item.get("kind"), str)}):
            errors.append("missing_constraint_clauses")
    return sorted(set(errors))


class Catalog:
    """Validity is filtered before retrieval; prose never controls constraints."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = Path(__file__).with_name("data") / "catalog.json"
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogError("无法读取模拟产品目录。") from exc
        if not isinstance(document, dict) or document.get("synthetic") is not True or not isinstance(document.get("products"), list):
            raise CatalogError("产品目录必须明确标为 synthetic=true。")
        self._records = deepcopy(document["products"])
        for product in self._records:
            if not isinstance(product, dict) or product.get("synthetic") is not True:
                raise CatalogError("每个产品必须明确标为 synthetic=true。")
            if not isinstance(product.get("product_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", product["product_id"]):
                raise CatalogError("product_id 无效。")
            start, end = _date(product.get("effective_from"), "effective_from"), _date(product.get("effective_to"), "effective_to")
            if start >= end:
                raise CatalogError("产品生效区间必须为非空区间。")

    def _active(self, as_of: str) -> list[dict[str, Any]]:
        query_date = _date(as_of, "as_of")
        return [deepcopy(product) for product in self._records if _date(product["effective_from"], "effective_from") <= query_date < _date(product["effective_to"], "effective_to")]

    def products(self, as_of: str = "2026-10-03") -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for product in self._active(as_of):
            grouped[product["product_id"]].append(product)
        result = []
        for product_id in sorted(grouped):
            records = grouped[product_id]
            if len(records) != 1:
                result.append({
                    "product_id": product_id, "name": product_id, "synthetic": True,
                    "validation_errors": ["version_conflict"],
                    "available_versions": sorted(str(record.get("version", "")) for record in records),
                    "clauses": [],
                })
            else:
                product = records[0]
                product["validation_errors"] = _validation_errors(product)
                result.append(product)
        # TermQA addresses terms by term_id. An ID shared by two active products
        # must never be resolved by whichever source happens to rank last.
        term_owners: dict[str, set[str]] = defaultdict(set)
        for product in result:
            for clause in product.get("clauses", []):
                if isinstance(clause, dict) and isinstance(clause.get("term_id"), str):
                    term_owners[clause["term_id"]].add(product["product_id"])
        ambiguous_products = set().union(*(owners for owners in term_owners.values() if len(owners) > 1))
        for product in result:
            if product["product_id"] in ambiguous_products:
                product["validation_errors"] = sorted(set(product["validation_errors"]) | {"ambiguous_term_id"})
        return result

    def fingerprint(self, as_of: str = "2026-10-03") -> str:
        # Include every conflicting active record rather than hiding its content.
        records = sorted(self._active(as_of), key=lambda product: (product["product_id"], str(product.get("version", "")), json.dumps(product, sort_keys=True, ensure_ascii=False)))
        payload = json.dumps({"as_of": as_of, "products": records}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def search(self, query: str, as_of: str = "2026-10-03", product_id: str | None = None, *, limit: int = 12) -> list[dict[str, Any]]:
        if not isinstance(query, str) or len(query) > 1000:
            raise CatalogError("query 必须为不超过 1000 字的字符串。", "invalid_query")
        if product_id is not None and (not isinstance(product_id, str) or len(product_id) > 64):
            raise CatalogError("product_id 无效。", "invalid_query")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CatalogError("limit 必须为 1 至 100 的整数。", "invalid_query")
        stripped = query.strip().lower()
        query_tokens = set(re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]", stripped))
        hits = []
        for product in self.products(as_of):
            if set(product["validation_errors"]) & {"version_conflict", "ambiguous_term_id"} or (product_id and product["product_id"] != product_id):
                continue
            for clause in product.get("clauses", []):
                if not isinstance(clause, dict) or not isinstance(clause.get("content"), str) or not isinstance(clause.get("term_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", clause["term_id"]):
                    continue
                document = " ".join(str(value) for value in (product["product_id"], product.get("name", ""), clause.get("kind", ""), clause["content"])).lower()
                tokens = set(re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]", document))
                score = len(tokens & query_tokens) + (20 if stripped and stripped in document else 0)
                if stripped and score == 0:
                    continue
                hits.append({
                    "product_id": product["product_id"], "product_name": product.get("name", product["product_id"]),
                    "term_id": clause.get("term_id"), "kind": clause.get("kind"),
                    "version": product.get("version"), "effective_from": product["effective_from"],
                    "effective_to": product["effective_to"], "as_of": as_of,
                    "content": clause["content"], "quote": clause["content"], "synthetic": True,
                    "catalog_validation_errors": product["validation_errors"],
                    "retrieval_score": score, "retrieval_method": "keyword_character_overlap_v1",
                })
        return sorted(hits, key=lambda hit: (-hit["retrieval_score"], hit["product_id"], str(hit["term_id"])))[:limit]
