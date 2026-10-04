from __future__ import annotations
from .domain import ConflictError, ValidationError

TITLE = '水库防汛调度与操作确认'
ENTITY = '调度指令'
ID_PREFIX = 'RF'

SEVERITIES = ['routine', 'attention', 'urgent', 'emergency']
STATES = ['draft', 'checked', 'authorized', 'executing', 'executed', 'closed']
TRANSITIONS = {
    'draft': ['checked'],
    'checked': ['authorized'],
    'authorized': ['executing'],
    'executing': ['executed'],
    'executed': ['closed'],
    'closed': [],
}
TRANSITION_ROLES = {
    'checked': ['duty_officer'],
    'authorized': ['chief_engineer'],
    'executing': ['dispatcher'],
    'executed': ['dispatcher'],
    'closed': ['chief_engineer'],
}
CREATE_ROLES = set(['duty_officer'])
RECORD_ROLES = set(['duty_officer', 'dispatcher'])
AUDIT_ROLES = set(['chief_engineer', 'viewer'])
VIEW_ROLES = set(['duty_officer', 'chief_engineer', 'dispatcher', 'viewer'])
# 值班员/调度员均可逐孔登记执行回执
GATE_RECEIPT_ROLES = set(['duty_officer', 'dispatcher'])
# 批次续办、库位调整由值班员/调度员处理
BATCH_MANAGE_ROLES = set(['duty_officer', 'dispatcher'])
RESERVOIR_ROLES = set(['duty_officer', 'dispatcher'])

SEVERITY_WEIGHT = {'routine': 1.0, 'attention': 3.0, 'urgent': 6.0, 'emergency': 9.0}
DEADLINE_HOURS = {'routine': 72, 'attention': 24, 'urgent': 8, 'emergency': 4}
TERMINAL_STATES = set(['closed'])

# 闸门到位判定容差（开度为[0,1]的比例值）
OPENING_TOLERANCE = 0.02


def priority_score(severity, quantity=0.0, threshold=1.0, open_records=0):
    if severity not in SEVERITY_WEIGHT:
        raise ValidationError("unknown severity")
    ratio = quantity / threshold if threshold > 0 else 1.0
    return max(0, min(10, int(round(SEVERITY_WEIGHT[severity] + min(4.0, ratio * 4.0) + min(3.0, float(open_records))))))


def response_deadline_hours(severity, quantity=0.0, threshold=1.0):
    if severity not in DEADLINE_HOURS:
        raise ValidationError("unknown severity")
    ratio = quantity / threshold if threshold > 0 else 1.0
    return max(1, int(DEADLINE_HOURS[severity] / max(1.0, ratio)))


def escalation_required(severity, quantity=0.0, threshold=1.0):
    return severity == SEVERITIES[-1] or (threshold > 0 and quantity >= threshold)


def can_transition(current, target):
    return target in TRANSITIONS.get(current, [])


def validate_transition(current, target):
    if current not in STATES or target not in STATES:
        raise ValidationError("未知状态")
    if not can_transition(current, target):
        raise ConflictError(f"不能从{current}转换到{target}")


def role_for_transition(target):
    return set(TRANSITION_ROLES.get(target, []))


def gate_opening_target(quantity, threshold):
    """依据库位(quantity)相对汛限(threshold)的富余比例计算冻结的闸门开度目标。

    目标开度为[0,1]：库位未超汛限(quantity<=threshold)时为0，超出越多开度越大，
    直至全开(1)。授权时按当时库位冻结，库位变化后仅对未到位孔按新依据重算。
    """
    if threshold <= 0:
        threshold = 1.0
    ratio = (quantity - threshold) / threshold
    return max(0.0, min(1.0, float(ratio)))


def in_position(actual, target, tolerance=OPENING_TOLERANCE):
    """实际开度与冻结目标开度一致（容差内）即到位。"""
    return abs(float(actual) - float(target)) <= tolerance


def all_holes_in_position(holes):
    """全部孔都有到位回执。

    holes为None表示无孔位要求（兼容旧流程），返回True；
    holes为空列表表示有指令但未登记孔位，返回False。
    """
    if holes is None:
        return True
    if not holes:
        return False
    return all(
        h.get('status') == 'in_position'
        and in_position(h.get('actual_opening'), h.get('target_opening'))
        for h in holes
    )


def completion_blockers(target, open_items, holes=None):
    """关闭/执行到位前的不变量检查。

    - 执行到位(executed)与关闭(closed)：全部孔有一致回执（到位）。
    - 仅关闭(closed)：没有未关闭事项（未关闭记录 + 未关闭拒动回执）。
    """
    blockers = []
    if target in ('executed', 'closed') and not all_holes_in_position(holes):
        blockers.append("仍有闸门未到位")
    if target == 'closed' and open_items > 0:
        blockers.append("仍有未关闭事项")
    return blockers
