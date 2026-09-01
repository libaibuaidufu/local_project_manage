"""全局配置。

所有值都可以用环境变量覆盖，方便临时改端口：`PPM_PORT=6000 python app.py`。
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = Path(os.environ.get("PPM_DB_PATH") or (DATA_DIR / "projects.db"))

# 管理器自身的监听地址。默认只监听回环地址：本工具能执行任意 Windows 命令，
# 暴露到局域网/公网等同于把这台机器交出去。
HOST = os.environ.get("PPM_HOST", "127.0.0.1")
PORT = int(os.environ.get("PPM_PORT", "5000"))
DEBUG = os.environ.get("PPM_DEBUG", "0") == "1"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# 监控线程采样间隔（秒）。psutil 的 cpu_percent 需要两次采样才有意义，
# 间隔越小越灵敏、开销越大；1.5 秒对 5~30 个项目足够。
MONITOR_INTERVAL = float(os.environ.get("PPM_MONITOR_INTERVAL", "1.5"))

# 停止进程的三段式超时（秒）：先温和通知、再 terminate、最后 kill。
STOP_GRACEFUL_TIMEOUT = 2.5
STOP_TERMINATE_TIMEOUT = 2.5
STOP_KILL_TIMEOUT = 2.0

# 重启时旧进程退出后额外等待的时间，给操作系统释放端口的机会。
RESTART_DELAY = 0.8

# 启动后同步观察这么久，用来捕获“命令根本跑不起来”的秒退失败。
START_PROBE_DELAY = 0.4

# STARTING 持续这么久且进程还活着，就认为进入 RUNNING。
STARTING_GRACE = 2.0

# auto_start 逐个启动的间隔，避免瞬间拉起几十个进程打满机器。
AUTO_START_INTERVAL = float(os.environ.get("PPM_AUTO_START_INTERVAL", "0.8"))

# 单次日志请求返回的最大字节数，避免超长日志一次性打爆浏览器。
LOG_READ_CHUNK = 256 * 1024


def ensure_dirs() -> None:
    """确保 data/ 与 logs/ 存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
