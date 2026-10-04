import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _run_batch(self, item, gates, outcomes=None):
        current = self.service.authorize_batch(
            item["id"],
            {"expected_version": item["version"], "basis": "库位超汛限0.8m",
             "gates": gates},
            "chief", "chief_engineer")
        self.service.dispatch_batch(current["id"], {}, "disp", "dispatcher")
        outcomes = outcomes or {}
        for gate in gates:
            code = gate["gate_code"]
            if outcomes.get(code, "arrived") == "refused":
                self.service.register_receipt(
                    current["id"], {"gate_code": code, "outcome": "refused"},
                    "duty1", "duty_officer")
            else:
                opening = outcomes.get(code, gate["target_opening"])
                self.service.register_receipt(
                    current["id"],
                    {"gate_code": code, "outcome": "arrived",
                     "actual_opening": opening},
                    "duty1", "duty_officer")
        current = self.service.settle_batch(current["id"], "disp", "dispatcher")
        return self.service.close_instruction(
            current["id"], {"expected_version": current["version"]},
            "chief", "chief_engineer")

    def test_complete_workflow_and_audit(self):
        item = self.service.create_item(
            {"title": "workflow item", "description": "complete business flow",
             "severity": "urgent", "quantity": 12, "threshold": 6,
             "external_ref": "WF-1"}, "creator", "duty_officer")
        self.assertEqual(item["status"], STATES[0])
        self.service.add_record(
            item["id"], {"kind": "evidence", "detail": "evidence registered",
                         "status": "closed", "external_ref": "EV-1"},
            "recorder", "duty_officer")
        current = self.service.transition(
            item["id"], STATES[1], item["version"], "reviewer",
            TRANSITION_ROLES[STATES[1]][0])
        current = self.service.transition(
            current["id"], STATES[2], current["version"], "chief",
            "chief_engineer")
        current = self._run_batch(
            current, [{"gate_code": "G1", "target_opening": 30.0},
                      {"gate_code": "G2", "target_opening": 30.0}])
        self.assertEqual(current["status"], STATES[-1])
        self.assertEqual(
            len(self.service.list_records(current["id"], "viewer")), 1)
        events = self.service.audit("viewer", current["id"])
        self.assertGreaterEqual(len(events), len(STATES) + 2)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_generic_execute_transition_is_rejected(self):
        # “点一次执行就按整条指令算完成”必须被拒绝
        item = self.service.create_item(
            {"title": "no batch", "description": "no gates",
             "severity": "routine", "quantity": 1, "threshold": 10,
             "external_ref": "WF-2"}, "creator", "duty_officer")
        current = self.service.transition(
            item["id"], STATES[1], item["version"], "r", "duty_officer")
        current = self.service.transition(
            current["id"], STATES[2], current["version"], "c",
            "chief_engineer")
        with self.assertRaises(ConflictError):
            self.service.transition(
                current["id"], "executing", current["version"], "c",
                "chief_engineer")


if __name__ == "__main__":
    unittest.main()
