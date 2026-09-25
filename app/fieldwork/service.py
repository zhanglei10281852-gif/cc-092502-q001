"""田野上下文模块的业务层：登记、封存、复核、关系校验与批量导入。

一致性约定：
- 所有写操作都在 BEGIN IMMEDIATE 事务内完成，版本号作为乐观并发令牌；
- 等同关系用并查集收缩为等价类，切叠/早晚归一化为“晚于”有向边，
  插入前校验自环、矛盾边与可推导出的关系循环；
- 封存与纠错会写入不可变的快照版本（含内容摘要），历史版本只可新增不可改写。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from app.database import connection, now
from app.fieldwork import store
from app.security import request_hash, stable_json
from app.service import ResearchService, ServiceError

WRITE_ROLES = {"owner", "researcher", "recorder"}
CORRECT_ROLES = {"owner", "researcher", "reviewer"}
REVIEW_ROLES = {"owner", "reviewer"}
READ_ROLES = {"owner", "researcher", "recorder", "reviewer", "viewer"}

_UNIT_FIELDS = ("kind", "label", "description", "square_id")


def _norm(value: str, field: str) -> str:
    text = str(value).strip().upper()
    if not text:
        raise ServiceError("invalid_identifier", f"{field}不能为空", 400)
    return text


class _Graph:
    """项目关系图：等同等价类（并查集）+ 类上的“晚于”有向边。"""

    def __init__(self, rows: list[sqlite3.Row]):
        self.parent: dict[int, int] = {}
        self.later: dict[int, set[int]] = {}
        for row in rows:
            if row["relation"] == "equals":
                self._union(row["from_unit"], row["to_unit"])
        for row in rows:
            if row["relation"] == "cuts":
                later, earlier = row["from_unit"], row["to_unit"]
            elif row["relation"] == "earlier":
                later, earlier = row["to_unit"], row["from_unit"]
            else:
                continue
            self.later.setdefault(self.find(later), set()).add(self.find(earlier))

    def find(self, unit_id: int) -> int:
        self.parent.setdefault(unit_id, unit_id)
        root = unit_id
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[unit_id] != root:
            self.parent[unit_id], unit_id = root, self.parent[unit_id]
        return root

    def _union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def reachable(self, start: int, target: int) -> bool:
        """target 是否可从 start 沿“晚于”边到达（即 target 更早）。"""
        seen, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self.later.get(node, ()))
        return False


class FieldworkService:
    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()
        self.research = ResearchService(self.db)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _idempotent(self, scope: str, key: str, payload: Any, produce: Callable[[sqlite3.Connection], dict[str, Any]]) -> dict[str, Any]:
        digest = request_hash(payload)
        with self._tx() as db:
            if key:
                old = db.execute("SELECT * FROM idempotency_records WHERE scope=? AND request_key=?", (scope, key)).fetchone()
                if old is not None:
                    if old["request_hash"] != digest:
                        raise ServiceError("idempotency_conflict", "幂等键对应的请求内容不同", 409)
                    return json.loads(old["response_json"])
            result = produce(db)
            if key:
                db.execute(
                    "INSERT INTO idempotency_records(scope,request_key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                    (scope, key, digest, stable_json(result), now()),
                )
            return result

    # ---- 基础读取 ----

    @staticmethod
    def _unit_or_404(db: sqlite3.Connection, project_id: int, unit_id: int) -> sqlite3.Row:
        row = store.get_unit(db, project_id, unit_id)
        if row is None:
            raise ServiceError("unit_not_found", "发掘单元不存在", 404)
        return row

    @staticmethod
    def _expect(row: sqlite3.Row, expected: int, allowed: set[str]) -> None:
        if row["version"] != expected:
            raise ServiceError("version_conflict", "数据版本已被其他操作变更，请刷新后重试", 409)
        if row["status"] not in allowed:
            raise ServiceError("invalid_status", f"单元当前状态为 {row['status']}，不允许执行该操作", 409)

    @staticmethod
    def _unit_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "number": row["number"], "kind": row["kind"], "label": row["label"], "status": row["status"], "version": row["version"]}

    @staticmethod
    def _version_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["snapshot"] = json.loads(data.pop("snapshot_json"))
        return data

    # ---- 探方 ----

    def create_square(self, project_id: int, payload: dict[str, Any], actor_id: int, key: str = "") -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, WRITE_ROLES)
        code = _norm(payload["code"], "探方编号")
        return self._idempotent(
            f"fieldwork.square.create:{project_id}", key, {"project_id": project_id, "payload": payload},
            lambda db: self._create_square(db, project_id, code, payload.get("name", ""), actor_id),
        )

    def _create_square(self, db: sqlite3.Connection, project_id: int, code: str, name: str, actor_id: int) -> dict[str, Any]:
        stamp = now()
        try:
            cursor = db.execute(
                "INSERT INTO context_squares(project_id,code,name,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (project_id, code, name, actor_id, stamp, stamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError("square_exists", "探方编号在项目中已存在", 409) from exc
        self.research.audit("context.square.create", "context_square", str(cursor.lastrowid), {"code": code, "name": name}, project_id=project_id, actor_id=actor_id)
        return dict(store.get_square(db, project_id, cursor.lastrowid))

    def list_squares(self, project_id: int, actor_id: int) -> list[dict[str, Any]]:
        self.research.require_role(project_id, actor_id, READ_ROLES)
        return [dict(row) for row in store.list_squares(self.db, project_id)]

    # ---- 发掘单元 ----

    def create_unit(self, project_id: int, payload: dict[str, Any], actor_id: int, key: str = "") -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, WRITE_ROLES)
        number = _norm(payload["number"], "单元编号")
        return self._idempotent(
            f"fieldwork.unit.create:{project_id}", key, {"project_id": project_id, "payload": payload},
            lambda db: self._create_unit(db, project_id, number, payload["kind"], payload["square_id"], payload.get("label", ""), payload.get("description", ""), actor_id),
        )

    def _create_unit(self, db: sqlite3.Connection, project_id: int, number: str, kind: str, square_id: int, label: str, description: str, actor_id: int) -> dict[str, Any]:
        if store.get_square(db, project_id, square_id) is None:
            raise ServiceError("square_not_found", "探方不存在", 404)
        stamp = now()
        try:
            cursor = db.execute(
                "INSERT INTO context_units(project_id,square_id,number,kind,label,description,submitted_by,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (project_id, square_id, number, kind, label, description, actor_id, actor_id, stamp, stamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError("unit_exists", "发掘单元编号在项目中已存在", 409) from exc
        self.research.audit("context.unit.create", "context_unit", str(cursor.lastrowid), {"number": number, "kind": kind, "square_id": square_id}, project_id=project_id, actor_id=actor_id)
        return dict(store.get_unit(db, project_id, cursor.lastrowid))

    def get_unit(self, project_id: int, unit_id: int, actor_id: int) -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, READ_ROLES)
        return dict(self._unit_or_404(self.db, project_id, unit_id))

    def list_units(self, project_id: int, actor_id: int, square_id: int | None = None, kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        self.research.require_role(project_id, actor_id, READ_ROLES)
        return [dict(row) for row in store.list_units(self.db, project_id, square_id, kind, status)]

    def update_unit(self, project_id: int, unit_id: int, payload: dict[str, Any], actor_id: int, key: str = "") -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, WRITE_ROLES)
        expected = payload["expected_version"]
        changes = {field: payload[field] for field in _UNIT_FIELDS if payload.get(field) is not None}
        if not changes:
            raise ServiceError("no_changes", "没有需要修改的字段", 400)

        def produce(db: sqlite3.Connection) -> dict[str, Any]:
            row = self._unit_or_404(db, project_id, unit_id)
            self._expect(row, expected, {"open"})
            if "square_id" in changes and store.get_square(db, project_id, changes["square_id"]) is None:
                raise ServiceError("square_not_found", "探方不存在", 404)
            assignments = ",".join(f"{field}=?" for field in changes)
            cursor = db.execute(
                f"UPDATE context_units SET {assignments}, version=version+1, updated_at=? WHERE id=? AND project_id=? AND version=? AND status='open'",
                (*changes.values(), now(), unit_id, project_id, expected),
            )
            if cursor.rowcount == 0:
                raise ServiceError("version_conflict", "数据版本已被其他操作变更，请刷新后重试", 409)
            self.research.audit("context.unit.update", "context_unit", str(unit_id), {"changes": changes, "version": expected + 1}, project_id=project_id, actor_id=actor_id)
            return dict(store.get_unit(db, project_id, unit_id))

        return self._idempotent(f"fieldwork.unit.update:{project_id}", key, {"unit_id": unit_id, "payload": payload}, produce)

    # ---- 封存 / 纠错 / 复核 ----

    def _snapshot(self, db: sqlite3.Connection, unit_row: sqlite3.Row, version: int, reason: str, actor_id: int, stamp: str) -> tuple[dict[str, Any], str]:
        relations = store.relations_for_unit(db, unit_row["id"])
        snapshot = {
            "unit": {key: unit_row[key] for key in ("id", "project_id", "square_id", "number", "kind", "label", "description")},
            "version": version,
            "reason": reason,
            "relations": [
                {"id": row["id"], "from_unit": row["from_unit"], "to_unit": row["to_unit"], "relation": row["relation"]}
                for row in relations
            ],
            "recorded_by": actor_id,
            "recorded_at": stamp,
        }
        return snapshot, request_hash(snapshot)

    def _insert_version(self, db: sqlite3.Connection, unit_id: int, version: int, reason: str, snapshot: dict[str, Any], digest: str, actor_id: int, stamp: str) -> dict[str, Any]:
        cursor = db.execute(
            "INSERT INTO context_unit_versions(unit_id,version,reason,snapshot_json,digest,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (unit_id, version, reason, stable_json(snapshot), digest, actor_id, stamp),
        )
        row = db.execute("SELECT * FROM context_unit_versions WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._version_dict(row)

    def seal_unit(self, project_id: int, unit_id: int, payload: dict[str, Any], actor_id: int, key: str = "") -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, WRITE_ROLES)
        expected = payload["expected_version"]

        def produce(db: sqlite3.Connection) -> dict[str, Any]:
            row = self._unit_or_404(db, project_id, unit_id)
            self._expect(row, expected, {"open"})
            stamp = now()
            new_version = row["version"] + 1
            snapshot, digest = self._snapshot(db, row, new_version, "", actor_id, stamp)
            cursor = db.execute(
                "UPDATE context_units SET status='sealed', version=?, submitted_by=?, updated_at=? WHERE id=? AND project_id=? AND version=? AND status='open'",
                (new_version, actor_id, stamp, unit_id, project_id, expected),
            )
            if cursor.rowcount == 0:
                raise ServiceError("version_conflict", "数据版本已被其他操作变更，请刷新后重试", 409)
            version_row = self._insert_version(db, unit_id, new_version, "", snapshot, digest, actor_id, stamp)
            self.research.audit("context.unit.seal", "context_unit", str(unit_id), {"version": new_version, "digest": digest}, project_id=project_id, actor_id=actor_id)
            return {"unit": dict(store.get_unit(db, project_id, unit_id)), "version": version_row}

        return self._idempotent(f"fieldwork.unit.seal:{project_id}", key, {"unit_id": unit_id, "payload": payload}, produce)

    def correct_unit(self, project_id: int, unit_id: int, payload: dict[str, Any], actor_id: int, key: str = "") -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, CORRECT_ROLES)
        expected = payload["expected_version"]
        reason = payload["reason"].strip()
        if not reason:
            raise ServiceError("reason_required", "纠错版本必须填写原因", 400)
        changes = {field: payload[field] for field in _UNIT_FIELDS if payload.get(field) is not None}
        if not changes:
            raise ServiceError("no_changes", "纠错版本至少需要修改一个字段", 400)

        def produce(db: sqlite3.Connection) -> dict[str, Any]:
            row = self._unit_or_404(db, project_id, unit_id)
            self._expect(row, expected, {"sealed", "reviewed"})
            if "square_id" in changes and store.get_square(db, project_id, changes["square_id"]) is None:
                raise ServiceError("square_not_found", "探方不存在", 404)
            stamp = now()
            new_version = row["version"] + 1
            assignments = ",".join(f"{field}=?" for field in changes)
            cursor = db.execute(
                f"UPDATE context_units SET {assignments}, status='sealed', version=?, submitted_by=?, reviewed_by=NULL, reviewed_at='', review_note='', updated_at=? WHERE id=? AND project_id=? AND version=? AND status IN ('sealed','reviewed')",
                (*changes.values(), new_version, actor_id, stamp, unit_id, project_id, expected),
            )
            if cursor.rowcount == 0:
                raise ServiceError("version_conflict", "数据版本已被其他操作变更，请刷新后重试", 409)
            updated = store.get_unit(db, project_id, unit_id)
            snapshot, digest = self._snapshot(db, updated, new_version, reason, actor_id, stamp)
            version_row = self._insert_version(db, unit_id, new_version, reason, snapshot, digest, actor_id, stamp)
            self.research.audit("context.unit.correct", "context_unit", str(unit_id), {"reason": reason, "changes": changes, "version": new_version, "digest": digest}, project_id=project_id, actor_id=actor_id)
            return {"unit": dict(updated), "version": version_row}

        return self._idempotent(f"fieldwork.unit.correct:{project_id}", key, {"unit_id": unit_id, "payload": payload}, produce)

    def review_unit(self, project_id: int, unit_id: int, payload: dict[str, Any], actor_id: int, key: str = "") -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, REVIEW_ROLES)
        expected = payload["expected_version"]
        note = payload.get("note", "")

        def produce(db: sqlite3.Connection) -> dict[str, Any]:
            row = self._unit_or_404(db, project_id, unit_id)
            self._expect(row, expected, {"sealed"})
            if row["submitted_by"] == actor_id:
                raise ServiceError("self_review", "复核人不能审核自己提交的数据", 403)
            stamp = now()
            cursor = db.execute(
                "UPDATE context_units SET status='reviewed', reviewed_by=?, reviewed_at=?, review_note=?, version=version+1, updated_at=? WHERE id=? AND project_id=? AND version=? AND status='sealed'",
                (actor_id, stamp, note, stamp, unit_id, project_id, expected),
            )
            if cursor.rowcount == 0:
                raise ServiceError("version_conflict", "数据版本已被其他操作变更，请刷新后重试", 409)
            self.research.audit("context.unit.review", "context_unit", str(unit_id), {"note": note}, project_id=project_id, actor_id=actor_id)
            return dict(store.get_unit(db, project_id, unit_id))

        return self._idempotent(f"fieldwork.unit.review:{project_id}", key, {"unit_id": unit_id, "payload": payload}, produce)

    def version_history(self, project_id: int, unit_id: int, actor_id: int) -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, READ_ROLES)
        self._unit_or_404(self.db, project_id, unit_id)
        return {"versions": [self._version_dict(row) for row in store.versions_for_unit(self.db, unit_id)]}

    # ---- 遗迹关系 ----

    def create_relation(self, project_id: int, payload: dict[str, Any], actor_id: int, key: str = "") -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, WRITE_ROLES)
        return self._idempotent(
            f"fieldwork.relation.create:{project_id}", key, {"project_id": project_id, "payload": payload},
            lambda db: self._create_relation(db, project_id, payload["from_unit"], payload["to_unit"], payload["relation"], actor_id),
        )

    def _create_relation(self, db: sqlite3.Connection, project_id: int, from_id: int, to_id: int, relation: str, actor_id: int) -> dict[str, Any]:
        if from_id == to_id:
            raise ServiceError("self_loop", "遗迹关系不允许指向单元自身", 400)
        first = self._unit_or_404(db, project_id, from_id)
        second = self._unit_or_404(db, project_id, to_id)
        if first["status"] != "open" or second["status"] != "open":
            raise ServiceError("unit_sealed", "已封存的单元不允许新增或变更关系", 409)
        if relation == "equals" and from_id > to_id:
            from_id, to_id = to_id, from_id
        existing = store.find_relation(db, project_id, from_id, to_id, relation)
        if existing is not None:
            return dict(existing)
        graph = _Graph(store.relations_for_project(db, project_id))
        if relation == "equals":
            ra, rb = graph.find(from_id), graph.find(to_id)
            if ra != rb and (graph.reachable(ra, rb) or graph.reachable(rb, ra)):
                raise ServiceError("relation_conflict", "等同关系与既有切叠/早晚关系矛盾", 409)
        else:
            later, earlier = (from_id, to_id) if relation == "cuts" else (to_id, from_id)
            root_later, root_earlier = graph.find(later), graph.find(earlier)
            if root_later == root_earlier:
                raise ServiceError("relation_conflict", "关系与既有等同关系矛盾", 409)
            if graph.reachable(root_earlier, root_later):
                raise ServiceError("relation_cycle", "该关系会与既有关系形成可推导的循环", 409)
        cursor = db.execute(
            "INSERT INTO context_relations(project_id,from_unit,to_unit,relation,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (project_id, from_id, to_id, relation, actor_id, now()),
        )
        self.research.audit("context.relation.create", "context_relation", str(cursor.lastrowid), {"from_unit": from_id, "to_unit": to_id, "relation": relation}, project_id=project_id, actor_id=actor_id)
        return dict(store.get_relation(db, project_id, cursor.lastrowid))

    def delete_relation(self, project_id: int, relation_id: int, actor_id: int) -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, WRITE_ROLES)
        with self._tx() as db:
            row = store.get_relation(db, project_id, relation_id)
            if row is None:
                raise ServiceError("relation_not_found", "遗迹关系不存在", 404)
            first = self._unit_or_404(db, project_id, row["from_unit"])
            second = self._unit_or_404(db, project_id, row["to_unit"])
            if first["status"] != "open" or second["status"] != "open":
                raise ServiceError("unit_sealed", "已封存的单元不允许新增或变更关系", 409)
            db.execute("DELETE FROM context_relations WHERE id=?", (relation_id,))
            self.research.audit("context.relation.delete", "context_relation", str(relation_id), {"from_unit": row["from_unit"], "to_unit": row["to_unit"], "relation": row["relation"]}, project_id=project_id, actor_id=actor_id)
        return {"deleted": relation_id}

    def direct_relations(self, project_id: int, unit_id: int, actor_id: int) -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, READ_ROLES)
        unit = self._unit_or_404(self.db, project_id, unit_id)
        items = []
        for row in store.relations_for_unit(self.db, unit_id):
            if row["relation"] == "equals":
                direction, perspective = "symmetric", "equals"
            elif row["from_unit"] == unit_id:
                direction = "outgoing"
                perspective = "cuts" if row["relation"] == "cuts" else "earlier_than"
            else:
                direction = "incoming"
                perspective = "cut_by" if row["relation"] == "cuts" else "later_than"
            other_id = row["to_unit"] if row["from_unit"] == unit_id else row["from_unit"]
            other = store.get_unit(self.db, project_id, other_id)
            items.append({
                "id": row["id"],
                "relation": row["relation"],
                "direction": direction,
                "perspective": perspective,
                "from_unit": row["from_unit"],
                "to_unit": row["to_unit"],
                "other": self._unit_summary(other),
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            })
        return {"unit": self._unit_summary(unit), "relations": items}

    def transitive_relations(self, project_id: int, unit_id: int, actor_id: int) -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, READ_ROLES)
        unit = self._unit_or_404(self.db, project_id, unit_id)
        graph = _Graph(store.relations_for_project(self.db, project_id))
        root = graph.find(unit_id)
        members: dict[int, set[int]] = {}
        for uid in list(graph.parent):
            members.setdefault(graph.find(uid), set()).add(uid)
        roots = set(members) | set(graph.later)
        for targets in graph.later.values():
            roots |= targets
        equals_ids = members.get(root, set()) - {unit_id}
        later_ids: set[int] = set()
        earlier_ids: set[int] = set()
        for other in roots:
            if other == root:
                continue
            if graph.reachable(root, other):
                earlier_ids |= members.get(other, set())
            if graph.reachable(other, root):
                later_ids |= members.get(other, set())
        summaries = {row["id"]: self._unit_summary(row) for row in store.units_by_ids(self.db, equals_ids | later_ids | earlier_ids)}
        ordered = lambda ids: sorted((summaries[uid] for uid in ids), key=lambda item: item["number"])
        return {"unit": self._unit_summary(unit), "equals": ordered(equals_ids), "later": ordered(later_ids), "earlier": ordered(earlier_ids)}

    # ---- 批量导入 ----

    def import_batch(self, project_id: int, rows: list[dict[str, Any]], actor_id: int, key: str = "") -> dict[str, Any]:
        self.research.require_role(project_id, actor_id, WRITE_ROLES)

        def produce(db: sqlite3.Connection) -> dict[str, Any]:
            results, ok = [], 0
            for index, row in enumerate(rows):
                db.execute(f"SAVEPOINT import_row_{index}")
                try:
                    created = self._import_row(db, project_id, row, actor_id)
                except ServiceError as exc:
                    db.execute(f"ROLLBACK TO import_row_{index}")
                    db.execute(f"RELEASE import_row_{index}")
                    results.append({"index": index, "status": "error", "op": row.get("op"), "code": exc.code, "message": exc.message})
                else:
                    db.execute(f"RELEASE import_row_{index}")
                    ok += 1
                    results.append({"index": index, "status": "ok", **created})
            summary = {"total": len(rows), "ok": ok, "errors": len(rows) - ok}
            self.research.audit("context.import", "project", str(project_id), summary, project_id=project_id, actor_id=actor_id)
            return {"project_id": project_id, "summary": summary, "results": results}

        return self._idempotent(f"fieldwork.import:{project_id}", key, {"project_id": project_id, "rows": rows}, produce)

    def _import_row(self, db: sqlite3.Connection, project_id: int, row: dict[str, Any], actor_id: int) -> dict[str, Any]:
        op = row.get("op")
        if op == "square":
            code = _norm(row["code"], "探方编号")
            created = self._create_square(db, project_id, code, row.get("name", ""), actor_id)
            return {"op": op, "resource": "context_square", "id": created["id"], "code": created["code"]}
        if op == "unit":
            square = store.get_square_by_code(db, project_id, _norm(row["square"], "探方编号"))
            if square is None:
                raise ServiceError("square_not_found", f"探方 {row['square']} 不存在", 404)
            number = _norm(row["number"], "单元编号")
            created = self._create_unit(db, project_id, number, row["kind"], square["id"], row.get("label", ""), row.get("description", ""), actor_id)
            return {"op": op, "resource": "context_unit", "id": created["id"], "number": created["number"]}
        if op == "relation":
            from_unit = store.get_unit_by_number(db, project_id, _norm(row["from"], "单元编号"))
            to_unit = store.get_unit_by_number(db, project_id, _norm(row["to"], "单元编号"))
            if from_unit is None or to_unit is None:
                raise ServiceError("unit_not_found", "关系端点的发掘单元不存在", 404)
            created = self._create_relation(db, project_id, from_unit["id"], to_unit["id"], row["relation"], actor_id)
            return {"op": op, "resource": "context_relation", "id": created["id"], "relation": created["relation"]}
        raise ServiceError("unknown_op", f"不支持的导入操作 {op}", 400)
