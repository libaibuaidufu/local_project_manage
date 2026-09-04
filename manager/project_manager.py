"""项目配置的增删改查与校验。

这一层只管配置，不碰进程；进程相关逻辑全在 process_manager.py。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from manager.db import connect
from models.project import KIND_PROCESS, KIND_STATIC, KINDS, Project, ProjectStatus

#: 环境变量名的合法字符，避免把乱七八糟的键塞进子进程环境
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 静态项目找不到用户指定入口时，按这个顺序猜
DEFAULT_ENTRIES = ("index.html", "index.htm")

_COLUMNS = (
    "id, name, kind, working_dir, command, entry_file, port, environment, auto_start, "
    "display_order, created_at, updated_at, last_pid, last_create_time, last_start_time, "
    "last_run_id, last_status, last_exit_code"
)


class ValidationError(ValueError):
    """配置校验失败。路由层会转成 HTTP 400。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_dir(raw: str) -> str:
    """规范化工作目录：展开 ~、去掉多余分隔符、统一成 Windows 反斜杠。"""
    path = os.path.expanduser(raw.strip().strip('"'))
    if not path:
        return ""
    return os.path.normpath(path)


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化前端提交的项目配置。

    返回清洗后的字段字典；任何不合法的输入都抛 ValidationError。
    """
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValidationError("项目名称不能为空")
    if len(name) > 80:
        raise ValidationError("项目名称过长（最多 80 字符）")

    kind = str(payload.get("kind") or KIND_PROCESS).strip() or KIND_PROCESS
    if kind not in KINDS:
        raise ValidationError(f"项目类型不合法：{kind}")

    working_dir = normalize_dir(str(payload.get("working_dir") or ""))
    if not working_dir:
        raise ValidationError("工作目录不能为空")

    if kind == KIND_STATIC:
        # 静态项目没有进程，命令和端口都用不上，直接清空，避免残留旧值让人误解
        command = ""
        entry_file = _validate_entry(payload.get("entry_file"))
        port = None
        environment: dict[str, str] = {}
        auto_start = False
    else:
        command = str(payload.get("command") or "").strip()
        if not command:
            raise ValidationError("启动命令不能为空")
        entry_file = ""
        port = _validate_port(payload.get("port"))
        environment = _validate_env(payload.get("environment"))
        auto_start = bool(payload.get("auto_start"))

    return {
        "name": name,
        "kind": kind,
        "working_dir": working_dir,
        "command": command,
        "entry_file": entry_file,
        "port": port,
        "environment": environment,
        "auto_start": auto_start,
    }


def _validate_entry(raw: Any) -> str:
    """校验静态项目的入口文件路径（相对 working_dir）。

    留空是允许的 —— 打开时再按 DEFAULT_ENTRIES 猜，或者直接列目录。
    这里只拦明显越界的写法；真正的安全边界在 routes/static_sites.py，
    那里对最终解析出的绝对路径做二次校验（不能只信这一层）。
    """
    entry = str(raw or "").strip().strip('"').replace("\\", "/").lstrip("/")
    if not entry:
        return ""
    if len(entry) > 255:
        raise ValidationError("入口文件路径过长")
    if ".." in entry.split("/"):
        raise ValidationError("入口文件不能包含 ..")
    if re.match(r"^[A-Za-z]:", entry) or entry.startswith("//"):
        raise ValidationError("入口文件要填相对工作目录的路径，不要填绝对路径")
    return entry


def _validate_port(raw: Any) -> int | None:
    """端口可以留空。填了就必须是 1~65535 的整数。"""
    if raw is None or raw == "":
        return None
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValidationError("端口必须是数字") from None
    if not 1 <= port <= 65535:
        raise ValidationError("端口必须在 1~65535 之间")
    return port


def _validate_env(raw: Any) -> dict[str, str]:
    """环境变量必须是 {字符串: 字符串}，键名符合环境变量命名规则。"""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise ValidationError("环境变量不是合法的 JSON") from None
    if not isinstance(raw, dict):
        raise ValidationError("环境变量必须是键值对对象")

    env: dict[str, str] = {}
    for key, value in raw.items():
        key = str(key).strip()
        if not key:
            continue  # 前端空行直接忽略，不算错误
        if not ENV_KEY_RE.match(key):
            raise ValidationError(f"环境变量名不合法：{key}")
        if value is None:
            value = ""
        if isinstance(value, (dict, list)):
            raise ValidationError(f"环境变量 {key} 的值必须是字符串")
        env[key] = str(value)
    return env


def static_info(project: Project) -> dict[str, Any]:
    """静态项目的目录信息：目录在不在、入口文件解析成什么、有几个网页文件。

    前端每 1~2 秒轮询一次状态，所以这里只做一层 ``os.scandir``，不递归。
    """
    info: dict[str, Any] = {"dir_exists": False, "entry": None, "html_count": 0}
    working_dir = project.working_dir
    if not working_dir or not os.path.isdir(working_dir):
        return info
    info["dir_exists"] = True

    root = Path(working_dir)
    if project.entry_file:
        # 配了入口就只认它；文件不在就报 None，让前端提示用户
        info["entry"] = project.entry_file if (root / project.entry_file).is_file() else None
    else:
        for name in DEFAULT_ENTRIES:
            if (root / name).is_file():
                info["entry"] = name
                break

    count = 0
    try:
        with os.scandir(working_dir) as entries:
            for item in entries:
                if item.is_file() and item.name.lower().endswith((".html", ".htm")):
                    count += 1
    except OSError:
        pass
    info["html_count"] = count
    return info


def list_projects() -> list[Project]:
    """按 display_order 排序返回所有项目，order 相同时按 id 排序。"""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM projects ORDER BY display_order, id"
        ).fetchall()
    return [Project.from_row(row) for row in rows]


def get_project(project_id: int) -> Project | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    return Project.from_row(row) if row else None


def create_project(payload: dict[str, Any]) -> Project:
    fields = validate(payload)
    now = _now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO projects
                (name, kind, working_dir, command, entry_file, port, environment,
                 auto_start, created_at, updated_at, last_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'STOPPED')
            """,
            (
                fields["name"],
                fields["kind"],
                fields["working_dir"],
                fields["command"],
                fields["entry_file"],
                fields["port"],
                json.dumps(fields["environment"], ensure_ascii=False),
                int(fields["auto_start"]),
                now,
                now,
            ),
        )
        new_id = int(cursor.lastrowid or 0)
    project = get_project(new_id)
    if project is None:  # 理论上不会发生，但不要返回 None 让调用方猜
        raise RuntimeError("项目创建后无法读回，请检查数据库")
    return project


def update_project(project_id: int, payload: dict[str, Any]) -> Project | None:
    """更新配置。不会自动重启正在运行的进程（需求 24）。"""
    if get_project(project_id) is None:
        return None
    fields = validate(payload)
    with connect() as conn:
        conn.execute(
            """
            UPDATE projects
               SET name = ?, kind = ?, working_dir = ?, command = ?, entry_file = ?,
                   port = ?, environment = ?, auto_start = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                fields["name"],
                fields["kind"],
                fields["working_dir"],
                fields["command"],
                fields["entry_file"],
                fields["port"],
                json.dumps(fields["environment"], ensure_ascii=False),
                int(fields["auto_start"]),
                _now(),
                project_id,
            ),
        )
    return get_project(project_id)


def delete_project(project_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0


def save_runtime(
    project_id: int,
    *,
    pid: int | None,
    create_time: float | None,
    start_time: str | None,
    run_id: str | None,
    status: str,
    exit_code: int | None,
) -> None:
    """持久化运行时快照，供管理器重启后重新接管进程。"""
    with connect() as conn:
        conn.execute(
            """
            UPDATE projects
               SET last_pid = ?, last_create_time = ?, last_start_time = ?,
                   last_run_id = ?, last_status = ?, last_exit_code = ?
             WHERE id = ?
            """,
            (pid, create_time, start_time, run_id, str(status), exit_code, project_id),
        )


def save_status(project_id: int, status: ProjectStatus | str) -> None:
    """只更新状态列，用于 STARTING/STOPPING 这类过渡状态。"""
    with connect() as conn:
        conn.execute(
            "UPDATE projects SET last_status = ? WHERE id = ?", (str(status), project_id)
        )


def update_display_order(order_list: list[int]) -> None:
    """批量更新项目的显示顺序。

    order_list 是按前端拖拽后的顺序排列的项目 ID 列表。
    每个 ID 的新 display_order 就是它在列表中的索引。
    """
    with connect() as conn:
        for index, project_id in enumerate(order_list):
            conn.execute(
                "UPDATE projects SET display_order = ? WHERE id = ?",
                (index, project_id)
            )
