"""Explicit profile fields; model output cannot choose an owner or confirm memory."""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: str
    tenant_id: str
    display_name: str


PROFILE_FIELDS = (
    "monthly_income", "essential_expense", "available_balance", "goal_amount",
    "horizon_months", "liquidity_need", "risk_preference", "goal_kind",
)


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    monthly_income: str | None = None
    essential_expense: str | None = None
    available_balance: str | None = None
    goal_amount: str | None = None
    horizon_months: int | None = Field(default=None, ge=1, le=1200, strict=True)
    liquidity_need: Literal["immediate", "flexible", "locked"] | None = None
    risk_preference: Literal["low", "medium", "high"] | None = None
    goal_kind: Literal["emergency", "purchase", "long_term"] | None = None
    currency: Literal["CNY"] | None = None

    @field_validator("monthly_income", "essential_expense", "available_balance", "goal_amount", mode="before")
    @classmethod
    def money(cls, value):
        if value is None:
            return value
        if not isinstance(value, str) or not re.fullmatch(r"\d{1,10}(?:\.\d{1,2})?", value):
            raise ValueError("Amounts must be nonnegative CNY strings with at most two decimal places")
        number = Decimal(value)
        if number > Decimal("1000000000"):
            raise ValueError("Amount exceeds the demo bound")
        return format(number.quantize(Decimal("0.01")), "f")

    @model_validator(mode="after")
    def explicit_nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("At least one explicit, non-null profile field is required")
        return self


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
