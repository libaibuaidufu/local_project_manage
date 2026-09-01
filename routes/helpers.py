"""路由公共部分：统一响应格式、获取 ProcessManager。"""

from __future__ import annotations

from typing import Any

from flask import current_app, jsonify
from flask.wrappers import Response

from manager.process_manager import ProcessManager


def get_manager() -> ProcessManager:
    """取挂在 app 上的 ProcessManager 单例。"""
    manager = current_app.extensions.get("process_manager")
    if manager is None:  # pragma: no cover - 只会在装配错误时发生
        raise RuntimeError("ProcessManager 未初始化")
    return manager


def ok(message: str = "", data: Any = None, status: int = 200) -> tuple[Response, int]:
    """成功响应：``{"success": true, "message": ..., "data": ...}``"""
    return jsonify({"success": True, "message": message, "data": data if data is not None else {}}), status


def fail(message: str, status: int = 400, data: Any = None) -> tuple[Response, int]:
    """失败响应。HTTP 状态码同步表达语义（400/404/409/500）。"""
    payload: dict[str, Any] = {"success": False, "message": message}
    if data is not None:
        payload["data"] = data
    return jsonify(payload), status
