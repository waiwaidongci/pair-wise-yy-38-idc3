from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, DEFAULT_TOLERANCE, NotFoundError,
                     ensure_role, normalize_severity, require_gate_code,
                     require_number, require_opening, require_text)
from .policy import normalize_gates, plan_openings
from .repository import Repository
from .rules import (AUDIT_ROLES, AUTHORIZE_BATCH_ROLES, CLOSE_ROLES,
                    CREATE_ROLES, DISPATCH_ROLES, ENTITY, RECEIPT_ROLES,
                    RECORD_ROLES, RECOMPUTE_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, gate_blockers,
                    openings_consistent, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

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

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        if target in ("executing", "executed"):
            raise ConflictError(
                "闸门批次必须走专用接口：授权/派工/续办/回执，不允许点一次执行即整条完成")
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        gates = self.repository.list_gates(item_id) if target == "closed" else None
        blockers = completion_blockers(target, self.repository.open_record_count(item_id),
                                       gates)
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        if target == "closed":
            self.repository.mark_batch_closed(item_id)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

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

    # ---- 可续作的闸门执行批次 ----
    def _batch_view(self, item_id: int) -> Dict[str, Any]:
        plan = self.repository.get_plan(item_id)
        if plan is None:
            raise NotFoundError("该指令尚未授权闸门执行批次")
        gates = self.repository.list_gates(item_id)
        rounds = self.repository.list_rounds(item_id)
        receipts = self.repository.list_receipts(item_id)
        open_items = gate_blockers(gates)
        return {"plan": plan, "gates": gates, "rounds": rounds,
                "receipts": receipts, "open_items": open_items,
                "all_arrived": bool(gates) and not open_items}

    def authorize_batch(self, item_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        """总工授权：冻结每孔开度目标，创建唯一执行批次，指令进入执行中。"""
        ensure_role(role, AUTHORIZE_BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "authorized":
            raise ConflictError(f"指令当前为{item['status']}，只有已授权指令能开批")
        gates = normalize_gates(payload.get("gates"))
        basis = require_text(payload.get("basis"), "basis", 500)
        tolerance = DEFAULT_TOLERANCE
        if "tolerance" in payload and payload["tolerance"] is not None:
            tolerance = require_number(payload["tolerance"], "tolerance", 0.0)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        plan = self.repository.create_plan(item_id, basis, tolerance, gates, actor)
        updated = self.repository.transition_item(item_id, "executing",
                                                   expected_version, actor)
        self.repository.append_audit("gate_batch_authorize", ENTITY, item_id, actor, {
            "batch_no": plan["batch_no"], "basis": basis, "tolerance": tolerance,
            "frozen_targets": gates,
        })
        result = self.enrich(updated)
        result["batch"] = self._batch_view(item_id)
        return result

    def _dispatch(self, item_id: int, phase: str, actor: str, role: str,
                  gate_codes: Optional[List[str]]) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "executing":
            raise ConflictError(f"指令当前为{item['status']}，批次只能在执行中派工/续办")
        plan = self.repository.get_plan(item_id)
        if plan is None:
            raise NotFoundError("执行批次不存在")
        open_codes = self.repository.open_gate_codes(item_id)
        if gate_codes is None:
            selected = open_codes
        else:
            requested = [require_gate_code(c) for c in gate_codes]
            known = {g["gate_code"] for g in self.repository.list_gates(item_id)}
            unknown = [c for c in requested if c not in known]
            if unknown:
                raise NotFoundError(f"未知闸门: {','.join(unknown)}")
            # 已到位孔不重做；回传失败的孔允许同批次继续登记，不新增派工
            selected = [c for c in requested if c in open_codes]
        if not selected:
            raise ConflictError("没有需要派工/续办的未到位孔；已到位孔不重做")
        need_round = []
        for code in selected:
            gate = self.repository.get_gate(item_id, code)
            if gate["status"] in ("pending", "refused", "mismatch"):
                need_round.append(code)
        round_info = None
        if need_round:
            attempt_no = self.repository.next_attempt_no(plan["id"])
            round_info = self.repository.dispatch_round(
                item_id, need_round, attempt_no, phase, actor)
        self.repository.append_audit(
            "gate_dispatch" if phase == "dispatch" else "gate_continue",
            ENTITY, item_id, actor,
            {"attempt_no": round_info["attempt_no"] if round_info else None,
             "new_round_gates": need_round,
             "receipt_only_gates": [c for c in selected if c not in need_round]})
        item = self.repository.get_item(item_id)
        result = self.enrich(item)
        result["batch"] = self._batch_view(item_id)
        result["dispatched_round"] = round_info
        return result

    def dispatch_batch(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """首次派工：对全部未到位孔发出执行，建立批次轮次。"""
        gate_codes = payload.get("gate_codes")
        return self._dispatch(item_id, "dispatch", actor, role, gate_codes)

    def continue_batch(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """恢复后续办：只续派拒动/未完成孔，已到位孔不重做，不重复追加。"""
        gate_codes = payload.get("gate_codes")
        return self._dispatch(item_id, "continue", actor, role, gate_codes)

    def register_receipt(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        """值班员逐孔登记实际开度。两个值班员同孔同时提交时先到结果生效。"""
        ensure_role(role, RECEIPT_ROLES)
        actor = require_text(actor, "actor", 100)
        gate_code = require_gate_code(payload.get("gate_code"))
        outcome = payload.get("outcome", "arrived")
        if outcome not in ("arrived", "refused"):
            raise ValueError("outcome必须是arrived或refused")
        item = self.repository.get_item(item_id)
        if item["status"] not in ("executing",):
            raise ConflictError("指令不在执行中，不能登记回执")
        plan = self.repository.get_plan(item_id)
        gate = self.repository.get_gate(item_id, gate_code)
        if gate is None:
            raise NotFoundError(f"闸门{gate_code}不在该批次中")
        if gate["status"] in ("pending", "refused", "mismatch"):
            raise ConflictError(
                f"孔{gate_code}当前为{gate['status']}，须先派工/续办后再登记回执")
        if gate["status"] == "arrived":
            raise ConflictError(f"孔{gate_code}已有生效到位回执，迟到结果不生效")
        actual = None
        if outcome == "arrived":
            actual = require_opening(payload.get("actual_opening"), "actual_opening")
        elif payload.get("actual_opening") is not None:
            actual = require_opening(payload.get("actual_opening"), "actual_opening")
        client_token = payload.get("client_token")
        if client_token is not None:
            client_token = require_text(client_token, "client_token", 100)
        consistent = True
        if actual is not None:
            consistent = openings_consistent(actual, gate["target_opening"],
                                             plan["tolerance"])
        receipt = self.repository.register_receipt(
            item_id, gate_code, outcome, actual, client_token, actor, consistent)
        self.repository.append_audit("gate_receipt", ENTITY, item_id, actor, {
            "gate_code": gate_code, "attempt_no": receipt["attempt_no"],
            "outcome": outcome, "actual_opening": actual,
            "consistent": consistent, "new_status": receipt["new_status"],
            "client_token": client_token,
        })
        result = self.enrich(self.repository.get_item(item_id))
        result["batch"] = self._batch_view(item_id)
        result["receipt"] = receipt
        return result

    def mark_feedback_lost(self, item_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        """登记现场回传失败：孔进入feedback_lost，批次保持执行中，等待同批次续办。"""
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        gate_code = require_gate_code(payload.get("gate_code"))
        item = self.repository.get_item(item_id)
        if item["status"] != "executing":
            raise ConflictError("指令不在执行中")
        gate = self.repository.mark_feedback_lost(item_id, gate_code, actor)
        self.repository.append_audit("gate_feedback_lost", ENTITY, item_id, actor, {
            "gate_code": gate_code, "attempt_no": gate["attempt_no"]})
        result = self.enrich(item)
        result["batch"] = self._batch_view(item_id)
        return result

    def recompute_basis(self, item_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        """库位变化后换依据：未到位孔按新依据重算目标并重新派工；已到位孔保留记录。"""
        ensure_role(role, RECOMPUTE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "executing":
            raise ConflictError("指令不在执行中，不能重算依据")
        plan = self.repository.get_plan(item_id)
        new_basis = require_text(payload.get("basis"), "basis", 500)
        gates_now = self.repository.list_gates(item_id)
        open_codes = [g["gate_code"] for g in gates_now if g["status"] != "arrived"]
        if not open_codes:
            raise ConflictError("全部孔已到位，无需重算")
        if "gates" in payload and payload["gates"] is not None:
            targets = normalize_gates(payload["gates"])
            target_codes = {t["gate_code"] for t in targets}
            if target_codes != set(open_codes):
                raise ConflictError("重算目标必须且只能覆盖未到位孔，已到位孔保留记录")
        else:
            basis_opening = require_number(payload.get("basis_opening"),
                                           "basis_opening", 0.0)
            overrides = payload.get("overrides") or {}
            if not isinstance(overrides, dict):
                raise ValueError("overrides必须是对象")
            targets = plan_openings(basis_opening, open_codes, overrides)
        self.repository.recompute_targets(item_id, new_basis, targets, actor)
        self.repository.append_audit("gate_recompute", ENTITY, item_id, actor, {
            "old_basis_version": plan["basis_version"],
            "new_basis": new_basis, "new_targets": targets,
            "preserved_arrived": [g["gate_code"] for g in gates_now
                                  if g["status"] == "arrived"]})
        result = self.enrich(self.repository.get_item(item_id))
        result["batch"] = self._batch_view(item_id)
        return result

    def settle_batch(self, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        """判定批次是否可完成：全部孔有一致到位回执且无未关闭事项时，指令才进入executed。"""
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "executing":
            raise ConflictError(f"指令当前为{item['status']}，批次不在执行中")
        batch = self._batch_view(item_id)
        blockers = batch["open_items"]
        if self.repository.open_record_count(item_id) > 0:
            blockers = blockers + ["仍有未关闭事项"]
        if blockers:
            raise ConflictError("批次未满足完成条件：" + "；".join(blockers))
        self.repository.mark_batch_completed(item_id)
        updated = self.repository.transition_item(item_id, "executed",
                                                   item["version"], actor)
        self.repository.append_audit("gate_batch_completed", ENTITY, item_id, actor, {
            "gates": [g["gate_code"] for g in batch["gates"]]})
        result = self.enrich(updated)
        result["batch"] = self._batch_view(item_id)
        return result

    def close_instruction(self, item_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        """总工关闭：全部孔一致回执、批次已完成、没有未关闭事项，责任链才能关闭。"""
        ensure_role(role, CLOSE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "executed":
            raise ConflictError(f"指令当前为{item['status']}，批次未完成不能关闭")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        gates = self.repository.list_gates(item_id)
        blockers = completion_blockers(
            "closed", self.repository.open_record_count(item_id), gates)
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, "closed",
                                                   expected_version, actor)
        self.repository.mark_batch_closed(item_id)
        self.repository.append_audit("gate_batch_closed", ENTITY, item_id, actor, {
            "gates": [g["gate_code"] for g in gates]})
        result = self.enrich(updated)
        result["batch"] = self._batch_view(item_id)
        return result

    def get_batch(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        return {"item": self.enrich(item), **self._batch_view(item_id)}

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
