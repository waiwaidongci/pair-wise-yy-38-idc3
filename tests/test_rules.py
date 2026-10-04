import unittest
from src import rules
from src.domain import ConflictError, ValidationError


class RulesTest(unittest.TestCase):
    def test_priority_deadline_and_escalation(self):
        low = rules.priority_score(rules.SEVERITIES[0], 1, 10, 0)
        high = rules.priority_score(rules.SEVERITIES[-1], 30, 10, 3)
        self.assertGreater(high, low)
        self.assertLessEqual(
            rules.response_deadline_hours(rules.SEVERITIES[-1], 30, 10),
            rules.response_deadline_hours(rules.SEVERITIES[0], 1, 10))
        self.assertTrue(rules.escalation_required(rules.SEVERITIES[-1], 1, 10))
        self.assertTrue(rules.escalation_required(rules.SEVERITIES[0], 10, 10))

    def test_transition_guards(self):
        self.assertTrue(rules.can_transition(rules.STATES[0], rules.STATES[1]))
        with self.assertRaises(ConflictError):
            rules.validate_transition(rules.STATES[0], rules.STATES[-1])
        with self.assertRaises(ValidationError):
            rules.priority_score("not-a-severity", 1, 1)

    def test_state_machine_includes_executing(self):
        self.assertIn("executing", rules.STATES)
        self.assertTrue(rules.can_transition("authorized", "executing"))
        self.assertTrue(rules.can_transition("executing", "executed"))
        self.assertFalse(rules.can_transition("executed", "authorized"))
        self.assertFalse(rules.can_transition("closed", "executed"))

    def test_gate_opening_target_frozen_basis(self):
        # 库位未超汛限 -> 0
        self.assertEqual(rules.gate_opening_target(5, 10), 0.0)
        # 库位等于汛限 -> 0
        self.assertEqual(rules.gate_opening_target(10, 10), 0.0)
        # 库位超出汛限一倍 -> 全开
        self.assertEqual(rules.gate_opening_target(20, 10), 1.0)
        # 中间线性
        self.assertAlmostEqual(rules.gate_opening_target(15, 10), 0.5)

    def test_in_position_tolerance(self):
        self.assertTrue(rules.in_position(1.0, 1.0))
        self.assertTrue(rules.in_position(0.8, 0.81))
        self.assertFalse(rules.in_position(0.0, 0.8))

    def test_all_holes_in_position(self):
        # None 兼容旧流程
        self.assertTrue(rules.all_holes_in_position(None))
        # 空列表 -> False
        self.assertFalse(rules.all_holes_in_position([]))
        holes = [
            {"status": "in_position", "actual_opening": 1.0, "target_opening": 1.0},
            {"status": "in_position", "actual_opening": 0.8, "target_opening": 0.8},
        ]
        self.assertTrue(rules.all_holes_in_position(holes))
        holes[1]["status"] = "refused"
        self.assertFalse(rules.all_holes_in_position(holes))

    def test_completion_blockers(self):
        refused = [{"status": "refused", "actual_opening": 0.0, "target_opening": 1.0}]
        self.assertTrue(any("未到位" in b for b in rules.completion_blockers("executed", 0, refused)))
        inpos = [{"status": "in_position", "actual_opening": 1.0, "target_opening": 1.0}]
        self.assertTrue(any("未关闭" in b for b in rules.completion_blockers("closed", 1, inpos)))
        self.assertEqual(rules.completion_blockers("closed", 0, inpos), [])


if __name__ == "__main__":
    unittest.main()
