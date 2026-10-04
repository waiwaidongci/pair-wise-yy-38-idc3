import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import TRANSITION_ROLES


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _authorize(self, item_id, version, holes=None):
        if holes is None:
            holes = [{"hole_no": 1, "target_opening": 1.0}]
        return self.service.transition(item_id, "authorized", version,
                                       "reviewer", TRANSITION_ROLES["authorized"][0], holes=holes)

    def test_permission_version_duplicate(self):
        item = self.service.create_item(
            {"title": "failure item", "description": "failure scenarios",
             "severity": "urgent", "quantity": 5, "threshold": 10, "external_ref": "FAIL-1"},
            "creator", "duty_officer")
        with self.assertRaises(PermissionDenied):
            self.service.transition(item["id"], "checked", 1, "attacker", "viewer")
        with self.assertRaises(ConflictError):
            self.service.transition(item["id"], "checked", 99,
                                    "reviewer", TRANSITION_ROLES["checked"][0])
        # 先到 checked
        current = self.service.transition(item["id"], "checked", item["version"],
                                          "reviewer", TRANSITION_ROLES["checked"][0])
        # 授权必须提供孔位与开度目标
        with self.assertRaises(ValidationError):
            self.service.transition(current["id"], "authorized", current["version"],
                                    "reviewer", TRANSITION_ROLES["authorized"][0])
        payload = {"kind": "action", "detail": "same reference",
                   "status": "open", "external_ref": "DUP-1"}
        self.service.add_record(item["id"], payload, "recorder", "duty_officer")
        with self.assertRaises(ConflictError):
            self.service.add_record(item["id"], payload, "recorder", "duty_officer")

    def test_close_blocked_by_open_record(self):
        item = self.service.create_item(
            {"title": "close blocker", "description": "x", "severity": "urgent",
             "quantity": 12, "threshold": 6}, "creator", "duty_officer")
        self.service.add_record(item["id"], {"kind": "issue", "detail": "open matter",
                                             "status": "open", "external_ref": "ISS-1"},
                                "recorder", "duty_officer")
        current = item
        current = self.service.transition(current["id"], "checked", current["version"],
                                          "reviewer", TRANSITION_ROLES["checked"][0])
        current = self._authorize(current["id"], current["version"])
        # 全部到位
        batch = self.service.list_execution_batches(current["id"], "viewer")[0]
        self.service.submit_gate_receipt(current["id"], batch["id"],
                                         {"hole_no": 1, "actual_opening": 1.0, "external_ref": "R-1"},
                                         "op", "dispatcher")
        current = self.service.get_item(current["id"], "viewer")
        current = self.service.transition(current["id"], "executed", current["version"],
                                          "reviewer", TRANSITION_ROLES["executed"][0])
        # 仍有未关闭事项 -> 总工不能关闭
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", TRANSITION_ROLES["closed"][0])

    def test_executed_blocked_when_hole_not_in_position(self):
        item = self.service.create_item(
            {"title": "not in position", "description": "x", "severity": "urgent",
             "quantity": 12, "threshold": 6}, "creator", "duty_officer")
        current = self.service.transition(item["id"], "checked", item["version"],
                                          "reviewer", TRANSITION_ROLES["checked"][0])
        current = self._authorize(current["id"], current["version"],
                                  holes=[{"hole_no": 1, "target_opening": 1.0},
                                         {"hole_no": 2, "target_opening": 1.0}])
        batch = self.service.list_execution_batches(current["id"], "viewer")[0]
        # 只登记1号孔，2号孔未登记
        self.service.submit_gate_receipt(current["id"], batch["id"],
                                         {"hole_no": 1, "actual_opening": 1.0, "external_ref": "R-1"},
                                         "op", "dispatcher")
        current = self.service.get_item(current["id"], "viewer")
        # 仍有闸门未到位 -> 不能执行到位
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], "executed", current["version"],
                                    "reviewer", TRANSITION_ROLES["executed"][0])


if __name__ == "__main__":
    unittest.main()
