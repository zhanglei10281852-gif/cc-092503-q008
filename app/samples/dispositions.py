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

DISPOSITION_REVIEW = "review"
DISPOSITION_RELOCATE = "relocate"
DISPOSITION_QUANTITY = "quantity_adjust"
DISPOSITION_LOSS = "report_loss"

APPROVAL_REQUIRED = {DISPOSITION_RELOCATE, DISPOSITION_QUANTITY}

STATE_IN_REVIEW = "in_review"
STATE_PENDING = "pending_approval"
STATE_REJECTED = "rejected"
STATE_READY = "ready"
STATE_EXECUTED = "executed"


def _evidence_digest(data: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "session_id": data["session_id"],
            "sample_id": data["sample_id"],
            "disposition": data["disposition"],
            "target_quantity": data.get("target_quantity"),
            "target_location_id": data.get("target_location_id"),
            "evidence_summary": data["evidence_summary"],
            "responsibility_note": data["responsibility_note"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DispositionRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def difference(self, session_id: int, sample_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM inventory_differences WHERE session_id=? AND sample_id=?",
            (session_id, sample_id),
        ).fetchone()
        return dict(row) if row else None

    def list_differences(self, session_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT d.*,s.sample_code,s.quantity AS current_quantity,s.version AS sample_version,
                          s.location_id AS current_location_id,s.reserved_quantity,
                          b.last_event_id,
                          dp.id AS disposition_id,dp.state AS disposition_state,
                          dp.disposition AS disposition_type
                   FROM inventory_differences d
                   JOIN samples s ON s.id=d.sample_id
                   JOIN inventory_snapshot_baselines b ON b.session_id=d.session_id AND b.sample_id=d.sample_id
                   LEFT JOIN discrepancy_dispositions dp ON dp.session_id=d.session_id AND dp.sample_id=d.sample_id
                   WHERE d.session_id=? ORDER BY s.sample_code""",
                (session_id,),
            ).fetchall()
        ]

    def get(self, disposition_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            """SELECT dp.*,s.sample_code,s.quantity AS current_quantity,s.reserved_quantity,
                      s.version AS sample_version,s.location_id AS current_location_id,
                      i.session_code,i.location_id AS session_location_id,
                      d.difference_kind,d.book_quantity,d.observed_quantity,d.quantity_delta,
                      b.last_event_id
               FROM discrepancy_dispositions dp
               JOIN inventory_sessions i ON i.id=dp.session_id
               JOIN inventory_differences d ON d.session_id=dp.session_id AND d.sample_id=dp.sample_id
               JOIN inventory_snapshot_baselines b ON b.session_id=dp.session_id AND b.sample_id=dp.sample_id
               JOIN samples s ON s.id=dp.sample_id
               WHERE dp.id=?""",
            (disposition_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("差异处置单不存在")
        item = dict(row)
        item["approvals"] = [
            dict(decision)
            for decision in self.connection.execute(
                "SELECT * FROM approval_decisions WHERE request_id=? ORDER BY id",
                (item["approval_request_id"],),
            ).fetchall()
        ] if item["approval_request_id"] else []
        return item

    def by_approval(self, request_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM discrepancy_dispositions WHERE approval_request_id=?",
            (request_id,),
        ).fetchone()
        return dict(row) if row else None

    def latest_event_id(self, sample_id: int) -> int:
        return int(
            self.connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM sample_events WHERE sample_id=?",
                (sample_id,),
            ).fetchone()[0]
        )

    def create(
        self,
        *,
        session_id: int,
        sample_id: int,
        difference_kind: str,
        book_quantity: float,
        observed_quantity: float | None,
        disposition: str,
        target_quantity: float | None,
        target_location_id: int | None,
        evidence_summary: str,
        responsibility_note: str,
        created_by: int,
        requires_approval: bool,
        reviewed_event_id: int,
        now: str,
    ) -> dict[str, Any]:
        code = f"DISP-{uuid.uuid4().hex[:12]}"
        cursor = self.connection.execute(
            """INSERT INTO discrepancy_dispositions(
                   disposition_code,session_id,sample_id,difference_kind,book_quantity,observed_quantity,
                   disposition,state,target_quantity,target_location_id,evidence_summary,responsibility_note,
                   created_by,requires_approval,reviewed_event_id,version,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (
                code, session_id, sample_id, difference_kind, book_quantity, observed_quantity,
                disposition, 'in_review', target_quantity, target_location_id,
                evidence_summary, responsibility_note, created_by, int(requires_approval),
                reviewed_event_id, now, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def update_fields(self, disposition_id: int, fields: dict[str, Any], now: str) -> None:
        assignments = ", ".join(f"{key}=?" for key in fields)
        self.connection.execute(
            f"UPDATE discrepancy_dispositions SET {assignments},state=?,version=version+1,updated_at=? WHERE id=?",
            (*fields.values(), STATE_IN_REVIEW, now, disposition_id),
        )

    def set_state(self, disposition_id: int, expected: str | tuple[str, ...], state: str, now: str) -> None:
        if isinstance(expected, str):
            expected_states = (expected,)
        else:
            expected_states = expected
        placeholders = ",".join("?" for _ in expected_states)
        cursor = self.connection.execute(
            f"UPDATE discrepancy_dispositions SET state=?,version=version+1,updated_at=? WHERE id=? AND state IN ({placeholders})",
            (state, now, disposition_id, *expected_states),
        )
        if cursor.rowcount != 1:
            raise ConflictError("差异处置单状态已变化，请刷新后重试")

    def attach_approval(self, disposition_id: int, request_id: int, state: str, now: str) -> None:
        self.connection.execute(
            "UPDATE discrepancy_dispositions SET approval_request_id=?,state=?,version=version+1,updated_at=? WHERE id=?",
            (request_id, state, now, disposition_id),
        )

    def mark_reviewed(self, disposition_id: int, reviewed_event_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE discrepancy_dispositions SET reviewed_event_id=?,version=version+1,updated_at=? WHERE id=?",
            (reviewed_event_id, now, disposition_id),
        )

    def mark_executed(self, disposition_id: int, executed_by: int, event_id: int, now: str) -> None:
        self.connection.execute(
            """UPDATE discrepancy_dispositions
               SET state='executed',executed_by=?,executed_at=?,executed_event_id=?,version=version+1,updated_at=?
               WHERE id=?""",
            (executed_by, now, event_id, now, disposition_id),
        )


class DispositionService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repo = DispositionRepository(connection)
        self.samples = SampleRepository(connection)
        self.locations = LocationRepository(connection)
        self.approvals = ApprovalRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ helpers

    def _session(self, session_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM inventory_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("盘点会话不存在")
        return dict(row)

    def _require_editable(self, item: dict[str, Any]) -> None:
        if item["state"] not in {STATE_IN_REVIEW, STATE_REJECTED}:
            raise ConflictError("差异处置单已提交审批或执行，不能修改")

    def _validate_targets(
        self,
        difference: dict[str, Any],
        sample: dict[str, Any],
        disposition: str,
        target_quantity: float | None,
        target_location_id: int | None,
    ) -> tuple[float | None, int | None]:
        observed = difference["observed_quantity"]
        observed_or_zero = float(observed) if observed is not None else 0.0
        if disposition == DISPOSITION_QUANTITY:
            if target_quantity is None:
                raise ValidationError("数量调整必须填写确认数量")
            if abs(target_quantity - float(sample["quantity"])) < 1e-9:
                raise ValidationError("确认数量与账面数量一致，无需数量调整")
            if target_quantity < float(sample["reserved_quantity"]) - 1e-9:
                raise ConflictError("确认数量不能低于已借出预留数量")
            target_location_id = None
        elif disposition == DISPOSITION_RELOCATE:
            if not target_location_id:
                raise ValidationError("移位必须填写目标位置")
            if sample["lifecycle_state"] in {"loaned", "pending_destruction", "destroyed"}:
                raise ConflictError("样品当前状态禁止移位")
            target = self.locations.get(target_location_id)
            if target["id"] == sample["location_id"]:
                raise ValidationError("目标位置与当前位置一致，无需移位")
            target_quantity = None
        elif disposition == DISPOSITION_LOSS:
            if observed_or_zero >= float(sample["quantity"]) - 1e-9:
                raise ValidationError("实盘数量不低于账面数量，不能按报失处理")
            if observed_or_zero < float(sample["reserved_quantity"]) - 1e-9:
                raise ConflictError("样品仍有未归还的借用预留，报失数量不能低于借出数量")
            target_quantity = observed_or_zero
            target_location_id = None
        else:
            target_quantity = None
            target_location_id = None
        return target_quantity, target_location_id

    # ------------------------------------------------------------------ create/update

    def create(self, principal: Principal, session_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("inventory.dispose")
        session = self._session(session_id)
        if session["state"] != "reconciling":
            raise ConflictError("只有差异复核阶段的盘点可以登记处置单")
        difference = self.repo.difference(session_id, data["sample_id"])
        if difference is None:
            raise NotFoundError("该样品在本次盘点中没有差异，无需处置")
        existing = self.connection.execute(
            "SELECT id FROM discrepancy_dispositions WHERE session_id=? AND sample_id=?",
            (session_id, data["sample_id"]),
        ).fetchone()
        if existing:
            raise ConflictError("该差异已存在处置单")
        sample = self.samples.get(data["sample_id"])
        disposition = data["disposition"]
        target_quantity, target_location_id = self._validate_targets(
            difference, sample, disposition, data.get("target_quantity"), data.get("target_location_id")
        )
        now = to_storage(self.clock.now())
        values = {
            "session_id": session_id,
            "sample_id": data["sample_id"],
            "disposition": disposition,
            "target_quantity": target_quantity,
            "target_location_id": target_location_id,
            "evidence_summary": data["evidence_summary"].strip(),
            "responsibility_note": data["responsibility_note"].strip(),
        }
        digest = _evidence_digest(values)
        item = self.repo.create(
            session_id=session_id,
            sample_id=data["sample_id"],
            difference_kind=difference["difference_kind"],
            book_quantity=difference["book_quantity"],
            observed_quantity=difference["observed_quantity"],
            disposition=disposition,
            target_quantity=target_quantity,
            target_location_id=target_location_id,
            evidence_summary=data["evidence_summary"].strip(),
            responsibility_note=data["responsibility_note"].strip(),
            created_by=principal.user_id,
            requires_approval=disposition in APPROVAL_REQUIRED,
            reviewed_event_id=self.repo.latest_event_id(data["sample_id"]),
            now=now,
        )
        self.audit.record(
            principal,
            "inventory.disposition.create",
            "discrepancy_disposition",
            str(item["id"]),
            after=item,
            metadata={"evidence_digest": digest, "disposition_code": item["disposition_code"]},
        )
        return item

    def update(self, principal: Principal, disposition_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("inventory.dispose")
        before = self.repo.get(disposition_id)
        self._require_editable(before)
        session = self._session(before["session_id"])
        if session["state"] != "reconciling":
            raise ConflictError("盘点已结束，不能修改处置单")
        merged = {
            "disposition": data.get("disposition", before["disposition"]),
            "target_quantity": data.get("target_quantity", before["target_quantity"]),
            "target_location_id": data.get("target_location_id", before["target_location_id"]),
            "evidence_summary": (data.get("evidence_summary") or before["evidence_summary"]).strip(),
            "responsibility_note": (data.get("responsibility_note") or before["responsibility_note"]).strip(),
        }
        sample = self.samples.get(before["sample_id"])
        difference = {
            "observed_quantity": before["observed_quantity"],
        }
        target_quantity, target_location_id = self._validate_targets(
            difference, sample, merged["disposition"], merged["target_quantity"], merged["target_location_id"]
        )
        now = to_storage(self.clock.now())
        if before.get("approval_request_id"):
            self.connection.execute(
                "UPDATE approval_requests SET state='cancelled',version=version+1,updated_at=? WHERE id=? AND state IN ('pending','approved','rejected')",
                (now, before["approval_request_id"]),
            )
        self.repo.update_fields(
            disposition_id,
            {
                "disposition": merged["disposition"],
                "target_quantity": target_quantity,
                "target_location_id": target_location_id,
                "evidence_summary": merged["evidence_summary"],
                "responsibility_note": merged["responsibility_note"],
                "requires_approval": int(merged["disposition"] in APPROVAL_REQUIRED),
                "approval_request_id": None,
            },
            now,
        )
        after = self.repo.get(disposition_id)
        self.audit.record(
            principal,
            "inventory.disposition.update",
            "discrepancy_disposition",
            str(disposition_id),
            before=before,
            after=after,
        )
        return after

    # ------------------------------------------------------------------ review

    def verify(self, principal: Principal, disposition_id: int, comment: str = "") -> dict[str, Any]:
        principal.require("inventory.dispose")
        before = self.repo.get(disposition_id)
        if before["state"] not in {STATE_IN_REVIEW, STATE_REJECTED, STATE_READY}:
            raise ConflictError("处置单仍在审批中，暂时不能复核")
        session = self._session(before["session_id"])
        if session["state"] != "reconciling":
            raise ConflictError("盘点已结束，不能复核处置单")
        now = to_storage(self.clock.now())
        latest_event_id = self.repo.latest_event_id(before["sample_id"])
        stale = latest_event_id > int(before["reviewed_event_id"])
        if before["state"] == STATE_READY and not stale:
            raise ConflictError("处置单已完成复核，等待执行或关账")
        if before["disposition"] in APPROVAL_REQUIRED:
            # 复核过期（盘点期间出现新的样品事件）时，旧审批结论作废，需要再次经过两人审批
            if before["approval_request_id"]:
                self.connection.execute(
                    "UPDATE approval_requests SET state='cancelled',version=version+1,updated_at=? WHERE id=? AND state IN ('pending','approved','rejected')",
                    (now, before["approval_request_id"]),
                )
            request = self.approvals.create(
                {
                    "action_type": "inventory_adjustment",
                    "resource_type": "discrepancy_disposition",
                    "resource_id": disposition_id,
                    "payload": {
                        "disposition_code": before["disposition_code"],
                        "session_id": before["session_id"],
                        "sample_id": before["sample_id"],
                        "disposition": before["disposition"],
                        "target_quantity": before["target_quantity"],
                        "target_location_id": before["target_location_id"],
                        "evidence_digest": _evidence_digest(
                            {
                                "session_id": before["session_id"],
                                "sample_id": before["sample_id"],
                                "disposition": before["disposition"],
                                "target_quantity": before["target_quantity"],
                                "target_location_id": before["target_location_id"],
                                "evidence_summary": before["evidence_summary"],
                                "responsibility_note": before["responsibility_note"],
                            }
                        ),
                    },
                    "expires_at": to_storage(self.clock.now() + timedelta(days=3)),
                },
                principal.user_id,
                f"APR-DISP-{uuid.uuid4().hex[:10]}",
                now,
            )
            self.repo.attach_approval(disposition_id, request["id"], STATE_PENDING, now)
        else:
            self.repo.set_state(
                disposition_id, (STATE_IN_REVIEW, STATE_READY), STATE_READY, now
            )
        self.repo.mark_reviewed(disposition_id, latest_event_id, now)
        after = self.repo.get(disposition_id)
        self.audit.record(
            principal,
            "inventory.disposition.verify",
            "discrepancy_disposition",
            str(disposition_id),
            before=before,
            after=after,
            metadata={"comment": comment, "latest_event_id": latest_event_id},
        )
        return after

    def mark_approved(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """审批通过后由 ApprovalService 回调，把处置单推进到待执行。"""
        if request["state"] != "approved" or request["resource_type"] != "discrepancy_disposition":
            return None
        link = self.repo.by_approval(request["id"])
        if link is None:
            return None
        item = self.repo.get(link["id"])
        if item["state"] == STATE_PENDING:
            now = to_storage(self.clock.now())
            self.repo.set_state(item["id"], STATE_PENDING, STATE_READY, now)
            item = self.repo.get(item["id"])
        return item

    def mark_rejected(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if request["state"] != "rejected" or request["resource_type"] != "discrepancy_disposition":
            return None
        link = self.repo.by_approval(request["id"])
        if link is None:
            return None
        before = self.repo.get(link["id"])
        if before["state"] == STATE_PENDING:
            now = to_storage(self.clock.now())
            self.repo.set_state(link["id"], STATE_PENDING, STATE_REJECTED, now)
            return self.repo.get(link["id"])
        return None

    # ------------------------------------------------------------------ execute

    def execute(self, principal: Principal, disposition_id: int) -> dict[str, Any]:
        principal.require("inventory.execute")
        before = self.repo.get(disposition_id)
        if before["disposition"] == DISPOSITION_REVIEW:
            raise ConflictError("复核结论不改动库存，无需执行")
        if before["state"] == STATE_EXECUTED:
            return {"disposition": before, "replayed": True}
        if before["state"] != STATE_READY:
            raise ConflictError("处置单尚未完成复核与双人审批，不能执行")
        session = self._session(before["session_id"])
        if session["state"] != "reconciling":
            raise ConflictError("盘点会话已结束，处置单不能再执行")
        now = to_storage(self.clock.now())

        # 执行时校验复核水位之后是否出现新的样品事件（借还、转移、消耗等）
        latest_event_id = self.repo.latest_event_id(before["sample_id"])
        if latest_event_id > before["reviewed_event_id"]:
            raise ConflictError(
                "盘点复核后样品发生了新的借还或移位事件，请重新复核后再执行",
                context={"reviewed_event_id": before["reviewed_event_id"], "latest_event_id": latest_event_id},
            )

        sample = self.samples.get(before["sample_id"])
        disposition = before["disposition"]
        event_type: str
        details: dict[str, Any] = {
            "disposition_id": disposition_id,
            "disposition_code": before["disposition_code"],
            "session_id": before["session_id"],
            "evidence_summary": before["evidence_summary"],
            "responsibility_note": before["responsibility_note"],
        }
        quantity_delta = 0.0
        from_state = sample["lifecycle_state"]
        to_state: str | None = None

        if disposition == DISPOSITION_QUANTITY:
            target_quantity = float(before["target_quantity"])
            quantity_delta = round(target_quantity - float(sample["quantity"]), 9)
            if abs(quantity_delta) < 1e-9:
                raise ConflictError("账面数量已与审批数量一致，无需调整")
            updated = self.samples.change_quantity(sample["id"], quantity_delta, sample["version"], now)
            event_type = "inventory.quantity_adjusted"
            details["target_quantity"] = target_quantity
            if target_quantity == 0 and float(updated["reserved_quantity"]) == 0:
                to_state = "consumed"
                updated = self.samples.set_state(sample["id"], to_state, updated["version"], now)
            sample_after = updated
        elif disposition == DISPOSITION_RELOCATE:
            target = self.locations.get(before["target_location_id"])
            cursor = self.connection.execute(
                """UPDATE samples SET location_id=?,custody_user_id=?,version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (target["id"], principal.user_id, now, sample["id"], sample["version"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("样品位置或版本已变化，请刷新后重试")
            event_type = "inventory.relocated"
            details["from_location_id"] = sample["location_id"]
            details["to_location_id"] = target["id"]
            sample_after = self.samples.get(sample["id"])
        elif disposition == DISPOSITION_LOSS:
            target_quantity = float(before["target_quantity"])
            quantity_delta = round(target_quantity - float(sample["quantity"]), 9)
            if quantity_delta >= -1e-9:
                raise ConflictError("账面数量已不高于报失数量，无需报失")
            updated = self.samples.change_quantity(sample["id"], quantity_delta, sample["version"], now)
            if target_quantity == 0 and float(updated["reserved_quantity"]) == 0:
                to_state = "consumed"
                updated = self.samples.set_state(sample["id"], to_state, updated["version"], now)
            case_code = f"ANM-LOSS-{uuid.uuid4().hex[:10]}"
            case_cursor = self.connection.execute(
                """INSERT INTO anomaly_cases(case_code,sample_id,batch_id,anomaly_type,severity,state,
                      detected_by,description,created_at,updated_at)
                   VALUES(?,?,?,'inventory_loss','high','open',?,?,?,?)""",
                (
                    case_code, sample["id"], sample["batch_id"], principal.user_id,
                    f"盘点报失：{before['disposition_code']}；证据：{before['evidence_summary']}；责任说明：{before['responsibility_note']}",
                    now, now,
                ),
            )
            event_type = "inventory.loss_reported"
            details["target_quantity"] = target_quantity
            details["anomaly_case_id"] = case_cursor.lastrowid
            sample_after = updated
            self.connection.execute(
                "UPDATE discrepancy_dispositions SET loss_case_id=? WHERE id=?",
                (case_cursor.lastrowid, disposition_id),
            )
        else:  # pragma: no cover - 防御性分支
            raise ConflictError("不支持的处置类型")

        details["approval_request_id"] = before["approval_request_id"]
        self.samples.append_event(
            sample["id"],
            event_type,
            principal.user_id,
            now,
            quantity_delta=quantity_delta,
            from_state=from_state,
            to_state=to_state,
            details=details,
            correlation_id=before["disposition_code"],
        )
        event_id = self.connection.execute(
            "SELECT MAX(id) FROM sample_events WHERE sample_id=?", (sample["id"],)
        ).fetchone()[0]
        self.repo.mark_executed(disposition_id, principal.user_id, int(event_id), now)
        if before["approval_request_id"]:
            self.connection.execute(
                "UPDATE approval_requests SET state='executed',version=version+1,updated_at=? WHERE id=?",
                (now, before["approval_request_id"]),
            )
        after = self.repo.get(disposition_id)
        self.audit.record(
            principal,
            "inventory.disposition.execute",
            "discrepancy_disposition",
            str(disposition_id),
            before=before,
            after=after,
            metadata={"event_id": event_id, "event_type": event_type},
        )
        return {"disposition": after, "sample": sample_after, "event_id": event_id, "replayed": False}

    # ------------------------------------------------------------------ queries

    def list(self, principal: Principal, session_id: int) -> dict[str, Any]:
        principal.require("inventory.manage")
        session = self._session(session_id)
        rows = self.repo.list_differences(session_id)
        return {"session": session, "differences": rows}

    def detail(self, principal: Principal, disposition_id: int) -> dict[str, Any]:
        principal.require("inventory.manage")
        return self.repo.get(disposition_id)
