"""项目日志：每次启动一个独立日志文件。

设计要点：子进程的 stdout/stderr **直接重定向到文件**，而不是走
subprocess.PIPE + 读取线程。原因：

1. 管道有固定缓冲区。如果管理器崩了或者读取线程死了，子进程写满管道后
   会被阻塞住 —— 项目本身就卡死了。重定向到文件不存在这个问题。
2. 需求 40 要求管理器重启不能影响正在运行的项目。文件句柄由子进程持有，
   管理器重启后项目继续往同一个文件写，日志不断档。
3. 少了每个项目一个读取线程，不会有线程泄漏（需求 36）。

代价是没法给每一行加管理器侧的时间戳。换来的是稳定性，V1 值得。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import config

#: 运行 ID 形如 2026-09-01_13-20-10-123，用于拼日志文件名。
#: 校验它可以防止 ?run_id=../../etc 这类路径穿越。
RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d{3})?$")

STREAMS = ("stdout", "stderr")
_SUFFIX = {"stdout": ".out.log", "stderr": ".err.log"}


@dataclass(slots=True)
class LogChunk:
    """一次增量读取的结果。"""

    text: str
    offset: int
    size: int
    truncated: bool


def new_run_id() -> str:
    """生成本次运行的 ID。带毫秒，避免同一秒内重启撞名。"""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]


def project_dir(project_id: int) -> Path:
    return config.LOG_DIR / f"project_{project_id}"


def log_path(project_id: int, run_id: str, stream: str = "stdout") -> Path:
    """拼日志文件路径。run_id 必须先通过校验。"""
    if not RUN_ID_RE.match(run_id):
        raise ValueError(f"非法的运行 ID：{run_id}")
    if stream not in _SUFFIX:
        raise ValueError(f"未知的日志流：{stream}")
    return project_dir(project_id) / f"{run_id}{_SUFFIX[stream]}"


def open_streams(project_id: int, run_id: str, header: str) -> dict[str, BinaryIO]:
    """为本次运行创建 stdout/stderr 两个日志文件，写入头部信息。

    用二进制追加模式：子进程的原始字节直接落盘，不做换行转换，
    也不会因为编码问题在写入侧抛异常。
    """
    directory = project_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)

    handles: dict[str, BinaryIO] = {}
    try:
        for stream in STREAMS:
            handle = open(log_path(project_id, run_id, stream), "ab", buffering=0)
            handle.write(header.encode("utf-8", errors="replace"))
            handles[stream] = handle
    except OSError:
        for handle in handles.values():
            handle.close()
        raise
    return handles


def append_footer(project_id: int, run_id: str, footer: str) -> None:
    """进程退出后追加尾部信息。

    单独开句柄而不是复用启动时的句柄：被接管的进程（管理器重启后重新发现的）
    在本进程里没有句柄，只能重新打开。
    """
    for stream in STREAMS:
        try:
            path = log_path(project_id, run_id, stream)
            if not path.exists():
                continue
            with open(path, "ab", buffering=0) as handle:
                handle.write(footer.encode("utf-8", errors="replace"))
        except (OSError, ValueError):
            # 日志写不进去不该影响进程状态收尾
            continue


def _decode_line(raw: bytes) -> str:
    """解码单行。UTF-8 优先，GBK 兜底，最后 replace 保证不抛异常。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def decode(raw: bytes) -> str:
    """解码子进程输出。**逐行**解码，不是整块解码。

    为什么逐行：同一个日志文件里可能混着两种编码 —— 管理器写的头部是
    UTF-8，而项目自己输出的可能是 GBK（Windows 上没设 PYTHONIOENCODING
    的 print 中文、.bat 脚本输出都是 GBK）。整块解码时任何一种编码都会
    在另一部分上失败，最后整个文件都变成乱码。逐行解码则各自都能正确还原。

    乱码可以接受，崩溃不行（需求 19）。
    """
    if not raw:
        return ""
    # 只归一化 \r\n，不动单独的 \r：进度条靠 \r 回到行首覆盖重写，
    # 把它也换成 \n 会让一个进度条变成几百行。
    text = "\n".join(_decode_line(line) for line in raw.split(b"\n"))
    return text.replace("\r\n", "\n")


def read_stream(project_id: int, run_id: str, stream: str, offset: int = 0) -> LogChunk:
    """从 offset 字节处增量读取日志。

    只返回完整行：文件末尾不完整的那一行留到下次读取，
    这样不会把一个多字节字符劈成两半导致乱码。
    """
    try:
        path = log_path(project_id, run_id, stream)
    except ValueError:
        return LogChunk(text="", offset=0, size=0, truncated=False)

    if not path.exists():
        return LogChunk(text="", offset=0, size=0, truncated=False)

    size = path.stat().st_size
    if offset > size:
        # 文件被换掉或清空了，从头再来
        offset = 0

    truncated = False
    start = offset
    if size - start > config.LOG_READ_CHUNK:
        # 日志太长（比如首次打开一个跑了一天的项目），只取尾部
        start = size - config.LOG_READ_CHUNK
        truncated = True

    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return LogChunk(text="", offset=offset, size=size, truncated=False)

    consumed = len(raw)
    tail_cut = 0
    if raw and not raw.endswith(b"\n"):
        # 保留最后一个不完整行，等它写完再读
        cut = raw.rfind(b"\n")
        if cut == -1:
            if not truncated:
                # 整块都还没换行，先不返回，避免半行乱码
                return LogChunk(text="", offset=offset, size=size, truncated=False)
        else:
            tail_cut = consumed - cut - 1
            raw = raw[: cut + 1]

    text = decode(raw)
    if truncated and text:
        text = "…（日志过长，仅显示最近部分）\n" + text.split("\n", 1)[-1]

    return LogChunk(
        text=text,
        offset=start + consumed - tail_cut,
        size=size,
        truncated=truncated,
    )


def list_runs(project_id: int) -> list[dict[str, object]]:
    """列出该项目的历史运行记录，最新的在前。"""
    directory = project_dir(project_id)
    if not directory.is_dir():
        return []

    runs: dict[str, dict[str, object]] = {}
    for path in directory.glob("*.log"):
        name = path.name
        for stream, suffix in _SUFFIX.items():
            if not name.endswith(suffix):
                continue
            run_id = name[: -len(suffix)]
            if not RUN_ID_RE.match(run_id):
                continue
            entry = runs.setdefault(run_id, {"run_id": run_id, "size": 0})
            try:
                entry["size"] = int(entry["size"]) + path.stat().st_size  # type: ignore[call-overload]
                entry.setdefault("mtime", path.stat().st_mtime)
            except OSError:
                pass
            break

    ordered = sorted(runs.values(), key=lambda item: str(item["run_id"]), reverse=True)
    return ordered


def build_header(
    *, name: str, working_dir: str, command: str, port: int | None, env: dict[str, str]
) -> str:
    """日志文件头，方便事后回溯这次是用什么配置跑的。"""
    lines = [
        "=" * 68,
        f"项目      : {name}",
        f"启动时间  : {datetime.now().isoformat(timespec='seconds')}",
        f"工作目录  : {working_dir}",
        f"启动命令  : {command}",
    ]
    if port:
        lines.append(f"端口      : {port}")
    if env:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(env.items()))
        lines.append(f"环境变量  : {rendered}")
    lines.extend(["=" * 68, ""])
    return "\r\n".join(lines)


def build_footer(exit_code: int | None, reason: str) -> str:
    """日志文件尾，记录退出码和退出原因。"""
    code = "未知" if exit_code is None else str(exit_code)
    return "\r\n".join(
        [
            "",
            "-" * 68,
            f"进程结束  : {datetime.now().isoformat(timespec='seconds')}",
            f"退出码    : {code}",
            f"结束原因  : {reason}",
            "-" * 68,
            "",
        ]
    )
