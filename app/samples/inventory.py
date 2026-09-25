from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.samples.repository import ApprovalRepository, LocationRepository, SampleRepository
from app.services.audit import AuditService

TERMINAL_DISPOSITION_STATES = {"resolved", "executed"}
ADJUSTMENT_ACTIONS = {"relocate", "adjust_quantity"}


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

    def seed_baselines(self, session_id: int, location_id: int) -> None:
        """锚定会话开始时位置上的样品：记录版本与最后一条样品事件，之后发生的借还/转移/消耗都会落在快照之后。"""
        self.connection.execute(
            """INSERT INTO inventory_session_baselines(session_id,sample_id,sample_version,last_event_id)
               SELECT ?,s.id,s.version,
                      COALESCE((SELECT MAX(e.id) FROM sample_events e WHERE e.sample_id=s.id),0)
               FROM samples s
               WHERE s.location_id=? AND s.lifecycle_state NOT IN ('destroyed','consumed')""",
            (session_id, location_id),
        )

    def ensure_baselines(self, session_id: int, location_id: int) -> None:
        """盘点期间新进入该位置的样品（新登记或调入）也补入基线，last_event_id=0 表示其到来本身就是快照后变更。"""
        self.connection.execute(
            """INSERT INTO inventory_session_baselines(session_id,sample_id,sample_version,last_event_id)
               SELECT ?,s.id,s.version,0 FROM samples s
               WHERE s.location_id=? AND s.lifecycle_state NOT IN ('destroyed','consumed')
               AND NOT EXISTS (
                   SELECT 1 FROM inventory_session_baselines b
                   WHERE b.session_id=? AND b.sample_id=s.id
               )""",
            (session_id, location_id, session_id),
        )

    def baselines(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT b.*,s.sample_code,s.quantity AS book_quantity,s.unit,
                      s.lifecycle_state,s.version AS current_version,s.location_id AS current_location_id,
                      c.observed_quantity,c.observed_present
               FROM inventory_session_baselines b
               JOIN samples s ON s.id=b.sample_id
               LEFT JOIN inventory_counts c ON c.session_id=b.session_id AND c.sample_id=b.sample_id
               WHERE b.session_id=? ORDER BY s.sample_code""",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def baseline_for_sample(self, session_id: int, sample_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM inventory_session_baselines WHERE session_id=? AND sample_id=?",
            (session_id, sample_id),
        ).fetchone()
        return dict(row) if row else None

    def reanchor_baseline(self, session_id: int, sample_id: int, version: int, last_event_id: int) -> None:
        """重新复核确认后，把基线推进到当前版本与事件位点；差异种类保持不变，执行时只校验此后的新变更。"""
        self.connection.execute(
            "UPDATE inventory_session_baselines SET sample_version=?,last_event_id=? WHERE session_id=? AND sample_id=?",
            (version, last_event_id, session_id, sample_id),
        )

    def set_difference_kind(self, session_id: int, sample_id: int, kind: str | None) -> None:
        self.connection.execute(
            "UPDATE inventory_session_baselines SET difference_kind=? WHERE session_id=? AND sample_id=?",
            (kind, session_id, sample_id),
        )

    def last_event_id(self, sample_id: int) -> int:
        return int(
            self.connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM sample_events WHERE sample_id=?", (sample_id,)
            ).fetchone()[0]
        )

    def events_after(self, sample_id: int, last_event_id: int, session_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT id,event_type,actor_user_id,quantity_delta,from_state,to_state,
                      details_json,occurred_at
               FROM sample_events
               WHERE sample_id=? AND id>?
                 AND NOT (event_type LIKE 'inventory.%'
                          AND CAST(json_extract(details_json,'$.session_id') AS INTEGER)=?)
               ORDER BY id""",
            (sample_id, last_event_id, session_id),
        ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            events.append(item)
        return events


class DispositionRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO inventory_dispositions(
                   disposition_code,session_id,sample_id,difference_kind,action,
                   target_quantity,target_location_id,evidence_summary,responsibility_note,
                   state,approval_request_id,expected_version,created_by,updated_by,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                values["disposition_code"], values["session_id"], values["sample_id"],
                values["difference_kind"], values["action"], values.get("target_quantity"),
                values.get("target_location_id"), values["evidence_summary"],
                values["responsibility_note"], values["state"], values.get("approval_request_id"),
                values.get("expected_version"), values["created_by"], values["updated_by"], now, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, disposition_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute(
                """SELECT d.*,s.sample_code,s.quantity AS book_quantity,s.version AS current_version,
                          s.location_id AS current_location_id,s.reserved_quantity,
                          tl.code AS target_location_code
                   FROM inventory_dispositions d
                   JOIN samples s ON s.id=d.sample_id
                   LEFT JOIN storage_locations tl ON tl.id=d.target_location_id
                   WHERE d.id=?""",
                (disposition_id,),
            ).fetchone(),
            "差异处置单不存在",
        )

    def by_session_sample(self, session_id: int, sample_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM inventory_dispositions WHERE session_id=? AND sample_id=?",
            (session_id, sample_id),
        ).fetchone()
        return dict(row) if row else None

    def list_for_session(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT d.*,s.sample_code,tl.code AS target_location_code
               FROM inventory_dispositions d
               JOIN samples s ON s.id=d.sample_id
               LEFT JOIN storage_locations tl ON tl.id=d.target_location_id
               WHERE d.session_id=? ORDER BY d.id""",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def replace(self, disposition_id: int, values: dict[str, Any], now: str) -> dict[str, Any]:
        self.connection.execute(
            """UPDATE inventory_dispositions SET
                   action=?,target_quantity=?,target_location_id=?,evidence_summary=?,
                   responsibility_note=?,state=?,approval_request_id=?,expected_version=?,
                   updated_by=?,executed_by=NULL,executed_at=NULL,updated_at=?
               WHERE id=?""",
            (
                values["action"], values.get("target_quantity"), values.get("target_location_id"),
                values["evidence_summary"], values["responsibility_note"], values["state"],
                values.get("approval_request_id"), values.get("expected_version"),
                values["updated_by"], now, disposition_id,
            ),
        )
        return self.get(disposition_id)

    def mark_needs_rereview(self, disposition_id: int, user_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE inventory_dispositions SET state='needs_rereview',updated_by=?,updated_at=? WHERE id=?",
            (user_id, now, disposition_id),
        )

    def mark_executed(self, disposition_id: int, user_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE inventory_dispositions SET state='executed',executed_by=?,executed_at=?,updated_by=?,updated_at=? WHERE id=?",
            (user_id, now, user_id, now, disposition_id),
        )


class InventoryService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.inventory = InventoryRepository(connection)
        self.dispositions = DispositionRepository(connection)
        self.locations = LocationRepository(connection)
        self.samples = SampleRepository(connection)
        self.approvals = ApprovalRepository(connection)
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
        self.inventory.seed_baselines(session["id"], location_id)
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

    def _difference(self, baseline: dict[str, Any], session_location_id: int) -> dict[str, Any] | None:
        """以会话开始时的样品基线为准对账，当前位置/账面已被借还冲掉也会暴露为差异。"""
        if baseline["current_location_id"] != session_location_id:
            return {
                "sample_id": baseline["sample_id"],
                "sample_code": baseline["sample_code"],
                "kind": "location_mismatch",
                "book_quantity": baseline["book_quantity"],
                "observed_quantity": baseline["observed_quantity"],
                "current_location_id": baseline["current_location_id"],
            }
        if baseline["observed_present"] is None:
            kind = "not_counted"
            observed = None
            delta = None
        elif not baseline["observed_present"]:
            kind = "missing"
            observed = None
            delta = None
        elif abs(float(baseline["observed_quantity"]) - float(baseline["book_quantity"])) > 1e-9:
            kind = "quantity_mismatch"
            observed = float(baseline["observed_quantity"])
            delta = observed - float(baseline["book_quantity"])
        else:
            return None
        return {
            "sample_id": baseline["sample_id"],
            "sample_code": baseline["sample_code"],
            "kind": kind,
            "book_quantity": baseline["book_quantity"],
            "observed_quantity": observed,
            "delta": delta,
        }

    def reconcile(self, principal: Principal, session_id: int) -> dict[str, Any]:
        principal.require("inventory.manage")
        before = self.inventory.get_session(session_id)
        if before["state"] != "counting":
            raise ConflictError("只有计数中的盘点可以生成差异")
        self.inventory.ensure_baselines(session_id, before["location_id"])
        differences = []
        for baseline in self.inventory.baselines(session_id):
            difference = self._difference(baseline, before["location_id"])
            self.inventory.set_difference_kind(
                session_id, baseline["sample_id"], difference["kind"] if difference else None
            )
            if difference:
                differences.append(difference)
        now = to_storage(self.clock.now())
        session = self.inventory.transition(session_id, "counting", "reconciling", now)
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

    # ------------------------------------------------------------------ 差异处置

    def _require_reconciling(self, session_id: int) -> dict[str, Any]:
        session = self.inventory.get_session(session_id)
        if session["state"] != "reconciling":
            raise ConflictError("盘点会话不在差异处置阶段")
        return session

    def _require_difference(self, session: dict[str, Any], sample_id: int) -> dict[str, Any]:
        baseline = self.inventory.baseline_for_sample(session["id"], sample_id)
        if baseline is None or not baseline.get("difference_kind"):
            raise ValidationError("该样品在本次盘点中没有待处置差异")
        return baseline

    def _drift_events(self, baseline: dict[str, Any], session_id: int, current_version: int) -> list[dict[str, Any]]:
        """找出盘点快照锚点之后发生的样品事件（借还、转移、消耗等）；本盘点自己写入的事件不计。"""
        events = self.inventory.events_after(
            baseline["sample_id"], baseline["last_event_id"], session_id
        )
        if baseline["sample_version"] != current_version and not events:
            events.append(
                {
                    "id": None,
                    "event_type": "inventory.version_changed",
                    "occurred_at": None,
                    "details": {
                        "expected_version": baseline["sample_version"],
                        "current_version": current_version,
                    },
                }
            )
        return events

    def _cancel_approval(self, request_id: int | None, now: str) -> None:
        if request_id is None:
            return
        self.connection.execute(
            "UPDATE approval_requests SET state='cancelled',version=version+1,updated_at=? WHERE id=? AND state IN ('pending','approved')",
            (now, request_id),
        )

    def _record_direct_event(
        self, principal: Principal, disposition: dict[str, Any], baseline: dict[str, Any], now: str
    ) -> None:
        event_type = "inventory.loss_reported" if disposition["action"] == "report_loss" else "inventory.reviewed"
        self.samples.append_event(
            disposition["sample_id"],
            event_type,
            principal.user_id,
            now,
            details={
                "disposition_id": disposition["id"],
                "disposition_code": disposition["disposition_code"],
                "session_id": disposition["session_id"],
                "difference_kind": baseline["difference_kind"],
                "evidence_summary": disposition["evidence_summary"],
                "responsibility_note": disposition["responsibility_note"],
            },
        )

    def create_disposition(self, principal: Principal, session_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("inventory.manage")
        session = self._require_reconciling(session_id)
        sample = self.samples.get(data["sample_id"])
        baseline = self._require_difference(session, data["sample_id"])
        action = data["action"]
        target_quantity = data.get("target_quantity")
        target_location_id = data.get("target_location_id")
        evidence = data["evidence_summary"].strip()
        responsibility = data["responsibility_note"].strip()
        if not evidence:
            raise ValidationError("必须填写证据摘要")
        if not responsibility:
            raise ValidationError("必须填写责任说明")
        if action == "adjust_quantity":
            if target_quantity is None:
                raise ValidationError("数量调整必须填写调整后数量")
            if target_quantity < 0:
                raise ValidationError("调整后数量不能为负数")
            if abs(target_quantity - float(sample["quantity"])) <= 1e-9:
                raise ValidationError("调整后数量与当前账面数量一致，无需调整")
        if action == "relocate":
            if target_location_id is None:
                raise ValidationError("移位处置必须选择目标位置")
            target = self.locations.get(target_location_id)
            if target["id"] == sample["location_id"]:
                raise ValidationError("目标位置与样品当前位置一致，无需移位")
        now = to_storage(self.clock.now())
        existing = self.dispositions.by_session_sample(session_id, data["sample_id"])
        if existing:
            existing = self._sync_approval_state(existing, now)
        if existing and existing["state"] in TERMINAL_DISPOSITION_STATES:
            raise ConflictError("差异已处置终结，不能重复提交")
        if existing and existing["state"] == "awaiting_approval":
            raise ConflictError("该差异已有待审批处置单，请等待审批结论或发起重新复核")

        drift = self._drift_events(baseline, session_id, sample["version"])
        needs_approval = action in ADJUSTMENT_ACTIONS
        if drift:
            state = "needs_rereview"
        elif needs_approval:
            state = "awaiting_approval"
        else:
            state = "resolved"
        code = existing["disposition_code"] if existing else f"DSP-{uuid.uuid4().hex[:12]}"
        values = {
            "disposition_code": code,
            "session_id": session_id,
            "sample_id": data["sample_id"],
            "difference_kind": baseline["difference_kind"],
            "action": action,
            "target_quantity": target_quantity,
            "target_location_id": target_location_id,
            "evidence_summary": evidence,
            "responsibility_note": responsibility,
            "state": state,
            "approval_request_id": None,
            "expected_version": sample["version"] if needs_approval else None,
            "created_by": principal.user_id,
            "updated_by": principal.user_id,
        }

        if existing:
            self._cancel_approval(existing["approval_request_id"], now)
            disposition = self.dispositions.replace(existing["id"], values, now)
        else:
            disposition = self.dispositions.create(values, now)

        if state == "needs_rereview":
            # 快照后样品已被借还/转移/消耗触动：只挂起处置单，库存不动，等待重新复核。
            pass
        elif needs_approval:
            approval_request_id = self._create_approval(principal, disposition, now)
            self.connection.execute(
                "UPDATE inventory_dispositions SET approval_request_id=? WHERE id=?",
                (approval_request_id, disposition["id"]),
            )
            disposition = self.dispositions.get(disposition["id"])
        else:
            self._record_direct_event(principal, disposition, baseline, now)
            self.inventory.reanchor_baseline(
                session_id, data["sample_id"], sample["version"],
                self.inventory.last_event_id(data["sample_id"]),
            )
            disposition = self.dispositions.get(disposition["id"])

        self.audit.record(
            principal,
            "inventory.disposition.create",
            "inventory_disposition",
            str(disposition["id"]),
            before=existing,
            after=disposition,
            metadata={"action": action, "requires_approval": needs_approval, "drift_event_count": len(drift)},
        )
        return disposition

    def confirm_rereview(self, principal: Principal, disposition_id: int, note: str = "") -> dict[str, Any]:
        """库管员现场重新复核：确认已知晓并消化快照后的借还等变更，推进锚点并恢复处置流程。

        锚点推进到当前事件位点——复核确认前发生的事件视为已核对；此后再出现的新事件，
        在执行时仍会被漂移校验与 expected_version 乐观锁拦下。
        """
        principal.require("inventory.manage")
        before = self.dispositions.get(disposition_id)
        self._require_reconciling(before["session_id"])
        if before["state"] != "needs_rereview":
            raise ConflictError("只有待重新复核的处置单可以确认复核", context={"state": before["state"]})
        baseline = self.inventory.baseline_for_sample(before["session_id"], before["sample_id"])
        sample = self.samples.get(before["sample_id"])
        acknowledged_events = self._drift_events(baseline, before["session_id"], sample["version"])
        now = to_storage(self.clock.now())
        needs_approval = before["action"] in ADJUSTMENT_ACTIONS
        if needs_approval:
            if before["action"] == "adjust_quantity":
                target_quantity = float(before["target_quantity"])
                if abs(target_quantity - float(sample["quantity"])) <= 1e-9:
                    raise ValidationError("调整后数量与当前账面数量已一致，请改写处置方式")
                if target_quantity < float(sample["reserved_quantity"]):
                    raise ConflictError("调整后数量小于已预留（借出未还）数量")
            elif before["action"] == "relocate" and before["target_location_id"] == sample["location_id"]:
                raise ValidationError("样品已位于目标位置，请改写处置方式")
            approval_request_id = self._create_approval(principal, before, now)
            self.connection.execute(
                """UPDATE inventory_dispositions
                   SET state='awaiting_approval',approval_request_id=?,expected_version=?,
                       updated_by=?,updated_at=? WHERE id=?""",
                (approval_request_id, sample["version"], principal.user_id, now, disposition_id),
            )
        else:
            self.connection.execute(
                "UPDATE inventory_dispositions SET state='resolved',updated_by=?,updated_at=? WHERE id=?",
                (principal.user_id, now, disposition_id),
            )
        # 先推进锚点再记事件，保证本盘点自己写入的事件不会被当成新变更。
        self.inventory.reanchor_baseline(
            before["session_id"], before["sample_id"], sample["version"],
            self.inventory.last_event_id(before["sample_id"]),
        )
        disposition = self.dispositions.get(disposition_id)
        if not needs_approval:
            self._record_direct_event(principal, disposition, baseline, now)
            disposition = self.dispositions.get(disposition_id)
        self.audit.record(
            principal,
            "inventory.disposition.rereview",
            "inventory_disposition",
            str(disposition_id),
            before=before,
            after=disposition,
            metadata={"note": note, "acknowledged_events": acknowledged_events},
        )
        return {"disposition": disposition, "acknowledged_events": acknowledged_events}

    def _create_approval(self, principal: Principal, disposition: dict[str, Any], now: str) -> int:
        payload = {
            "disposition_id": disposition["id"],
            "disposition_code": disposition["disposition_code"],
            "session_id": disposition["session_id"],
            "sample_id": disposition["sample_id"],
            "difference_kind": disposition["difference_kind"],
            "action": disposition["action"],
            "target_quantity": disposition["target_quantity"],
            "target_location_id": disposition["target_location_id"],
        }
        request = self.approvals.create(
            {
                "action_type": "inventory_adjustment",
                "resource_type": "inventory_disposition",
                "resource_id": disposition["id"],
                "payload": payload,
                "expires_at": to_storage(self.clock.now() + timedelta(days=3)),
            },
            principal.user_id,
            f"ADJ-{uuid.uuid4().hex[:12]}",
            now,
        )
        return request["id"]

    def _sync_approval_state(self, disposition: dict[str, Any], now: str) -> dict[str, Any]:
        """审批被驳回或过期时，处置单回到 rejected，库管员可据此改写后重新提交。"""
        if disposition["state"] != "awaiting_approval" or disposition["approval_request_id"] is None:
            return disposition
        approval = self.approvals.get(disposition["approval_request_id"])
        if approval["state"] in {"rejected", "expired"}:
            self.connection.execute(
                "UPDATE inventory_dispositions SET state='rejected',updated_at=? WHERE id=? AND state='awaiting_approval'",
                (now, disposition["id"]),
            )
            return self.dispositions.get(disposition["id"])
        return disposition

    def recheck_disposition(self, principal: Principal, disposition_id: int) -> dict[str, Any]:
        principal.require("inventory.manage")
        now = to_storage(self.clock.now())
        before = self._sync_approval_state(self.dispositions.get(disposition_id), now)
        self._require_reconciling(before["session_id"])
        if before["state"] == "executed":
            raise ConflictError("处置已执行，不能重新复核")
        baseline = self.inventory.baseline_for_sample(before["session_id"], before["sample_id"])
        new_events = self._drift_events(baseline, before["session_id"], before["current_version"])
        drifted = bool(new_events)
        if drifted:
            self._cancel_approval(before["approval_request_id"], now)
            self.dispositions.mark_needs_rereview(disposition_id, principal.user_id, now)
        after = self.dispositions.get(disposition_id)
        self.audit.record(
            principal,
            "inventory.disposition.recheck",
            "inventory_disposition",
            str(disposition_id),
            before=before,
            after=after,
            metadata={"drifted": drifted, "new_events": new_events},
        )
        return {"disposition": after, "drifted": drifted, "new_events": new_events}

    def execute_adjustment(self, principal: Principal, disposition_id: int) -> dict[str, Any]:
        principal.require("inventory.manage")
        now = to_storage(self.clock.now())
        before = self._sync_approval_state(self.dispositions.get(disposition_id), now)
        self._require_reconciling(before["session_id"])
        if before["state"] == "executed":
            return {"disposition": before, "replayed": True}
        if before["action"] not in ADJUSTMENT_ACTIONS:
            raise ValidationError("只有数量调整和移位处置需要执行")
        if before["state"] != "awaiting_approval":
            raise ConflictError("处置单不在待执行状态，请重新提交处置", context={"state": before["state"]})
        approval = self.approvals.get(before["approval_request_id"])
        if approval["state"] != "approved":
            raise ConflictError("调整尚未通过两人审批", context={"approval_state": approval["state"]})
        approvers = {row["approver_user_id"] for row in approval["decisions"] if row["decision"] == "approve"}
        if len(approvers) < 2:
            raise ConflictError("数量及位置调整必须经两名不同审批人同意")

        sample = self.samples.get(before["sample_id"])
        baseline = self.inventory.baseline_for_sample(before["session_id"], before["sample_id"])
        new_events = self._drift_events(baseline, before["session_id"], sample["version"])
        if new_events:
            self._cancel_approval(approval["id"], now)
            self.dispositions.mark_needs_rereview(disposition_id, principal.user_id, now)
            after = self.dispositions.get(disposition_id)
            self.audit.record(
                principal,
                "inventory.adjustment.blocked",
                "inventory_disposition",
                str(disposition_id),
                before=before,
                after=after,
                metadata={"new_events": new_events},
            )
            # 库存改动尚未发生；先提交处置单状态与审计，再抛出冲突。
            # 路由事务随后的 rollback 在无活动事务时为空操作。
            self.connection.commit()
            raise ConflictError(
                "盘点快照后样品发生了新的变更，库存未改动，请重新复核后再提交调整",
                context={"new_events": new_events, "disposition_id": disposition_id},
            )

        # 校验通过后所有库存改动与事件写入在同一事务内，任一步失败整体回滚，库存保持原样。
        if before["action"] == "adjust_quantity":
            new_quantity = float(before["target_quantity"])
            if new_quantity < float(sample["reserved_quantity"]):
                raise ConflictError("调整后数量小于已预留（借出未还）数量")
            cursor = self.connection.execute(
                """UPDATE samples SET quantity=?,version=version+1,updated_at=?
                   WHERE id=? AND version=? AND ?>=reserved_quantity""",
                (new_quantity, now, sample["id"], before["expected_version"], new_quantity),
            )
            if cursor.rowcount != 1:
                raise ConflictError("样品版本已变化，请重新复核")
            updated = self.samples.get(sample["id"])
            new_state = updated["lifecycle_state"]
            if new_quantity == 0:
                new_state = "consumed"
            elif new_state == "consumed":
                new_state = "available"
            if new_state != updated["lifecycle_state"]:
                updated = self.samples.set_state(sample["id"], new_state, updated["version"], now)
            self.samples.append_event(
                sample["id"],
                "inventory.adjusted",
                principal.user_id,
                now,
                quantity_delta=round(new_quantity - float(sample["quantity"]), 9),
                from_state=sample["lifecycle_state"],
                to_state=new_state,
                details={
                    "disposition_id": disposition_id,
                    "disposition_code": before["disposition_code"],
                    "session_id": before["session_id"],
                    "approval_request_id": approval["id"],
                    "book_quantity": sample["quantity"],
                    "target_quantity": new_quantity,
                },
            )
        else:
            target = self.locations.get(before["target_location_id"])
            if target["id"] == sample["location_id"]:
                raise ConflictError("样品已在目标位置，无需移位")
            cursor = self.connection.execute(
                """UPDATE samples SET location_id=?,custody_user_id=?,version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (target["id"], principal.user_id, now, sample["id"], before["expected_version"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("样品版本已变化，请重新复核")
            updated = self.samples.get(sample["id"])
            self.samples.append_event(
                sample["id"],
                "inventory.relocated",
                principal.user_id,
                now,
                details={
                    "disposition_id": disposition_id,
                    "disposition_code": before["disposition_code"],
                    "session_id": before["session_id"],
                    "approval_request_id": approval["id"],
                    "from_location_id": sample["location_id"],
                    "to_location_id": target["id"],
                },
            )

        approval_cursor = self.connection.execute(
            "UPDATE approval_requests SET state='executed',version=version+1,updated_at=? WHERE id=? AND state='approved'",
            (now, approval["id"]),
        )
        if approval_cursor.rowcount != 1:
            raise ConflictError("审批状态已变化，终止执行")
        self.dispositions.mark_executed(disposition_id, principal.user_id, now)
        self.inventory.reanchor_baseline(
            before["session_id"], sample["id"], updated["version"],
            self.inventory.last_event_id(sample["id"]),
        )
        result = self.dispositions.get(disposition_id)
        self.audit.record(
            principal,
            "inventory.adjustment.execute",
            "inventory_disposition",
            str(disposition_id),
            before=before,
            after=result,
            metadata={"approval_request_id": approval["id"]},
        )
        return {"disposition": result, "sample": updated, "replayed": False}

    def list_dispositions(self, principal: Principal, session_id: int) -> list[dict[str, Any]]:
        principal.require("inventory.manage")
        self.inventory.get_session(session_id)
        return self.dispositions.list_for_session(session_id)

    def close(self, principal: Principal, session_id: int) -> dict[str, Any]:
        principal.require("inventory.manage")
        before = self.inventory.get_session(session_id)
        if before["state"] != "reconciling":
            raise ConflictError("盘点会话尚未进入差异复核阶段")
        now = to_storage(self.clock.now())
        by_sample = {item["sample_id"]: item for item in self.dispositions.list_for_session(session_id)}
        unresolved: list[dict[str, Any]] = []
        for baseline in self.inventory.baselines(session_id):
            kind = baseline["difference_kind"]
            if not kind:
                continue
            disposition = by_sample.get(baseline["sample_id"])
            if disposition is None:
                unresolved.append({"sample_code": baseline["sample_code"], "reason": "差异尚未处置"})
                continue
            # 复核/报失结论作出之后若又发生借还等业务事件，结论已被冲掉，须重新复核。
            # 已执行的调整在执行时通过了漂移校验且锚点已推进，不再翻案。
            if disposition["state"] == "resolved":
                drift = self._drift_events(
                    baseline, session_id, baseline["current_version"]
                )
                if drift:
                    self.dispositions.mark_needs_rereview(disposition["id"], principal.user_id, now)
                    unresolved.append(
                        {
                            "sample_code": baseline["sample_code"],
                            "reason": "结论后发生新的样品事件，请重新复核",
                        }
                    )
                    continue
            if disposition["state"] not in TERMINAL_DISPOSITION_STATES:
                unresolved.append(
                    {
                        "sample_code": baseline["sample_code"],
                        "reason": f"处置未终结（{disposition['state']}）",
                    }
                )
        if unresolved:
            # 挂起状态先落库（仅状态更新，未触碰库存），随后抛错的事务回滚为空操作。
            self.connection.commit()
            raise ConflictError("仍有未对账的盘点差异", context={"differences": unresolved})
        result = self.inventory.transition(
            session_id, "reconciling", "closed", to_storage(self.clock.now())
        )
        self.audit.record(
            principal,
            "inventory.close",
            "inventory_session",
            str(session_id),
            before=before,
            after=result,
            metadata={"disposition_count": len(by_sample)},
        )
        return result

    def close_without_adjustment(self, principal: Principal, session_id: int) -> dict[str, Any]:
        # 兼容旧入口：关闭前必须全部差异对账。
        return self.close(principal, session_id)


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
