"""测试夹具。

每个测试用独立的临时数据库和日志目录，互不干扰。
app 用 ``bootstrap=False`` 创建：不起监控线程、不接管遗留进程、不跑 auto_start。
状态检测靠 ``status()`` 里的存活探测，跟真实运行时走的是同一条路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from app import create_app  # noqa: E402
from routes.helpers import get_manager  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把数据目录和日志目录指到 tmp_path。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "projects.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    # 测试不需要等真实的启动观察期
    monkeypatch.setattr(config, "START_PROBE_DELAY", 0.3)
    monkeypatch.setattr(config, "STARTING_GRACE", 0.5)
    return tmp_path


@pytest.fixture
def app(env):
    application = create_app(bootstrap=False)
    yield application
    # 收尾：把测试期间起的进程都杀掉，别留下孤儿
    with application.app_context():
        manager = get_manager()
    for project_id in list(manager._runtimes):
        runtime = manager._runtimes.get(project_id)
        if runtime is not None:
            from manager.process_manager import _kill_tree

            _kill_tree(runtime.pid, runtime.create_time)
    manager.shutdown()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def manager(app):
    with app.app_context():
        return get_manager()


@pytest.fixture
def scripts(tmp_path):
    """写几个测试用的 Python 脚本，返回它们所在目录。"""
    directory = tmp_path / "workdir"
    directory.mkdir()

    (directory / "sleeper.py").write_text(
        "import sys, time\n"
        "print('SLEEPER_STARTED', flush=True)\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    (directory / "talker.py").write_text(
        "import sys\n"
        "print('标准输出：你好', flush=True)\n"
        "print('标准错误：出错了', file=sys.stderr, flush=True)\n"
        "import time; time.sleep(300)\n",
        encoding="utf-8",
    )
    (directory / "crasher.py").write_text(
        "import sys, time\n"
        "print('about to crash', flush=True)\n"
        "time.sleep(0.6)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    (directory / "env_echo.py").write_text(
        "import os, time\n"
        "print('MY_VAR=' + os.environ.get('MY_VAR', 'MISSING'), flush=True)\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    return directory


def make_payload(directory: Path, script: str, **overrides) -> dict:
    """构造创建项目的请求体。用当前解释器，保证测试环境一定跑得起来。"""
    payload = {
        "name": script.replace(".py", ""),
        "working_dir": str(directory),
        "command": f'"{sys.executable}" {script}',
        "port": None,
        "environment": {},
        "auto_start": False,
    }
    payload.update(overrides)
    return payload
