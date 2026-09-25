from __future__ import annotations


def _login(client, username: str, password: str = "Approver!23"):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password, "client_label": "tests"},
    )
    assert response.status_code == 200, response.text
    return {"headers": {"Authorization": f"Bearer {response.json()['token']}"}, "body": response.json()}


def _approver(client, admin, username: str):
    created = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": username,
            "password": "Approver!23",
            "display_name": f"审批人{username}",
            "role_codes": ["approver"],
        },
    )
    assert created.status_code == 201, created.text
    return _login(client, username)


def _setup_counted_session(client, admin, *, observed_quantity, code="DSP-01", sample_quantity=100):
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
        json={"batch_code": f"BATCH-{code}", "project_code": "DSP", "expected_count": 1},
    ).json()
    sample = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={
            "sample_code": f"S-{code}",
            "batch_id": batch["id"],
            "sample_type": "水样",
            "quantity": sample_quantity,
            "unit": "mL",
            "location_id": location["id"],
        },
    ).json()
    session = client.post(
        "/api/sample-operations/inventory",
        headers=admin["headers"],
        json={"location_id": location["id"], "session_code": f"INV-{code}"},
    ).json()
    count = client.post(
        f"/api/sample-operations/inventory/{session['id']}/counts",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "observed_present": observed_quantity is not None,
            "observed_quantity": observed_quantity,
        },
    )
    assert count.status_code == 200, count.text
    reconciled = client.post(
        f"/api/sample-operations/inventory/{session['id']}/reconcile",
        headers=admin["headers"],
    ).json()
    return location, batch, sample, session, reconciled


def _approve_twice(client, approver_one, approver_two, approval_id):
    first = client.post(
        f"/api/samples/approvals/{approval_id}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve", "comment": "情况属实"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "pending"
    second = client.post(
        f"/api/samples/approvals/{approval_id}/decisions",
        headers=approver_two["headers"],
        json={"decision": "approve", "comment": "同意调整"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "approved"
    return second.json()


def test_quantity_adjustment_requires_two_approvals_and_writes_event(client, admin):
    approver_one = _approver(client, admin, "appr_one")
    approver_two = _approver(client, admin, "appr_two")
    _, _, sample, session, reconciled = _setup_counted_session(client, admin, observed_quantity=90)
    assert [item["kind"] for item in reconciled["differences"]] == ["quantity_mismatch"]

    created = client.post(
        f"/api/sample-operations/inventory/{session['id']}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "disposition": "quantity_adjust",
            "target_quantity": 90,
            "evidence_summary": "复称两次均为 90mL，量筒校准记录见附件摘要",
            "responsibility_note": "自然挥发，库管员张三无主观责任",
        },
    )
    assert created.status_code == 201, created.text
    disposition = created.json()
    assert disposition["state"] == "in_review"

    verified = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/verify",
        headers=admin["headers"],
        json={"comment": "证据齐全"},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["state"] == "pending_approval"
    approval_id = verified.json()["approval_request_id"]

    # 申请人不能审批自己的处置单
    own = client.post(
        f"/api/samples/approvals/{approval_id}/decisions",
        headers=admin["headers"],
        json={"decision": "approve"},
    )
    assert own.status_code == 422

    # 未经双人审批不能执行
    forbidden = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert forbidden.status_code == 409

    client.post(
        f"/api/samples/approvals/{approval_id}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve"},
    )
    executed_forbidden = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert executed_forbidden.status_code == 409

    second = client.post(
        f"/api/samples/approvals/{approval_id}/decisions",
        headers=approver_two["headers"],
        json={"decision": "approve"},
    )
    assert second.json()["state"] == "approved"

    detail = client.get(
        f"/api/sample-operations/dispositions/{disposition['id']}", headers=admin["headers"]
    )
    assert detail.json()["state"] == "ready"

    executed = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 201, executed.text
    body = executed.json()
    assert body["sample"]["quantity"] == 90
    assert body["replayed"] is False

    sample_detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    last_event = sample_detail["events"][-1]
    assert last_event["event_type"] == "inventory.quantity_adjusted"
    assert last_event["quantity_delta"] == -10
    assert last_event["correlation_id"] == disposition["disposition_code"]
    assert last_event["details"]["evidence_summary"].startswith("复称两次")

    # 执行结果独立于审批单留痕，审批单已执行
    assert client.get(
        f"/api/sample-operations/dispositions/{disposition['id']}", headers=admin["headers"]
    ).json()["state"] == "executed"

    closed = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["state"] == "closed"


def test_relocate_adjustment_dual_approval_flow(client, admin):
    approver_one = _approver(client, admin, "loc_one")
    approver_two = _approver(client, admin, "loc_two")
    target_location = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": "LOC-MOVE-TARGET",
            "building": "样品楼",
            "room": "低温库",
            "cabinet": "一号柜",
            "shelf": "一层",
            "sensitivity": "normal",
            "capacity_units": 50,
        },
    ).json()
    # 盘点位置账面上有样品，但现场没找到（缺失），实际在低温库找到，选择移位处置
    _, _, sample, session, reconciled = _setup_counted_session(
        client, admin, observed_quantity=None, code="MOVE"
    )
    assert [item["kind"] for item in reconciled["differences"]] == ["missing"]

    disposition = client.post(
        f"/api/sample-operations/inventory/{session['id']}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "disposition": "relocate",
            "target_location_id": target_location["id"],
            "evidence_summary": "在低温库一号柜找到样品，标签与登记一致",
            "responsibility_note": "上次借用归还时归位错误，已纠正登记",
        },
    )
    assert disposition.status_code == 201, disposition.text
    item = disposition.json()
    verified = client.post(
        f"/api/sample-operations/dispositions/{item['id']}/verify",
        headers=admin["headers"],
        json={"comment": "提交双人审批"},
    )
    assert verified.json()["state"] == "pending_approval"
    _approve_twice(client, approver_one, approver_two, verified.json()["approval_request_id"])

    executed = client.post(
        f"/api/sample-operations/dispositions/{item['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 201, executed.text
    assert executed.json()["sample"]["location_id"] == target_location["id"]

    sample_detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert sample_detail["events"][-1]["event_type"] == "inventory.relocated"
    assert sample_detail["events"][-1]["details"]["to_location_id"] == target_location["id"]

    closed = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert closed.status_code == 200, closed.text


def test_relocate_requires_target_and_rejects_same_location(client, admin):
    _, _, sample, session, _ = _setup_counted_session(
        client, admin, observed_quantity=None, code="BADMOVE"
    )
    missing_target = client.post(
        f"/api/sample-operations/inventory/{session['id']}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "disposition": "relocate",
            "evidence_summary": "在其他位置找到",
            "responsibility_note": "归位错误",
        },
    )
    assert missing_target.status_code == 422
    same = client.post(
        f"/api/sample-operations/inventory/{session['id']}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "disposition": "relocate",
            "target_location_id": sample["location_id"],
            "evidence_summary": "原位找到",
            "responsibility_note": "归位错误",
        },
    )
    assert same.status_code == 422


def test_review_disposition_needs_no_approval_and_closes(client, admin):
    _, _, sample, session, reconciled = _setup_counted_session(
        client, admin, observed_quantity=100, code="REV"
    )
    assert reconciled["differences"] == []
    # 无差异时可以直接关账
    closed = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"


def test_report_loss_creates_anomaly_and_adjusts_stock(client, admin):
    _, _, sample, session, _ = _setup_counted_session(
        client, admin, observed_quantity=0, code="LOSS"
    )
    created = client.post(
        f"/api/sample-operations/inventory/{session['id']}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "disposition": "report_loss",
            "evidence_summary": "监控显示 9 月 24 日后柜门异常开启，样品去向不明",
            "responsibility_note": "值班员李四待核查，先行报失",
        },
    )
    assert created.status_code == 201, created.text
    disposition = created.json()
    assert disposition["requires_approval"] == 0
    assert disposition["target_quantity"] == 0

    verified = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/verify",
        headers=admin["headers"],
        json={"comment": "证据充分"},
    )
    assert verified.json()["state"] == "ready"

    executed = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 201, executed.text
    assert executed.json()["sample"]["quantity"] == 0

    detail = client.get(
        f"/api/sample-operations/dispositions/{disposition['id']}", headers=admin["headers"]
    ).json()
    assert detail["loss_case_id"] is not None
    anomalies = client.get("/api/samples/anomalies/list", headers=admin["headers"]).json()
    assert any(item["id"] == detail["loss_case_id"] for item in anomalies)

    sample_detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert sample_detail["events"][-1]["event_type"] == "inventory.loss_reported"

    closed = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"


def test_close_blocked_until_all_dispositions_resolved(client, admin):
    _, _, sample, session, _ = _setup_counted_session(
        client, admin, observed_quantity=95, code="OPEN"
    )
    blocked = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["unresolved"][0]["reason"] == "缺少差异处置单"

    created = client.post(
        f"/api/sample-operations/inventory/{session['id']}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "disposition": "review",
            "evidence_summary": "等待实验室出具称量校准报告",
            "responsibility_note": "疑似量具偏差，暂不下结论",
        },
    )
    still_blocked = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert still_blocked.status_code == 409
    assert "尚未完成复核" in still_blocked.json()["error"]["context"]["unresolved"][0]["reason"]

    client.post(
        f"/api/sample-operations/dispositions/{created.json()['id']}/verify",
        headers=admin["headers"],
        json={"comment": "维持账面"},
    )
    closed = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert closed.status_code == 200, closed.text


def test_new_sample_event_after_review_forces_re_review_and_keeps_stock(client, admin):
    approver_one = _approver(client, admin, "evt_one")
    approver_two = _approver(client, admin, "evt_two")
    borrower = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": "borrower_x",
            "password": "Borrower!23",
            "display_name": "借用人员",
            "role_codes": ["researcher"],
        },
    ).json()
    _, _, sample, session, _ = _setup_counted_session(
        client, admin, observed_quantity=90, code="STALE"
    )
    disposition = client.post(
        f"/api/sample-operations/inventory/{session['id']}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "disposition": "quantity_adjust",
            "target_quantity": 90,
            "evidence_summary": "复称为 90mL",
            "responsibility_note": "挥发损耗",
        },
    ).json()
    verified = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/verify",
        headers=admin["headers"],
        json={"comment": "提交审批"},
    ).json()
    first_approval = verified["approval_request_id"]
    _approve_twice(client, approver_one, approver_two, first_approval)

    # 盘点复核后发生借用，执行必须被拦截，且库存不能被改动
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "borrower_user_id": borrower["id"],
            "quantity": 5,
            "due_at": "2026-10-01T00:00:00+00:00",
        },
    )
    assert loan.status_code == 201, loan.text

    stale = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert stale.status_code == 409
    assert "重新复核" in stale.json()["error"]["message"]

    sample_after_failure = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert sample_after_failure["quantity"] == 100

    # 处置单仍是 ready，但需要重新复核；直接关账同样被拦截
    close_blocked = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert close_blocked.status_code == 409
    assert close_blocked.json()["error"]["context"]["need_recheck"]

    # 重新复核后旧审批单作废，生成新的双人审批
    reverified = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/verify",
        headers=admin["headers"],
        json={"comment": "已考虑借用 5mL"},
    )
    assert reverified.status_code == 200, reverified.text
    new_approval = reverified.json()["approval_request_id"]
    assert new_approval != first_approval
    old_request = client.post(
        f"/api/samples/approvals/{first_approval}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve"},
    )
    assert old_request.status_code == 409

    _approve_twice(client, approver_one, approver_two, new_approval)
    executed = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 201, executed.text
    assert executed.json()["sample"]["quantity"] == 90

    closed = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert closed.status_code == 200, closed.text


def test_rejected_disposition_can_be_edited_and_resubmitted(client, admin):
    approver_one = _approver(client, admin, "rej_one")
    approver_two = _approver(client, admin, "rej_two")
    _, _, sample, session, _ = _setup_counted_session(
        client, admin, observed_quantity=80, code="REJ"
    )
    disposition = client.post(
        f"/api/sample-operations/inventory/{session['id']}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "disposition": "quantity_adjust",
            "target_quantity": 80,
            "evidence_summary": "复称 80mL",
            "responsibility_note": "原因待查",
        },
    ).json()
    verified = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/verify",
        headers=admin["headers"],
        json={"comment": "提交"},
    ).json()
    approval_id = verified["approval_request_id"]
    client.post(
        f"/api/samples/approvals/{approval_id}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve"},
    )
    rejected = client.post(
        f"/api/samples/approvals/{approval_id}/decisions",
        headers=approver_two["headers"],
        json={"decision": "reject", "comment": "证据不足"},
    )
    assert rejected.json()["state"] == "rejected"
    detail = client.get(
        f"/api/sample-operations/dispositions/{disposition['id']}", headers=admin["headers"]
    )
    assert detail.json()["state"] == "rejected"

    updated = client.patch(
        f"/api/sample-operations/dispositions/{disposition['id']}",
        headers=admin["headers"],
        json={"evidence_summary": "补充监控记录与复称视频摘要，确认为 80mL"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["state"] == "in_review"

    reverified = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/verify",
        headers=admin["headers"],
        json={"comment": "补充证据后重新提交"},
    ).json()
    assert reverified["state"] == "pending_approval"
    _approve_twice(client, approver_one, approver_two, reverified["approval_request_id"])
    executed = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 201
    assert executed.json()["sample"]["quantity"] == 80

    closed = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert closed.status_code == 200


def test_execute_is_idempotent_and_restart_safe(client, admin):
    approver_one = _approver(client, admin, "idem_one")
    approver_two = _approver(client, admin, "idem_two")
    _, _, sample, session, _ = _setup_counted_session(
        client, admin, observed_quantity=88, code="IDEM"
    )
    disposition = client.post(
        f"/api/sample-operations/inventory/{session['id']}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "disposition": "quantity_adjust",
            "target_quantity": 88,
            "evidence_summary": "复称 88mL",
            "responsibility_note": "损耗",
        },
    ).json()
    client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/verify",
        headers=admin["headers"],
        json={"comment": "提交"},
    )
    pending = client.get(
        f"/api/sample-operations/dispositions/{disposition['id']}", headers=admin["headers"]
    ).json()["approval_request_id"]
    _approve_twice(client, approver_one, approver_two, pending)

    first = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert second.status_code == 201
    assert second.json()["replayed"] is True

    # 审批单状态已落库为 executed，重放不会重复写事件
    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    adjust_events = [e for e in detail["events"] if e["event_type"] == "inventory.quantity_adjusted"]
    assert len(adjust_events) == 1


def test_differences_listing_shapes(client, admin):
    _, _, _, session, reconciled = _setup_counted_session(
        client, admin, observed_quantity=90, code="LIST"
    )
    assert reconciled["digest"]
    listing = client.get(
        f"/api/sample-operations/inventory/{session['id']}/differences",
        headers=admin["headers"],
    )
    assert listing.status_code == 200
    row = listing.json()["differences"][0]
    assert row["difference_kind"] == "quantity_mismatch"
    assert row["disposition_id"] is None
    assert row["last_event_id"] >= 1


def test_event_during_counting_forces_re_review_after_approval(client, admin):
    approver_one = _approver(client, admin, "cnt_one")
    approver_two = _approver(client, admin, "cnt_two")
    borrower = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": "borrower_y",
            "password": "Borrower!23",
            "display_name": "借用人员乙",
            "role_codes": ["researcher"],
        },
    ).json()
    _, _, sample, session, _ = _setup_counted_session(
        client, admin, observed_quantity=90, code="DURING"
    )
    disposition = client.post(
        f"/api/sample-operations/inventory/{session['id']}/dispositions",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "disposition": "quantity_adjust",
            "target_quantity": 90,
            "evidence_summary": "复称 90mL",
            "responsibility_note": "挥发损耗",
        },
    ).json()
    # 在处置单复核之前（盘点期间）发生借用并归还：基线之后存在新事件
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "borrower_user_id": borrower["id"],
            "quantity": 3,
            "due_at": "2026-10-01T00:00:00+00:00",
        },
    )
    assert loan.status_code == 201
    returned = client.post(
        f"/api/samples/loans/{loan.json()['id']}/returns",
        headers=admin["headers"],
        json={"quantity": 3},
    )
    assert returned.status_code == 200

    verified = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/verify",
        headers=admin["headers"],
        json={"comment": "已知悉借还事件"},
    )
    assert verified.status_code == 200, verified.text
    first_approval = verified.json()["approval_request_id"]
    _approve_twice(client, approver_one, approver_two, first_approval)

    # 审批后再发生一笔转移，执行被拦截，库存不变
    target_location = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": "LOC-DURING-TARGET",
            "building": "样品楼",
            "room": "低温库",
            "cabinet": "二号柜",
            "shelf": "一层",
            "sensitivity": "normal",
            "capacity_units": 50,
        },
    ).json()
    moved = client.post(
        f"/api/sample-operations/{sample['id']}/transfers",
        headers=admin["headers"],
        json={
            "location_id": target_location["id"],
            "expected_version": sample["version"] + 2,
            "reason": "临时移位",
        },
    )
    assert moved.status_code == 200, moved.text

    stale = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert stale.status_code == 409
    sample_unchanged = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert sample_unchanged["quantity"] == 100

    # 重新复核后旧审批作废，新审批通过方可执行
    reverified = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/verify",
        headers=admin["headers"],
        json={"comment": "移位已确认"},
    )
    assert reverified.json()["approval_request_id"] != first_approval
    _approve_twice(client, approver_one, approver_two, reverified.json()["approval_request_id"])
    executed = client.post(
        f"/api/sample-operations/dispositions/{disposition['id']}/execute",
        headers=admin["headers"],
    )
    assert executed.status_code == 201, executed.text
    assert executed.json()["sample"]["quantity"] == 90

    closed = client.post(
        f"/api/sample-operations/inventory/{session['id']}/close",
        headers=admin["headers"],
    )
    assert closed.status_code == 200, closed.text
