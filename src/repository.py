from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES

GATE_STATUS_CHECKS = ("'pending','dispatched','arrived','refused',"
                      "'feedback_lost','mismatch'")


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN (""" + statuses + """)),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                -- 每条指令至多一个可续作的执行批次（batch_no固定为1）
                CREATE TABLE IF NOT EXISTS gate_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
                    batch_no INTEGER NOT NULL DEFAULT 1,
                    basis_version INTEGER NOT NULL DEFAULT 1,
                    basis TEXT NOT NULL,
                    tolerance REAL NOT NULL,
                    batch_status TEXT NOT NULL DEFAULT 'open'
                        CHECK(batch_status IN ('open','completed','closed')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES gate_plans(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    gate_code TEXT NOT NULL,
                    target_opening REAL NOT NULL,
                    basis_version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({gstat})),
                    attempt_no INTEGER NOT NULL DEFAULT 0,
                    actual_opening REAL,
                    arrival_target REAL,
                    arrived_at TEXT,
                    arrived_by TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(plan_id, gate_code)
                );
                CREATE TABLE IF NOT EXISTS dispatch_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES gate_plans(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    attempt_no INTEGER NOT NULL,
                    phase TEXT NOT NULL CHECK(phase IN ('dispatch','continue')),
                    gate_codes TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(plan_id, attempt_no)
                );
                CREATE TABLE IF NOT EXISTS gate_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gate_id INTEGER NOT NULL REFERENCES gates(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    attempt_no INTEGER NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('arrived','refused')),
                    actual_opening REAL,
                    client_token TEXT,
                    effective INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                -- 同一次现场动作的回传重试：同token只追加一次
                CREATE UNIQUE INDEX IF NOT EXISTS ux_receipt_token
                    ON gate_receipts(item_id, gate_id, client_token)
                    WHERE client_token IS NOT NULL;
                -- 同一派工轮次内每孔至多一条到位回执：同轮并发后到者不追加；
                -- 纠偏重派后attempt_no自增，可登记新一轮的到位回执
                CREATE UNIQUE INDEX IF NOT EXISTS ux_gate_arrival_per_attempt
                    ON gate_receipts(gate_id, attempt_no) WHERE outcome='arrived';
                CREATE INDEX IF NOT EXISTS ix_gates_item ON gates(item_id);
                CREATE INDEX IF NOT EXISTS ix_rounds_item ON dispatch_rounds(item_id);
                CREATE INDEX IF NOT EXISTS ix_receipts_item ON gate_receipts(item_id);
            """.format(gstat=GATE_STATUS_CHECKS))

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 闸门执行批次 ----
    def get_plan(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gate_plans WHERE item_id=?", (item_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_plan(self, item_id: int, basis: str, tolerance: float,
                    gates: List[Dict[str, float]], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT 1 FROM gate_plans WHERE item_id=?", (item_id,)
            ).fetchone()
            if existing is not None:
                raise ConflictError("该指令已存在执行批次，授权只能冻结一次")
            cur = self.conn.execute(
                """INSERT INTO gate_plans(item_id, batch_no, basis_version, basis,
                   tolerance, batch_status, created_by, created_at, updated_at)
                   VALUES(?,1,1,?,?,'open',?,?,?)""",
                (item_id, basis, tolerance, actor, now, now),
            )
            plan_id = int(cur.lastrowid)
            for gate in gates:
                self.conn.execute(
                    """INSERT INTO gates(plan_id, item_id, gate_code, target_opening,
                       basis_version, status, attempt_no, version)
                       VALUES(?,?,?,?,1,'pending',0,1)""",
                    (plan_id, item_id, gate["gate_code"], gate["target_opening"]),
                )
        return self.get_plan(item_id)

    def list_gates(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM gates WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_gate(self, item_id: int, gate_code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gates WHERE item_id=? AND gate_code=?",
                (item_id, gate_code),
            ).fetchone()
        return dict(row) if row else None

    def open_gate_codes(self, item_id: int) -> List[str]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT gate_code FROM gates WHERE item_id=?
                   AND status!='arrived' ORDER BY id""",
                (item_id,),
            ).fetchall()
        return [row["gate_code"] for row in rows]

    def next_attempt_no(self, plan_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(attempt_no),0) AS n FROM dispatch_rounds WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        return int(row["n"]) + 1

    def dispatch_round(self, item_id: int, gate_codes: List[str], attempt_no: int,
                       phase: str, actor: str) -> Dict[str, Any]:
        """为指定孔创建一轮派工/续办，并把这些孔推进到dispatched。重试不重复追加。"""
        now = utc_now()
        with self._lock, self.conn:
            plan = self.conn.execute(
                "SELECT * FROM gate_plans WHERE item_id=?", (item_id,)
            ).fetchone()
            if plan is None:
                raise NotFoundError("执行批次不存在")
            codes_json = json.dumps(gate_codes, ensure_ascii=False)
            try:
                cur = self.conn.execute(
                    """INSERT INTO dispatch_rounds(plan_id, item_id, attempt_no, phase,
                       gate_codes, created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (plan["id"], item_id, attempt_no, phase, codes_json, actor, now),
                )
                round_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该批次轮次已存在，重试不能重复追加") from exc
            for code in gate_codes:
                self.conn.execute(
                    """UPDATE gates SET status='dispatched', attempt_no=?, version=version+1
                       WHERE item_id=? AND gate_code=? AND status!='arrived'""",
                    (attempt_no, item_id, code),
                )
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatch_rounds WHERE id=?", (round_id,)
            ).fetchone()
        result = dict(row)
        result["gate_codes"] = json.loads(result["gate_codes"])
        return result

    def list_rounds(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dispatch_rounds WHERE item_id=? ORDER BY attempt_no",
                (item_id,),
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["gate_codes"] = json.loads(entry["gate_codes"])
            result.append(entry)
        return result

    def list_receipts(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM gate_receipts WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def find_receipt_by_token(self, gate_id: int, client_token: Optional[str]
                              ) -> Optional[Dict[str, Any]]:
        if not client_token:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gate_receipts WHERE gate_id=? AND client_token=?",
                (gate_id, client_token),
            ).fetchone()
        return dict(row) if row else None

    def register_receipt(self, item_id: int, gate_code: str, outcome: str,
                         actual_opening: Optional[float], client_token: Optional[str],
                         actor: str, consistent: bool) -> Dict[str, Any]:
        """原子登记回执：先到结果生效，迟到回传仅留痕；拒动孔保持拒动等待恢复续办。"""
        now = utc_now()
        with self._lock, self.conn:
            gate = self.conn.execute(
                "SELECT * FROM gates WHERE item_id=? AND gate_code=?",
                (item_id, gate_code),
            ).fetchone()
            if gate is None:
                raise NotFoundError(f"闸门{gate_code}不在该批次中")
            if gate["status"] == "arrived":
                # 已有生效到位回执：迟到回传不追加（防重复），由服务层返回409
                raise ConflictError(f"孔{gate_code}已有生效到位回执，迟到结果不生效")
            if client_token:
                dup = self.conn.execute(
                    "SELECT * FROM gate_receipts WHERE gate_id=? AND client_token=?",
                    (gate["id"], client_token),
                ).fetchone()
                if dup is not None:
                    raise ConflictError("同一次回传已登记，重试不重复追加")
            if outcome == "arrived":
                new_status = "arrived" if consistent else "mismatch"
                # 开度不符不算生效到位：保留有效回执槽位，纠偏重派后可登记真正到位回执
                effective = 1 if consistent else 0
            else:
                # 拒动：指令留在执行中；仅当孔尚无拒动留痕时把本次记为生效
                refused_count = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM gate_receipts WHERE gate_id=? AND outcome='refused'",
                    (gate["id"],),
                ).fetchone()["n"]
                new_status = "refused"
                effective = 1 if refused_count == 0 else 0
            try:
                cur = self.conn.execute(
                    """INSERT INTO gate_receipts(gate_id, item_id, attempt_no, outcome,
                       actual_opening, client_token, effective, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (gate["id"], item_id, gate["attempt_no"], outcome,
                     actual_opening, client_token, effective, actor, now),
                )
                receipt_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("并发登记冲突：另一值班员的结果已先生效") from exc
            if outcome == "arrived":
                if consistent:
                    updated = self.conn.execute(
                        """UPDATE gates SET status='arrived', actual_opening=?,
                           arrival_target=target_opening, arrived_at=?, arrived_by=?,
                           version=version+1 WHERE id=? AND version=?""",
                        (actual_opening, now, actor, gate["id"], gate["version"]),
                    )
                else:
                    updated = self.conn.execute(
                        """UPDATE gates SET status='mismatch', actual_opening=?,
                           version=version+1 WHERE id=? AND version=?""",
                        (actual_opening, gate["id"], gate["version"]),
                    )
                if updated.rowcount == 0:
                    raise ConflictError("并发登记冲突：另一值班员的结果已先生效")
            else:
                updated = self.conn.execute(
                    """UPDATE gates SET status='refused', version=version+1
                       WHERE id=? AND version=?""",
                    (gate["id"], gate["version"]),
                )
                if updated.rowcount == 0:
                    raise ConflictError("并发登记冲突：闸门状态已被更新")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gate_receipts WHERE id=?", (receipt_id,)
            ).fetchone()
        receipt = dict(row)
        receipt["new_status"] = new_status
        receipt["superseded"] = 0
        return receipt

    def mark_feedback_lost(self, item_id: int, gate_code: str, actor: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            gate = self.conn.execute(
                "SELECT * FROM gates WHERE item_id=? AND gate_code=?",
                (item_id, gate_code),
            ).fetchone()
            if gate is None:
                raise NotFoundError(f"闸门{gate_code}不在该批次中")
            if gate["status"] != "dispatched":
                raise ConflictError(f"孔{gate_code}当前为{gate['status']}，无需标记回传丢失")
            self.conn.execute(
                "UPDATE gates SET status='feedback_lost', version=version+1 WHERE id=?",
                (gate["id"],),
            )
        return self.get_gate(item_id, gate_code)

    def recompute_targets(self, item_id: int, new_basis: str,
                          targets: List[Dict[str, float]], actor: str) -> Dict[str, Any]:
        """库位变化后：未到位孔按新依据重算并回到pending；已到位孔保留记录不动。"""
        now = utc_now()
        target_map = {g["gate_code"]: g["target_opening"] for g in targets}
        with self._lock, self.conn:
            plan = self.conn.execute(
                "SELECT * FROM gate_plans WHERE item_id=?", (item_id,)
            ).fetchone()
            if plan is None:
                raise NotFoundError("执行批次不存在")
            new_version = int(plan["basis_version"]) + 1
            self.conn.execute(
                """UPDATE gate_plans SET basis_version=?, basis=?, updated_at=?
                   WHERE id=?""",
                (new_version, new_basis, now, plan["id"]),
            )
            self.conn.execute(
                """UPDATE gates SET target_opening=?, basis_version=?, status='pending',
                   attempt_no=0, version=version+1
                   WHERE item_id=? AND gate_code=? AND status!='arrived'""",
            ) if False else None
            for code, opening in target_map.items():
                self.conn.execute(
                    """UPDATE gates SET target_opening=?, basis_version=?, status='pending',
                       attempt_no=0, version=version+1
                       WHERE item_id=? AND gate_code=? AND status!='arrived'""",
                    (opening, new_version, item_id, code),
                )
        return self.get_plan(item_id)

    def mark_batch_completed(self, item_id: int) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE gate_plans SET batch_status='completed', updated_at=? WHERE item_id=?",
                (now, item_id),
            )

    def mark_batch_closed(self, item_id: int) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE gate_plans SET batch_status='closed', updated_at=? WHERE item_id=?",
                (now, item_id),
            )

    def close(self) -> None:
        with self._lock:
            self.conn.close()
