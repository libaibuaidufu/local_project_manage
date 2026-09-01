"""静态页面项目的托管。

为什么必须由管理器代为托管，而不是直接给个 ``file://`` 链接：

1. 浏览器**禁止** http:// 页面跳转到 file://（Chrome/Edge 直接拦掉并报
   "Not allowed to load local resource"），所以管理界面上的 file 链接点了没反应。
2. ``file://`` 下 ``fetch()`` / ES module / XHR 全部会被 CORS 拦死，稍微
   现代一点的页面在本地双击打开就是坏的。

所以这里起一条 ``/sites/<id>/<路径>`` 的静态服务，用 Flask 的
``send_from_directory`` 发文件。

安全边界
--------
这个工具本身就能执行任意命令，但那是用户显式配置的；文件服务不一样，
路径来自 URL，属于**外部输入**，必须挡住 ``../`` 逃逸。做法是：把
working_dir 和目标路径都 resolve 成绝对真实路径，再用 ``is_relative_to``
确认目标确实在目录内 —— 不做字符串前缀比较（软链接、大小写、8.3 短名
都能绕过前缀判断）。
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, send_from_directory
from werkzeug.utils import safe_join

from manager import project_manager as repo
from manager.project_manager import DEFAULT_ENTRIES

bp = Blueprint("sites", __name__, url_prefix="/sites")


def _load_static_project(project_id: int):
    """取静态项目。不存在或不是静态项目都当 404。"""
    project = repo.get_project(project_id)
    if project is None or not project.is_static:
        abort(404, description="静态项目不存在")
    return project


def _site_root(project) -> Path:
    """项目根目录的真实绝对路径。目录不存在时 404。"""
    try:
        root = Path(project.working_dir).resolve(strict=True)
    except (OSError, RuntimeError):
        abort(404, description=f"目录不存在：{project.working_dir}")
    if not root.is_dir():
        abort(404, description=f"不是目录：{project.working_dir}")
    return root


def _resolve_inside(root: Path, relative: str) -> Path:
    """把相对路径解析到 root 内部，越界就 403。

    两道关卡：先用 werkzeug 的 ``safe_join`` 拦掉明显的 ``..`` 和绝对路径，
    再对 resolve 后的真实路径做归属校验（safe_join 不跟软链接）。
    """
    joined = safe_join(str(root), relative)
    if joined is None:
        abort(403, description="非法路径")
    try:
        target = Path(joined).resolve()
    except (OSError, RuntimeError):
        abort(404, description="路径无法解析")
    if target != root and not target.is_relative_to(root):
        abort(403, description="非法路径")
    return target


def _pick_entry(root: Path, configured: str) -> str | None:
    """决定打开哪个文件：用户配置的优先，否则猜 index.html。"""
    if configured:
        candidate = _resolve_inside(root, configured)
        return configured if candidate.is_file() else None
    for name in DEFAULT_ENTRIES:
        if (root / name).is_file():
            return name
    return None


@bp.get("/<int:project_id>/")
def site_index(project_id: int):
    """打开静态项目。有入口文件就发它，没有就列目录，方便用户自己挑。"""
    project = _load_static_project(project_id)
    root = _site_root(project)

    entry = _pick_entry(root, project.entry_file)
    if entry:
        return send_from_directory(root, entry)
    return _render_listing(project, root, "")


@bp.get("/<int:project_id>/<path:subpath>")
def site_file(project_id: int, subpath: str):
    """发送项目内的文件；指向目录时列目录内容。"""
    project = _load_static_project(project_id)
    root = _site_root(project)
    target = _resolve_inside(root, subpath)

    if target.is_dir():
        # 目录：先找 index.html，找不到就列出来
        for name in DEFAULT_ENTRIES:
            if (target / name).is_file():
                return send_from_directory(root, f"{subpath.rstrip('/')}/{name}")
        return _render_listing(project, root, subpath)

    if not target.is_file():
        abort(404, description=f"文件不存在：{subpath}")
    return send_from_directory(root, subpath)


def _render_listing(project, root: Path, subpath: str):
    """列目录。没有 index.html 的项目（比如放了几个独立 html）靠这个页面进去。"""
    current = _resolve_inside(root, subpath) if subpath else root
    prefix = subpath.strip("/")

    dirs: list[dict[str, str]] = []
    files: list[dict[str, str]] = []
    try:
        for item in sorted(current.iterdir(), key=lambda p: p.name.lower()):
            if item.name.startswith("."):
                continue  # .git 之类的噪音，不列
            href = f"{prefix}/{item.name}" if prefix else item.name
            if item.is_dir():
                dirs.append({"name": item.name, "href": href})
            else:
                files.append({
                    "name": item.name,
                    "href": href,
                    "size": _human_size(item),
                })
    except OSError as exc:
        abort(500, description=f"无法读取目录：{exc}")

    parent = None
    if prefix:
        head = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        parent = head
    return render_template(
        "listing.html",
        project=project,
        subpath=prefix,
        parent=parent,
        dirs=dirs,
        files=files,
    )


def _human_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "—"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@bp.get("/<int:project_id>")
def site_index_noslash(project_id: int):
    """``/sites/1`` 重定向到 ``/sites/1/``。

    末尾斜杠必须有，否则页面里的相对路径（``./app.js``）会被解析到
    ``/sites/app.js``，所有资源 404。
    """
    return redirect(f"/sites/{project_id}/")


