import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import TRANSITION_ROLES


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_complete_workflow_with_gate_receipts(self):
        item = self.service.create_item(
            {"title": "workflow item", "description": "complete business flow",
             "severity": "urgent", "quantity": 12, "threshold": 6, "external_ref": "WF-1"},
            "creator", "duty_officer")
        self.assertEqual(item["status"], "draft")
        self.service.add_record(item["id"], {"kind": "evidence", "detail": "evidence registered",
                                             "status": "closed", "external_ref": "EV-1"},
                                "recorder", "duty_officer")
        current = item
        # draft -> checked
        current = self.service.transition(current["id"], "checked", current["version"],
                                          "reviewer", TRANSITION_ROLES["checked"][0])
        # checked -> authorized：授权时冻结每孔开度目标
        holes = [
            {"hole_no": 1, "target_opening": 1.0},
            {"hole_no": 2, "target_opening": 1.0},
            {"hole_no": 3, "target_opening": 1.0},
        ]
        current = self.service.transition(current["id"], "authorized", current["version"],
                                          "reviewer", TRANSITION_ROLES["authorized"][0], holes=holes)
        self.assertEqual(current["status"], "authorized")
        frozen = self.service.list_gate_holes(current["id"], "viewer")
        self.assertEqual(len(frozen), 3)
        # 逐孔登记实际开度（执行回执）
        batch = self.service.list_execution_batches(current["id"], "viewer")[0]
        self.assertEqual(batch["seq"], 1)
        for h in holes:
            r = self.service.submit_gate_receipt(
                current["id"], batch["id"],
                {"hole_no": h["hole_no"], "actual_opening": 1.0, "external_ref": f"RCP-{h['hole_no']}"},
                "operator", "dispatcher")
            self.assertTrue(r["in_position"])
        # 首次回执后自动进入执行中
        current = self.service.get_item(current["id"], "viewer")
        self.assertEqual(current["status"], "executing")
        # executing -> executed：全部孔到位
        current = self.service.transition(current["id"], "executed", current["version"],
                                          "reviewer", TRANSITION_ROLES["executed"][0])
        # executed -> closed：全部孔一致回执且无未关闭事项，总工才能关闭
        current = self.service.transition(current["id"], "closed", current["version"],
                                          "reviewer", TRANSITION_ROLES["closed"][0])
        self.assertEqual(current["status"], "closed")
        events = self.service.audit("viewer", current["id"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_authorized_requires_holes(self):
        item = self.service.create_item(
            {"title": "no holes", "description": "x", "severity": "urgent",
             "quantity": 12, "threshold": 6}, "creator", "duty_officer")
        current = self.service.transition(item["id"], "checked", item["version"],
                                          "reviewer", TRANSITION_ROLES["checked"][0])
        with self.assertRaises(Exception):
            self.service.transition(current["id"], "authorized", current["version"],
                                    "reviewer", TRANSITION_ROLES["authorized"][0])


if __name__ == "__main__":
    unittest.main()
