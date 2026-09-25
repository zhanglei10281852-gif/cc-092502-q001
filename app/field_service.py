"""田野上下文服务：探方/发掘单元/层位/遗迹的登记、封存、版本纠错与关系管理。

关系图规则（仅以已通过复核的 approved 边为准，pending 边不参与推导）：
- earlier（早于）与 cuts（切叠）为有向时态边，方向为 source -> target 表示
  source 在时代上早于/切叠 target；
- equivalent（等同）为无向边，用并查收缩聚为等价类；
- 拒绝自环；拒绝已能从图中推导出的冗余边；拒绝逆序矛盾边与任何循环。

所有跨表写入都放在 BEGIN IMMEDIATE 事务中；封存生成带哈希链的不可变快照，
封存后的纠错只能追加带原因的新版本（乐观锁 base_version）。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any, Iterable

from app.database import connection, now, transaction
from app.security import request_hash, stable_json
from app.service import ResearchService, ServiceError

UNIT_WRITERS = {"owner", "researcher", "recorder"}
RELATION_REVIEWERS = {"owner", "reviewer"}
PROJECT_READERS = {"owner", "researcher", "recorder", "reviewer", "viewer"}
TEMPORAL_KINDS = ("earlier", "cuts")


class _DSU:
    def __init__(self, nodes: Iterable[int]):
        self.parent = {node: node for node in nodes}

    def find(self, node: int) -> int:
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != node:
            node, self.parent[node] = self.parent[node], root
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _reachable(adj: dict[int, set[int]], start: int, goal: int) -> bool:
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for nxt in adj.get(node, ()):  # 收缩后可能出现自环，视为可达
            if nxt == goal:
                return True
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def _has_cycle(adj: dict[int, set[int]]) -> bool:
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[int, int] = defaultdict(int)
    for node in list(adj):
        if color[node] != WHITE:
            continue
        stack: list[tuple[int, Iterable[int]]] = [(node, iter(adj.get(node, ())))]
        color[node] = GRAY
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if color[nxt] == GRAY:
                    return True  # 含自环（nxt==node）
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(adj.get(nxt, ()))))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()
    return False


def _temporal_order(source_id: int, target_id: int, kind: str) -> tuple[int, int]:
    """把时态关系换算成"早 -> 晚"的年代序。

    earlier(s,t)：s 早于 t，序 s -> t；
    cuts(s,t)（s 切叠/打破 t）：切叠者晚于被切叠者，序 t -> s。
    """
    return (source_id, target_id) if kind == "earlier" else (target_id, source_id)


def build_graph(edges: Iterable[tuple[int, int, str]]):
    """构造等价类并查集与"早 -> 晚"年代序邻接表（边上保留原始关系类型标签）。"""
    edges = list(edges)
    nodes: set[int] = set()
    for s, t, _ in edges:
        nodes.add(s)
        nodes.add(t)
    dsu = _DSU(nodes)
    for s, t, kind in edges:
        if kind == "equivalent":
            dsu.union(s, t)
    order: dict[int, set[int]] = defaultdict(set)
    labeled: dict[int, set[tuple[int, str]]] = defaultdict(set)
    for s, t, kind in edges:
        if kind in TEMPORAL_KINDS:
            early, late = _temporal_order(s, t, kind)
            re, rl = dsu.find(early), dsu.find(late)
            order[re].add(rl)
            labeled[re].add((rl, kind))
    return dsu, order, labeled, nodes


def validate_new_edge(
    existing: list[tuple[int, int, str]],
    source_id: int,
    target_id: int,
    kind: str,
) -> str | None:
    """返回错误代码；None 表示候选边可以加入。

    判定均在"早 -> 晚"年代序（经等价类收缩）上进行：
    自环 -> 冗余（年代序已存在）-> 矛盾（逆向年代序/成环）。
    """
    if source_id == target_id:
        return "self_loop"
    dsu, order, _, _ = build_graph(existing)
    dsu.parent.setdefault(source_id, source_id)  # 尚无关系的单元不在边节点集中
    dsu.parent.setdefault(target_id, target_id)
    rs, rt = dsu.find(source_id), dsu.find(target_id)
    if kind == "equivalent":
        if rs == rt:
            return "relation_redundant"  # 已属同一等价类
        if _reachable(order, rs, rt) or _reachable(order, rt, rs):
            return "relation_contradiction"  # 已有年代次序的单元不能再判为等同
        return None
    if rs == rt:
        return "relation_contradiction"  # 等同单元之间不能再有时态关系
    early, late = (rs, rt) if kind == "earlier" else (rt, rs)
    if _reachable(order, early, late):
        return "relation_redundant"  # 年代序已可推导
    if _reachable(order, late, early):
        return "relation_contradiction"  # 逆向年代序：矛盾且会成环
    order[early].add(late)
    try:
        if _has_cycle(order):
            return "relation_cycle"
    finally:
        order[early].discard(late)
    return None


class FieldService:
    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()
        self.core = ResearchService(self.db)

    # ------------------------------------------------------------------ 工具

    def _reader(self, project_id: int, actor_id: int) -> None:
        self.core.require_role(project_id, actor_id, PROJECT_READERS)

    def _writer(self, project_id: int, actor_id: int) -> None:
        self.core.require_role(project_id, actor_id, UNIT_WRITERS)

    def _idem_lookup(self, scope: str, key: str, digest: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT request_hash,response_json FROM idempotency_records WHERE scope=? AND request_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != digest:
            raise ServiceError("idempotency_conflict", "幂等键对应的请求内容不同", 409)
        replay = json.loads(row["response_json"])
        replay["idempotent_replay"] = True
        return replay

    def _idem_store(self, db: sqlite3.Connection, scope: str, key: str, digest: str, response: dict[str, Any], stamp: str) -> None:
        db.execute(
            "INSERT INTO idempotency_records(scope,request_key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
            (scope, key, digest, stable_json(response), stamp),
        )

    def _unit_row(self, db: sqlite3.Connection, project_id: int, code: str) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM field_units WHERE project_id=? AND code=?", (project_id, code)).fetchone()

    @staticmethod
    def unit_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["attributes"] = json.loads(data.pop("attributes_json"))
        return data

    @staticmethod
    def relation_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["attributes"] = json.loads(data.pop("attributes_json"))
        return data

    def _snapshot_content(self, unit: sqlite3.Row, version_no: int) -> dict[str, Any]:
        return {
            "project_id": unit["project_id"],
            "unit_id": unit["id"],
            "code": unit["code"],
            "unit_type": unit["unit_type"],
            "title": unit["title"],
            "attributes": json.loads(unit["attributes_json"]),
            "version_no": version_no,
            "created_by": unit["created_by"],
            "unit_created_at": unit["created_at"],
        }

    # ------------------------------------------------------------------ 单元

    def create_unit(self, project_id: int, actor_id: int, payload: dict[str, Any], idem_key: str = "") -> dict[str, Any]:
        self._writer(project_id, actor_id)
        digest = request_hash(payload)
        scope, key = "field.unit.create", f"{project_id}:{idem_key}"
        if idem_key:
            replay = self._idem_lookup(scope, key, digest)
            if replay is not None:
                return replay
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                if self._unit_row(db, project_id, payload["code"]) is not None:
                    raise ServiceError("unit_exists", "项目内单元编号已存在", 409)
                cursor = db.execute(
                    "INSERT INTO field_units(project_id,code,unit_type,title,attributes_json,current_version,status,created_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,1,'draft',?,?,?)",
                    (project_id, payload["code"], payload["unit_type"], payload.get("title", ""), stable_json(payload.get("attributes", {})), actor_id, stamp, stamp),
                )
                unit_id = cursor.lastrowid
                db.execute(
                    "INSERT INTO field_unit_versions(unit_id,project_id,version_no,unit_type,title,attributes_json,change_reason,status,created_by,created_at)"
                    " VALUES(?,?,1,?,?,?, '','draft',?,?)",
                    (unit_id, project_id, payload["unit_type"], payload.get("title", ""), stable_json(payload.get("attributes", {})), actor_id, stamp),
                )
                result = self.unit_dict(db.execute("SELECT * FROM field_units WHERE id=?", (unit_id,)).fetchone())
                self.core.audit("field.unit.create", "field_unit", payload["code"], {"unit_id": unit_id, **payload}, project_id=project_id, actor_id=actor_id)
                if idem_key:
                    self._idem_store(db, scope, key, digest, result, stamp)
                return result
        except sqlite3.IntegrityError as exc:
            raise ServiceError("unit_exists", "项目内单元编号已存在", 409) from exc

    def update_unit(self, project_id: int, actor_id: int, code: str, payload: dict[str, Any]) -> dict[str, Any]:
        """修改未封存单元的草稿（就地更新当前草稿版本，不产生新版本号）。"""
        self._writer(project_id, actor_id)
        with transaction(immediate=True) as db:
            unit = self._unit_row(db, project_id, code)
            if unit is None:
                raise ServiceError("unit_not_found", "发掘单元不存在", 404)
            if unit["status"] != "draft":
                raise ServiceError("unit_sealed", "单元已封存，纠错只能创建带原因的新版本", 409)
            if payload.get("base_version") is not None and payload["base_version"] != unit["current_version"]:
                raise ServiceError("version_conflict", "单元版本已变化，请基于最新版本修改", 409)
            title = payload["title"] if payload.get("title") is not None else unit["title"]
            attributes = payload["attributes"] if payload.get("attributes") is not None else json.loads(unit["attributes_json"])
            stamp = now()
            db.execute("UPDATE field_units SET title=?,attributes_json=?,updated_at=? WHERE id=?", (title, stable_json(attributes), stamp, unit["id"]))
            db.execute(
                "UPDATE field_unit_versions SET title=?,attributes_json=?,created_by=?,created_at=? WHERE unit_id=? AND version_no=?",
                (title, stable_json(attributes), actor_id, stamp, unit["id"], unit["current_version"]),
            )
            self.core.audit("field.unit.update", "field_unit", code, {"title": title, "attributes": attributes}, project_id=project_id, actor_id=actor_id)
            return self.unit_dict(self._unit_row(db, project_id, code))

    def seal_unit(self, project_id: int, actor_id: int, code: str) -> dict[str, Any]:
        self._writer(project_id, actor_id)
        with transaction(immediate=True) as db:
            unit = self._unit_row(db, project_id, code)
            if unit is None:
                raise ServiceError("unit_not_found", "发掘单元不存在", 404)
            if unit["status"] != "draft":
                raise ServiceError("unit_sealed", "单元已封存，不能重复封存", 409)
            version_no = unit["current_version"]
            content = self._snapshot_content(unit, version_no)
            prev = db.execute("SELECT content_digest FROM field_snapshots WHERE unit_id=? ORDER BY version_no DESC LIMIT 1", (unit["id"],)).fetchone()
            prev_digest = prev["content_digest"] if prev else ""
            digest = request_hash({"prev_digest": prev_digest, "content": content})
            stamp = now()
            db.execute(
                "INSERT INTO field_snapshots(unit_id,project_id,version_no,content_json,content_digest,prev_digest,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (unit["id"], project_id, version_no, stable_json(content), digest, prev_digest, actor_id, stamp),
            )
            db.execute("UPDATE field_units SET status='sealed',sealed_by=?,sealed_at=?,updated_at=? WHERE id=?", (actor_id, stamp, stamp, unit["id"]))
            db.execute("UPDATE field_unit_versions SET status='sealed' WHERE unit_id=? AND version_no=?", (unit["id"], version_no))
            self.core.audit("field.unit.seal", "field_unit", code, {"version_no": version_no, "content_digest": digest}, project_id=project_id, actor_id=actor_id)
            return {
                "unit": self.unit_dict(self._unit_row(db, project_id, code)),
                "snapshot": {"version_no": version_no, "content": content, "content_digest": digest, "prev_digest": prev_digest, "sealed_at": stamp},
            }

    def correct_unit(self, project_id: int, actor_id: int, code: str, payload: dict[str, Any]) -> dict[str, Any]:
        """封存后纠错：追加带原因的新版本（乐观锁），单元重新进入草稿待复核封存。"""
        self._writer(project_id, actor_id)
        with transaction(immediate=True) as db:
            unit = self._unit_row(db, project_id, code)
            if unit is None:
                raise ServiceError("unit_not_found", "发掘单元不存在", 404)
            if payload["base_version"] != unit["current_version"]:
                raise ServiceError("version_conflict", "基准版本不是当前版本，纠错冲突", 409)
            if unit["status"] != "sealed":
                raise ServiceError("unit_not_sealed", "单元尚未封存，普通修改即可，无需纠错版本", 409)
            title = payload["title"] if payload.get("title") is not None else unit["title"]
            attributes = payload["attributes"] if payload.get("attributes") is not None else json.loads(unit["attributes_json"])
            new_version = unit["current_version"] + 1
            stamp = now()
            db.execute(
                "INSERT INTO field_unit_versions(unit_id,project_id,version_no,unit_type,title,attributes_json,change_reason,status,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?, 'draft',?,?)",
                (unit["id"], project_id, new_version, unit["unit_type"], title, stable_json(attributes), payload["change_reason"], actor_id, stamp),
            )
            db.execute(
                "UPDATE field_units SET title=?,attributes_json=?,current_version=?,status='draft',updated_at=? WHERE id=?",
                (title, stable_json(attributes), new_version, stamp, unit["id"]),
            )
            self.core.audit(
                "field.unit.correct", "field_unit", code,
                {"base_version": payload["base_version"], "new_version": new_version, "change_reason": payload["change_reason"], "title": title, "attributes": attributes},
                project_id=project_id, actor_id=actor_id,
            )
            return self.unit_dict(self._unit_row(db, project_id, code))

    def get_unit(self, project_id: int, actor_id: int, code: str) -> dict[str, Any]:
        self._reader(project_id, actor_id)
        unit = self._unit_row(self.db, project_id, code)
        if unit is None:
            raise ServiceError("unit_not_found", "发掘单元不存在", 404)
        result = self.unit_dict(unit)
        snap = self.db.execute("SELECT version_no,content_digest,created_at AS sealed_at FROM field_snapshots WHERE unit_id=? ORDER BY version_no DESC LIMIT 1", (unit["id"],)).fetchone()
        result["latest_snapshot"] = dict(snap) if snap else None
        return result

    def list_units(self, project_id: int, actor_id: int) -> dict[str, Any]:
        self._reader(project_id, actor_id)
        rows = self.db.execute("SELECT * FROM field_units WHERE project_id=? ORDER BY code", (project_id,)).fetchall()
        return {"data": [self.unit_dict(row) for row in rows]}

    def version_history(self, project_id: int, actor_id: int, code: str) -> dict[str, Any]:
        self._reader(project_id, actor_id)
        unit = self._unit_row(self.db, project_id, code)
        if unit is None:
            raise ServiceError("unit_not_found", "发掘单元不存在", 404)
        rows = self.db.execute(
            "SELECT v.version_no,v.status,v.change_reason,v.created_by,v.created_at,"
            "s.content_digest,s.prev_digest,s.content_json,s.created_at AS sealed_at "
            "FROM field_unit_versions v LEFT JOIN field_snapshots s ON s.unit_id=v.unit_id AND s.version_no=v.version_no"
            " WHERE v.unit_id=? ORDER BY v.version_no",
            (unit["id"],),
        ).fetchall()
        versions = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = None
            if item["content_digest"]:
                item["snapshot"] = {"content_digest": item.pop("content_digest"), "prev_digest": item.pop("prev_digest"), "sealed_at": item.pop("sealed_at")}
            else:
                item.pop("content_digest"); item.pop("prev_digest"); item.pop("sealed_at")
            item.pop("content_json")
            versions.append(item)
        return {"unit": code, "current_version": unit["current_version"], "versions": versions}

    # ------------------------------------------------------------------ 关系

    def _approved_edges(self, db: sqlite3.Connection, project_id: int) -> list[tuple[int, int, str]]:
        return [(r["source_id"], r["target_id"], r["kind"]) for r in db.execute(
            "SELECT source_id,target_id,kind FROM field_relations WHERE project_id=? AND status='approved'", (project_id,))]

    def create_relation(self, project_id: int, actor_id: int, payload: dict[str, Any], idem_key: str = "") -> dict[str, Any]:
        self._writer(project_id, actor_id)
        digest = request_hash(payload)
        scope, key = "field.relation.create", f"{project_id}:{idem_key}"
        if idem_key:
            replay = self._idem_lookup(scope, key, digest)
            if replay is not None:
                return replay
        stamp = now()
        with transaction(immediate=True) as db:
            source = self._unit_row(db, project_id, payload["source"])
            target = self._unit_row(db, project_id, payload["target"])
            if source is None or target is None:
                missing = payload["source"] if source is None else payload["target"]
                raise ServiceError("unknown_unit", f"关系引用的单元不存在: {missing}", 404)
            if source["id"] == target["id"]:
                raise ServiceError("self_loop", "关系不能指向单元自身（自环）", 422)
            dup = db.execute(
                "SELECT id,status FROM field_relations WHERE project_id=? AND source_id=? AND target_id=? AND kind=?",
                (project_id, source["id"], target["id"], payload["kind"]),
            ).fetchone()
            if dup is not None and dup["status"] != "rejected":
                raise ServiceError("relation_exists", f"该关系已登记（当前状态 {dup['status']}）", 409)
            error = validate_new_edge(self._approved_edges(db, project_id), source["id"], target["id"], payload["kind"])
            if error is not None:
                message = {
                    "relation_redundant": "该关系可由已有关系推导，属于冗余边",
                    "relation_contradiction": "该关系与已有关系矛盾",
                    "relation_cycle": "加入该关系会形成关系循环",
                }[error]
                raise ServiceError(error, message, 422)
            if dup is not None:  # 复用此前被驳回的边，修正证据后重新进入待复核
                db.execute(
                    "UPDATE field_relations SET evidence=?,attributes_json=?,status='pending',created_by=?,"
                    "reviewed_by=NULL,review_comment='',reviewed_at='',updated_at=? WHERE id=?",
                    (payload.get("evidence", ""), stable_json(payload.get("attributes", {})), actor_id, stamp, dup["id"]),
                )
                relation_id = dup["id"]
            else:
                cursor = db.execute(
                    "INSERT INTO field_relations(project_id,source_id,target_id,kind,evidence,attributes_json,status,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,'pending',?,?)",
                    (project_id, source["id"], target["id"], payload["kind"], payload.get("evidence", ""), stable_json(payload.get("attributes", {})), actor_id, stamp),
                )
                relation_id = cursor.lastrowid
            result = self._load_relation(db, relation_id)
            self.core.audit("field.relation.create", "field_relation", str(relation_id), {**payload, "relation_id": relation_id, "resubmitted": dup is not None}, project_id=project_id, actor_id=actor_id)
            if idem_key:
                self._idem_store(db, scope, key, digest, result, stamp)
            return result

    def review_relation(self, project_id: int, reviewer_id: int, relation_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.core.require_role(project_id, reviewer_id, RELATION_REVIEWERS)
        with transaction(immediate=True) as db:
            rel = db.execute("SELECT * FROM field_relations WHERE id=? AND project_id=?", (relation_id, project_id)).fetchone()
            if rel is None:
                raise ServiceError("relation_not_found", "关系不存在", 404)
            if rel["status"] != "pending":
                raise ServiceError("relation_reviewed", "该关系已复核，不能重复审核", 409)
            if rel["created_by"] == reviewer_id:
                raise ServiceError("self_review_forbidden", "复核人不能审核自己提交的关系", 403)
            stamp = now()
            if payload["decision"] == "approved":
                # 提交后可能已有其他关系通过复核，批准时按当前图重新校验
                error = validate_new_edge(self._approved_edges(db, project_id), rel["source_id"], rel["target_id"], rel["kind"])
                if error is not None:
                    message = {
                        "relation_redundant": "该关系已可由现有关系推导",
                        "relation_contradiction": "该关系与现有关系矛盾",
                        "relation_cycle": "批准该关系会形成关系循环",
                    }[error]
                    raise ServiceError(error, message, 409)
            db.execute(
                "UPDATE field_relations SET status=?,reviewed_by=?,review_comment=?,reviewed_at=?,updated_at=? WHERE id=?",
                (payload["decision"], reviewer_id, payload.get("comment", ""), stamp, stamp, relation_id),
            )
            db.execute(
                "INSERT INTO field_relation_reviews(relation_id,project_id,decision,comment,reviewer_id,created_at) VALUES(?,?,?,?,?,?)",
                (relation_id, project_id, payload["decision"], payload.get("comment", ""), reviewer_id, stamp),
            )
            source_code = db.execute("SELECT code FROM field_units WHERE id=?", (rel["source_id"],)).fetchone()["code"]
            target_code = db.execute("SELECT code FROM field_units WHERE id=?", (rel["target_id"],)).fetchone()["code"]
            self.core.audit(
                "field.relation.review", "field_relation", str(relation_id),
                {"relation_id": relation_id, "source": source_code, "target": target_code, **payload},
                project_id=project_id, actor_id=reviewer_id,
            )
            return self._load_relation(db, relation_id)

    def _load_relation(self, db: sqlite3.Connection, relation_id: int) -> dict[str, Any]:
        row = db.execute(
            "SELECT r.*,su.code AS source_code,tu.code AS target_code,cu.display_name AS created_name,ru.display_name AS reviewed_name"
            " FROM field_relations r"
            " JOIN field_units su ON su.id=r.source_id JOIN field_units tu ON tu.id=r.target_id"
            " LEFT JOIN users cu ON cu.id=r.created_by LEFT JOIN users ru ON ru.id=r.reviewed_by WHERE r.id=?",
            (relation_id,),
        ).fetchone()
        return self.relation_dict(row)

    def list_relations(self, project_id: int, actor_id: int, status: str | None = None, kind: str | None = None) -> dict[str, Any]:
        self._reader(project_id, actor_id)
        sql = ("SELECT r.*,su.code AS source_code,tu.code AS target_code,cu.display_name AS created_name,ru.display_name AS reviewed_name"
               " FROM field_relations r JOIN field_units su ON su.id=r.source_id JOIN field_units tu ON tu.id=r.target_id"
               " LEFT JOIN users cu ON cu.id=r.created_by LEFT JOIN users ru ON ru.id=r.reviewed_by WHERE r.project_id=?")
        args: list[Any] = [project_id]
        if status:
            sql += " AND r.status=?"; args.append(status)
        if kind:
            sql += " AND r.kind=?"; args.append(kind)
        sql += " ORDER BY r.id"
        return {"data": [self.relation_dict(row) for row in self.db.execute(sql, args)]}

    def direct_relations(self, project_id: int, actor_id: int, code: str, status: str | None = None) -> dict[str, Any]:
        """某单元的直接关系（入边与出边双向可查）。"""
        self._reader(project_id, actor_id)
        unit = self._unit_row(self.db, project_id, code)
        if unit is None:
            raise ServiceError("unit_not_found", "发掘单元不存在", 404)
        sql = ("SELECT r.*,su.code AS source_code,tu.code AS target_code,cu.display_name AS created_name,ru.display_name AS reviewed_name"
               " FROM field_relations r JOIN field_units su ON su.id=r.source_id JOIN field_units tu ON tu.id=r.target_id"
               " LEFT JOIN users cu ON cu.id=r.created_by LEFT JOIN users ru ON ru.id=r.reviewed_by"
               " WHERE r.project_id=? AND (r.source_id=? OR r.target_id=?)")
        args: list[Any] = [project_id, unit["id"], unit["id"]]
        if status:
            sql += " AND r.status=?"; args.append(status)
        sql += " ORDER BY r.id"
        data = []
        for row in self.db.execute(sql, args):
            item = self.relation_dict(row)
            item["direction"] = "outgoing" if row["source_id"] == unit["id"] else "incoming"
            item["other"] = row["target_code"] if item["direction"] == "outgoing" else row["source_code"]
            data.append(item)
        return {"unit": code, "data": data}

    def transitive_relations(self, project_id: int, actor_id: int, code: str) -> dict[str, Any]:
        """仅基于 approved 边推导传递关系。

        在等价类收缩后的"早 -> 晚"年代序上：正向可达为晚于本单元，逆向可达为
        早于本单元；via_kinds 给出路径上出现过的原始关系类型（earlier/cuts）。
        """
        self._reader(project_id, actor_id)
        unit = self._unit_row(self.db, project_id, code)
        if unit is None:
            raise ServiceError("unit_not_found", "发掘单元不存在", 404)
        dsu, _, labeled, nodes = build_graph(self._approved_edges(self.db, project_id))
        empty = {"unit": code, "equivalent": [], "earlier": [], "later": [], "temporal": []}
        if unit["id"] not in nodes:
            return empty
        root = dsu.find(unit["id"])
        codes = {row["id"]: row["code"] for row in self.db.execute("SELECT id,code FROM field_units WHERE project_id=?", (project_id,))}

        def members(rep: int) -> list[str]:
            return sorted(codes[n] for n in nodes if dsu.find(n) == rep and n != unit["id"])

        reverse: dict[int, set[tuple[int, str]]] = defaultdict(set)
        for rs, edges in labeled.items():
            for rt, kind in edges:
                reverse[rt].add((rs, kind))

        def closure(neighbours: dict[int, set[tuple[int, str]]]) -> dict[int, set[str]]:
            acc: dict[int, set[str]] = {}
            stack = [(root, frozenset())]
            seen: set[tuple[int, frozenset]] = {(root, frozenset())}
            while stack:
                node, kinds = stack.pop()
                for nxt, edge_kind in neighbours.get(node, ()):
                    nxt_kinds = kinds | {edge_kind}
                    if nxt != root:
                        acc.setdefault(nxt, set()).update(nxt_kinds)
                    state = (nxt, nxt_kinds)
                    if state not in seen:
                        seen.add(state)
                        stack.append(state)
            return acc

        earlier_reps = closure(reverse)
        later_reps = closure(labeled)
        def class_codes(rep: int) -> list[str]:
            return sorted(codes[n] for n in nodes if dsu.find(n) == rep)

        earlier_reps = closure(reverse)
        later_reps = closure(labeled)
        temporal: list[dict[str, Any]] = []
        for rep, kinds in earlier_reps.items():
            temporal.append({"code": codes[rep], "position": "earlier", "via_kinds": sorted(kinds), "via_equivalent_members": members(rep)})
        for rep, kinds in later_reps.items():
            temporal.append({"code": codes[rep], "position": "later", "via_kinds": sorted(kinds), "via_equivalent_members": members(rep)})
        temporal.sort(key=lambda item: item["code"])
        earlier_codes = sorted(code for rep in earlier_reps for code in class_codes(rep))
        later_codes = sorted(code for rep in later_reps for code in class_codes(rep))
        return {
            "unit": code,
            "equivalent": members(root),
            "earlier": earlier_codes,
            "later": later_codes,
            "temporal": temporal,
        }

    def unit_audit(self, project_id: int, actor_id: int, code: str) -> dict[str, Any]:
        self._reader(project_id, actor_id)
        if self._unit_row(self.db, project_id, code) is None:
            raise ServiceError("unit_not_found", "发掘单元不存在", 404)
        rows = self.db.execute(
            "SELECT * FROM audit_events WHERE project_id=? AND resource_type IN ('field_unit','field_relation') ORDER BY id",
            (project_id,),
        ).fetchall()
        data = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if row["resource_type"] == "field_unit":
                if row["resource_id"] != code:
                    continue
            elif payload.get("source") != code and payload.get("target") != code:
                continue
            data.append(dict(row))
        return {"unit": code, "data": data}

    # ------------------------------------------------------------------ 导入

    def import_batch(self, project_id: int, actor_id: int, payload: dict[str, Any], idem_key: str = "") -> dict[str, Any]:
        """整批原子导入：逐行收集错误；任一行失败则整体回滚，不留半条关系。"""
        self._writer(project_id, actor_id)
        digest = request_hash(payload)
        scope, key = "field.import", f"{project_id}:{idem_key}"
        if idem_key:
            replay = self._idem_lookup(scope, key, digest)
            if replay is not None:
                return replay
        stamp = now()
        with transaction(immediate=True) as db:
            errors: list[dict[str, Any]] = []
            id_map: dict[str, int] = {r["code"]: r["id"] for r in db.execute("SELECT id,code FROM field_units WHERE project_id=?", (project_id,))}
            imported_units: list[str] = []
            seen_codes: set[str] = set()

            for line, item in enumerate(payload.get("units", []), start=1):
                if item["code"] in seen_codes or item["code"] in id_map:
                    errors.append({"section": "units", "line": line, "code": item["code"], "code_error": "unit_exists", "message": "单元编号在项目内或批次内重复"})
                    continue
                seen_codes.add(item["code"])
                cursor = db.execute(
                    "INSERT INTO field_units(project_id,code,unit_type,title,attributes_json,current_version,status,created_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,1,'draft',?,?,?)",
                    (project_id, item["code"], item["unit_type"], item.get("title", ""), stable_json(item.get("attributes", {})), actor_id, stamp, stamp),
                )
                unit_id = cursor.lastrowid
                id_map[item["code"]] = unit_id
                db.execute(
                    "INSERT INTO field_unit_versions(unit_id,project_id,version_no,unit_type,title,attributes_json,change_reason,status,created_by,created_at)"
                    " VALUES(?,?,1,?,?,?, '','draft',?,?)",
                    (unit_id, project_id, item["unit_type"], item.get("title", ""), stable_json(item.get("attributes", {})), actor_id, stamp),
                )
                imported_units.append(item["code"])

            provisional = self._approved_edges(db, project_id)
            seen_relations: set[tuple[str, str, str]] = set()
            relations_to_insert: list[dict[str, Any]] = []
            for line, item in enumerate(payload.get("relations", []), start=1):
                ref = (item["source"], item["target"], item["kind"])
                source_id = id_map.get(item["source"])
                target_id = id_map.get(item["target"])
                if source_id is None or target_id is None:
                    missing = item["source"] if source_id is None else item["target"]
                    errors.append({"section": "relations", "line": line, "ref": ref, "code_error": "unknown_unit", "message": f"引用的单元不存在: {missing}"})
                    continue
                if source_id == target_id:
                    errors.append({"section": "relations", "line": line, "ref": ref, "code_error": "self_loop", "message": "关系不能指向单元自身"})
                    continue
                dup = db.execute(
                    "SELECT 1 FROM field_relations WHERE project_id=? AND source_id=? AND target_id=? AND kind=?",
                    (project_id, source_id, target_id, item["kind"]),
                ).fetchone()
                if dup is not None or ref in seen_relations:
                    errors.append({"section": "relations", "line": line, "ref": ref, "code_error": "relation_exists", "message": "关系重复"})
                    continue
                error = validate_new_edge(provisional, source_id, target_id, item["kind"])
                if error is not None:
                    message = {
                        "relation_redundant": "关系可由已有关系推导",
                        "relation_contradiction": "关系与已有关系矛盾",
                        "relation_cycle": "关系在批次内形成循环",
                        "self_loop": "关系不能指向单元自身",
                    }[error]
                    errors.append({"section": "relations", "line": line, "ref": ref, "code_error": error, "message": message})
                    continue
                seen_relations.add(ref)
                provisional.append((source_id, target_id, item["kind"]))
                relations_to_insert.append({"source_id": source_id, "target_id": target_id, **item})

            if errors:
                raise ServiceError("import_rejected", "批量导入存在错误，已整体回滚", 422, details={"lines": errors, "imported": {"units": 0, "relations": 0}})

            imported_relations: list[dict[str, Any]] = []
            for item in relations_to_insert:
                cursor = db.execute(
                    "INSERT INTO field_relations(project_id,source_id,target_id,kind,evidence,attributes_json,status,created_by,created_at)"
                    " VALUES(?,?,?,?,?, '{}','pending',?,?)",
                    (project_id, item["source_id"], item["target_id"], item["kind"], item.get("evidence", ""), actor_id, stamp),
                )
                imported_relations.append(self._load_relation(db, cursor.lastrowid))
            self.core.audit(
                "field.import", "field_batch", f"{project_id}:{stamp}",
                {"units": len(imported_units), "relations": len(imported_relations)}, project_id=project_id, actor_id=actor_id,
            )
            result = {"units": imported_units, "relations": imported_relations, "imported": {"units": len(imported_units), "relations": len(imported_relations)}}
            if idem_key:
                self._idem_store(db, scope, key, digest, result, stamp)
            return result
