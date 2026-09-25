"""田野上下文模块的仓储结构。

包含探方/发掘单元/层位/遗迹（统一为 field_units）、不可变版本与快照、
双向可追踪的关系边，以及关系复核记录。所有表均通过外键挂在 projects 之下，
跨表写入由服务层放在同一个即时事务中。
"""
from __future__ import annotations

FIELD_SCHEMA = """
CREATE TABLE IF NOT EXISTS field_units (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 code TEXT NOT NULL,
 unit_type TEXT NOT NULL CHECK(unit_type IN ('trench','excavation_unit','layer','feature','paleochannel')),
 title TEXT NOT NULL DEFAULT '',
 attributes_json TEXT NOT NULL DEFAULT '{}',
 current_version INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','sealed')),
 created_by INTEGER NOT NULL REFERENCES users(id),
 sealed_by INTEGER REFERENCES users(id),
 sealed_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id,code)
);
CREATE INDEX IF NOT EXISTS idx_field_units_project ON field_units(project_id,status);
CREATE TABLE IF NOT EXISTS field_unit_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 unit_id INTEGER NOT NULL REFERENCES field_units(id) ON DELETE CASCADE,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 version_no INTEGER NOT NULL,
 unit_type TEXT NOT NULL,
 title TEXT NOT NULL DEFAULT '',
 attributes_json TEXT NOT NULL DEFAULT '{}',
 change_reason TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('draft','sealed','superseded')),
 created_by INTEGER NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL,
 UNIQUE(unit_id,version_no)
);
CREATE INDEX IF NOT EXISTS idx_field_versions_unit ON field_unit_versions(unit_id,version_no);
CREATE TABLE IF NOT EXISTS field_snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 unit_id INTEGER NOT NULL REFERENCES field_units(id) ON DELETE CASCADE,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 version_no INTEGER NOT NULL,
 content_json TEXT NOT NULL,
 content_digest TEXT NOT NULL,
 prev_digest TEXT NOT NULL DEFAULT '',
 created_by INTEGER NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL,
 UNIQUE(unit_id,version_no)
);
CREATE TABLE IF NOT EXISTS field_relations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 source_id INTEGER NOT NULL REFERENCES field_units(id),
 target_id INTEGER NOT NULL REFERENCES field_units(id),
 kind TEXT NOT NULL CHECK(kind IN ('earlier','cuts','equivalent')),
 evidence TEXT NOT NULL DEFAULT '',
 attributes_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 created_by INTEGER NOT NULL REFERENCES users(id),
 reviewed_by INTEGER REFERENCES users(id),
 review_comment TEXT NOT NULL DEFAULT '',
 reviewed_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL DEFAULT '',
 UNIQUE(project_id,source_id,target_id,kind)
);
CREATE INDEX IF NOT EXISTS idx_field_rel_source ON field_relations(project_id,source_id,status);
CREATE INDEX IF NOT EXISTS idx_field_rel_target ON field_relations(project_id,target_id,status);
CREATE INDEX IF NOT EXISTS idx_field_rel_kind ON field_relations(project_id,kind,status);
CREATE TABLE IF NOT EXISTS field_relation_reviews (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 relation_id INTEGER NOT NULL REFERENCES field_relations(id) ON DELETE CASCADE,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
 comment TEXT NOT NULL DEFAULT '',
 reviewer_id INTEGER NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_field_rel_reviews ON field_relation_reviews(relation_id,id);
"""


def init_field_db(db) -> None:
    db.executescript(FIELD_SCHEMA)
