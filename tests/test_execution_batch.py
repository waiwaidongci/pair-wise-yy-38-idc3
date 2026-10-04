import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError
from src.repository import Repository
from src.service import Service
from src.rules import TRANSITION_ROLES


class ExecutionBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _setup(self, holes, quantity=12):
        item = self.service.create_item(
            {"title": "batch item", "description": "x", "severity": "urgent",
             "quantity": quantity, "threshold": 6}, "creator", "duty_officer")
        current = self.service.transition(item["id"], "checked", item["version"],
                                          "reviewer", TRANSITION_ROLES["checked"][0])
        current = self.service.transition(current["id"], "authorized", current["version"],
                                          "reviewer", TRANSITION_ROLES["authorized"][0], holes=holes)
        return current

    def _batch(self, item_id):
        return self.service.list_execution_batches(item_id, "viewer")[0]

    def test_idempotent_retry_no_duplicate(self):
        current = self._setup([{"hole_no": 1, "target_opening": 1.0}])
        batch = self._batch(current["id"])
        r1 = self.service.submit_gate_receipt(
            current["id"], batch["id"],
            {"hole_no": 1, "actual_opening": 1.0, "external_ref": "R-1"},
            "op", "dispatcher")
        # 回传失败后重试：同批次同 external_ref -> 返回已有回执，不重复追加
        r2 = self.service.submit_gate_receipt(
            current["id"], batch["id"],
            {"hole_no": 1, "actual_opening": 1.0, "external_ref": "R-1"},
            "op", "dispatcher")
        self.assertEqual(r1["id"], r2["id"])
        receipts = self.service.list_gate_receipts(current["id"], "viewer")
        self.assertEqual(len(receipts), 1)

    def test_first_arriving_wins_in_position(self):
        current = self._setup([{"hole_no": 1, "target_opening": 1.0}])
        batch = self._batch(current["id"])
        # 两个值班员同时提交同一孔：先到结果生效
        self.service.submit_gate_receipt(
            current["id"], batch["id"],
            {"hole_no": 1, "actual_opening": 1.0, "external_ref": "A"},
            "officer1", "duty_officer")
        with self.assertRaises(ConflictError):
            self.service.submit_gate_receipt(
                current["id"], batch["id"],
                {"hole_no": 1, "actual_opening": 1.0, "external_ref": "B"},
                "officer2", "duty_officer")
        receipts = self.service.list_gate_receipts(current["id"], "viewer")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["external_ref"], "A")

    def test_first_arriving_wins_refusal(self):
        current = self._setup([{"hole_no": 1, "target_opening": 1.0}])
        batch = self._batch(current["id"])
        # 1号值班员先提交（拒动，实际0开度）
        self.service.submit_gate_receipt(
            current["id"], batch["id"],
            {"hole_no": 1, "actual_opening": 0.0, "external_ref": "A"},
            "officer1", "duty_officer")
        # 2号值班员后提交（到位）：同批次同孔已有回执，先到结果生效
        with self.assertRaises(ConflictError):
            self.service.submit_gate_receipt(
                current["id"], batch["id"],
                {"hole_no": 1, "actual_opening": 1.0, "external_ref": "B"},
                "officer2", "duty_officer")
        receipts = self.service.list_gate_receipts(current["id"], "viewer")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["external_ref"], "A")
        self.assertEqual(receipts[0]["status"], "open")

    def test_refused_hole_stays_executing(self):
        current = self._setup([{"hole_no": 1, "target_opening": 1.0},
                               {"hole_no": 2, "target_opening": 1.0}])
        batch = self._batch(current["id"])
        # 1号孔到位，2号孔拒动（实际0开度）
        self.service.submit_gate_receipt(
            current["id"], batch["id"],
            {"hole_no": 1, "actual_opening": 1.0, "external_ref": "R-1"},
            "op", "dispatcher")
        self.service.submit_gate_receipt(
            current["id"], batch["id"],
            {"hole_no": 2, "actual_opening": 0.0, "external_ref": "R-2"},
            "op", "dispatcher")
        current = self.service.get_item(current["id"], "viewer")
        self.assertEqual(current["status"], "executing")
        # 拒动产生未关闭事项
        self.assertGreater(self.repo.open_gate_receipt_count(current["id"]), 0)
        # 不能执行到位
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], "executed", current["version"],
                                    "reviewer", TRANSITION_ROLES["executed"][0])

    def test_recovery_batch_only_remaining_holes(self):
        current = self._setup([{"hole_no": 1, "target_opening": 1.0},
                               {"hole_no": 2, "target_opening": 1.0}])
        batch = self._batch(current["id"])
        self.service.submit_gate_receipt(
            current["id"], batch["id"],
            {"hole_no": 1, "actual_opening": 1.0, "external_ref": "R-1"},
            "op", "dispatcher")
        self.service.submit_gate_receipt(
            current["id"], batch["id"],
            {"hole_no": 2, "actual_opening": 0.0, "external_ref": "R-2"},
            "op", "dispatcher")
        # 恢复后续办：新批次只含剩余孔（2号），1号孔不重做
        recovery = self.service.create_recovery_batch(current["id"], "op", "dispatcher")
        self.assertEqual(recovery["seq"], 2)
        # 1号孔已到位，不能重复登记
        with self.assertRaises(ConflictError):
            self.service.submit_gate_receipt(
                current["id"], recovery["id"],
                {"hole_no": 1, "actual_opening": 1.0, "external_ref": "R-3"},
                "op", "dispatcher")
        # 2号孔续办到位
        r = self.service.submit_gate_receipt(
            current["id"], recovery["id"],
            {"hole_no": 2, "actual_opening": 1.0, "external_ref": "R-4"},
            "op", "dispatcher")
        self.assertTrue(r["in_position"])
        # 拒动回执随到位关闭
        self.assertEqual(self.repo.open_gate_receipt_count(current["id"]), 0)
        current = self.service.get_item(current["id"], "viewer")
        current = self.service.transition(current["id"], "executed", current["version"],
                                          "reviewer", TRANSITION_ROLES["executed"][0])
        current = self.service.transition(current["id"], "closed", current["version"],
                                          "reviewer", TRANSITION_ROLES["closed"][0])
        self.assertEqual(current["status"], "closed")

    def test_reservoir_level_recompute_pending_only(self):
        # 初始库位9，汛限6 -> 目标开度 (9-6)/6=0.5
        current = self._setup([{"hole_no": 1, "target_opening": 0.5},
                               {"hole_no": 2, "target_opening": 0.5}], quantity=9)
        batch = self._batch(current["id"])
        self.service.submit_gate_receipt(
            current["id"], batch["id"],
            {"hole_no": 1, "actual_opening": 0.5, "external_ref": "R-1"},
            "op", "dispatcher")
        # 库位变化：未到位孔(2号)按新依据重算，已到位孔(1号)保留记录
        updated = self.service.update_reservoir_level(current["id"], 12.0, "op", "dispatcher")
        self.assertEqual(updated["quantity"], 12.0)
        holes = self.service.list_gate_holes(current["id"], "viewer")
        h1 = next(h for h in holes if h["hole_no"] == 1)
        h2 = next(h for h in holes if h["hole_no"] == 2)
        # 1号孔保留原冻结目标与到位记录
        self.assertEqual(h1["target_opening"], 0.5)
        self.assertEqual(h1["status"], "in_position")
        # 2号孔按新库位(12,汛限6)重算：(12-6)/6=1.0
        self.assertEqual(h2["target_opening"], 1.0)
        self.assertNotEqual(h2["status"], "in_position")

    def test_close_requires_consistent_receipts(self):
        current = self._setup([{"hole_no": 1, "target_opening": 1.0},
                               {"hole_no": 2, "target_opening": 1.0}])
        batch = self._batch(current["id"])
        # 只登记1号孔，2号孔未登记 -> 不能执行到位/关闭
        self.service.submit_gate_receipt(
            current["id"], batch["id"],
            {"hole_no": 1, "actual_opening": 1.0, "external_ref": "R-1"},
            "op", "dispatcher")
        current = self.service.get_item(current["id"], "viewer")
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], "executed", current["version"],
                                    "reviewer", TRANSITION_ROLES["executed"][0])


if __name__ == "__main__":
    unittest.main()
