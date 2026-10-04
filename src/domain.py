from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['routine', 'attention', 'urgent', 'emergency']; STATES=['draft', 'checked', 'authorized', 'executing', 'executed', 'closed']; ROLES=['duty_officer', 'chief_engineer', 'dispatcher', 'viewer']
# 闸门执行批次的逐孔状态：
# pending待派工 dispatched已派工待回执 arrived到位 refused拒动 feedback_lost回传丢失 mismatch开度不符
GATE_STATES=['pending','dispatched','arrived','refused','feedback_lost','mismatch']
GATE_OPEN_STATES=set(GATE_STATES)-{'arrived'}
RECEIPT_OUTCOMES=['arrived','refused']
DEFAULT_TOLERANCE=0.5
MAX_OPENING=100.0
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def require_gate_code(value,field="gate_code"):
    value=require_text(value,field,50)
    if any(ch.isspace() for ch in value): raise ValidationError(f"{field}不能包含空白字符")
    return value
def require_opening(value,field="opening"):
    number=require_number(value,field,0.0)
    if number>MAX_OPENING: raise ValidationError(f"{field}不能大于{MAX_OPENING}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
