"""项目模型与状态枚举。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProjectStatus(StrEnum):
    """项目运行状态。

    STOPPED  已停止（正常退出，或从未启动）
    STARTING 启动中（进程已创建，还在观察期）
    RUNNING  运行中
    STOPPING 停止中（正在杀进程树）
    ERROR    异常（启动失败，或非 0 退出码）

    STATIC   静态页面项目的固定状态，**不属于**上面那套进程状态机：
             它没有进程，不会流转，永远是这个值。放在这里只是为了让前端
             能统一按 status 渲染卡片。
    """

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"
    STATIC = "STATIC"


#: 这些状态意味着“进程应该还活着”，管理器重启后需要尝试重新接管。
LIVE_STATUSES = frozenset({ProjectStatus.STARTING, ProjectStatus.RUNNING, ProjectStatus.STOPPING})

#: 项目类型。process = 要跑起来的服务；static = 只是一堆网页文件，由管理器代为托管。
KIND_PROCESS = "process"
KIND_STATIC = "static"
KINDS = (KIND_PROCESS, KIND_STATIC)


@dataclass(slots=True)
class Project:
    """一个被管理的项目。

    last_* 字段用于管理器重启后重新发现仍在运行的进程：单看 PID 不可靠
    （Windows 会复用 PID），所以同时保存进程创建时间做二次校验。
    """

    id: int | None = None
    name: str = ""
    kind: str = KIND_PROCESS
    working_dir: str = ""
    command: str = ""
    #: 静态项目的入口文件，相对 working_dir，比如 "index.html"
    entry_file: str = ""
    port: int | None = None
    environment: dict[str, str] = field(default_factory=dict)
    auto_start: bool = False
    created_at: str = ""
    updated_at: str = ""
    # 运行时快照，持久化以便跨管理器重启恢复
    last_pid: int | None = None
    last_create_time: float | None = None
    last_start_time: str | None = None
    last_run_id: str | None = None
    last_status: str = ProjectStatus.STOPPED
    last_exit_code: int | None = None

    @property
    def is_static(self) -> bool:
        """静态项目没有进程，start/stop/restart/日志 这些操作对它都不适用。"""
        return self.kind == KIND_STATIC

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Project":
        """从数据库行构造。environment 列存的是 JSON 文本。"""
        raw_env = row["environment"]
        try:
            env = json.loads(raw_env) if raw_env else {}
            if not isinstance(env, dict):
                env = {}
        except (json.JSONDecodeError, TypeError):
            # 手工改坏了数据库也不该让整个列表页 500
            env = {}
        return cls(
            id=row["id"],
            name=row["name"],
            kind=row["kind"] or KIND_PROCESS,
            working_dir=row["working_dir"],
            command=row["command"],
            entry_file=row["entry_file"] or "",
            port=row["port"],
            environment={str(k): str(v) for k, v in env.items()},
            auto_start=bool(row["auto_start"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_pid=row["last_pid"],
            last_create_time=row["last_create_time"],
            last_start_time=row["last_start_time"],
            last_run_id=row["last_run_id"],
            last_status=row["last_status"] or ProjectStatus.STOPPED,
            last_exit_code=row["last_exit_code"],
        )

    def to_dict(self) -> dict[str, Any]:
        """给前端用的配置视图（不含运行时指标，那些由 ProcessManager 合并）。"""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "working_dir": self.working_dir,
            "command": self.command,
            "entry_file": self.entry_file,
            "port": self.port,
            "environment": dict(self.environment),
            "auto_start": self.auto_start,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
