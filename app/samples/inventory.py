from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.samples.repository import LocationRepository, SampleRepository
from app.services.audit import AuditService


def _row(row: sqlite3.Row | None, message: str) -> dict[str, Any]:
    if row is None:
        raise NotFoundError(message)
    return dict(row)


class InventoryRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create_session(
        self,
        session_code: str,
        location_id: int,
        started_by: int,
        snapshot_version: int,
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO inventory_sessions(
                   session_code,location_id,started_by,state,snapshot_version,
                   started_at,created_at,updated_at
               ) VALUES(?,?,?,'draft',?,?,?,?)""",
            (session_code, location_id, started_by, snapshot_version, now, now, now),
        )
        return self.get_session(cursor.lastrowid)

    def get_session(self, session_id: int) -> dict[str, Any]:
        session = _row(
            self.connection.execute(
                """SELECT i.*,l.code AS location_code,l.sensitivity AS location_sensitivity
                   FROM inventory_sessions i JOIN storage_locations l ON l.id=i.location_id
                   WHERE i.id=?""",
                (session_id,),
            ).fetchone(),
            "盘点会话不存在",
        )
        session["counts"] = [
            dict(row)
            for row in self.connection.execute(
                """SELECT c.*,s.sample_code,s.quantity AS book_quantity,s.unit,s.lifecycle_state
                   FROM inventory_counts c JOIN samples s ON s.id=c.sample_id
                   WHERE c.session_id=? ORDER BY s.sample_code""",
                (session_id,),
            ).fetchall()
        ]
        return session

    def active_for_location(self, location_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM inventory_sessions
               WHERE location_id=? AND state NOT IN ('closed','cancelled') ORDER BY id DESC LIMIT 1""",
            (location_id,),
        ).fetchone()
        return dict(row) if row else None

    def transition(self, session_id: int, expected_state: str, state: str, now: str) -> dict[str, Any]:
        closed_at = now if state == "closed" else None
        cursor = self.connection.execute(
            """UPDATE inventory_sessions SET state=?,closed_at=COALESCE(?,closed_at),updated_at=?
               WHERE id=? AND state=?""",
            (state, closed_at, now, session_id, expected_state),
        )
        if cursor.rowcount != 1:
            raise ConflictError("盘点会话状态已变化")
        return self.get_session(session_id)

    def upsert_count(
        self,
        session_id: int,
        sample_id: int,
        observed_quantity: float | None,
        observed_present: bool,
        counted_by: int,
        note: str,
        now: str,
    ) -> dict[str, Any]:
        self.connection.execute(
            """INSERT INTO inventory_counts(
                   session_id,sample_id,observed_quantity,observed_present,counted_by,counted_at,note
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(session_id,sample_id) DO UPDATE SET
                   observed_quantity=excluded.observed_quantity,
                   observed_present=excluded.observed_present,
                   counted_by=excluded.counted_by,
                   counted_at=excluded.counted_at,
                   note=excluded.note""",
            (session_id, sample_id, observed_quantity, int(observed_present), counted_by, now, note),
        )
        return dict(
            self.connection.execute(
                "SELECT * FROM inventory_counts WHERE session_id=? AND sample_id=?",
                (session_id, sample_id),
            ).fetchone()
        )

    def expected_samples(self, location_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM samples WHERE location_id=?
               AND lifecycle_state NOT IN ('destroyed','consumed') ORDER BY sample_code""",
            (location_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def snapshot_baselines(self, session_id: int, samples: list[dict[str, Any]], now: str) -> None:
        """记录盘点快照时刻每个样品的事件水位。"""
        for sample in samples:
            last_event_id = self.connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM sample_events WHERE sample_id=?",
                (sample["id"],),
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO inventory_snapshot_baselines(session_id,sample_id,last_event_id) VALUES(?,?,?)",
                (session_id, sample["id"], last_event_id),
            )


class InventoryService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.inventory = InventoryRepository(connection)
        self.locations = LocationRepository(connection)
        self.samples = SampleRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def start(self, principal: Principal, location_id: int, session_code: str | None = None) -> dict[str, Any]:
        principal.require("inventory.manage")
        self.locations.get(location_id)
        if self.inventory.active_for_location(location_id):
            raise ConflictError("该位置已有未结束的盘点")
        now = to_storage(self.clock.now())
        snapshot = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(version),0) FROM samples WHERE location_id=?",
                (location_id,),
            ).fetchone()[0]
        )
        code = session_code or f"INV-{uuid.uuid4().hex[:12]}"
        session = self.inventory.create_session(code, location_id, principal.user_id, snapshot, now)
        # 冻结盘点快照：记录该位置每件样品在盘点开始时的事件水位
        self.inventory.snapshot_baselines(session["id"], self.inventory.expected_samples(location_id), now)
        session = self.inventory.transition(session["id"], "draft", "counting", now)
        self.audit.record(
            principal,
            "inventory.start",
            "inventory_session",
            str(session["id"]),
            after=session,
            metadata={"location_id": location_id, "snapshot_version": snapshot},
        )
        return session

    def count(
        self,
        principal: Principal,
        session_id: int,
        sample_id: int,
        observed_present: bool,
        observed_quantity: float | None,
        note: str = "",
    ) -> dict[str, Any]:
        principal.require("inventory.manage")
        session = self.inventory.get_session(session_id)
        if session["state"] != "counting":
            raise ConflictError("盘点会话不在计数阶段")
        sample = self.samples.get(sample_id)
        if sample["location_id"] != session["location_id"]:
            raise ValidationError("样品不属于本次盘点位置")
        if observed_present and observed_quantity is None:
            raise ValidationError("发现样品时必须填写实盘数量")
        if observed_quantity is not None and observed_quantity < 0:
            raise ValidationError("实盘数量不能为负数")
        return self.inventory.upsert_count(
            session_id,
            sample_id,
            observed_quantity,
            observed_present,
            principal.user_id,
            note,
            to_storage(self.clock.now()),
        )

    def reconcile(self, principal: Principal, session_id: int) -> dict[str, Any]:
        principal.require("inventory.manage")
        before = self.inventory.get_session(session_id)
        if before["state"] != "counting":
            raise ConflictError("只有计数中的盘点可以生成差异")
        expected = self.inventory.expected_samples(before["location_id"])
        counts = {item["sample_id"]: item for item in before["counts"]}
        differences = []
        for sample in expected:
            count = counts.get(sample["id"])
            if count is None:
                differences.append(
                    {
                        "sample_id": sample["id"],
                        "sample_code": sample["sample_code"],
                        "kind": "not_counted",
                        "book_quantity": sample["quantity"],
                        "observed_quantity": None,
                    }
                )
                continue
            if not count["observed_present"]:
                differences.append(
                    {
                        "sample_id": sample["id"],
                        "sample_code": sample["sample_code"],
                        "kind": "missing",
                        "book_quantity": sample["quantity"],
                        "observed_quantity": None,
                    }
                )
                continue
            observed = float(count["observed_quantity"])
            if abs(observed - sample["quantity"]) > 1e-9:
                differences.append(
                    {
                        "sample_id": sample["id"],
                        "sample_code": sample["sample_code"],
                        "kind": "quantity_mismatch",
                        "book_quantity": sample["quantity"],
                        "observed_quantity": observed,
                        "delta": observed - sample["quantity"],
                    }
                )
        now = to_storage(self.clock.now())
        session = self.inventory.transition(session_id, "counting", "reconciling", now)

        # 持久化差异清单；盘点期间新移入本位置的样品补齐快照水位。
        # 快照水位在盘点开始时冻结（见 start），用于识别盘点期间发生的借还、转移、消耗等事件。
        self.connection.execute("DELETE FROM inventory_differences WHERE session_id=?", (session_id,))
        for sample in expected:
            exists = self.connection.execute(
                "SELECT 1 FROM inventory_snapshot_baselines WHERE session_id=? AND sample_id=?",
                (session_id, sample["id"]),
            ).fetchone()
            if not exists:
                last_event_id = self.connection.execute(
                    "SELECT COALESCE(MAX(id),0) FROM sample_events WHERE sample_id=?",
                    (sample["id"],),
                ).fetchone()[0]
                self.connection.execute(
                    "INSERT INTO inventory_snapshot_baselines(session_id,sample_id,last_event_id) VALUES(?,?,?)",
                    (session_id, sample["id"], last_event_id),
                )
        for item in differences:
            self.connection.execute(
                """INSERT INTO inventory_differences(
                       session_id,sample_id,difference_kind,book_quantity,observed_quantity,quantity_delta,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    session_id,
                    item["sample_id"],
                    item["kind"],
                    item["book_quantity"],
                    item["observed_quantity"],
                    item.get("delta"),
                    now,
                ),
            )
        digest = hashlib.sha256(
            json.dumps(differences, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.audit.record(
            principal,
            "inventory.reconcile",
            "inventory_session",
            str(session_id),
            before=before,
            after=session,
            metadata={"difference_count": len(differences), "difference_digest": digest},
        )
        return {"session": session, "differences": differences, "digest": digest}

    def close(self, principal: Principal, session_id: int) -> dict[str, Any]:
        principal.require("inventory.manage")
        before = self.inventory.get_session(session_id)
        if before["state"] != "reconciling":
            raise ConflictError("盘点会话尚未进入差异复核阶段")
        differences = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM inventory_differences WHERE session_id=?",
                (session_id,),
            ).fetchall()
        ]
        now = to_storage(self.clock.now())
        unresolved: list[dict[str, Any]] = []
        recheck: list[dict[str, Any]] = []
        for difference in differences:
            disposition_row = self.connection.execute(
                "SELECT * FROM discrepancy_dispositions WHERE session_id=? AND sample_id=?",
                (session_id, difference["sample_id"]),
            ).fetchone()
            if disposition_row is None:
                unresolved.append({"sample_id": difference["sample_id"], "reason": "缺少差异处置单"})
                continue
            disposition = dict(disposition_row)
            latest_event_id = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(id),0) FROM sample_events WHERE sample_id=?",
                    (difference["sample_id"],),
                ).fetchone()[0]
            )
            stock_changing = disposition["disposition"] in {"relocate", "quantity_adjust", "report_loss"}
            if stock_changing:
                # 数量、移位、报失处置必须真正执行落地后才能对账；
                # 执行是原子操作且执行前已校验事件水位，执行后的正常借还/消耗属于新业务，不翻旧账。
                if disposition["state"] == "ready" and latest_event_id > int(disposition["reviewed_event_id"]):
                    recheck.append(
                        {
                            "sample_id": difference["sample_id"],
                            "reviewed_event_id": disposition["reviewed_event_id"],
                            "latest_event_id": latest_event_id,
                        }
                    )
                    continue
                if disposition["state"] != "executed":
                    pending_text = {
                        "in_review": "处置单尚未完成复核",
                        "pending_approval": "处置单等待双人审批",
                        "ready": "处置单已审批通过但尚未执行",
                        "rejected": "处置单审批被驳回，需修改后重新提交",
                    }.get(disposition["state"], "处置单未结案")
                    unresolved.append({"sample_id": difference["sample_id"], "reason": pending_text})
                continue
            # 仅复核、不改动库存的处置单：必须完成复核且关账前事件水位仍然有效
            if disposition["state"] != "ready":
                pending_text = {
                    "in_review": "处置单尚未完成复核",
                    "pending_approval": "处置单等待双人审批",
                    "rejected": "处置单审批被驳回，需修改后重新提交",
                }.get(disposition["state"], "处置单未结案")
                unresolved.append({"sample_id": difference["sample_id"], "reason": pending_text})
                continue
            if latest_event_id > int(disposition["reviewed_event_id"]):
                recheck.append(
                    {
                        "sample_id": difference["sample_id"],
                        "reviewed_event_id": disposition["reviewed_event_id"],
                        "latest_event_id": latest_event_id,
                    }
                )
        if recheck:
            raise ConflictError(
                "复核结论之后样品发生了新的借还或移位事件，必须重新复核",
                context={"need_recheck": recheck},
            )
        if unresolved:
            raise ConflictError("仍有未对账的盘点差异", context={"unresolved": unresolved})
        result = self.inventory.transition(session_id, "reconciling", "closed", now)
        self.audit.record(
            principal,
            "inventory.close",
            "inventory_session",
            str(session_id),
            before=before,
            after=result,
            metadata={"difference_count": len(differences)},
        )
        return result


class StockSummaryService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def by_location(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("samples.read")
        rows = self.connection.execute(
            """SELECT l.id,l.code,l.sensitivity,l.capacity_units,
                      COUNT(s.id) AS sample_count,
                      COALESCE(SUM(CASE WHEN s.lifecycle_state NOT IN ('destroyed','consumed') THEN s.quantity ELSE 0 END),0) AS quantity,
                      SUM(CASE WHEN s.lifecycle_state='loaned' THEN 1 ELSE 0 END) AS loaned_count,
                      SUM(CASE WHEN s.lifecycle_state='quarantined' THEN 1 ELSE 0 END) AS quarantined_count
               FROM storage_locations l LEFT JOIN samples s ON s.location_id=l.id
               WHERE l.active=1 GROUP BY l.id ORDER BY l.code"""
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item["sensitivity"] != "normal" and not (
                "*" in principal.permissions or "locations.read_sensitive" in principal.permissions
            ):
                item["code"] = f"MASKED-{item['id']:04d}"
            result.append(item)
        return result

    def by_state(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("samples.read")
        rows = self.connection.execute(
            """SELECT lifecycle_state,unit,COUNT(*) AS sample_count,SUM(quantity) AS quantity,
                      SUM(reserved_quantity) AS reserved_quantity
               FROM samples GROUP BY lifecycle_state,unit ORDER BY lifecycle_state,unit"""
        ).fetchall()
        return [dict(row) for row in rows]
