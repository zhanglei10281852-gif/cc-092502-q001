from __future__ import annotations

import os
import threading

import pytest
from fastapi.testclient import TestClient

PASSWORD = "Passw0rd!2345"


def _make_user(client, username, display):
    user = client.post("/api/users", json={"username": username, "display_name": display, "password": PASSWORD})
    assert user.status_code == 201
    login = client.post("/api/sessions", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200
    return {"user": user.json(), "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


@pytest.fixture()
def team(client, owner):
    project = client.post(
        "/api/projects",
        json={"code": "BAOJIA", "name": "鲍家遗址发掘", "site_name": "溧阳鲍家遗址"},
        headers=owner["headers"],
    ).json()
    members = {"owner": owner}
    for username, display, role in [
        ("recorder", "记录员", "recorder"),
        ("reviewer", "复核员", "reviewer"),
        ("researcher", "研究员", "researcher"),
        ("viewer", "访客", "viewer"),
    ]:
        member = _make_user(client, username, display)
        added = client.post(
            f"/api/projects/{project['id']}/members",
            json={"user_id": member["user"]["id"], "role": role},
            headers=owner["headers"],
        )
        assert added.status_code == 200
        members[role] = member
    return {"project": project, "members": members}


@pytest.fixture()
def square(client, team):
    headers = team["members"]["recorder"]["headers"]
    response = client.post(
        f"/api/projects/{team['project']['id']}/fieldwork/squares",
        json={"code": "t101", "name": "北城墙探方"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()


def _base(team):
    return f"/api/projects/{team['project']['id']}/fieldwork"


def _unit(client, team, square, number, kind="layer", headers=None, **extra):
    response = client.post(
        f"{_base(team)}/units",
        json={"square_id": square["id"], "number": number, "kind": kind, **extra},
        headers=headers or team["members"]["recorder"]["headers"],
    )
    assert response.status_code == 201, response.json()
    return response.json()


def _relation(client, team, from_id, to_id, relation, headers=None):
    return client.post(
        f"{_base(team)}/relations",
        json={"from_unit": from_id, "to_unit": to_id, "relation": relation},
        headers=headers or team["members"]["recorder"]["headers"],
    )


def test_numbers_unique_within_project(client, team, square):
    recorder = team["members"]["recorder"]["headers"]
    dup_square = client.post(f"{_base(team)}/squares", json={"code": "T101", "name": "重复探方"}, headers=recorder)
    assert dup_square.status_code == 409
    assert dup_square.json()["error"]["code"] == "square_exists"

    _unit(client, team, square, "N1")
    dup_unit = client.post(
        f"{_base(team)}/units",
        json={"square_id": square["id"], "number": "n1", "kind": "layer"},
        headers=recorder,
    )
    assert dup_unit.status_code == 409
    assert dup_unit.json()["error"]["code"] == "unit_exists"

    owner = team["members"]["owner"]["headers"]
    other = client.post("/api/projects", json={"code": "OTHER", "name": "其他项目", "site_name": "遗址"}, headers=owner).json()
    other_square = client.post(f"/api/projects/{other['id']}/fieldwork/squares", json={"code": "T101"}, headers=owner)
    assert other_square.status_code == 201
    other_unit = client.post(
        f"/api/projects/{other['id']}/fieldwork/units",
        json={"square_id": other_square.json()["id"], "number": "N1", "kind": "layer"},
        headers=owner,
    )
    assert other_unit.status_code == 201


def test_recorder_only_modifies_unsealed(client, team, square):
    recorder = team["members"]["recorder"]["headers"]
    viewer = team["members"]["viewer"]["headers"]
    unit = _unit(client, team, square, "S1")

    updated = client.patch(f"{_base(team)}/units/{unit['id']}", json={"expected_version": 1, "label": "第1层"}, headers=recorder)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    sealed = client.post(f"{_base(team)}/units/{unit['id']}/seal", json={"expected_version": 2}, headers=recorder)
    assert sealed.status_code == 200
    assert sealed.json()["unit"]["status"] == "sealed"

    blocked = client.patch(f"{_base(team)}/units/{unit['id']}", json={"expected_version": 3, "label": "改动"}, headers=recorder)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "invalid_status"

    denied = client.post(
        f"{_base(team)}/units/{unit['id']}/correct",
        json={"expected_version": 3, "reason": "尝试修改", "label": "改动"},
        headers=recorder,
    )
    assert denied.status_code == 403

    viewer_write = client.patch(f"{_base(team)}/units/{unit['id']}", json={"expected_version": 3, "label": "改动"}, headers=viewer)
    assert viewer_write.status_code == 403
    assert client.get(f"{_base(team)}/units/{unit['id']}", headers=viewer).status_code == 200

    other = _unit(client, team, square, "S2")
    frozen = _relation(client, team, other["id"], unit["id"], "cuts")
    assert frozen.status_code == 409
    assert frozen.json()["error"]["code"] == "unit_sealed"


def test_reviewer_cannot_review_own_submission(client, team, square):
    owner = team["members"]["owner"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    unit = _unit(client, team, square, "H1", kind="ash_pit", headers=owner)
    sealed = client.post(f"{_base(team)}/units/{unit['id']}/seal", json={"expected_version": 1}, headers=owner)
    assert sealed.status_code == 200

    self_review = client.post(f"{_base(team)}/units/{unit['id']}/review", json={"expected_version": 2}, headers=owner)
    assert self_review.status_code == 403
    assert self_review.json()["error"]["code"] == "self_review"

    reviewed = client.post(f"{_base(team)}/units/{unit['id']}/review", json={"expected_version": 2, "note": "记录完整"}, headers=reviewer)
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "reviewed"

    again = client.post(f"{_base(team)}/units/{unit['id']}/review", json={"expected_version": 3}, headers=reviewer)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_status"


def test_relation_rules(client, team, square):
    a = _unit(client, team, square, "R1")
    b = _unit(client, team, square, "R2")
    c = _unit(client, team, square, "R3")
    d = _unit(client, team, square, "R4")

    self_loop = _relation(client, team, a["id"], a["id"], "cuts")
    assert self_loop.status_code == 400
    assert self_loop.json()["error"]["code"] == "self_loop"

    first = _relation(client, team, a["id"], b["id"], "cuts")
    assert first.status_code == 201
    duplicate = _relation(client, team, a["id"], b["id"], "cuts")
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == first.json()["id"]

    reverse = _relation(client, team, b["id"], a["id"], "cuts")
    assert reverse.status_code == 409
    assert reverse.json()["error"]["code"] == "relation_cycle"

    assert _relation(client, team, b["id"], c["id"], "cuts").status_code == 201
    cycle = _relation(client, team, a["id"], c["id"], "earlier")
    assert cycle.status_code == 409
    assert cycle.json()["error"]["code"] == "relation_cycle"

    assert _relation(client, team, a["id"], d["id"], "equals").status_code == 201
    swapped = _relation(client, team, d["id"], a["id"], "equals")
    assert swapped.status_code == 201

    via_equals = _relation(client, team, a["id"], d["id"], "cuts")
    assert via_equals.status_code == 409
    assert via_equals.json()["error"]["code"] == "relation_conflict"

    equals_conflict = _relation(client, team, a["id"], b["id"], "equals")
    assert equals_conflict.status_code == 409
    assert equals_conflict.json()["error"]["code"] == "relation_conflict"

    transitive_conflict = _relation(client, team, b["id"], d["id"], "cuts")
    assert transitive_conflict.status_code == 409
    assert transitive_conflict.json()["error"]["code"] == "relation_cycle"


def test_direct_and_transitive_queries(client, team, square):
    h1 = _unit(client, team, square, "H1", kind="ash_pit")
    l1 = _unit(client, team, square, "L1")
    l2 = _unit(client, team, square, "L2")
    l3 = _unit(client, team, square, "L3")
    g1 = _unit(client, team, square, "G1", kind="channel")
    g1b = _unit(client, team, square, "G1B", kind="channel")

    assert _relation(client, team, h1["id"], l1["id"], "cuts").status_code == 201
    assert _relation(client, team, l1["id"], l2["id"], "cuts").status_code == 201
    assert _relation(client, team, l2["id"], l3["id"], "cuts").status_code == 201
    assert _relation(client, team, g1["id"], g1b["id"], "equals").status_code == 201
    assert _relation(client, team, g1["id"], l3["id"], "cuts").status_code == 201

    viewer = team["members"]["viewer"]["headers"]
    direct = client.get(f"{_base(team)}/units/{l1['id']}/relations", headers=viewer)
    assert direct.status_code == 200
    relations = direct.json()["relations"]
    assert len(relations) == 2
    perspectives = {(item["perspective"], item["other"]["number"]) for item in relations}
    assert perspectives == {("cut_by", "H1"), ("cuts", "L2")}

    transitive_h1 = client.get(f"{_base(team)}/units/{h1['id']}/relations/transitive", headers=viewer).json()
    assert sorted(u["number"] for u in transitive_h1["earlier"]) == ["L1", "L2", "L3"]
    assert transitive_h1["later"] == []

    transitive_l3 = client.get(f"{_base(team)}/units/{l3['id']}/relations/transitive", headers=viewer).json()
    assert sorted(u["number"] for u in transitive_l3["later"]) == ["G1", "G1B", "H1", "L1", "L2"]

    transitive_g1b = client.get(f"{_base(team)}/units/{g1b['id']}/relations/transitive", headers=viewer).json()
    assert [u["number"] for u in transitive_g1b["equals"]] == ["G1"]
    assert [u["number"] for u in transitive_g1b["earlier"]] == ["L3"]

    direct_g1b = client.get(f"{_base(team)}/units/{g1b['id']}/relations", headers=viewer).json()
    assert [(item["perspective"], item["other"]["number"]) for item in direct_g1b["relations"]] == [("equals", "G1")]


def test_seal_snapshot_and_correction_versions(client, team, square):
    from app.security import request_hash

    recorder = team["members"]["recorder"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    viewer = team["members"]["viewer"]["headers"]
    unit = _unit(client, team, square, "L1", label="第1层", description="表土层")

    sealed = client.post(f"{_base(team)}/units/{unit['id']}/seal", json={"expected_version": 1}, headers=recorder)
    assert sealed.status_code == 200
    body = sealed.json()
    assert body["unit"]["status"] == "sealed"
    assert body["unit"]["version"] == 2
    digest_v2 = body["version"]["digest"]
    assert request_hash(body["version"]["snapshot"]) == digest_v2

    missing_reason = client.post(
        f"{_base(team)}/units/{unit['id']}/correct",
        json={"expected_version": 2, "description": "无原因"},
        headers=researcher,
    )
    assert missing_reason.status_code == 422

    corrected = client.post(
        f"{_base(team)}/units/{unit['id']}/correct",
        json={"expected_version": 2, "reason": "现场复核发现层位归属错误", "description": "表土层（修正）"},
        headers=researcher,
    )
    assert corrected.status_code == 200
    corrected_body = corrected.json()
    assert corrected_body["unit"]["version"] == 3
    assert corrected_body["unit"]["status"] == "sealed"
    assert corrected_body["unit"]["description"] == "表土层（修正）"
    assert corrected_body["version"]["reason"] == "现场复核发现层位归属错误"
    assert corrected_body["version"]["digest"] != digest_v2

    history = client.get(f"{_base(team)}/units/{unit['id']}/versions", headers=viewer).json()["versions"]
    assert [item["version"] for item in history] == [2, 3]
    assert history[0]["digest"] == digest_v2
    assert history[0]["snapshot"]["unit"]["description"] == "表土层"
    assert request_hash(history[1]["snapshot"]) == history[1]["digest"]

    reviewed = client.post(f"{_base(team)}/units/{unit['id']}/review", json={"expected_version": 3, "note": "同意修正"}, headers=reviewer)
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "reviewed"


def test_batch_import_partial_errors_and_atomic_rows(client, team):
    recorder = team["members"]["recorder"]["headers"]
    rows = [
        {"op": "square", "code": "t201", "name": "扩方"},
        {"op": "unit", "number": "M1", "kind": "layer", "square": "T201"},
        {"op": "unit", "number": "M1", "kind": "layer", "square": "T201"},
        {"op": "unit", "number": "M2", "kind": "ash_pit", "square": "T999"},
        {"op": "unit", "number": "M2", "kind": "ash_pit", "square": "T201"},
        {"op": "relation", "from": "M1", "to": "M2", "relation": "earlier"},
        {"op": "relation", "from": "M1", "to": "M2", "relation": "cuts"},
        {"op": "relation", "from": "M1", "to": "M9", "relation": "cuts"},
    ]
    response = client.post(f"{_base(team)}/import", json={"rows": rows}, headers={**recorder, "Idempotency-Key": "batch-1"})
    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == {"total": 8, "ok": 4, "errors": 4}
    outcomes = [(item["index"], item["status"], item.get("code") if item["status"] == "error" else None) for item in body["results"]]
    assert outcomes == [
        (0, "ok", None),
        (1, "ok", None),
        (2, "error", "unit_exists"),
        (3, "error", "square_not_found"),
        (4, "ok", None),
        (5, "ok", None),
        (6, "error", "relation_cycle"),
        (7, "error", "unit_not_found"),
    ]

    units = client.get(f"{_base(team)}/units", headers=recorder).json()["data"]
    assert sorted(item["number"] for item in units) == ["M1", "M2"]
    m1 = next(item for item in units if item["number"] == "M1")
    relations = client.get(f"{_base(team)}/units/{m1['id']}/relations", headers=recorder).json()["relations"]
    assert len(relations) == 1
    assert relations[0]["relation"] == "earlier"

    audit = client.get(f"/api/audit?project_id={team['project']['id']}&resource_type=context_relation", headers=recorder).json()["data"]
    assert len([event for event in audit if event["action"] == "context.relation.create"]) == 1

    replay = client.post(f"{_base(team)}/import", json={"rows": rows}, headers={**recorder, "Idempotency-Key": "batch-1"})
    assert replay.status_code == 200
    assert replay.json() == body
    assert len(client.get(f"{_base(team)}/units", headers=recorder).json()["data"]) == 2

    conflict = client.post(f"{_base(team)}/import", json={"rows": rows[:1]}, headers={**recorder, "Idempotency-Key": "batch-1"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_idempotent_requests(client, team, square):
    recorder = team["members"]["recorder"]["headers"]
    payload = {"square_id": square["id"], "number": "K1", "kind": "layer", "label": "扰土层"}
    first = client.post(f"{_base(team)}/units", json=payload, headers={**recorder, "Idempotency-Key": "u-1"})
    second = client.post(f"{_base(team)}/units", json=payload, headers={**recorder, "Idempotency-Key": "u-1"})
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    units = client.get(f"{_base(team)}/units", headers=recorder).json()["data"]
    assert len([item for item in units if item["number"] == "K1"]) == 1

    changed = client.post(f"{_base(team)}/units", json={**payload, "label": "改动"}, headers={**recorder, "Idempotency-Key": "u-1"})
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "idempotency_conflict"

    repeated = client.post(f"{_base(team)}/units", json=payload, headers=recorder)
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "unit_exists"


def test_concurrent_version_conflict(client, team, square):
    from app.database import close_connection
    from app.fieldwork.service import FieldworkService
    from app.service import ServiceError

    unit = _unit(client, team, square, "C1")
    project_id = team["project"]["id"]
    owner_id = team["members"]["owner"]["user"]["id"]
    barrier = threading.Barrier(2)
    results = []

    def attempt(label):
        service = FieldworkService()
        barrier.wait()
        try:
            updated = service.update_unit(project_id, unit["id"], {"expected_version": 1, "label": label}, owner_id)
            results.append(("ok", updated["version"]))
        except ServiceError as exc:
            results.append((exc.code, None))
        finally:
            close_connection()

    threads = [threading.Thread(target=attempt, args=(f"层位描述{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(result[0] for result in results) == ["ok", "version_conflict"]
    final = FieldworkService().get_unit(project_id, unit["id"], owner_id)
    assert final["version"] == 2


def test_failed_operations_roll_back(client, team, square):
    recorder = team["members"]["recorder"]["headers"]
    a = _unit(client, team, square, "F1")
    b = _unit(client, team, square, "F2")

    assert _relation(client, team, a["id"], b["id"], "cuts").status_code == 201
    conflict = _relation(client, team, b["id"], a["id"], "cuts")
    assert conflict.status_code == 409

    relations = client.get(f"{_base(team)}/units/{a['id']}/relations", headers=recorder).json()["relations"]
    assert len(relations) == 1
    audit = client.get(f"/api/audit?project_id={team['project']['id']}&resource_type=context_relation", headers=recorder).json()["data"]
    assert len(audit) == 1

    stale = client.post(f"{_base(team)}/units/{a['id']}/seal", json={"expected_version": 99}, headers=recorder)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"
    assert client.get(f"{_base(team)}/units/{a['id']}/versions", headers=recorder).json()["versions"] == []
    assert client.get(f"{_base(team)}/units/{a['id']}", headers=recorder).json()["status"] == "open"


def test_audit_events_queryable_per_unit(client, team, square):
    recorder = team["members"]["recorder"]["headers"]
    viewer = team["members"]["viewer"]["headers"]
    unit = _unit(client, team, square, "A1")
    assert client.post(f"{_base(team)}/units/{unit['id']}/seal", json={"expected_version": 1}, headers=recorder).status_code == 200

    events = client.get(
        f"/api/audit?project_id={team['project']['id']}&resource_type=context_unit&resource_id={unit['id']}",
        headers=viewer,
    )
    assert events.status_code == 200
    actions = [event["action"] for event in events.json()["data"]]
    assert actions == ["context.unit.create", "context.unit.seal"]
    assert all(event["resource_id"] == str(unit["id"]) for event in events.json()["data"])


def test_restart_consistency(tmp_path):
    os.environ["ARCHAEOLOGY_DATABASE_PATH"] = str(tmp_path / "restart.db")
    from app.database import close_connection

    close_connection()
    from app.main import app

    with TestClient(app) as client:
        owner = _make_user(client, "owner", "负责人")
        project = client.post(
            "/api/projects",
            json={"code": "BJ", "name": "鲍家遗址", "site_name": "溧阳鲍家遗址"},
            headers=owner["headers"],
        ).json()
        recorder = _make_user(client, "recorder", "记录员")
        client.post(f"/api/projects/{project['id']}/members", json={"user_id": recorder["user"]["id"], "role": "recorder"}, headers=owner["headers"])
        base = f"/api/projects/{project['id']}/fieldwork"
        square = client.post(f"{base}/squares", json={"code": "T1"}, headers=recorder["headers"]).json()
        l1 = client.post(f"{base}/units", json={"square_id": square["id"], "number": "L1", "kind": "layer"}, headers=recorder["headers"]).json()
        payload = {"square_id": square["id"], "number": "L2", "kind": "layer", "label": "第2层"}
        l2 = client.post(f"{base}/units", json=payload, headers={**recorder["headers"], "Idempotency-Key": "restart-l2"}).json()
        client.post(f"{base}/relations", json={"from_unit": l1["id"], "to_unit": l2["id"], "relation": "cuts"}, headers=recorder["headers"])
        sealed = client.post(f"{base}/units/{l1['id']}/seal", json={"expected_version": 1}, headers=recorder["headers"]).json()
        digest = sealed["version"]["digest"]
        before = client.get(f"{base}/units/{l2['id']}/relations/transitive", headers=recorder["headers"]).json()
        assert [item["number"] for item in before["later"]] == ["L1"]

    with TestClient(app) as again:
        history = again.get(f"{base}/units/{l1['id']}/versions", headers=recorder["headers"]).json()["versions"]
        assert len(history) == 1
        assert history[0]["digest"] == digest
        after = again.get(f"{base}/units/{l2['id']}/relations/transitive", headers=recorder["headers"]).json()
        assert after == before
        replay = again.post(f"{base}/units", json=payload, headers={**recorder["headers"], "Idempotency-Key": "restart-l2"})
        assert replay.status_code == 201
        assert replay.json()["id"] == l2["id"]
        units = again.get(f"{base}/units", headers=recorder["headers"]).json()["data"]
        assert sorted(item["number"] for item in units) == ["L1", "L2"]

    close_connection()
