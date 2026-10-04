import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "failure item", "description": "failure scenarios",
             "severity": "urgent", "quantity": 5, "threshold": 10,
             "external_ref": "FAIL-1"}, "creator", "duty_officer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _to_authorized(self, versioned_item):
        current = self.service.transition(
            versioned_item["id"], STATES[1], versioned_item["version"],
            "r", "duty_officer")
        return self.service.transition(
            current["id"], STATES[2], current["version"], "c",
            "chief_engineer")

    def test_permission_version_duplicate_and_invariant(self):
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.item["id"], STATES[1], 1,
                                    "attacker", "viewer")
        with self.assertRaises(ConflictError):
            self.service.transition(self.item["id"], STATES[1], 99,
                                    "reviewer", "duty_officer")
        payload = {"kind": "action", "detail": "same reference",
                   "status": "open", "external_ref": "DUP-1"}
        self.service.add_record(self.item["id"], payload, "recorder",
                                "duty_officer")
        with self.assertRaises(ConflictError):
            self.service.add_record(self.item["id"], payload, "recorder",
                                    "duty_officer")

    def test_batch_cannot_close_while_gate_refused_and_record_open(self):
        current = self._to_authorized(self.item)
        current = self.service.authorize_batch(
            current["id"],
            {"expected_version": current["version"], "basis": "b1",
             "gates": [{"gate_code": "G1", "target_opening": 20.0}]},
            "chief", "chief_engineer")
        self.service.dispatch_batch(current["id"], {}, "d", "dispatcher")
        self.service.register_receipt(
            current["id"], {"gate_code": "G1", "outcome": "refused"},
            "duty1", "duty_officer")
        # 拒动孔未恢复：批次无法完成
        with self.assertRaises(ConflictError):
            self.service.settle_batch(current["id"], "d", "dispatcher")


if __name__ == "__main__":
    unittest.main()
