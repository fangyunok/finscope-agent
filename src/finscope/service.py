"""Principal-scoped memory, confirmation, and traceable financial analysis."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .database import Database
from .schemas import PROFILE_FIELDS, Principal, ProfilePatch, canonical_json


class DomainError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _version(value):
    if type(value) is not int or value < 0:
        raise DomainError("invalid_version", "画像版本必须是非负整数。")
    return value


class FinanceService:
    def __init__(self, database_path: str | Path, *, catalog=None):
        self.database = Database(database_path)
        self.database_path = self.database.path
        self._catalog = catalog

    @property
    def catalog(self):
        if self._catalog is None:
            from .catalog import Catalog
            self._catalog = Catalog()
        return self._catalog

    def seed_demo(self):
        from .resources import data_path
        payload = json.loads(data_path("demo_users.json").read_text(encoding="utf-8"))
        users = payload["users"] if isinstance(payload, dict) else payload
        with self.database.transaction(write=True) as connection:
            for user in users:
                connection.execute("INSERT OR IGNORE INTO users(user_id,tenant_id,display_name) VALUES(?,?,?)", (user["user_id"], user["tenant_id"], user["display_name"]))
        return {"synthetic": True, "users": len(users), "profiles_automatically_confirmed": False}

    def list_demo_users(self):
        with self.database.transaction() as connection:
            return [dict(row) for row in connection.execute("SELECT user_id,tenant_id,display_name FROM users WHERE active=1 ORDER BY user_id")]

    def authenticate_demo(self, user_id):
        if not isinstance(user_id, str):
            raise DomainError("invalid_identity", "演示身份无效。", 401)
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM users WHERE user_id=? AND active=1", (user_id,)).fetchone()
            if not row:
                raise DomainError("invalid_identity", "演示身份无效。", 401)
            return Principal(user_id=row["user_id"], tenant_id=row["tenant_id"], display_name=row["display_name"])

    def _principal(self, connection, principal):
        if not isinstance(principal, Principal):
            raise DomainError("invalid_identity", "需要服务端身份。", 401)
        row = connection.execute("SELECT * FROM users WHERE user_id=? AND active=1", (principal.user_id,)).fetchone()
        if not row or any(row[key] != getattr(principal, key) for key in ("user_id", "tenant_id", "display_name")):
            raise DomainError("invalid_identity", "身份已失效。", 401)
        return row

    def _profile(self, connection, user):
        row = connection.execute("SELECT * FROM profiles WHERE owner_id=?", (user["user_id"],)).fetchone()
        data = json.loads(row["data_json"]) if row else {}
        missing = [key for key in PROFILE_FIELDS if key not in data]
        sources = [dict(source) for source in connection.execute("SELECT field,value_json,event_id,source,confirmed_version,confirmed_at FROM field_sources WHERE owner_id=? ORDER BY field", (user["user_id"],))]
        for source in sources:
            source["value"] = json.loads(source.pop("value_json"))
        return {"version": user["profile_version"], "memory_epoch": user["memory_epoch"], "feedback_version": user["feedback_version"], "data": data, "field_sources": sources, "missing_fields": missing, "complete": not missing, "updated_at": row["updated_at"] if row else None}

    def get_profile(self, principal):
        with self.database.transaction() as connection:
            return self._profile(connection, self._principal(connection, principal))

    @staticmethod
    def _patch(patch):
        try:
            return ProfilePatch.model_validate(patch).model_dump(exclude_unset=True)
        except (ValidationError, ValueError, TypeError):
            raise DomainError("invalid_profile", "画像仅接受明确字段；金额需为人民币字符串，最多两位小数。") from None

    @staticmethod
    def _epoch(user, expected):
        if expected is not None and (type(expected) is not int or expected != user["memory_epoch"]):
            raise DomainError("memory_deleted", "任务开始后的记忆已被删除；请创建新任务。", 409)

    def _event(self, connection, owner_id, kind, payload):
        event_id = "event-" + uuid.uuid4().hex
        connection.execute("INSERT INTO events VALUES(?,?,?,?,?)", (event_id, owner_id, kind, canonical_json(payload), _now()))
        return event_id

    def propose_profile_update(self, principal, patch, source="manual", expected_version=None, expected_memory_epoch=None):
        changes = self._patch(patch)
        if source not in {"manual", "model", "fixture"}:
            raise DomainError("invalid_source", "画像来源无效。")
        if expected_version is not None:
            _version(expected_version)
        with self.database.transaction(write=True) as connection:
            user = self._principal(connection, principal)
            self._epoch(user, expected_memory_epoch)
            if expected_version is not None and expected_version != user["profile_version"]:
                raise DomainError("stale_profile", "画像已更新，请重新读取。", 409)
            profile = self._profile(connection, user)
            candidate = {**profile["data"], **changes}
            proposal_id = "proposal-" + uuid.uuid4().hex
            digest = _hash({"owner_id": user["user_id"], "base_version": user["profile_version"], "memory_epoch": user["memory_epoch"], "changes": changes, "candidate": candidate})
            connection.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proposal_id, user["user_id"], user["memory_epoch"], user["profile_version"], canonical_json(changes), canonical_json(candidate), digest, source, "pending", None, _now()))
            self._event(connection, user["user_id"], "profile_proposed", {"proposal_id": proposal_id, "base_version": user["profile_version"], "source": source})
            return self._proposal(connection, user, proposal_id)

    def _proposal(self, connection, user, proposal_id):
        row = connection.execute("SELECT * FROM proposals WHERE proposal_id=? AND owner_id=? AND memory_epoch=?", (proposal_id, user["user_id"], user["memory_epoch"])).fetchone()
        if not row:
            raise DomainError("not_found", "画像提议不存在。", 404)
        changes = json.loads(row["changes_json"])
        candidate = json.loads(row["candidate_json"])
        base = connection.execute("SELECT data_json FROM profile_history WHERE owner_id=? AND version=?", (user["user_id"], row["base_version"])).fetchone()
        previous = json.loads(base["data_json"]) if base else {}
        return {"proposal_id": proposal_id, "memory_epoch": row["memory_epoch"], "base_version": row["base_version"], "changes": changes, "candidate": candidate, "diff": {key: {"before": previous.get(key), "after": value} for key, value in changes.items()}, "content_hash": row["content_hash"], "status": row["status"], "source": row["source"], "confirmed_version": row["confirmed_version"], "created_at": row["created_at"]}

    def get_proposal(self, principal, proposal_id):
        with self.database.transaction() as connection:
            return self._proposal(connection, self._principal(connection, principal), proposal_id)

    def list_proposals(self, principal):
        with self.database.transaction() as connection:
            user = self._principal(connection, principal)
            ids = [row[0] for row in connection.execute("SELECT proposal_id FROM proposals WHERE owner_id=? AND memory_epoch=? ORDER BY created_at DESC LIMIT 50", (user["user_id"], user["memory_epoch"]))]
            return [self._proposal(connection, user, proposal_id) for proposal_id in ids]

    def confirm_profile_update(self, principal, proposal_id, expected_version, expected_hash, confirmed=True):
        _version(expected_version)
        if confirmed is not True:
            raise DomainError("confirmation_required", "需要明确确认画像变更。")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise DomainError("invalid_hash", "需要当前提议的内容摘要。")
        with self.database.transaction(write=True) as connection:
            user = self._principal(connection, principal)
            proposal = self._proposal(connection, user, proposal_id)
            if expected_hash != proposal["content_hash"] or expected_version != proposal["base_version"]:
                raise DomainError("stale_proposal", "提议版本或内容已变化。", 409)
            if proposal["status"] == "confirmed" and user["profile_version"] == proposal["confirmed_version"]:
                return self._profile(connection, user)
            if proposal["status"] != "pending" or user["profile_version"] != expected_version:
                raise DomainError("stale_proposal", "旧提议不能覆盖新画像。", 409)
            candidate = {**self._profile(connection, user)["data"], **proposal["changes"]}
            digest = _hash({"owner_id": user["user_id"], "base_version": expected_version, "memory_epoch": user["memory_epoch"], "changes": proposal["changes"], "candidate": candidate})
            if digest != expected_hash:
                raise DomainError("stale_proposal", "当前画像与提议快照不同。", 409)
            version, timestamp = expected_version + 1, _now()
            event_id = self._event(connection, user["user_id"], "profile_confirmed", {"proposal_id": proposal_id, "version": version})
            connection.execute("UPDATE users SET profile_version=? WHERE user_id=?", (version, user["user_id"]))
            connection.execute("INSERT INTO profiles VALUES(?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET data_json=excluded.data_json,version=excluded.version,memory_epoch=excluded.memory_epoch,updated_at=excluded.updated_at", (user["user_id"], canonical_json(candidate), version, user["memory_epoch"], timestamp))
            for key, value in proposal["changes"].items():
                connection.execute("INSERT INTO field_sources VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id,field) DO UPDATE SET value_json=excluded.value_json,event_id=excluded.event_id,source=excluded.source,confirmed_version=excluded.confirmed_version,confirmed_at=excluded.confirmed_at", (user["user_id"], key, canonical_json(value), event_id, proposal["source"], version, timestamp))
            connection.execute("INSERT INTO profile_history VALUES(?,?,?,?,?,?)", (user["user_id"], version, user["memory_epoch"], canonical_json(candidate), event_id, timestamp))
            connection.execute("UPDATE proposals SET status='confirmed',confirmed_version=? WHERE proposal_id=?", (version, proposal_id))
            invalidation = {"status": "failed", "error": {"code": "stale_profile", "message": "分析期间画像已更新，请创建新任务。"}}
            connection.execute("UPDATE runs SET status='failed',result_json=?,completed_at=? WHERE owner_id=? AND status='running'", (canonical_json(invalidation), timestamp, user["user_id"]))
            return self._profile(connection, self._principal(connection, principal))

    def delete_memory(self, principal, confirmed=True):
        if confirmed is not True:
            raise DomainError("confirmation_required", "需要明确确认删除记忆。")
        with self.database.transaction(write=True) as connection:
            user = self._principal(connection, principal)
            for table in ("profiles", "field_sources", "profile_history", "proposals", "feedback", "runs", "events"):
                connection.execute(f"DELETE FROM {table} WHERE owner_id=?", (user["user_id"],))
            epoch = user["memory_epoch"] + 1
            version = user["profile_version"] + 1
            connection.execute("UPDATE users SET memory_epoch=?,profile_version=?,feedback_version=feedback_version+1 WHERE user_id=?", (epoch, version, user["user_id"]))
            return {"deleted": True, "memory_epoch": epoch, "version": version, "scope": ["profile", "field_sources", "history", "proposals", "feedback", "runs", "events"], "index_or_cache_used": False}

    def calculate(self, principal):
        from .calculations import calculate_goal
        profile = self.get_profile(principal)
        if not profile["complete"]:
            raise DomainError("incomplete_profile", "请先确认完整画像。")
        try:
            result = calculate_goal(profile["data"])
        except ValueError:
            raise DomainError("invalid_profile", "当前画像无法计算，请核对明确字段。") from None
        return {**result, "profile_version": profile["version"], "memory_epoch": profile["memory_epoch"]}

    def search_terms(self, principal, query="", as_of="2026-10-03", product_id=None):
        with self.database.transaction() as connection:
            self._principal(connection, principal)
        if not isinstance(query, str) or len(query) > 500 or (product_id is not None and (not isinstance(product_id, str) or len(product_id) > 100)):
            raise DomainError("invalid_query", "条款查询无效。")
        try:
            return self.catalog.search(query, as_of=as_of, product_id=product_id)
        except ValueError:
            raise DomainError("invalid_catalog_query", "条款日期或版本无效。") from None

    def record_feedback(self, principal, product_id, action="favorite", confirmed=True):
        if confirmed is not True:
            raise DomainError("confirmation_required", "需要明确确认反馈。")
        if action not in {"favorite", "exclude", "clear"}:
            raise DomainError("invalid_feedback", "反馈仅支持收藏、排除或清除。")
        if not isinstance(product_id, str) or product_id not in {product["product_id"] for product in self.catalog.products("2026-10-03")}:
            raise DomainError("invalid_product", "模拟产品不存在。")
        with self.database.transaction(write=True) as connection:
            user = self._principal(connection, principal)
            previous = connection.execute("SELECT action FROM feedback WHERE owner_id=? AND product_id=?", (user["user_id"], product_id)).fetchone()
            changed = (previous is not None) if action == "clear" else (previous is None or previous["action"] != action)
            if action == "clear":
                connection.execute("DELETE FROM feedback WHERE owner_id=? AND product_id=?", (user["user_id"], product_id))
            else:
                connection.execute("INSERT INTO feedback VALUES(?,?,?,?,?) ON CONFLICT(owner_id,product_id) DO UPDATE SET action=excluded.action,memory_epoch=excluded.memory_epoch,created_at=excluded.created_at", (user["user_id"], product_id, action, user["memory_epoch"], _now()))
            self._event(connection, user["user_id"], "explicit_feedback", {"product_id": product_id, "action": action, "risk_preference_updated": False})
            if changed:
                connection.execute("UPDATE users SET feedback_version=feedback_version+1 WHERE user_id=?", (user["user_id"],))
                failure = {"status": "failed", "error": {"code": "stale_feedback", "message": "分析期间反馈已更新，请创建新任务。"}}
                connection.execute("UPDATE runs SET status='failed',result_json=?,completed_at=? WHERE owner_id=? AND status='running'", (canonical_json(failure), _now(), user["user_id"]))
            return {"product_id": product_id, "action": action, "risk_preference_updated": False}

    def list_feedback(self, principal):
        with self.database.transaction() as connection:
            user = self._principal(connection, principal)
            return [dict(row) for row in connection.execute("SELECT product_id,action,created_at FROM feedback WHERE owner_id=? AND memory_epoch=? ORDER BY product_id", (user["user_id"], user["memory_epoch"]))]

    def match(self, principal, as_of="2026-10-03"):
        from .matching import match_products
        with self.database.transaction() as connection:
            user = self._principal(connection, principal)
            profile = self._profile(connection, user)
            exclusions = [row[0] for row in connection.execute("SELECT product_id FROM feedback WHERE owner_id=? AND memory_epoch=? AND action='exclude'", (user["user_id"], user["memory_epoch"]))]
            feedback = [dict(row) for row in connection.execute("SELECT product_id,action,created_at FROM feedback WHERE owner_id=? AND memory_epoch=? ORDER BY product_id", (user["user_id"], user["memory_epoch"]))]
        if not profile["complete"]:
            raise DomainError("incomplete_profile", "请先确认完整画像及风险偏好。")
        try:
            result = match_products(profile["data"], self.catalog, as_of=as_of, excluded_product_ids=exclusions)
        except ValueError:
            raise DomainError("invalid_profile_or_catalog", "画像或条款无法匹配，请核查。") from None
        return {**result, "profile_version": profile["version"], "memory_epoch": profile["memory_epoch"], "feedback_version": user["feedback_version"], "feedback": feedback}

    def create_run(self, principal, message, mode, request_id=None):
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
            raise DomainError("invalid_message", "任务输入需要 1–4000 个字符。")
        if mode not in {"fixture", "qwen", "api"}:
            raise DomainError("invalid_mode", "模型模式无效。")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 100):
            raise DomainError("invalid_request_id", "任务请求编号无效。")
        with self.database.transaction(write=True) as connection:
            user = self._principal(connection, principal)
            digest = _hash({"message": message, "mode": mode, "memory_epoch": user["memory_epoch"], "profile_version": user["profile_version"], "feedback_version": user["feedback_version"]})
            prior = connection.execute("SELECT * FROM runs WHERE owner_id=? AND request_id=?", (user["user_id"], request_id)).fetchone() if request_id else None
            if prior:
                if digest != prior["request_hash"]:
                    raise DomainError("idempotency_conflict", "请求编号已经用于不同任务或画像版本。", 409)
                return {**self._run(prior), "_created": False}
            run_id = "run-" + uuid.uuid4().hex
            connection.execute("INSERT INTO runs(run_id,owner_id,memory_epoch,profile_version,mode,message,status,request_id,request_hash,result_json,created_at,completed_at,feedback_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, user["user_id"], user["memory_epoch"], user["profile_version"], mode, message, "running", request_id, digest, None, _now(), None, user["feedback_version"]))
            return {**self._run(connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()), "_created": True}

    @staticmethod
    def _run(row):
        result = dict(row)
        raw_result = result.pop("result_json")
        result["result"] = json.loads(raw_result) if raw_result else None
        result.pop("request_hash", None)
        return result

    def complete_run(self, principal, run_id, result):
        if not isinstance(result, dict):
            raise DomainError("invalid_result", "任务结果无效。")
        status = result.get("status", "completed")
        if status not in {"completed", "pending_confirmation", "needs_profile", "failed", "insufficient_evidence", "pending_review", "source_preview"}:
            raise DomainError("invalid_result", "任务状态无效。")
        with self.database.transaction(write=True) as connection:
            user = self._principal(connection, principal)
            row = connection.execute("SELECT * FROM runs WHERE run_id=? AND owner_id=?", (run_id, user["user_id"])).fetchone()
            if not row:
                raise DomainError("not_found", "任务不存在或记忆已删除。", 404)
            self._epoch(user, row["memory_epoch"])
            if user["profile_version"] != row["profile_version"]:
                raise DomainError("stale_profile", "分析期间画像已更新，请创建新任务。", 409)
            if user["feedback_version"] != row["feedback_version"]:
                raise DomainError("stale_feedback", "分析期间反馈已更新，请创建新任务。", 409)
            if row["status"] != "running":
                if row["result_json"] != canonical_json(result):
                    raise DomainError("run_conflict", "已完成任务不能被覆盖。", 409)
                return self._run(row)
            connection.execute("UPDATE runs SET status=?,result_json=?,completed_at=? WHERE run_id=?", (status, canonical_json(result), _now(), run_id))
            return self._run(connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

    def get_run(self, principal, run_id):
        with self.database.transaction() as connection:
            user = self._principal(connection, principal)
            row = connection.execute("SELECT * FROM runs WHERE run_id=? AND owner_id=? AND memory_epoch=?", (run_id, user["user_id"], user["memory_epoch"])).fetchone()
            if not row:
                raise DomainError("not_found", "任务不存在或记忆已删除。", 404)
            return self._run(row)

    def list_runs(self, principal):
        with self.database.transaction() as connection:
            user = self._principal(connection, principal)
            return [self._run(row) for row in connection.execute("SELECT * FROM runs WHERE owner_id=? AND memory_epoch=? ORDER BY created_at DESC LIMIT 50", (user["user_id"], user["memory_epoch"]))]
