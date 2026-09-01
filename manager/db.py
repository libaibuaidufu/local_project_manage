"""SQLite 连接与建表。

项目配置量很小（几十行），用标准库 sqlite3 就够了，不引入 ORM。
每次操作开一个短连接，天然避免 sqlite3 对象跨线程使用的限制
（Flask 请求线程、监控线程、自动启动线程都会写库）。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    kind             TEXT    NOT NULL DEFAULT 'process',
    working_dir      TEXT    NOT NULL,
    command          TEXT    NOT NULL DEFAULT '',
    entry_file       TEXT    NOT NULL DEFAULT '',
    port             INTEGER,
    environment      TEXT    NOT NULL DEFAULT '{}',
    auto_start       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    last_pid         INTEGER,
    last_create_time REAL,
    last_start_time  TEXT,
    last_run_id      TEXT,
    last_status      TEXT    NOT NULL DEFAULT 'STOPPED',
    last_exit_code   INTEGER
);
"""


def _db_path() -> Path:
    return config.DB_PATH


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """打开一个连接，正常结束自动提交，异常自动回滚。"""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        # WAL 让读写不互相阻塞：监控线程写状态时前端仍能正常读列表
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """建表，并把上一份数据库留作备份。幂等，每次启动都可以调。

    备份在打开任何连接之前做 —— 这个项目用"每操作短连接"模式，连接全部
    关闭后 WAL 已合并回主文件，此刻 projects.db 是静止的，直接复制是安全的。
    只保留一代（projects.db.bak）：数据库损坏或被误删时至少能回滚到上次启动。
    """
    path = _db_path()
    if path.exists():
        backup = path.parent / (path.name + ".bak")
        try:
            shutil.copyfile(path, backup)
        except OSError as exc:
            # 备份失败不阻塞启动，但必须留下记录
            log.warning("数据库备份失败（%s）：%s", backup, exc)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


#: 后来加的列 -> 建列语句。老库缺哪列就补哪列。
#: 只做加列，不改也不删已有列 —— 迁移出错的代价是用户的项目配置。
_ADDED_COLUMNS = {
    "kind": "ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'process'",
    "entry_file": "ALTER TABLE projects ADD COLUMN entry_file TEXT NOT NULL DEFAULT ''",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """把老版本的库升级到当前 schema。

    ``CREATE TABLE IF NOT EXISTS`` 对已存在的表什么都不做，所以早于静态页面
    支持创建的库不会有 kind / entry_file 列，必须显式补。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    for column, ddl in _ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(ddl)
            log.info("数据库迁移：新增列 %s", column)
