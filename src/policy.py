from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .domain import MAX_OPENING, require_gate_code, require_opening


def normalize_gates(gates) -> List[Dict[str, float]]:
    """校验授权负载中的闸门清单，返回冻结用的 [{gate_code,target_opening}] 快照。"""
    if not isinstance(gates, list) or not gates:
        from .domain import ValidationError
        raise ValidationError("gates必须是非空列表，逐孔指定闸门")
    result: List[Dict[str, float]] = []
    seen = set()
    for entry in gates:
        if not isinstance(entry, dict):
            from .domain import ValidationError
            raise ValidationError("每个闸门必须是对象")
        code = require_gate_code(entry.get("gate_code"))
        if code in seen:
            from .domain import ValidationError
            raise ValidationError(f"闸门{code}重复出现")
        seen.add(code)
        if "target_opening" not in entry:
            from .domain import ValidationError
            raise ValidationError(f"闸门{code}缺少target_opening，授权时必须冻结目标开度")
        opening = require_opening(entry["target_opening"], "target_opening")
        result.append({"gate_code": code, "target_opening": opening})
    return result


def plan_openings(basis_opening: float, gate_codes: Iterable[str],
                  overrides: Optional[Dict[str, float]] = None) -> List[Dict[str, float]]:
    """依据总开度目标重算各孔目标：未显式指定的孔平均分摊，单孔不超过上限。

    overrides用于库位变化后的新依据重算；返回与normalize_gates同构的冻结快照。
    """
    basis_opening = require_opening(basis_opening, "basis_opening")
    codes = [require_gate_code(code) for code in gate_codes]
    if not codes:
        from .domain import ValidationError
        raise ValidationError("gate_codes不能为空")
    overrides = overrides or {}
    clean_overrides = {require_gate_code(k): require_opening(v, "target_opening")
                       for k, v in overrides.items()}
    unknown = set(clean_overrides) - set(codes)
    if unknown:
        from .domain import ValidationError
        raise ValidationError(f"未知闸门: {','.join(sorted(unknown))}")
    total_override = sum(clean_overrides.values())
    rest = [code for code in codes if code not in clean_overrides]
    share = 0.0
    if rest:
        remaining = max(0.0, basis_opening - total_override)
        share = round(remaining / len(rest), 4)
        if share > MAX_OPENING:
            from .domain import ValidationError
            raise ValidationError("平均分摊后单孔开度超过上限")
    result = []
    for code in codes:
        opening = clean_overrides.get(code, share)
        result.append({"gate_code": code, "target_opening": opening})
    return result
