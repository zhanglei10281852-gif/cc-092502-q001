"""田野模块 HTTP 端到端测试：会话鉴权、项目角色与查询接口。"""
from __future__ import annotations

import pytest


@pytest.fixture()
def world(client):
    def signup(username: str, password: str = "Passw0rd!2345"):
        created = client.post("/api/users", json={"username": username, "display_name": username, "password": password})
        assert created.status_code == 201
        token = client.post("/api/sessions", json={"username": username, "password": password}).json()["token"]
        return created.json()["id"], {"Authorization": f"Bearer {token}"}

    owner_id, owner_h = signup("fieldowner")
    reviewer_id, reviewer_h = signup("fieldreviewer")
    _, other_h = signup("fieldother")
    project = client.post("/api/projects", json={"code": "FIELD", "name": "鲍家", "site_name": "溧阳"}, headers=owner_h).json()
    pid = project["id"]
    assert client.post(f"/api/projects/{pid}/members", json={"user_id": reviewer_id, "role": "reviewer"}, headers=owner_h).status_code == 200
    base = f"/api/projects/{pid}/field"
    return client, base, owner_h, reviewer_h, other_h


def _create_unit(client, base, headers, code, unit_type="layer"):
    return client.post(f"{base}/units", json={"code": code, "unit_type": unit_type, "title": code}, headers=headers)


def test_http_full_review_workflow(world):
    client, base, owner, reviewer, other = world
    # 无项目角色的用户被拒绝
    assert _create_unit(client, base, other, "X").status_code == 403
    for code in ("T01", "H07", "G03"):
        assert _create_unit(client, base, owner, code, "feature" if code == "H07" else "layer").status_code == 201
    # 编号项目内唯一
    assert _create_unit(client, base, owner, "T01").status_code == 409
    rel = client.post(f"{base}/relations", json={"source": "T01", "target": "H07", "kind": "earlier"}, headers=owner)
    assert rel.status_code == 201
    rid = rel.json()["id"]
    # 提交人不能自审
    assert client.post(f"{base}/relations/{rid}/review", json={"decision": "approved"}, headers=owner).status_code == 403
    # 复核员审核通过
    reviewed = client.post(f"{base}/relations/{rid}/review", json={"decision": "approved", "comment": "层位关系明确"}, headers=reviewer)
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "approved"
    # 直接关系（双向可查）与版本历史/审计
    direct = client.get(f"{base}/units/H07/relations", headers=reviewer).json()
    assert {(d["direction"], d["other"]) for d in direct["data"]} == {("incoming", "T01")}
    versions = client.get(f"{base}/units/T01/versions", headers=reviewer).json()
    assert versions["current_version"] == 1
    audit = client.get(f"{base}/units/T01/audit", headers=reviewer).json()
    assert any(e["action"] == "field.relation.review" for e in audit["data"])


def test_http_seal_correction_and_conflict(world):
    client, base, owner, reviewer, other = world
    _create_unit(client, base, owner, "L88")
    seal = client.post(f"{base}/units/L88/seal", headers=owner)
    assert seal.status_code == 201 and seal.json()["snapshot"]["content_digest"]
    # 封存后普通修改被拒
    assert client.patch(f"{base}/units/L88", json={"title": "改"}, headers=owner).status_code == 409
    # 纠错必须带原因（422 校验）与基准版本
    bad = client.post(f"{base}/units/L88/corrections", json={"base_version": 1}, headers=owner)
    assert bad.status_code == 422
    ok = client.post(f"{base}/units/L88/corrections", json={"change_reason": "出土遗物年代修正", "base_version": 1}, headers=owner)
    assert ok.status_code == 201 and ok.json()["current_version"] == 2
    # 已有更新草稿版本：过时基准直接冲突
    stale = client.post(f"{base}/units/L88/corrections", json={"change_reason": "再次纠错", "base_version": 1}, headers=owner)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    assert client.post(f"{base}/units/L88/seal", headers=owner).status_code == 201
    # v2 封存后，基于 v1 的纠错仍因版本冲突失败
    stale2 = client.post(f"{base}/units/L88/corrections", json={"change_reason": "过时纠错", "base_version": 1}, headers=owner)
    assert stale2.status_code == 409 and stale2.json()["error"]["code"] == "version_conflict"


def test_http_graph_rules_and_transitive(world):
    client, base, owner, reviewer, _ = world
    for code in ("a", "b", "c"):
        _create_unit(client, base, owner, code)
    first = client.post(f"{base}/relations", json={"source": "a", "target": "b", "kind": "earlier"}, headers=owner).json()["id"]
    client.post(f"{base}/relations/{first}/review", json={"decision": "approved"}, headers=reviewer)
    second = client.post(f"{base}/relations", json={"source": "b", "target": "c", "kind": "earlier"}, headers=owner).json()["id"]
    client.post(f"{base}/relations/{second}/review", json={"decision": "approved"}, headers=reviewer)
    # 自环 422
    assert client.post(f"{base}/relations", json={"source": "a", "target": "a", "kind": "cuts"}, headers=owner).status_code == 422
    # 可推导关系被拒
    redundant = client.post(f"{base}/relations", json={"source": "a", "target": "c", "kind": "earlier"}, headers=owner)
    assert redundant.status_code == 422 and redundant.json()["error"]["code"] == "relation_redundant"
    # 传递闭包
    tr = client.get(f"{base}/units/a/transitive", headers=owner).json()
    assert tr["later"] == ["b", "c"] and tr["earlier"] == []
    trc = client.get(f"{base}/units/c/transitive", headers=owner).json()
    assert trc["earlier"] == ["a", "b"]


def test_http_import_line_errors_rollback_and_idempotency(world):
    client, base, owner, _, _ = world
    payload = {
        "units": [{"code": "I1", "unit_type": "trench"}, {"code": "I2", "unit_type": "layer"}],
        "relations": [
            {"source": "I1", "target": "I2", "kind": "cuts"},
            {"source": "I2", "target": "MISSING", "kind": "earlier"},
        ],
    }
    bad = client.post(f"{base}/import", json=payload, headers=owner)
    assert bad.status_code == 422
    details = bad.json()["error"]["details"]
    assert len(details["lines"]) == 1 and details["lines"][0]["code_error"] == "unknown_unit"
    assert client.get(f"{base}/units/I1", headers=owner).status_code == 404  # 整体回滚
    payload["relations"] = [{"source": "I1", "target": "I2", "kind": "cuts"}]
    h1 = {**owner, "Idempotency-Key": "import-9"}
    first = client.post(f"{base}/import", json=payload, headers=h1)
    second = client.post(f"{base}/import", json=payload, headers=h1)
    assert first.status_code == second.status_code == 200
    assert second.json()["idempotent_replay"] is True


def test_http_requires_bearer(world):
    client, base, *_ = world
    assert client.get(f"{base}/units").status_code == 422  # 缺少鉴权头
