from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import ID_PREFIX, STATES, in_position


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
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
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
                CREATE TABLE IF NOT EXISTS execution_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','completed')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, seq)
                );
                CREATE TABLE IF NOT EXISTS gate_holes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    hole_no INTEGER NOT NULL,
                    target_opening REAL NOT NULL,
                    target_basis REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','refused','in_position')),
                    actual_opening REAL,
                    receipt_id INTEGER,
                    frozen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, hole_no)
                );
                CREATE TABLE IF NOT EXISTS gate_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL
                        REFERENCES execution_batches(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    hole_no INTEGER NOT NULL,
                    actual_opening REAL NOT NULL,
                    target_opening REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'closed'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, external_ref),
                    UNIQUE(batch_id, hole_no)
                );
            """)

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

    # ------------------------------------------------------------------
    # 闸门孔位
    # ------------------------------------------------------------------
    def authorize_item(self, item_id: int, expected_version: int, actor: str,
                       holes: List[Dict[str, Any]], basis: float) -> Dict[str, Any]:
        """授权：乐观锁更新状态 + 冻结每孔开度目标 + 建立首个执行批次（原子）。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE items SET status='authorized', version=version+1, updated_at=? "
                "WHERE id=? AND version=?",
                (now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
            for h in holes:
                self.conn.execute(
                    """INSERT INTO gate_holes(item_id, hole_no, target_opening, target_basis,
                       status, actual_opening, receipt_id, frozen_at, updated_at)
                       VALUES(?,?,?,?, 'pending', NULL, NULL, ?, ?)""",
                    (item_id, h["hole_no"], h["target_opening"], basis, now, now),
                )
            self.conn.execute(
                """INSERT INTO execution_batches(item_id, seq, status, created_by, created_at)
                   VALUES(?,1,'open',?,?)""",
                (item_id, actor, now),
            )
        return self.get_item(item_id)

    def list_gate_holes(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM gate_holes WHERE item_id=? ORDER BY hole_no", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_gate_hole(self, item_id: int, hole_no: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gate_holes WHERE item_id=? AND hole_no=?",
                (item_id, hole_no),
            ).fetchone()
        return dict(row) if row else None

    def recompute_pending_holes(self, item_id: int, quantity: float,
                                threshold: float) -> List[Dict[str, Any]]:
        """库位变化后，对未到位孔按新依据重算开度目标；已到位孔保留记录。"""
        from .rules import gate_opening_target
        now = utc_now()
        recomputed: List[Dict[str, Any]] = []
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT * FROM gate_holes WHERE item_id=? AND status!='in_position' ORDER BY hole_no",
                (item_id,),
            ).fetchall()
            for row in rows:
                new_target = gate_opening_target(quantity, threshold)
                self.conn.execute(
                    """UPDATE gate_holes SET target_opening=?, target_basis=?, frozen_at=?, updated_at=?
                       WHERE id=?""",
                    (new_target, quantity, now, now, row["id"]),
                )
                recomputed.append({
                    "hole_no": row["hole_no"],
                    "old_target": row["target_opening"],
                    "new_target": new_target,
                })
        return recomputed

    def update_item_quantity(self, item_id: int, quantity: float) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE items SET quantity=?, updated_at=? WHERE id=?",
                (quantity, utc_now(), item_id),
            )

    # ------------------------------------------------------------------
    # 执行批次
    # ------------------------------------------------------------------
    def create_execution_batch(self, item_id: int, seq: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO execution_batches(item_id, seq, status, created_by, created_at)
                   VALUES(?,?, 'open',?,?)""",
                (item_id, seq, actor, now),
            )
            batch_id = int(cur.lastrowid)
        return self.get_execution_batch(batch_id)

    def get_execution_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM execution_batches WHERE id=?", (batch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("执行批次不存在")
        return dict(row)

    def get_latest_batch(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM execution_batches WHERE item_id=? ORDER BY seq DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_execution_batches(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM execution_batches WHERE item_id=? ORDER BY seq", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 闸门执行回执
    # ------------------------------------------------------------------
    def find_gate_receipt(self, batch_id: int, external_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gate_receipts WHERE batch_id=? AND external_ref=?",
                (batch_id, external_ref),
            ).fetchone()
        return dict(row) if row else None

    def find_gate_receipt_by_hole(self, batch_id: int, hole_no: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gate_receipts WHERE batch_id=? AND hole_no=?",
                (batch_id, hole_no),
            ).fetchone()
        return dict(row) if row else None

    def submit_gate_receipt(self, item_id: int, batch_id: int, hole_no: int,
                            actual: float, external_ref: str,
                            actor: str) -> Dict[str, Any]:
        """逐孔登记实际开度。

        - 同批次同external_ref：幂等重试，直接返回已有回执（不重复追加）。
        - 孔已到位：拒绝（已到位孔不重做）。
        - 同批次同孔已有回执：拒绝（先到结果生效）。
        - 否则插入回执：到位则置孔位到位并关闭该孔历史拒动回执；
          未到位（拒动）则置孔位拒动，指令留在执行中。
        """
        now = utc_now()
        with self._lock, self.conn:
            existing = self.find_gate_receipt(batch_id, external_ref)
            if existing is not None:
                return {"receipt": existing, "created": False, "auto_executing": False}

            hole = self.get_gate_hole(item_id, hole_no)
            if hole is None:
                raise ValidationError("孔位不存在")
            if hole["status"] == "in_position":
                raise ConflictError("该孔已到位，不能重复登记")
            dup = self.find_gate_receipt_by_hole(batch_id, hole_no)
            if dup is not None:
                raise ConflictError("该孔已有回执，先到结果生效")

            target = float(hole["target_opening"])
            pos = in_position(actual, target)
            status = "closed" if pos else "open"
            cur = self.conn.execute(
                """INSERT INTO gate_receipts(batch_id, item_id, hole_no, actual_opening,
                   target_opening, status, external_ref, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (batch_id, item_id, hole_no, actual, target, status, external_ref, actor, now),
            )
            receipt_id = int(cur.lastrowid)
            if pos:
                self.conn.execute(
                    """UPDATE gate_holes SET status='in_position', actual_opening=?,
                       receipt_id=?, updated_at=? WHERE id=?""",
                    (actual, receipt_id, now, hole["id"]),
                )
                # 该孔历史拒动回执（未关闭事项）随到位而关闭
                self.conn.execute(
                    """UPDATE gate_receipts SET status='closed'
                       WHERE item_id=? AND hole_no=? AND status='open'""",
                    (item_id, hole_no),
                )
            else:
                self.conn.execute(
                    """UPDATE gate_holes SET status='refused', actual_opening=?, updated_at=?
                       WHERE id=?""",
                    (actual, now, hole["id"]),
                )

            # 授权后首次登记回执，自动进入执行中
            item = self.get_item(item_id)
            auto_executing = False
            if item["status"] == "authorized":
                self.conn.execute(
                    "UPDATE items SET status='executing', version=version+1, updated_at=? WHERE id=? AND version=?",
                    (now, item_id, item["version"]),
                )
                auto_executing = True

            row = self.conn.execute(
                "SELECT * FROM gate_receipts WHERE id=?", (receipt_id,)
            ).fetchone()
        return {"receipt": dict(row), "created": True, "auto_executing": auto_executing}

    def list_gate_receipts(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM gate_receipts WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_gate_receipt_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM gate_receipts WHERE item_id=? AND status='open'",
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

    def close(self) -> None:
        with self._lock:
            self.conn.close()
