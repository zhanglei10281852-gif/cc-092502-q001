"""田野上下文模块测试。

HTTP 层通过 TestClient 验证角色、封存、复核、关系图校验、导入与幂等；
持久化层直接在文件 SQLite 上验证并发版本冲突、事务回滚和服务重启一致性。
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app import database

# --------------------------------------------------------------------- 夹具


@pytest.fixture()
def field_env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "field.db"
    monkeypatch.setenv("ARCHAEOLOGY_DATABASE_PATH", str(db_path))
    database.close_connection()
    database.init_db()
    from app.field_store import init_field_db
    init_field_db(database.connection())
    yield tmp_path
    database.close_connection()


@pytest.fixture()
def svc(field_env):
    from app.field_service import FieldService
    from app.service import ResearchService
    core = ResearchService()

    def make_user(username: str) -> int:
        return core.create_user({"username": username, "display_name": username, "password": f"{username}Pass!2345"})["id"]

    lead = make_user("lead01")
    recorder = make_user("rec01")
    reviewer = make_user("rev01")
    outsider = make_user("out01")
    stamp = database.now()
    project = core.db.execute(
        "INSERT INTO projects(code,name,site_name,created_at,updated_at) VALUES('BJP','鲍家','溧阳',?,?)",
        (stamp, stamp),
    )
    pid = project.lastrowid
    for uid, role in [(lead, "owner"), (recorder, "recorder"), (reviewer, "reviewer"), (outsider, "viewer")]:
        core.db.execute("INSERT INTO project_members(project_id,user_id,role,joined_at) VALUES(?,?,?,?)", (pid, uid, role, stamp))
    return {
        "fs": FieldService(),
        "core": core,
        "pid": pid,
        "lead": lead,
        "recorder": recorder,
        "reviewer": reviewer,
        "outsider": outsider,
    }


def make_unit(fs, pid, actor, code, unit_type="layer", attributes=None):
    return fs.create_unit(pid, actor, {"code": code, "unit_type": unit_type, "title": code, "attributes": attributes or {}})


def add_approved(fs, pid, s, t, kind, recorder, reviewer):
    rid = fs.create_relation(pid, recorder, {"source": s, "target": t, "kind": kind, "evidence": "", "attributes": {}})["id"]
    fs.review_relation(pid, reviewer, rid, {"decision": "approved", "comment": ""})
    return rid


# ----------------------------------------------------------------- 登记与权限


def test_unit_code_unique_within_project(svc):
    fs, pid, lead = svc["fs"], svc["pid"], svc["lead"]
    make_unit(fs, pid, lead, "T101", "trench")
    from app.service import ServiceError
    with pytest.raises(ServiceError) as exc:
        make_unit(fs, pid, lead, "T101", "trench")
    assert exc.value.code == "unit_exists"


def test_outsider_cannot_write_units(svc):
    from app.service import ServiceError
    with pytest.raises(ServiceError) as exc:
        make_unit(svc["fs"], svc["pid"], svc["outsider"], "T9")
    assert exc.value.status == 403


def test_only_unsealed_can_be_modified(svc):
    from app.service import ServiceError
    fs, pid, recorder = svc["fs"], svc["pid"], svc["recorder"]
    make_unit(fs, pid, recorder, "L1")
    assert fs.update_unit(pid, recorder, "L1", {"title": "新标题", "attributes": None, "base_version": 1})["title"] == "新标题"
    fs.seal_unit(pid, recorder, "L1")
    with pytest.raises(ServiceError) as exc:
        fs.update_unit(pid, recorder, "L1", {"title": "x", "attributes": None, "base_version": None})
    assert exc.value.code == "unit_sealed"


# ----------------------------------------------------------------- 封存与版本


def test_seal_creates_immutable_snapshot_and_correction_chains(svc):
    fs, pid, recorder = svc["fs"], svc["pid"], svc["recorder"]
    make_unit(fs, pid, recorder, "L2", attributes={"depth": 30})
    sealed1 = fs.seal_unit(pid, recorder, "L2")
    d1 = sealed1["snapshot"]["content_digest"]
    assert sealed1["snapshot"]["prev_digest"] == ""
    assert sealed1["unit"]["status"] == "sealed"
    corrected = fs.correct_unit(pid, recorder, "L2", {"change_reason": "测年数据更新", "attributes": {"depth": 35}, "base_version": 1})
    assert corrected["current_version"] == 2 and corrected["status"] == "draft"
    sealed2 = fs.seal_unit(pid, recorder, "L2")
    assert sealed2["snapshot"]["version_no"] == 2
    assert sealed2["snapshot"]["prev_digest"] == d1  # 哈希链
    history = fs.version_history(pid, recorder, "L2")
    assert [v["version_no"] for v in history["versions"]] == [1, 2]
    assert history["versions"][0]["status"] == "sealed"
    assert history["versions"][1]["change_reason"] == "测年数据更新"
    # v1 快照内容不可变
    snap1 = fs.db.execute("SELECT content_json FROM field_snapshots WHERE unit_id=? AND version_no=1", (sealed1["unit"]["id"],)).fetchone()
    assert '"depth":30' in snap1["content_json"]


def test_concurrent_correction_conflict(svc):
    """两个线程基于同一封存版本并发纠错：一个成功，另一个得到 version_conflict。"""
    from app.service import ServiceError
    from app.database import close_connection, connection as get_connection
    from app.field_service import FieldService
    fs, pid, recorder = svc["fs"], svc["pid"], svc["recorder"]
    make_unit(fs, pid, recorder, "L3")
    fs.seal_unit(pid, recorder, "L3")

    errors: list[Exception] = []

    def worker(reason: str):
        try:
            local = FieldService(get_connection())  # 线程内首次调用会建立独立连接
            local.correct_unit(pid, recorder, "L3", {"change_reason": reason, "attributes": {"r": reason}, "base_version": 1})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("甲纠错",))
    t2 = threading.Thread(target=worker, args=("乙纠错",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert len(errors) == 1
    assert isinstance(errors[0], ServiceError) and errors[0].code == "version_conflict"
    row = fs.db.execute("SELECT current_version,status,attributes_json FROM field_units WHERE code='L3'").fetchone()
    assert row["current_version"] == 2  # 只有一个纠错落库
    close_connection()


# ----------------------------------------------------------------- 复核与关系图


def test_reviewer_cannot_review_own_relation(svc):
    from app.service import ServiceError
    fs, pid, lead = svc["fs"], svc["pid"], svc["lead"]
    make_unit(fs, pid, lead, "A"); make_unit(fs, pid, lead, "B")
    rid = fs.create_relation(pid, lead, {"source": "A", "target": "B", "kind": "earlier", "evidence": "", "attributes": {}})["id"]
    # 提交人即便具备复核角色，也不能审核自己的提交
    with pytest.raises(ServiceError) as exc:
        fs.review_relation(pid, lead, rid, {"decision": "approved", "comment": ""})
    assert exc.value.code == "self_review_forbidden"


def test_self_loop_rejected(svc):
    from app.service import ServiceError
    fs, pid, recorder = svc["fs"], svc["pid"], svc["recorder"]
    make_unit(fs, pid, recorder, "S")
    with pytest.raises(ServiceError) as exc:
        fs.create_relation(pid, recorder, {"source": "S", "target": "S", "kind": "cuts", "evidence": "", "attributes": {}})
    assert exc.value.code == "self_loop"


def test_redundant_contradictory_and_cycle_edges_rejected(svc):
    from app.service import ServiceError
    fs, pid, rec, rev = svc["fs"], svc["pid"], svc["recorder"], svc["reviewer"]
    for code in ["A", "B", "C", "D", "E"]:
        make_unit(fs, pid, rec, code)
    # A 早于 B；C 切叠 B（C 晚于 B）-> 年代序 A -> B -> C
    add_approved(fs, pid, "A", "B", "earlier", rec, rev)
    add_approved(fs, pid, "C", "B", "cuts", rec, rev)
    # A 早于 C 已可经混合关系推导 -> 冗余
    with pytest.raises(ServiceError) as exc:
        fs.create_relation(pid, rec, {"source": "A", "target": "C", "kind": "earlier", "evidence": "", "attributes": {}})
    assert exc.value.code == "relation_redundant"
    # C 早于 A 与年代序相反 -> 矛盾
    with pytest.raises(ServiceError) as exc:
        fs.create_relation(pid, rec, {"source": "C", "target": "A", "kind": "earlier", "evidence": "", "attributes": {}})
    assert exc.value.code == "relation_contradiction"
    # 两条互逆边在待复核阶段可以共存；批准第一条后，批准第二条时复核复检必须拦下
    r1 = fs.create_relation(pid, rec, {"source": "D", "target": "E", "kind": "earlier", "evidence": "", "attributes": {}})["id"]
    back = fs.create_relation(pid, rec, {"source": "E", "target": "D", "kind": "earlier", "evidence": "", "attributes": {}})["id"]
    fs.review_relation(pid, rev, r1, {"decision": "approved", "comment": ""})
    with pytest.raises(ServiceError) as exc:
        fs.review_relation(pid, rev, back, {"decision": "approved", "comment": ""})
    assert exc.value.code in {"relation_contradiction", "relation_cycle"}
    # 被挡下的边仍为 pending，可改判驳回
    fs.review_relation(pid, rev, back, {"decision": "rejected", "comment": "互逆矛盾"})
    assert fs.db.execute("SELECT status FROM field_relations WHERE id=?", (back,)).fetchone()["status"] == "rejected"


def test_cuts_direction_and_equivalence_transitive(svc):
    fs, pid, rec, rev = svc["fs"], svc["pid"], svc["recorder"], svc["reviewer"]
    for code in ["A", "A2", "B", "C"]:
        make_unit(fs, pid, rec, code)
    add_approved(fs, pid, "A", "A2", "equivalent", rec, rev)
    add_approved(fs, pid, "A", "B", "earlier", rec, rev)
    add_approved(fs, pid, "C", "B", "cuts", rec, rev)  # C 切叠 B -> C 最晚
    # A2 与 A 等同，故 A2 早于 C 可推导
    from app.service import ServiceError
    with pytest.raises(ServiceError) as exc:
        fs.create_relation(pid, rec, {"source": "A2", "target": "C", "kind": "earlier", "evidence": "", "attributes": {}})
    assert exc.value.code == "relation_redundant"
    tr = fs.transitive_relations(pid, rec, "B")
    assert tr["equivalent"] == []
    assert tr["earlier"] == ["A", "A2"]   # A/A2 早于 B（C 也经 cuts 与 B 同时序端点，不在此列）
    tr_a2 = fs.transitive_relations(pid, rec, "A2")
    assert tr_a2["equivalent"] == ["A"]
    assert "B" in tr_a2["later"] and "C" in tr_a2["later"]
    assert any(t["code"] == "C" and "cuts" in t["via_kinds"] for t in tr_a2["temporal"])


def test_pending_edges_do_not_derive(svc):
    fs, pid, rec = svc["fs"], svc["pid"], svc["recorder"]
    make_unit(fs, pid, rec, "P1"); make_unit(fs, pid, rec, "P2")
    fs.create_relation(pid, rec, {"source": "P1", "target": "P2", "kind": "earlier", "evidence": "", "attributes": {}})
    tr = fs.transitive_relations(pid, rec, "P1")
    assert tr["temporal"] == []
    direct = fs.direct_relations(pid, rec, "P1")["data"]
    assert len(direct) == 1 and direct[0]["status"] == "pending"


def test_concurrent_opposite_approvals_one_rejected(svc):
    """并发批准两条互逆边时，图校验必须拦下后者，不得产生矛盾环。"""
    from app.database import close_connection, connection as get_connection
    from app.field_service import FieldService
    from app.service import ServiceError
    fs, pid, rec, rev = svc["fs"], svc["pid"], svc["recorder"], svc["reviewer"]
    make_unit(fs, pid, rec, "X"); make_unit(fs, pid, rec, "Y")
    r1 = fs.create_relation(pid, rec, {"source": "X", "target": "Y", "kind": "earlier", "evidence": "", "attributes": {}})["id"]
    r2 = fs.create_relation(pid, rec, {"source": "Y", "target": "X", "kind": "earlier", "evidence": "", "attributes": {}})["id"]
    errors: list[Exception] = []

    def approve(rid: int):
        try:
            FieldService(get_connection()).review_relation(pid, rev, rid, {"decision": "approved", "comment": ""})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=approve, args=(r1,))
    t2 = threading.Thread(target=approve, args=(r2,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert len(errors) == 1 and isinstance(errors[0], ServiceError)
    approved = fs.db.execute("SELECT COUNT(*) AS n FROM field_relations WHERE status='approved'").fetchone()["n"]
    assert approved == 1
    close_connection()


# ----------------------------------------------------------------- 批量导入


def test_import_reports_lines_and_rolls_back_atomically(svc):
    from app.service import ServiceError
    fs, pid, rec = svc["fs"], svc["pid"], svc["recorder"]
    make_unit(fs, pid, rec, "EXIST")
    payload = {
        "units": [
            {"code": "N1", "unit_type": "layer", "title": "", "attributes": {}},
            {"code": "N1", "unit_type": "feature", "title": "", "attributes": {}},  # 批内重复
        ],
        "relations": [
            {"source": "N1", "target": "N1", "kind": "cuts", "evidence": ""},       # 自环
            {"source": "N1", "target": "GHOST", "kind": "earlier", "evidence": ""},  # 未知单元
        ],
    }
    with pytest.raises(ServiceError) as exc:
        fs.import_batch(pid, rec, payload)
    assert exc.value.code == "import_rejected"
    lines = exc.value.details["lines"]
    assert len(lines) == 3
    assert {line["code_error"] for line in lines} == {"unit_exists", "self_loop", "unknown_unit"}
    # 整体回滚：不留半个单元/半条关系
    assert fs._unit_row(fs.db, pid, "N1") is None
    assert fs.db.execute("SELECT COUNT(*) AS n FROM field_relations").fetchone()["n"] == 0
    assert fs.db.execute("SELECT COUNT(*) AS n FROM field_units").fetchone()["n"] == 1


def test_import_success_then_idempotent_replay(svc):
    fs, pid, rec = svc["fs"], svc["pid"], svc["recorder"]
    payload = {
        "units": [
            {"code": "U1", "unit_type": "trench", "title": "", "attributes": {}},
            {"code": "U2", "unit_type": "layer", "title": "", "attributes": {}},
        ],
        "relations": [{"source": "U1", "target": "U2", "kind": "cuts", "evidence": "冲沟切穿"}],
    }
    first = fs.import_batch(pid, rec, payload, idem_key="batch-1")
    assert first["imported"] == {"units": 2, "relations": 1}
    second = fs.import_batch(pid, rec, payload, idem_key="batch-1")
    assert second["idempotent_replay"] is True
    assert fs.db.execute("SELECT COUNT(*) AS n FROM field_units").fetchone()["n"] == 2
    assert fs.db.execute("SELECT COUNT(*) AS n FROM field_relations").fetchone()["n"] == 1


# ----------------------------------------------------------------- 重启与审计


def test_state_survives_service_restart(svc, field_env: Path):
    from app import database
    from app.field_service import FieldService
    fs, pid, rec, rev = svc["fs"], svc["pid"], svc["recorder"], svc["reviewer"]
    make_unit(fs, pid, rec, "R1")
    fs.seal_unit(pid, rec, "R1")
    digest_before = fs.db.execute("SELECT content_digest FROM field_snapshots").fetchone()["content_digest"]
    make_unit(fs, pid, rec, "R2")
    add_approved(fs, pid, "R1", "R2", "earlier", rec, rev)
    # 模拟服务重启：关闭线程连接后重新打开同一数据库文件
    database.close_connection()
    database.init_db()
    from app.field_store import init_field_db
    init_field_db(database.connection())
    restarted = FieldService()
    unit = restarted.get_unit(pid, rec, "R1")
    assert unit["status"] == "sealed"
    assert unit["latest_snapshot"]["content_digest"] == digest_before
    tr = restarted.transitive_relations(pid, rec, "R1")
    assert [t["code"] for t in tr["temporal"]] == ["R2"]
    assert len(restarted.unit_audit(pid, rec, "R1")["data"]) >= 3


def test_audit_events_for_unit_and_relations(svc):
    fs, pid, rec, rev = svc["fs"], svc["pid"], svc["recorder"], svc["reviewer"]
    make_unit(fs, pid, rec, "AU1"); make_unit(fs, pid, rec, "AU2")
    rid = fs.create_relation(pid, rec, {"source": "AU1", "target": "AU2", "kind": "cuts", "evidence": "", "attributes": {}})["id"]
    fs.review_relation(pid, rev, rid, {"decision": "rejected", "comment": "证据不足"})
    events = fs.unit_audit(pid, rec, "AU1")["data"]
    actions = [e["action"] for e in events]
    assert "field.unit.create" in actions
    assert "field.relation.create" in actions
    assert "field.relation.review" in actions
