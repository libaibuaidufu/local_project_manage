"""Python 项目启动器 —— 入口。

用法::

    python app.py
    # 或
    uv run python app.py

然后打开 http://127.0.0.1:5000
"""

from __future__ import annotations

import logging
import sys

from flask import Flask, jsonify, render_template
from werkzeug.exceptions import HTTPException

import config
from manager.db import init_db
from manager.process_manager import ProcessManager
from routes import processes, projects, static_sites


def create_app(*, bootstrap: bool = True) -> Flask:
    """装配应用。

    ``bootstrap=False`` 供测试使用：不接管遗留进程、不起监控线程、
    不执行 auto_start，测试之间互不干扰。
    """
    config.ensure_dirs()
    init_db()

    app = Flask(__name__)
    app.json.ensure_ascii = False  # 让接口里的中文可读

    manager = ProcessManager()
    app.extensions["process_manager"] = manager
    if bootstrap:
        manager.bootstrap()

    app.register_blueprint(projects.bp)
    app.register_blueprint(processes.bp)
    app.register_blueprint(static_sites.bp)

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        """把 Werkzeug 的 HTML 错误页也统一成 JSON 格式。"""
        return jsonify({"success": False, "message": exc.description}), exc.code or 500

    @app.errorhandler(Exception)
    def handle_error(exc: Exception):
        """兜底：任何未捕获异常都记日志并返回 500，不要泄漏栈到前端。"""
        app.logger.exception("未处理的异常：%s", exc)
        return jsonify({"success": False, "message": f"服务端错误：{exc}"}), 500

    return app


def _force_utf8_console() -> None:
    """把标准输出/错误切到 UTF-8。

    必须在任何输出之前调用。Windows 控制台默认用系统代码页（英文系统是
    cp1252，中文系统是 cp936）。如果代码页放不下中文，``print`` 会直接抛
    UnicodeEncodeError 把管理器崩掉 —— 在非中文 Windows 上必然发生。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            # pythonw 之类没有真实流的场景，忽略即可
            continue


def _banner() -> None:
    print()
    print("  Python 项目启动器")
    print(f"  → http://{config.HOST}:{config.PORT}")
    if config.HOST not in config.LOOPBACK_HOSTS:
        print()
        print("  ⚠ 警告：当前监听非回环地址，局域网内任何人都能通过本服务")
        print("    在这台机器上执行任意命令。V1 没有任何身份验证。")
    print()


def main() -> int:
    _force_utf8_console()
    logging.basicConfig(
        level=logging.DEBUG if config.DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    app = create_app()
    _banner()
    try:
        # use_reloader=False：重载器会 fork 出第二个进程，导致 auto_start
        # 执行两遍、监控线程也翻倍。
        app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG,
                threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n已退出。正在运行的项目进程不受影响，仍在后台运行。")
    finally:
        manager = app.extensions.get("process_manager")
        if manager is not None:
            manager.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
