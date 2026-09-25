"""田野上下文模块的仓储层：表结构与基础查询，业务规则在 service 层。"""
from __future__ import annotations

import sqlite3
from typing import Iterable

FIELDWORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_squares (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 code TEXT NOT NULL,
 name TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id,code)
);
CREATE TABLE IF NOT EXISTS context_units (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 square_id INTEGER NOT NULL REFERENCES context_squares(id),
 number TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('layer','ash_pit','channel','feature')),
 label TEXT NOT NULL DEFAULT '',
 description TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','sealed','reviewed')),
 version INTEGER NOT NULL DEFAULT 1,
 submitted_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 reviewed_at TEXT NOT NULL DEFAULT '',
 review_note TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id,number)
);
CREATE TABLE IF NOT EXISTS context_unit_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 unit_id INTEGER NOT NULL REFERENCES context_units(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 reason TEXT NOT NULL DEFAULT '',
 snapshot_json TEXT NOT NULL,
 digest TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(unit_id,version)
);
CREATE TABLE IF NOT EXISTS context_relations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 from_unit INTEGER NOT NULL REFERENCES context_units(id) ON DELETE CASCADE,
 to_unit INTEGER NOT NULL REFERENCES context_units(id) ON DELETE CASCADE,
 relation TEXT NOT NULL CHECK(relation IN ('cuts','earlier','equals')),
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(from_unit,to_unit,relation)
);
CREATE INDEX IF NOT EXISTS idx_context_units_project ON context_units(project_id,status);
CREATE INDEX IF NOT EXISTS idx_context_relations_project ON context_relations(project_id);
CREATE INDEX IF NOT EXISTS idx_context_relations_to ON context_relations(to_unit);
CREATE INDEX IF NOT EXISTS idx_context_versions_unit ON context_unit_versions(unit_id,version);
"""

UNIT_KINDS = ("layer", "ash_pit", "channel", "feature")
RELATION_TYPES = ("cuts", "earlier", "equals")


def get_square(db: sqlite3.Connection, project_id: int, square_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM context_squares WHERE project_id=? AND id=?", (project_id, square_id)).fetchone()


def get_square_by_code(db: sqlite3.Connection, project_id: int, code: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM context_squares WHERE project_id=? AND code=?", (project_id, code)).fetchone()


def list_squares(db: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM context_squares WHERE project_id=? ORDER BY code", (project_id,)).fetchall()


def get_unit(db: sqlite3.Connection, project_id: int, unit_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM context_units WHERE project_id=? AND id=?", (project_id, unit_id)).fetchone()


def get_unit_by_number(db: sqlite3.Connection, project_id: int, number: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM context_units WHERE project_id=? AND number=?", (project_id, number)).fetchone()


def list_units(db: sqlite3.Connection, project_id: int, square_id: int | None = None, kind: str | None = None, status: str | None = None) -> list[sqlite3.Row]:
    clauses, params = ["project_id=?"], [project_id]
    if square_id is not None:
        clauses.append("square_id=?")
        params.append(square_id)
    if kind is not None:
        clauses.append("kind=?")
        params.append(kind)
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    return db.execute(f"SELECT * FROM context_units WHERE {' AND '.join(clauses)} ORDER BY number,id", params).fetchall()


def units_by_ids(db: sqlite3.Connection, ids: Iterable[int]) -> list[sqlite3.Row]:
    values = sorted(set(ids))
    if not values:
        return []
    marks = ",".join("?" for _ in values)
    return db.execute(f"SELECT * FROM context_units WHERE id IN ({marks})", values).fetchall()


def get_relation(db: sqlite3.Connection, project_id: int, relation_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM context_relations WHERE project_id=? AND id=?", (project_id, relation_id)).fetchone()


def find_relation(db: sqlite3.Connection, project_id: int, from_unit: int, to_unit: int, relation: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM context_relations WHERE project_id=? AND from_unit=? AND to_unit=? AND relation=?",
        (project_id, from_unit, to_unit, relation),
    ).fetchone()


def relations_for_project(db: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM context_relations WHERE project_id=? ORDER BY id", (project_id,)).fetchall()


def relations_for_unit(db: sqlite3.Connection, unit_id: int) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM context_relations WHERE from_unit=? OR to_unit=? ORDER BY id", (unit_id, unit_id)).fetchall()


def versions_for_unit(db: sqlite3.Connection, unit_id: int) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM context_unit_versions WHERE unit_id=? ORDER BY version", (unit_id,)).fetchall()
