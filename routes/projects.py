"""项目配置的 CRUD 接口。"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime

from flask import Blueprint, request, send_file

from manager import project_manager as repo
from manager.project_manager import ValidationError
from models.project import LIVE_STATUSES
from routes.helpers import fail, get_manager, ok

bp = Blueprint("projects", __name__, url_prefix="/api/projects")


def _dir_warning(working_dir: str) -> str | None:
    """目录不存在时给个提醒，但不阻止保存 —— 用户可能打算稍后再建。"""
    if working_dir and not os.path.isdir(working_dir):
        return f"注意：工作目录当前不存在（{working_dir}），启动时会失败。"
    return None


@bp.get("")
def list_projects():
    """列表页数据：配置 + 实时状态 + 运行计数。"""
    manager = get_manager()
    snapshot = manager.snapshot(repo.list_projects())
    return ok(data=snapshot)


@bp.get("/<int:project_id>")
def get_project(project_id: int):
    project = repo.get_project(project_id)
    if project is None:
        return fail("项目不存在", 404)
    return ok(data=get_manager().status(project))


@bp.post("")
def create_project():
    try:
        project = repo.create_project(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return fail(str(exc), 400)

    message = "项目已创建"
    warning = _dir_warning(project.working_dir)
    if warning:
        message = f"{message}。{warning}"
    return ok(message, data=get_manager().status(project), status=201)


@bp.put("/<int:project_id>")
def update_project(project_id: int):
    existing = repo.get_project(project_id)
    if existing is None:
        return fail("项目不存在", 404)

    try:
        project = repo.update_project(project_id, request.get_json(silent=True) or {})
    except ValidationError as exc:
        return fail(str(exc), 400)
    if project is None:  # 并发删除
        return fail("项目不存在", 404)

    # 需求 24：运行中改配置不自动重启，但要明确告诉用户
    payload = get_manager().status(project)
    if payload["status"] in LIVE_STATUSES:
        message = "配置已保存，当前运行中的进程不会自动更新。如需应用新配置，请手动重启项目。"
    else:
        message = "配置已保存"
    warning = _dir_warning(project.working_dir)
    if warning:
        message = f"{message} {warning}"
    return ok(message, data=payload)


@bp.delete("/<int:project_id>")
def delete_project(project_id: int):
    project = repo.get_project(project_id)
    if project is None:
        return fail("项目不存在", 404)

    # 需求 25：运行中不允许直接删，否则会留下管不到的孤儿进程
    if get_manager().status(project)["status"] in LIVE_STATUSES:
        return fail("请先停止项目，再删除。", 409)

    if not repo.delete_project(project_id):
        return fail("项目不存在", 404)
    return ok("项目已删除")


# ---------------------------------------------------------- 备份：导出 / 导入

#: 备份文件里每条项目的字段。只含配置 —— PID、启动时间这些运行时状态
#: 属于当时那台机器的瞬时信息，恢复时没有意义，还容易误导。
_EXPORT_FIELDS = ("name", "working_dir", "command", "port", "environment", "auto_start")

#: 单次导入上限，防手滑导进一个巨型文件把库塞爆
_IMPORT_LIMIT = 1000


@bp.get("/export")
def export_projects():
    """把全部项目配置下载为 JSON 备份文件。"""
    items = []
    for project in repo.list_projects():
        item = {field: getattr(project, field) for field in _EXPORT_FIELDS}
        item["created_at"] = project.created_at  # 仅参考信息，导入时不使用
        items.append(item)

    payload = {
        "format": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(items),
        "projects": items,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(
        io.BytesIO(text.encode("utf-8")),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"projects-backup-{stamp}.json",
    )


@bp.post("/import")
def import_projects():
    """导入备份文件。合并模式，按名称匹配，不删除任何现有项目。

    * 文件里的项目，库里有同名 → 更新该项目的配置
    * 没有同名 → 新增
    * 库里有、文件里没有的 → 完全不动（导入只做加法和更新，不做删除）
    * 单条数据非法只跳过该条，原因列在返回里，不影响其他条
    """
    body = request.get_json(silent=True)
    if body is None:
        return fail("无法解析文件内容：不是合法的 JSON", 400)

    if isinstance(body, list):
        items = body  # 手工编辑过的裸数组也接受
    elif isinstance(body, dict):
        items = body.get("projects")
        if not isinstance(items, list):
            return fail("文件格式不对：缺少 projects 列表", 400)
    else:
        return fail("文件格式不对：需要 JSON 对象或数组", 400)

    if not items:
        return fail("文件里没有任何项目", 400)
    if len(items) > _IMPORT_LIMIT:
        return fail(f"一次最多导入 {_IMPORT_LIMIT} 个项目", 400)

    existing = {p.name: p for p in repo.list_projects()}
    created = updated = 0
    errors: list[str] = []
    running_updated: list[str] = []

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(f"第 {index} 条：不是对象")
            continue
        try:
            fields = repo.validate(item)
        except ValidationError as exc:
            errors.append(f"第 {index} 条（{item.get('name', '未命名')}）：{exc}")
            continue

        name = fields["name"]
        current = existing.get(name)
        if current is not None:
            project = repo.update_project(current.id, fields)
            if project is None:  # 并发删除，罕见
                errors.append(f"第 {index} 条（{name}）：更新时项目消失")
                continue
            updated += 1
            if get_manager().status(project)["status"] in LIVE_STATUSES:
                running_updated.append(name)
        else:
            project = repo.create_project(fields)
            existing[name] = project  # 同名第二条会走更新，行为一致
            created += 1

    message = f"导入完成：新增 {created} 个，更新 {updated} 个"
    if errors:
        shown = errors[:5]
        more = f" 等 {len(errors)} 条" if len(errors) > 5 else ""
        message += f"，跳过：\n" + "\n".join(shown) + more
    if running_updated:
        message += f"\n注意：{'、'.join(running_updated)} 正在运行，新配置需手动重启后生效"

    return ok(message, data={
        "created": created,
        "updated": updated,
        "skipped": len(errors),
        "errors": errors,
    })
