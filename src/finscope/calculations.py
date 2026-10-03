"""Deterministic cash-flow calculations in yuan, with no interest forecast."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, localcontext
import re
from typing import Any


MONEY_FIELDS = ("monthly_income", "essential_expense", "available_balance", "goal_amount")
PREFERENCE_FIELDS = ("liquidity_need", "risk_preference", "goal_kind")
MONEY_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,2})?\Z")


class ProfileValidationError(ValueError):
    """An input error with machine-readable fields, never a guessed profile."""

    def __init__(self, message: str, *, missing_fields: list[str] | None = None):
        super().__init__(message)
        self.code = "missing_profile_fields" if missing_fields else "invalid_profile"
        self.missing_fields = missing_fields or []


def money(value: Any, field: str = "amount") -> Decimal:
    """Parse a nonnegative money string; reject floats and hidden rounding."""
    if not isinstance(value, str) or not MONEY_PATTERN.fullmatch(value):
        raise ProfileValidationError(f"{field} 必须为非负人民币金额字符串，最多两位小数。")
    return Decimal(value).quantize(Decimal("0.01"))


def validate_profile(profile: dict[str, Any], *, preferences: bool = False) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ProfileValidationError("画像必须为对象。")
    required = list(MONEY_FIELDS) + ["horizon_months"]
    if preferences:
        required += list(PREFERENCE_FIELDS)
    missing = [key for key in required if profile.get(key) is None or profile.get(key) == ""]
    if missing:
        raise ProfileValidationError("请补充并确认字段：" + "、".join(missing), missing_fields=missing)
    result = {key: format(money(profile[key], key), ".2f") for key in MONEY_FIELDS}
    horizon = profile["horizon_months"]
    if type(horizon) is not int or not 1 <= horizon <= 1200:
        raise ProfileValidationError("horizon_months 必须是 1 至 1200 的整数。")
    result["horizon_months"] = horizon
    if profile.get("currency", "CNY") != "CNY":
        raise ProfileValidationError("当前计算器仅支持 CNY，不能自动转换币种。")
    result["currency"] = "CNY"
    if preferences:
        options = {
            "liquidity_need": {"immediate", "flexible", "locked"},
            "risk_preference": {"low", "medium", "high"},
            "goal_kind": {"emergency", "purchase", "long_term"},
        }
        for key, values in options.items():
            if not isinstance(profile[key], str) or profile[key] not in values:
                raise ProfileValidationError(f"{key} 的取值无效。")
            result[key] = profile[key]
    return result


def calculate_goal(profile: dict[str, Any]) -> dict[str, Any]:
    """Calculate labelled, whole-month savings arithmetic from explicit inputs."""
    values = validate_profile(profile)
    with localcontext() as context:
        context.prec = 50
        income, expense, balance, target = (Decimal(values[key]) for key in MONEY_FIELDS)
        surplus = income - expense
        gap = max(Decimal("0.00"), target - balance)
        if gap == 0:
            months, status = 0, "already_achieved"
        elif surplus <= 0:
            months, status = None, "unreachable"
        else:
            months = int((gap / surplus).to_integral_value(rounding=ROUND_CEILING))
            status = "reachable"
        horizon = values["horizon_months"]
        return {
            "operation": "cashflow_goal_v1",
            "inputs": values,
            "monthly_surplus": format(surplus, ".2f"),
            "goal_gap": format(gap, ".2f"),
            "months_to_goal": months,
            "horizon_months": horizon,
            "horizon_feasible": months is not None and months <= horizon,
            "projected_balance_at_horizon": format(balance + surplus * horizon, ".2f"),
            "status": status,
            "units": {"money": "CNY yuan", "duration": "whole months"},
            "rounding": "金额输入最多两位小数；达成月数向上取整，不舍入金额。",
            "assumptions": [
                "每月收入与必要支出保持不变，每月结余全部用于该单一目标。",
                "忽略利息、投资收益、通胀、税费及收入支出的未来变化。",
                "按完整月份累计；初始可用余额均可用于该目标。",
                "期限末余额为线性收支推算，负值表示现金流缺口，不表示可借款额度。",
            ],
            "financial_advice": False,
        }
