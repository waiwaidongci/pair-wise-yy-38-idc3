from __future__ import annotations
from .domain import (DEFAULT_TOLERANCE, GATE_OPEN_STATES, ConflictError,
                     ValidationError)
TITLE='水库防汛调度与操作确认'; ENTITY='调度指令'; ID_PREFIX='RF'
SEVERITIES=['routine', 'attention', 'urgent', 'emergency']
STATES=['draft', 'checked', 'authorized', 'executing', 'executed', 'closed']
# authorized -> executing：总工授权（冻结每孔开度目标并开批）
# executing -> executing：派工/续办（调度员，不改变指令状态，责任链保持打开）
# executing -> executed：全部孔到位且回执一致（系统在续办登记时判定）
TRANSITIONS={'draft': ['checked'], 'checked': ['authorized'],
             'authorized': ['executing'], 'executing': ['executed'],
             'executed': ['closed'], 'closed': []}
TRANSITION_ROLES={'checked': ['duty_officer'],
                  'authorized': ['chief_engineer'],
                  'executing': ['chief_engineer'],
                  'executed': ['dispatcher'],
                  'closed': ['chief_engineer']}
# 闸门执行批次专用操作的角色矩阵
AUTHORIZE_BATCH_ROLES=set(['chief_engineer'])
DISPATCH_ROLES=set(['dispatcher'])
RECEIPT_ROLES=set(['duty_officer','dispatcher'])
RECOMPUTE_ROLES=set(['chief_engineer'])
CLOSE_ROLES=set(['chief_engineer'])
CREATE_ROLES=set(['duty_officer']); RECORD_ROLES=set(['duty_officer', 'dispatcher']); AUDIT_ROLES=set(['chief_engineer', 'viewer']); VIEW_ROLES=set(['duty_officer', 'chief_engineer', 'dispatcher', 'viewer'])
SEVERITY_WEIGHT={'routine': 1.0, 'attention': 3.0, 'urgent': 6.0, 'emergency': 9.0}; DEADLINE_HOURS={'routine': 72, 'attention': 24, 'urgent': 8, 'emergency': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def openings_consistent(actual,target,tolerance=DEFAULT_TOLERANCE):
    """到位回执一致性判定：实际开度与该孔到位时冻结的目标快照相差不超过容差（开度百分比点）。"""
    return abs(float(actual)-float(target))<=float(tolerance)
def gate_blockers(gates):
    """返回阻止批次完成/指令关闭的未关闭孔位事项。"""
    blockers=[]
    for gate in gates:
        state=gate['status']
        if state=='arrived':
            if not openings_consistent(gate.get('actual_opening',0.0),
                                       gate.get('arrival_target',gate.get('target_opening',0.0)),
                                       gate.get('tolerance',DEFAULT_TOLERANCE)):
                blockers.append(f"孔{gate['gate_code']}回执与目标开度不一致")
        elif state in GATE_OPEN_STATES:
            reason={'pending':'未派工','dispatched':'缺少回执','refused':'设备拒动',
                    'feedback_lost':'回传丢失','mismatch':'开度不符未纠偏'}[state]
            blockers.append(f"孔{gate['gate_code']}{reason}")
    return blockers
def completion_blockers(target,open_records,gates=None):
    blockers=[]
    if target in TERMINAL_STATES and open_records>0:
        blockers.append("仍有未关闭事项")
    if target in TERMINAL_STATES:
        blockers.extend(gate_blockers(gates or []))
    return blockers
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))