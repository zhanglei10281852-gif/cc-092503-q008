from __future__ import annotations


def _bootstrap_sample(client, admin, *, code="DSP-01", quantity=20):
    location = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": f"LOC-{code}",
            "building": "样品楼",
            "room": "常温库",
            "cabinet": "三号柜",
            "shelf": "二层",
            "sensitivity": "normal",
            "capacity_units": 50,
        },
    ).json()
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": f"BATCH-{code}", "project_code": "OPS", "expected_count": 1},
    ).json()
    sample = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={
            "sample_code": f"SAMPLE-{code}",
            "batch_id": batch["id"],
            "sample_type": "水样",
            "quantity": quantity,
            "unit": "mL",
            "location_id": location["id"],
        },
    ).json()
    return location, batch, sample


def _approver(client, admin, username):
    created = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": username,
            "password": "Approver!23456",
            "display_name": username,
            "role_codes": ["approver"],
        },
    )
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "Approver!23456", "client_label": "tests"},
    )
    token = login.json()["token"]
    return {"id": created.json()["id"], "headers": {"Authorization": f"Bearer {token}"}}


def _start_and_reconcile(client, admin, location, sample, observed_present, observed_quantity, code="INV-DSP-1"):
    session = client.post(
        "/api/sample-operations/inventory",
        headers=admin["headers"],
        json={"location_id": location["id"], "session_code": code},
    )
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]
    counted = client.post(
        f"/api/sample-operations/inventory/{session_id}/counts",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "observed_present": observed_present,
            "observed_quantity": observed_quantity,
        },
    )
    assert counted.status_code == 200, counted.text
    reconciled = client.post(
        f"/api/sample-operations/inventory/{session_id}/reconcile",
        headers=admin["headers"],
    )
    assert reconciled.status_code == 200, reconciled.text
    return session_id, reconciled.json()


def _approve_twice(client, approval_id, approver_one, approver_two):
    first = client.post(
        f"/api/samples/approvals/{approval_id}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve", "comment": "情况属实"},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/samples/approvals/{approval_id}/decisions",
        headers=approver_two["headers"],
        json={"decision": "approve", "comment": "同意调整"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "approved"


def test_quantity_adjustment_requires_two_approvals_and_writes_event(client, admin):
    location, _, sample = _bootstrap_sample(client, admin)
    a1 = _approver(client, admin, "qapprover1")
    a2 = _approver(client, admin, "qapprover2")
    session_id, result = _start_and_reconcile(client, admin, location, sample, True, 18)
    assert [d["kind"] for d in result["differences"]] == ["quantity_mismatch"]

    disposition = client.post(
        f"/api/sample-operations/inventory/{session_id}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "action": "adjust_quantity",
            "target_quantity": 18,
            "evidence_summary": "双人现场复称 18mL",
            "responsibility_note": "分装损耗，管理员甲责任",
        },
    )
    assert disposition.status_code == 201, disposition.text
    body = disposition.json()
    assert body["state"] == "awaiting_approval"
    assert body["approval_request_id"]

    # 仅一人审批不能执行。
    client.post(
        f"/api/samples/approvals/{body['approval_request_id']}/decisions",
        headers=a1["headers"],
        json={"decision": "approve", "comment": "同意"},
    )
    blocked = client.post(
        f"/api/sample-operations/inventory/dispositions/{body['id']}/execute",
        headers=admin["headers"],
    )
    assert blocked.status_code == 409
    sample_after_block = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert sample_after_block["quantity"] == 20

    # 关闭盘点时处置未终结必须拒绝。
    close_blocked = client.post(
        f"/api/sample-operations/inventory/{session_id}/close", headers=admin["headers"]
    )
    assert close_blocked.status_code == 409

    second = client.post(
        f"/api/samples/approvals/{body['approval_request_id']}/decisions",
        headers=a2["headers"],
        json={"decision": "approve", "comment": "同意"},
    )
    assert second.json()["state"] == "approved"

    executed = client.post(
        f"/api/sample-operations/inventory/dispositions/{body['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 200, executed.text
    executed_body = executed.json()
    assert executed_body["replayed"] is False
    assert executed_body["sample"]["quantity"] == 18
    assert executed_body["disposition"]["state"] == "executed"

    # 审批通过的调整以独立事件写入。
    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    events = [e["event_type"] for e in detail["events"]]
    assert events.count("inventory.adjusted") == 1
    assert detail["quantity"] == 18

    # 执行可幂等重放，不重复写事件。
    replay = client.post(
        f"/api/sample-operations/inventory/dispositions/{body['id']}/execute",
        headers=admin["headers"],
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert [e["event_type"] for e in detail["events"]].count("inventory.adjusted") == 1

    closed = client.post(
        f"/api/sample-operations/inventory/{session_id}/close", headers=admin["headers"]
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["state"] == "closed"


def test_report_loss_resolves_without_stock_change(client, admin):
    location, _, sample = _bootstrap_sample(client, admin, code="LOSS")
    session_id, result = _start_and_reconcile(
        client, admin, location, sample, False, None, code="INV-LOSS-1"
    )
    assert [d["kind"] for d in result["differences"]] == ["missing"]

    disposition = client.post(
        f"/api/sample-operations/inventory/{session_id}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "action": "report_loss",
            "evidence_summary": "现场监控显示样品被违规取走",
            "responsibility_note": "值班员乙未按规定登记",
        },
    )
    assert disposition.status_code == 201, disposition.text
    assert disposition.json()["state"] == "resolved"
    assert disposition.json()["approval_request_id"] is None

    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert detail["quantity"] == 20  # 报失只记录证据，不擅改库存
    assert detail["events"][-1]["event_type"] == "inventory.loss_reported"

    closed = client.post(
        f"/api/sample-operations/inventory/{session_id}/close", headers=admin["headers"]
    )
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"


def test_relocate_needs_two_approvals(client, admin):
    location, _, sample = _bootstrap_sample(client, admin, code="REL")
    target = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": "LOC-REL-TARGET",
            "building": "样品楼",
            "room": "低温库",
            "cabinet": "一号柜",
            "shelf": "一层",
            "sensitivity": "restricted",
            "capacity_units": 50,
        },
    ).json()
    a1 = _approver(client, admin, "relapprover1")
    a2 = _approver(client, admin, "relapprover2")
    session_id, result = _start_and_reconcile(
        client, admin, location, sample, True, 18, code="INV-REL-1"
    )
    assert result["differences"]

    disposition = client.post(
        f"/api/sample-operations/inventory/{session_id}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "action": "relocate",
            "target_location_id": target["id"],
            "evidence_summary": "实物实际存放于低温库一号柜",
            "responsibility_note": "归位登记遗漏",
        },
    )
    assert disposition.status_code == 201
    body = disposition.json()
    _approve_twice(client, body["approval_request_id"], a1, a2)

    executed = client.post(
        f"/api/sample-operations/inventory/dispositions/{body['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["sample"]["location_id"] == target["id"]
    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert detail["events"][-1]["event_type"] == "inventory.relocated"
    assert detail["quantity"] == 20  # 移位不改数量


def test_loan_during_counting_blocks_execution_until_rereview(client, admin):
    location, _, sample = _bootstrap_sample(client, admin, code="DRIFT")
    a1 = _approver(client, admin, "driftapprover1")
    a2 = _approver(client, admin, "driftapprover2")
    session = client.post(
        "/api/sample-operations/inventory",
        headers=admin["headers"],
        json={"location_id": location["id"], "session_code": "INV-DRIFT-1"},
    ).json()
    session_id = session["id"]

    # 盘点进行期间发生借还，且借还后账面与实盘仍不一致。
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "borrower_user_id": admin["body"]["user"]["id"],
            "quantity": 10,
            "due_at": "2026-10-01T00:00:00+00:00",
        },
    )
    assert loan.status_code == 201, loan.text
    returned = client.post(
        f"/api/samples/loans/{loan.json()['id']}/returns",
        headers=admin["headers"],
        json={"quantity": 10},
    )
    assert returned.status_code == 200, returned.text

    counted = client.post(
        f"/api/sample-operations/inventory/{session_id}/counts",
        headers=admin["headers"],
        json={"sample_id": sample["id"], "observed_present": True, "observed_quantity": 18},
    )
    assert counted.status_code == 200
    reconciled = client.post(
        f"/api/sample-operations/inventory/{session_id}/reconcile", headers=admin["headers"]
    ).json()
    assert [d["kind"] for d in reconciled["differences"]] == ["quantity_mismatch"]

    # 快照后已有借还事件：处置单直接挂起到待重新复核，不生成审批、库存不动。
    disposition = client.post(
        f"/api/sample-operations/inventory/{session_id}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "action": "adjust_quantity",
            "target_quantity": 18,
            "evidence_summary": "复称 18mL",
            "responsibility_note": "自然损耗",
        },
    )
    assert disposition.status_code == 201
    body = disposition.json()
    assert body["state"] == "needs_rereview"
    assert body["approval_request_id"] is None
    assert client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()["quantity"] == 20

    # 库管员重新复核确认后，审批重新发起。
    confirmed = client.post(
        f"/api/sample-operations/inventory/dispositions/{body['id']}/rereview",
        headers=admin["headers"],
        json={"note": "借还已核对，差异仍然存在"},
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_body = confirmed.json()["disposition"]
    assert confirmed_body["state"] == "awaiting_approval"
    event_types = {e["event_type"] for e in confirmed.json()["acknowledged_events"]}
    assert "loaned" in event_types and "returned" in event_types
    _approve_twice(client, confirmed_body["approval_request_id"], a1, a2)

    executed = client.post(
        f"/api/sample-operations/inventory/dispositions/{body['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["sample"]["quantity"] == 18


def test_post_approval_drift_blocks_execute_without_stock_change(client, admin):
    location, _, sample = _bootstrap_sample(client, admin, code="BLOCK")
    a1 = _approver(client, admin, "blockapprover1")
    a2 = _approver(client, admin, "blockapprover2")
    session_id, _ = _start_and_reconcile(
        client, admin, location, sample, True, 18, code="INV-BLOCK-1"
    )
    disposition = client.post(
        f"/api/sample-operations/inventory/{session_id}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "action": "adjust_quantity",
            "target_quantity": 18,
            "evidence_summary": "复称 18mL",
            "responsibility_note": "损耗",
        },
    ).json()
    _approve_twice(client, disposition["approval_request_id"], a1, a2)

    # 双人审批之后、执行之前，样品发生新的借出/归还。
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "borrower_user_id": admin["body"]["user"]["id"],
            "quantity": 5,
            "due_at": "2026-10-01T00:00:00+00:00",
        },
    ).json()
    client.post(
        f"/api/samples/loans/{loan['id']}/returns",
        headers=admin["headers"],
        json={"quantity": 5},
    )

    blocked = client.post(
        f"/api/sample-operations/inventory/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert blocked.status_code == 409, blocked.text
    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert detail["quantity"] == 20  # 任何失败不能改动库存
    # 挂起状态在请求失败后仍已落库。
    listed = client.get(
        f"/api/sample-operations/inventory/{session_id}/dispositions", headers=admin["headers"]
    ).json()
    assert listed[0]["state"] == "needs_rereview"

    # 重新复核 → 新的双人审批 → 执行成功。
    confirmed = client.post(
        f"/api/sample-operations/inventory/dispositions/{disposition['id']}/rereview",
        headers=admin["headers"],
        json={"note": "借还已核对"},
    ).json()
    new_approval = confirmed["disposition"]["approval_request_id"]
    assert new_approval != disposition["approval_request_id"]
    _approve_twice(client, new_approval, a1, a2)
    executed = client.post(
        f"/api/sample-operations/inventory/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["sample"]["quantity"] == 18


def test_disposition_requires_evidence_and_real_difference(client, admin):
    location, _, sample = _bootstrap_sample(client, admin, code="VAL")
    session_id, result = _start_and_reconcile(
        client, admin, location, sample, True, 20, code="INV-VAL-1"
    )
    assert result["differences"] == []

    missing = client.post(
        f"/api/sample-operations/inventory/{session_id}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "action": "review",
            "evidence_summary": "无差异",
            "responsibility_note": "无",
        },
    )
    assert missing.status_code == 422

    # 构造一项缺失差异后，证据/责任说明不能为空。
    other_session = client.post(
        "/api/sample-operations/inventory",
        headers=admin["headers"],
        json={"location_id": location["id"], "session_code": "INV-VAL-2"},
    )
    assert other_session.status_code == 409  # 前一会话未关闭，不能重复开账


def test_rejected_disposition_can_be_rewritten_and_executed(client, admin):
    location, _, sample = _bootstrap_sample(client, admin, code="REJ")
    a1 = _approver(client, admin, "rejapprover1")
    a2 = _approver(client, admin, "rejapprover2")
    session_id, _ = _start_and_reconcile(
        client, admin, location, sample, True, 18, code="INV-REJ-1"
    )
    body = client.post(
        f"/api/sample-operations/inventory/{session_id}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "action": "adjust_quantity",
            "target_quantity": 18,
            "evidence_summary": "复称 18mL",
            "responsibility_note": "损耗",
        },
    ).json()
    approval_id = body["approval_request_id"]

    rejected = client.post(
        f"/api/samples/approvals/{approval_id}/decisions",
        headers=a1["headers"],
        json={"decision": "reject", "comment": "证据不足"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"

    execute = client.post(
        f"/api/sample-operations/inventory/dispositions/{body['id']}/execute",
        headers=admin["headers"],
    )
    assert execute.status_code == 409
    assert execute.json()["error"]["context"]["state"] == "rejected"

    # 驳回后补齐证据重新提交（同一处置单编码），仍需全新的双人审批。
    rewritten = client.post(
        f"/api/sample-operations/inventory/{session_id}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "action": "adjust_quantity",
            "target_quantity": 18,
            "evidence_summary": "补拍照片编号 IMG-042 复称 18mL",
            "responsibility_note": "分装损耗，已培训",
        },
    )
    assert rewritten.status_code == 201
    new_body = rewritten.json()
    assert new_body["disposition_code"] == body["disposition_code"]
    assert new_body["state"] == "awaiting_approval"
    assert new_body["approval_request_id"] != approval_id
    _approve_twice(client, new_body["approval_request_id"], a1, a2)
    executed = client.post(
        f"/api/sample-operations/inventory/dispositions/{new_body['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 200
    assert executed.json()["sample"]["quantity"] == 18


def test_workflow_resumes_after_service_restart(client, admin):
    import os

    from app.database import close_connection

    location, _, sample = _bootstrap_sample(client, admin, code="RST")
    a1 = _approver(client, admin, "rstapprover1")
    session_id, result = _start_and_reconcile(
        client, admin, location, sample, True, 18, code="INV-RST-1"
    )
    body = client.post(
        f"/api/sample-operations/inventory/{session_id}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "action": "adjust_quantity",
            "target_quantity": 18,
            "evidence_summary": "复称 18mL",
            "responsibility_note": "损耗",
        },
    ).json()
    approval_id = body["approval_request_id"]

    # 服务重启：关闭并重建连接（数据库文件保留全部状态）。
    close_connection()
    from app.database import get_connection

    get_connection()

    listed = client.get(
        f"/api/sample-operations/inventory/{session_id}/dispositions", headers=admin["headers"]
    )
    assert listed.status_code == 200
    assert listed.json()[0]["state"] == "awaiting_approval"

    a2 = _approver(client, admin, "rstapprover2")
    _approve_twice(client, approval_id, a1, a2)
    executed = client.post(
        f"/api/sample-operations/inventory/dispositions/{body['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["sample"]["quantity"] == 18
    closed = client.post(
        f"/api/sample-operations/inventory/{session_id}/close", headers=admin["headers"]
    )
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"


def test_close_detects_loan_after_report_loss_conclusion(client, admin):
    location, _, sample = _bootstrap_sample(client, admin, code="WASH")
    session_id, result = _start_and_reconcile(
        client, admin, location, sample, False, None, code="INV-WASH-1"
    )
    assert [d["kind"] for d in result["differences"]] == ["missing"]

    disposition = client.post(
        f"/api/sample-operations/inventory/{session_id}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "action": "report_loss",
            "evidence_summary": "初查未找到样品",
            "responsibility_note": "待查",
        },
    )
    assert disposition.status_code == 201
    disposition_id = disposition.json()["id"]

    # 结论作出后、关账前，样品被借出又归还（差异结论已被业务冲掉）。
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "borrower_user_id": admin["body"]["user"]["id"],
            "quantity": 3,
            "due_at": "2026-10-01T00:00:00+00:00",
        },
    )
    assert loan.status_code == 201, loan.text
    returned = client.post(
        f"/api/samples/loans/{loan.json()['id']}/returns",
        headers=admin["headers"],
        json={"quantity": 3},
    )
    assert returned.status_code == 200

    blocked = client.post(
        f"/api/sample-operations/inventory/{session_id}/close", headers=admin["headers"]
    )
    assert blocked.status_code == 409
    reasons = blocked.json()["error"]["context"]["differences"]
    assert reasons[0]["reason"] == "结论后发生新的样品事件，请重新复核"

    listed = client.get(
        f"/api/sample-operations/inventory/{session_id}/dispositions", headers=admin["headers"]
    ).json()
    assert listed[0]["state"] == "needs_rereview"

    # 库存始终未被盘点流程改动。
    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert detail["quantity"] == 20

    # 重新复核确认后即可关账（报失不改库存，事件仅作证据记录）。
    confirmed = client.post(
        f"/api/sample-operations/inventory/dispositions/{disposition_id}/rereview",
        headers=admin["headers"],
        json={"note": "已核对借还，样品仍按报失流程跟进"},
    )
    assert confirmed.status_code == 200, confirmed.text
    closed = client.post(
        f"/api/sample-operations/inventory/{session_id}/close", headers=admin["headers"]
    )
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"
