import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.rules import gate_blockers
from src.service import Service

GATES = [{"gate_code": "G1", "target_opening": 30.0},
         {"gate_code": "G2", "target_opening": 40.0},
         {"gate_code": "G3", "target_opening": 50.0}]


class GateBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "gates.db"))
        self.svc = Service(self.repo)
        item = self.svc.create_item(
            {"title": "泄洪指令", "description": "多孔同开",
             "severity": "emergency", "quantity": 90, "threshold": 50,
             "external_ref": "ORDER-1"}, "duty", "duty_officer")
        item = self.svc.transition(item["id"], "checked", item["version"],
                                   "duty", "duty_officer")
        self.item = self.svc.transition(item["id"], "authorized",
                                        item["version"], "chief",
                                        "chief_engineer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def authorize(self, gates=None, version=None):
        return self.svc.authorize_batch(
            self.item["id"],
            {"expected_version": version or self.item["version"],
             "basis": "库位超汛限0.8m/入库3200", "gates": gates or GATES},
            "chief", "chief_engineer")

    def dispatch(self, gates=None):
        return self.svc.dispatch_batch(
            self.item["id"], {"gate_codes": gates}, "disp", "dispatcher")

    def arrive(self, code, opening, actor="duty1", token=None):
        payload = {"gate_code": code, "outcome": "arrived",
                   "actual_opening": opening}
        if token:
            payload["client_token"] = token
        return self.svc.register_receipt(self.item["id"], payload, actor,
                                         "duty_officer")

    def refuse(self, code, actor="duty1", token=None):
        payload = {"gate_code": code, "outcome": "refused"}
        if token:
            payload["client_token"] = token
        return self.svc.register_receipt(self.item["id"], payload, actor,
                                         "duty_officer")

    # 1. 授权冻结每孔开度目标；只有总工能授权
    def test_authorization_freezes_targets_and_roles(self):
        res = self.authorize()
        self.assertEqual(res["status"], "executing")
        gates = {g["gate_code"]: g for g in res["batch"]["gates"]}
        self.assertEqual(gates["G1"]["target_opening"], 30.0)
        self.assertEqual(gates["G2"]["target_opening"], 40.0)
        self.assertEqual(gates["G3"]["target_opening"], 50.0)
        for gate in gates.values():
            self.assertEqual(gate["status"], "pending")
        # 同一指令只能开一个批次
        with self.assertRaises(ConflictError):
            self.svc.authorize_batch(
                self.item["id"],
                {"expected_version": res["version"], "basis": "b",
                 "gates": GATES}, "chief", "chief_engineer")
        # 值班员不能授权
        ver = self.item["version"]
        with self.assertRaises(PermissionDenied):
            self.svc.authorize_batch(
                self.item["id"],
                {"expected_version": ver, "basis": "b", "gates": GATES},
                "duty1", "duty_officer")

    # 2. 两个值班员同时提交同一孔：先到结果生效，后到仅冲突
    def test_concurrent_same_gate_first_wins(self):
        self.authorize()
        self.dispatch()
        barrier = threading.Barrier(2)
        errors = []
        winners = []

        def submit(opening, actor, token):
            try:
                barrier.wait()
                self.arrive("G1", opening, actor, token)
                winners.append(actor)
            except Exception as exc:  # noqa: BLE001
                errors.append((actor, exc))

        t1 = threading.Thread(target=submit, args=(30.0, "duty1", "tok-A"))
        t2 = threading.Thread(target=submit, args=(33.0, "duty2", "tok-B"))
        t1.start(); t2.start(); t1.join(); t2.join()
        # 恰好一人成功一人冲突
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(winners), 1)
        self.assertIsInstance(errors[0][1], ConflictError)
        gate = self.repo.get_gate(self.item["id"], "G1")
        winner_value = {"duty1": 30.0, "duty2": 33.0}[winners[0]]
        self.assertEqual(gate["actual_opening"], winner_value)
        self.assertIn(gate["status"], ("arrived", "mismatch"))
        # 该轮只有一条到位回执落库，重试/并发都不重复追加
        arrivals = [r for r in self.repo.list_receipts(self.item["id"])
                    if r["gate_id"] == gate["id"] and r["outcome"] == "arrived"]
        self.assertEqual(len(arrivals), 1)
        self.assertEqual(arrivals[0]["actual_opening"], winner_value)

    # 3. 回传失败按同一批次续办；重试不重复追加（client_token幂等）
    def test_feedback_lost_retry_is_idempotent(self):
        self.authorize()
        self.dispatch()
        # G1现场已动作，但回传失败
        self.svc.mark_feedback_lost(
            self.item["id"], {"gate_code": "G1"}, "disp", "dispatcher")
        rounds_before = len(self.repo.list_rounds(self.item["id"]))
        # 用同一批次续办：feedback_lost孔直接补登记，不再新增派工轮次
        self.svc.continue_batch(
            self.item["id"], {"gate_codes": ["G1"]}, "disp", "dispatcher")
        self.assertEqual(len(self.repo.list_rounds(self.item["id"])),
                         rounds_before)
        # 网络重试同一回执两次，只追加一次
        self.arrive("G1", 30.0, "duty1", token="obs-G1-001")
        with self.assertRaises(ConflictError):
            self.arrive("G1", 30.0, "duty1", token="obs-G1-001")
        g1_receipts = [r for r in self.repo.list_receipts(self.item["id"])
                       if r["client_token"] == "obs-G1-001"]
        self.assertEqual(len(g1_receipts), 1)

    # 4. 某孔拒动：指令留在执行中；已到位孔不重做；恢复后只续办剩余孔
    def test_refused_keeps_open_and_resume_only_remaining(self):
        res = self.authorize()
        self.dispatch()
        self.arrive("G1", 30.0)
        self.refuse("G2")
        self.arrive("G3", 50.0)
        # 批次不能完成，指令仍在执行中
        with self.assertRaises(ConflictError):
            self.svc.settle_batch(self.item["id"], "disp", "dispatcher")
        self.assertEqual(self.repo.get_item(self.item["id"])["status"],
                         "executing")
        # 恢复后续办：只有G2进新一轮；G1/G3不重做
        cont = self.svc.continue_batch(
            self.item["id"], {"gate_codes": ["G1", "G2", "G3"]}, "disp",
            "dispatcher")
        self.assertEqual(cont["dispatched_round"]["gate_codes"], ["G2"])
        self.assertEqual(cont["dispatched_round"]["attempt_no"], 2)
        self.arrive("G2", 40.0, actor="duty2")
        settled = self.svc.settle_batch(self.item["id"], "disp", "dispatcher")
        self.assertEqual(settled["status"], "executed")
        # 到位孔只保留最初回执，未被重复追加
        receipts = self.repo.list_receipts(self.item["id"])
        g1 = self.repo.get_gate(self.item["id"], "G1")
        g1_arrivals = [r for r in receipts if r["gate_id"] == g1["id"]
                       and r["outcome"] == "arrived"]
        self.assertEqual(len(g1_arrivals), 1)
        self.assertEqual(g1["attempt_no"], 1)

    # 5. 库位变化：未到位孔按新依据重算，已到位孔保留记录
    def test_recompute_preserves_arrived_and_retargets_rest(self):
        self.authorize()
        self.dispatch()
        self.arrive("G1", 30.0)
        self.refuse("G2")
        self.arrive("G3", 50.0)
        # 库位变化后重算：G2新目标60，G1/G3保持
        res = self.svc.recompute_basis(
            self.item["id"],
            {"basis": "库位回落0.3m/新依据",
             "gates": [{"gate_code": "G2", "target_opening": 60.0}]},
            "chief", "chief_engineer")
        gates = {g["gate_code"]: g for g in res["batch"]["gates"]}
        self.assertEqual(gates["G1"]["target_opening"], 30.0)
        self.assertEqual(gates["G1"]["status"], "arrived")
        self.assertEqual(gates["G2"]["target_opening"], 60.0)
        self.assertEqual(gates["G2"]["status"], "pending")
        self.assertEqual(gates["G2"]["basis_version"], 2)
        self.assertEqual(gates["G3"]["status"], "arrived")
        # 重算不能覆盖已到位孔
        with self.assertRaises(ConflictError):
            self.svc.recompute_basis(
                self.item["id"],
                {"basis": "bad",
                 "gates": [{"gate_code": "G1", "target_opening": 99.0},
                           {"gate_code": "G2", "target_opening": 60.0}]},
                "chief", "chief_engineer")
        # 按新目标续派G2并到位
        self.svc.continue_batch(
            self.item["id"], {"gate_codes": ["G2"]}, "disp", "dispatcher")
        self.arrive("G2", 60.0)
        settled = self.svc.settle_batch(self.item["id"], "disp", "dispatcher")
        self.assertEqual(settled["status"], "executed")

    # 6. 全部孔有一致回执且无未关闭事项，总工才能关闭
    def test_close_requires_consistent_receipts_and_chief(self):
        res = self.authorize()
        self.dispatch()
        self.arrive("G1", 30.0)
        self.arrive("G2", 40.0)
        self.arrive("G3", 50.0)
        settled = self.svc.settle_batch(self.item["id"], "disp", "dispatcher")
        # 调度员不能关闭
        with self.assertRaises(PermissionDenied):
            self.svc.close_instruction(
                self.item["id"], {"expected_version": settled["version"]},
                "disp", "dispatcher")
        closed = self.svc.close_instruction(
            self.item["id"], {"expected_version": settled["version"]},
            "chief", "chief_engineer")
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(self.repo.verify_audit_chain())

    # 7. 开度不一致：出现未关闭事项，不能完成；纠偏后可完成
    def test_inconsistent_opening_blocks_completion(self):
        self.authorize()
        self.dispatch()
        self.arrive("G1", 30.0)
        self.arrive("G2", 40.0)
        r = self.arrive("G3", 80.0)  # 目标50，默认容差0.5，明显不符
        self.assertEqual(r["receipt"]["new_status"], "mismatch")
        blockers = gate_blockers(r["batch"]["gates"])
        self.assertTrue(any("G3" in b for b in blockers))
        with self.assertRaises(ConflictError):
            self.svc.settle_batch(self.item["id"], "disp", "dispatcher")
        # 纠偏：续办G3，重新到位
        self.svc.continue_batch(
            self.item["id"], {"gate_codes": ["G3"]}, "disp", "dispatcher")
        self.arrive("G3", 50.0)
        settled = self.svc.settle_batch(self.item["id"], "disp", "dispatcher")
        self.assertEqual(settled["status"], "executed")

    # 8. 未关闭records也阻止关闭
    def test_open_record_blocks_close(self):
        res = self.authorize()
        self.dispatch()
        for gate in GATES:
            self.arrive(gate["gate_code"], gate["target_opening"])
        self.svc.add_record(
            self.item["id"], {"kind": "issue", "detail": "下游漂浮物待清理",
                              "status": "open"}, "duty", "duty_officer")
        with self.assertRaises(ConflictError):
            self.svc.settle_batch(self.item["id"], "disp", "dispatcher")

    # 9. 库位变化后按新依据自动分摊重算（不逐孔指定时）
    def test_recompute_with_basis_opening_allocation(self):
        from src.policy import plan_openings
        plan = plan_openings(100.0, ["G2", "G4", "G5"], overrides={"G2": 60.0})
        self.assertEqual(plan, [{"gate_code": "G2", "target_opening": 60.0},
                                {"gate_code": "G4", "target_opening": 20.0},
                                {"gate_code": "G5", "target_opening": 20.0}])
        self.authorize()
        self.dispatch()
        self.arrive("G1", 30.0)
        self.refuse("G2")
        self.arrive("G3", 50.0)
        res = self.svc.recompute_basis(
            self.item["id"], {"basis": "新依据", "basis_opening": 80.0},
            "chief", "chief_engineer")
        g2 = {g["gate_code"]: g for g in res["batch"]["gates"]}["G2"]
        self.assertEqual(g2["target_opening"], 80.0)
        self.assertEqual(g2["status"], "pending")


if __name__ == "__main__":
    unittest.main()
