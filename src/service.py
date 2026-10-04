from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ValidationError, ensure_role, normalize_severity,
                     require_hole_no, require_number, require_opening, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_MANAGE_ROLES, CREATE_ROLES, ENTITY,
                    GATE_RECEIPT_ROLES, RECORD_ROLES, RESERVOIR_ROLES,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    in_position, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ------------------------------------------------------------------
    # 指令创建
    # ------------------------------------------------------------------
    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    # ------------------------------------------------------------------
    # 通用记录（复核证据等）
    # ------------------------------------------------------------------
    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    # ------------------------------------------------------------------
    # 状态机：授权冻结孔位目标；执行到位/关闭校验回执与未关闭事项
    # ------------------------------------------------------------------
    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str, holes: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")

        frozen: Optional[List[Dict[str, Any]]] = None
        if target == "authorized":
            frozen = self._validate_holes(holes)
            updated = self.repository.authorize_item(
                item_id, expected_version, actor, frozen, item["quantity"])
        else:
            if target in ("executed", "closed"):
                hole_rows = self.repository.list_gate_holes(item_id)
                open_items = (self.repository.open_record_count(item_id)
                              + self.repository.open_gate_receipt_count(item_id))
                blockers = completion_blockers(target, open_items, hole_rows)
                if blockers:
                    raise ConflictError("；".join(blockers))
            updated = self.repository.transition_item(item_id, target, expected_version, actor)

        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        if frozen is not None:
            self.repository.append_audit("freeze_holes", ENTITY, item_id, actor, {"holes": frozen})
        return self.enrich(updated)

    @staticmethod
    def _validate_holes(holes: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        if not isinstance(holes, list) or not holes:
            raise ValidationError("授权时必须提供非空的闸门孔位与开度目标")
        frozen: List[Dict[str, Any]] = []
        seen = set()
        for h in holes:
            if not isinstance(h, dict):
                raise ValidationError("孔位必须是对象")
            hole_no = require_hole_no(h.get("hole_no"))
            target = require_opening(h.get("target_opening"), "target_opening")
            if hole_no in seen:
                raise ValidationError(f"孔位{hole_no}重复")
            seen.add(hole_no)
            frozen.append({"hole_no": hole_no, "target_opening": target})
        return frozen

    # ------------------------------------------------------------------
    # 闸门执行回执（逐孔登记，幂等重试，先到结果生效）
    # ------------------------------------------------------------------
    def submit_gate_receipt(self, item_id: int, batch_id: int,
                            payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, GATE_RECEIPT_ROLES)
        actor = require_text(actor, "actor", 100)
        hole_no = require_hole_no(payload.get("hole_no"))
        actual = require_opening(payload.get("actual_opening"), "actual_opening")
        external_ref = require_text(payload.get("external_ref"), "external_ref", 100)

        item = self.repository.get_item(item_id)
        batch = self.repository.get_execution_batch(batch_id)
        if batch["item_id"] != item_id:
            raise ValidationError("批次不属于该指令")
        if batch["status"] != "open":
            raise ConflictError("批次已完成，不能追加回执")

        result = self.repository.submit_gate_receipt(
            item_id, batch_id, hole_no, actual, external_ref, actor)
        receipt = result["receipt"]
        if result["created"]:
            self.repository.append_audit("gate_receipt", ENTITY, item_id, actor, {
                "batch_id": batch_id, "seq": batch["seq"], "hole_no": hole_no,
                "actual_opening": actual, "target_opening": receipt["target_opening"],
                "in_position": receipt["status"] == "closed",
                "external_ref": external_ref,
                "auto_executing": result["auto_executing"],
            })
        return self._enrich_receipt(receipt)

    @staticmethod
    def _enrich_receipt(receipt: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(receipt)
        result["in_position"] = in_position(
            receipt["actual_opening"], receipt["target_opening"])
        return result

    # ------------------------------------------------------------------
    # 批次续办：恢复后只续办剩余孔，已到位孔不重做
    # ------------------------------------------------------------------
    def create_recovery_batch(self, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] not in ("authorized", "executing"):
            raise ConflictError("当前状态不能续办批次")
        holes = self.repository.list_gate_holes(item_id)
        remaining = [h for h in holes if h["status"] != "in_position"]
        if not remaining:
            raise ConflictError("没有未到位孔，无需续办")
        latest = self.repository.get_latest_batch(item_id)
        seq = (latest["seq"] if latest else 0) + 1
        batch = self.repository.create_execution_batch(item_id, seq, actor)
        self.repository.append_audit("batch", ENTITY, item_id, actor, {
            "batch_id": batch["id"], "seq": seq,
            "remaining_holes": [h["hole_no"] for h in remaining],
        })
        return batch

    def list_execution_batches(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.list_execution_batches(item_id)

    def list_gate_holes(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.list_gate_holes(item_id)

    def list_gate_receipts(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return [self._enrich_receipt(r) for r in self.repository.list_gate_receipts(item_id)]

    # ------------------------------------------------------------------
    # 库位变化：未到位孔按新依据重算，已到位孔保留记录
    # ------------------------------------------------------------------
    def update_reservoir_level(self, item_id: int, quantity: float,
                               actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESERVOIR_ROLES)
        actor = require_text(actor, "actor", 100)
        quantity = require_number(quantity, "quantity")
        item = self.repository.get_item(item_id)
        if item["status"] not in ("authorized", "executing"):
            raise ConflictError("当前状态不能调整库位")
        old_quantity = item["quantity"]
        recomputed = self.repository.recompute_pending_holes(item_id, quantity, item["threshold"])
        self.repository.update_item_quantity(item_id, quantity)
        self.repository.append_audit("reservoir", ENTITY, item_id, actor, {
            "old_quantity": old_quantity, "new_quantity": quantity,
            "recomputed": recomputed,
        })
        return self.enrich(self.repository.get_item(item_id))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
