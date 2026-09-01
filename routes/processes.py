"""进程控制与日志接口。"""

from __future__ import annotations

from flask import Blueprint, request

from manager import logger as log_store
from manager import project_manager as repo
from routes.helpers import fail, get_manager, ok

bp = Blueprint("processes", __name__, url_prefix="/api/projects")


def _get(project_id: int):
    """取项目，只处理 404。读接口（status）用这个。"""
    project = repo.get_project(project_id)
    if project is None:
        return None, fail("项目不存在", 404)
    return project, None


def _load(project_id: int):
    """取项目，并拒绝静态项目 —— 进程操作对它没有意义。

    start/stop/restart/logs 都走这里，拦一处就够，免得各写一遍还漏掉某个。
    ``status`` 是只读查询，静态项目也要能查（前端卡片靠它渲染），所以走 ``_get``。
    """
    project, error = _get(project_id)
    if error:
        return None, error
    if project.is_static:
        # 400 而不是 404：项目确实存在，只是这个操作对它没意义
        return None, fail("静态页面项目没有进程，无法执行该操作", 400)
    return project, None


@bp.post("/<int:project_id>/start")
def start(project_id: int):
    project, error = _load(project_id)
    if error:
        return error
    result = get_manager().start(project)
    if result.success:
        return ok(result.message, result.data, result.http_status)
    return fail(result.message, result.http_status, result.data or None)


@bp.post("/<int:project_id>/stop")
def stop(project_id: int):
    project, error = _load(project_id)
    if error:
        return error
    result = get_manager().stop(project)
    if result.success:
        return ok(result.message, result.data, result.http_status)
    return fail(result.message, result.http_status, result.data or None)


@bp.post("/<int:project_id>/restart")
def restart(project_id: int):
    project, error = _load(project_id)
    if error:
        return error
    result = get_manager().restart(project)
    if result.success:
        return ok(result.message, result.data, result.http_status)
    return fail(result.message, result.http_status, result.data or None)


@bp.get("/<int:project_id>/status")
def status(project_id: int):
    # 静态项目也能查状态，status() 内部会返回 STATIC 视图
    project, error = _get(project_id)
    if error:
        return error
    return ok(data=get_manager().status(project))


@bp.get("/<int:project_id>/logs")
def logs(project_id: int):
    """增量拉日志。

    查询参数：
        run_id     指定运行；省略则用最近一次运行
        stdout_at  stdout 已读到的字节偏移
        stderr_at  stderr 已读到的字节偏移
    """
    project, error = _load(project_id)
    if error:
        return error

    # 参数校验放最前面：不管有没有日志，非法输入都该报 400
    try:
        stdout_at = max(0, int(request.args.get("stdout_at", 0)))
        stderr_at = max(0, int(request.args.get("stderr_at", 0)))
    except ValueError:
        return fail("日志偏移必须是数字", 400)

    requested = (request.args.get("run_id") or "").strip()
    if requested and not log_store.RUN_ID_RE.match(requested):
        return fail("非法的运行 ID", 400)

    run_id = requested or project.last_run_id
    if not run_id:
        return ok(
            data={
                "run_id": None,
                "runs": [],
                "stdout": {"text": "", "offset": 0},
                "stderr": {"text": "", "offset": 0},
            }
        )

    out = log_store.read_stream(project_id, run_id, "stdout", stdout_at)
    err = log_store.read_stream(project_id, run_id, "stderr", stderr_at)
    return ok(
        data={
            "run_id": run_id,
            "runs": log_store.list_runs(project_id),
            "stdout": {"text": out.text, "offset": out.offset, "size": out.size},
            "stderr": {"text": err.text, "offset": err.offset, "size": err.size},
        }
    )
