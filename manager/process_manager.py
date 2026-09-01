"""进程管理：启动 / 停止 / 重启 / 状态采集。

线程模型
--------
* Flask 请求线程：调用 start/stop/restart/status。
* 一个全局监控线程：定时采样 CPU/内存、检测进程死亡、回收句柄。
  所有项目共用一个线程，避免"每项目两个线程"的泄漏风险（需求 36）。

锁的约定
--------
* ``_state_lock`` 保护 ``_runtimes`` 字典和 Runtime 字段，只做内存操作，持有时间极短。
* 每个项目一把 ``_op_locks`` 操作锁，串行化该项目的 start/stop/restart。
  杀进程树最坏要等 7 秒，这段时间不持有 state lock，状态查询不受影响。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, BinaryIO

import psutil

import config
from manager import logger as log_store
from manager import project_manager as repo
from models.project import LIVE_STATUSES, Project, ProjectStatus

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# 让子进程日志即时可见：Python 默认在非 TTY 下全缓冲，不设这个会导致
# 日志窗口几分钟都是空的。用户在项目配置里显式设置同名变量可以覆盖。
DEFAULT_CHILD_ENV = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}


@dataclass(slots=True)
class ActionResult:
    """start/stop/restart 的统一返回。``http_status`` 直接交给路由层。"""

    success: bool
    message: str
    http_status: int = 200
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Runtime:
    """一个正在运行（或刚刚死掉、还没收尾）的进程。"""

    project_id: int
    pid: int
    run_id: str
    started_at: float
    create_time: float | None
    status: ProjectStatus
    popen: subprocess.Popen[bytes] | None = None
    proc: psutil.Process | None = None
    handles: dict[str, BinaryIO] = field(default_factory=dict)
    #: pid -> psutil.Process，跨采样复用。cpu_percent() 依赖对象内部的
    #: 上一次采样时间，每次新建对象会永远返回 0.0。
    tree_cache: dict[int, psutil.Process] = field(default_factory=dict)
    cpu: float = 0.0
    memory: float = 0.0
    #: True 表示这次退出是用户点的"停止"，不该标记成异常
    intentional: bool = False
    stopping: bool = False
    finalized: bool = False
    #: True 表示这个进程是管理器重启后重新接管的，拿不到退出码
    adopted: bool = False
    exit_code: int | None = None
    error: str | None = None
    #: 启动时的端口配置快照，用于判断端口是否已监听（避免每次查库）
    port: int | None = None


class ProcessManager:
    """管理所有项目进程的生命周期。"""

    def __init__(self) -> None:
        self._runtimes: dict[int, Runtime] = {}
        self._state_lock = threading.RLock()
        self._op_locks: dict[int, threading.Lock] = {}
        self._op_locks_guard = threading.Lock()
        self._monitor: threading.Thread | None = None
        self._stop_event = threading.Event()
        #: 正在监听的端口集合，每个采样周期算一次，供前端显示"端口已就绪"
        self._listening_ports: frozenset[int] = frozenset()

    # ---------------------------------------------------------------- 生命周期

    def bootstrap(self) -> None:
        """管理器启动时调用：接管遗留进程、拉起监控线程、执行自动启动。"""
        self._adopt_previous()
        self._start_monitor()
        self._schedule_auto_start()

    def shutdown(self) -> None:
        """只停监控线程。子进程独立于管理器生命周期，故意不杀（需求 40）。"""
        self._stop_event.set()
        monitor, self._monitor = self._monitor, None
        if monitor and monitor.is_alive():
            monitor.join(timeout=3.0)

    def _op_lock(self, project_id: int) -> threading.Lock:
        with self._op_locks_guard:
            lock = self._op_locks.get(project_id)
            if lock is None:
                lock = threading.Lock()
                self._op_locks[project_id] = lock
            return lock

    # -------------------------------------------------------------------- 启动

    def start(self, project: Project) -> ActionResult:
        """启动项目。立即返回，不等待进程结束（需求 35）。"""
        assert project.id is not None
        project_id = project.id

        with self._op_lock(project_id):
            # 已经在跑就不要再拉一个（需求 39）
            existing = self._live_runtime(project_id)
            if existing is not None:
                return ActionResult(
                    False,
                    "项目已经在运行",
                    409,
                    {"pid": existing.pid, "status": str(existing.status)},
                )

            if not project.working_dir or not os.path.isdir(project.working_dir):
                message = f"工作目录不存在：{project.working_dir}"
                self._mark_error(project_id, message)
                return ActionResult(False, message, 400, {"reason": "working_dir_missing"})

            try:
                return self._spawn(project)
            except Exception as exc:  # noqa: BLE001 - 启动失败的原因五花八门
                log.exception("项目 %s 启动失败", project_id)
                message = f"无法启动项目，请检查启动命令：{exc}"
                self._mark_error(project_id, message)
                return ActionResult(False, message, 500, {"reason": "spawn_failed"})

    def _spawn(self, project: Project) -> ActionResult:
        """真正创建子进程。调用方已持有该项目的操作锁。"""
        assert project.id is not None
        project_id = project.id

        run_id = log_store.new_run_id()
        env = self._build_env(project.environment)
        header = log_store.build_header(
            name=project.name,
            working_dir=project.working_dir,
            command=project.command,
            port=project.port,
            env=project.environment,
        )
        handles = log_store.open_streams(project_id, run_id, header)

        try:
            popen = subprocess.Popen(  # noqa: S602 - 执行用户自己配置的命令即本工具的功能
                project.command,
                cwd=project.working_dir,
                env=env,
                shell=True,
                stdout=handles["stdout"],
                stderr=handles["stderr"],
                stdin=subprocess.DEVNULL,
                creationflags=self._creation_flags(),
                close_fds=True,
            )
        except Exception:
            for handle in handles.values():
                handle.close()
            raise

        return self._register(project, popen, run_id, handles)

    def _register(
        self,
        project: Project,
        popen: subprocess.Popen[bytes],
        run_id: str,
        handles: dict[str, BinaryIO],
    ) -> ActionResult:
        """记录新进程，并短暂观察它是否秒退。"""
        assert project.id is not None
        project_id = project.id
        now = time.time()

        try:
            proc: psutil.Process | None = psutil.Process(popen.pid)
            create_time: float | None = proc.create_time()
            proc.cpu_percent(None)  # 预热，让后续采样有基准
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            proc, create_time = None, None

        runtime = Runtime(
            project_id=project_id,
            pid=popen.pid,
            run_id=run_id,
            started_at=now,
            create_time=create_time,
            status=ProjectStatus.STARTING,
            popen=popen,
            proc=proc,
            handles=handles,
            port=project.port,
        )
        with self._state_lock:
            self._runtimes[project_id] = runtime

        repo.save_runtime(
            project_id,
            pid=popen.pid,
            create_time=create_time,
            start_time=datetime.now().isoformat(timespec="seconds"),
            run_id=run_id,
            status=ProjectStatus.STARTING,
            exit_code=None,
        )

        # 命令写错（比如 xxx_command abc）时 cmd.exe 会立刻退出，
        # 这里同步等一小会儿就能把失败当场报给用户，而不是先说成功再变红。
        time.sleep(config.START_PROBE_DELAY)
        code = popen.poll()
        if code is not None and code != 0:
            tail = self._tail_for_error(project_id, run_id)
            self._finalize(runtime, exit_code=code, reason="启动后立即退出")
            message = "无法启动项目，请检查启动命令。"
            if tail:
                message = f"{message}\n{tail}"
            return ActionResult(False, message, 500, {"exit_code": code, "run_id": run_id})

        return ActionResult(
            True,
            "项目启动成功",
            200,
            {"pid": popen.pid, "run_id": run_id, "status": str(ProjectStatus.STARTING)},
        )

    # -------------------------------------------------------------------- 停止

    def stop(self, project: Project) -> ActionResult:
        """停止项目，连带整个进程树（需求 11）。"""
        assert project.id is not None
        project_id = project.id

        with self._op_lock(project_id):
            runtime = self._live_runtime(project_id)
            if runtime is None:
                return ActionResult(False, "项目没有在运行", 409)

            with self._state_lock:
                runtime.stopping = True  # 让监控线程跳过它，避免重复收尾
                runtime.intentional = True
                runtime.status = ProjectStatus.STOPPING
            repo.save_status(project_id, ProjectStatus.STOPPING)

            killed = _kill_tree(runtime.pid, runtime.create_time)
            self._finalize(runtime, exit_code=self._exit_code(runtime), reason="用户手动停止")

            if not killed:
                return ActionResult(
                    False,
                    "进程可能没有完全退出，请用任务管理器确认",
                    500,
                    {"pid": runtime.pid},
                )
            return ActionResult(True, "项目已停止", 200, {"pid": runtime.pid})

    # -------------------------------------------------------------------- 重启

    def restart(self, project: Project) -> ActionResult:
        """先停、等旧进程完全退出、再启动（需求 12）。"""
        assert project.id is not None

        with self._op_lock(project.id):
            runtime = self._live_runtime(project.id)
            if runtime is not None:
                with self._state_lock:
                    runtime.stopping = True
                    runtime.intentional = True
                    runtime.status = ProjectStatus.STOPPING
                repo.save_status(project.id, ProjectStatus.STOPPING)
                _kill_tree(runtime.pid, runtime.create_time)
                self._finalize(
                    runtime, exit_code=self._exit_code(runtime), reason="重启：停止旧进程"
                )
                # 给操作系统一点时间释放端口，否则新进程可能撞上 Address already in use
                time.sleep(config.RESTART_DELAY)

            try:
                result = self._spawn(project)
            except Exception as exc:  # noqa: BLE001
                log.exception("项目 %s 重启失败", project.id)
                message = f"重启失败：{exc}"
                self._mark_error(project.id, message)
                return ActionResult(False, message, 500)

        if result.success:
            result.message = "项目已重启"
        return result

    # -------------------------------------------------------------------- 状态

    def status(self, project: Project) -> dict[str, Any]:
        """合并配置与运行时状态，给前端一个完整视图。

        这个接口每 1~2 秒被调用一次，所以只读缓存值：CPU/内存由监控线程
        采样，这里不做 psutil 遍历。只做一次极轻量的存活检查，
        让"进程刚死"能立刻反映出来，而不是等下一个采样周期。
        """
        assert project.id is not None
        payload = project.to_dict()

        if project.is_static:
            # 静态项目没有进程，状态只反映"目录和入口文件在不在"
            info = repo.static_info(project)
            payload.update(
                {
                    "status": str(ProjectStatus.STATIC),
                    "pid": None,
                    "cpu": None,
                    "memory": None,
                    "uptime": None,
                    "run_id": None,
                    "exit_code": None,
                    "port_listening": False,
                    "adopted": False,
                    "url": f"/sites/{project.id}/",
                    "dir_exists": info["dir_exists"],
                    "resolved_entry": info["entry"],
                    "html_count": info["html_count"],
                }
            )
            return payload

        runtime = self._runtimes.get(project.id)
        if runtime is not None and not runtime.finalized:
            alive = _is_alive(runtime)
            if not alive and not runtime.stopping:
                self._finalize(runtime, exit_code=self._exit_code(runtime), reason="进程退出")
                runtime = None
            elif alive:
                payload.update(self._runtime_view(runtime))
                return payload

        # 没有活的 runtime：状态来自数据库（跨管理器重启也能保留）
        payload.update(
            {
                "status": str(project.last_status or ProjectStatus.STOPPED),
                "pid": None,
                "cpu": None,
                "memory": None,
                "uptime": None,
                "run_id": project.last_run_id,
                "exit_code": project.last_exit_code,
                "port_listening": False,
                "adopted": False,
            }
        )
        if payload["status"] in {ProjectStatus.STARTING, ProjectStatus.RUNNING, ProjectStatus.STOPPING}:
            # 数据库里写着运行中但内存没有 runtime：说明进程已经不在了
            payload["status"] = str(ProjectStatus.STOPPED)
        return payload

    def _runtime_view(self, runtime: Runtime) -> dict[str, Any]:
        with self._state_lock:
            status = runtime.status
            # STARTING 观察期过了还活着 → 认为真的跑起来了
            if status is ProjectStatus.STARTING and (
                time.time() - runtime.started_at >= config.STARTING_GRACE
            ):
                status = runtime.status = ProjectStatus.RUNNING
                repo.save_status(runtime.project_id, status)

            base = runtime.create_time or runtime.started_at
            return {
                "status": str(status),
                "pid": runtime.pid,
                "cpu": round(runtime.cpu, 1),
                "memory": round(runtime.memory, 1),
                "uptime": max(0, int(time.time() - base)),
                "run_id": runtime.run_id,
                "exit_code": None,
                "adopted": runtime.adopted,
                "port_listening": self._port_listening(runtime),
            }

    def _port_listening(self, runtime: Runtime) -> bool:
        """该项目配置的端口是否真的在监听。用监控线程算好的集合，不现场扫描。"""
        return bool(runtime.port and runtime.port in self._listening_ports)

    def snapshot(self, projects: list[Project]) -> dict[str, Any]:
        """列表页用的批量状态。"""
        items = [self.status(project) for project in projects]
        running = sum(
            1
            for item in items
            if item["status"] in {ProjectStatus.RUNNING, ProjectStatus.STARTING}
        )
        return {"projects": items, "running": running, "total": len(items)}

    # ---------------------------------------------------------------- 监控线程

    def _start_monitor(self) -> None:
        if self._monitor and self._monitor.is_alive():
            return
        self._stop_event.clear()
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="ppm-monitor", daemon=True
        )
        self._monitor.start()

    def _monitor_loop(self) -> None:
        """定时采样。任何异常都必须吞在循环里，线程死掉状态就永远不更新了。"""
        while not self._stop_event.wait(config.MONITOR_INTERVAL):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - 监控线程绝不能退出（需求 34）
                log.exception("监控循环出错")

    def _tick(self) -> None:
        self._listening_ports = _scan_listening_ports()

        with self._state_lock:
            runtimes = list(self._runtimes.values())

        for runtime in runtimes:
            if runtime.finalized or runtime.stopping:
                continue  # 正在被 stop() 处理，别抢
            try:
                if _is_alive(runtime):
                    self._sample(runtime)
                else:
                    self._finalize(
                        runtime, exit_code=self._exit_code(runtime), reason="进程退出"
                    )
            except Exception:  # noqa: BLE001 - 单个项目出问题不能影响其他项目
                log.exception("采样项目 %s 时出错", runtime.project_id)

    def _sample(self, runtime: Runtime) -> None:
        """采集整个进程树的 CPU 与内存。

        为什么算整棵树：``shell=True`` 时我们记录的 PID 是 cmd.exe，真正干活的
        python.exe 是它的子进程。只看父进程会永远显示 0%。
        """
        procs = self._refresh_tree(runtime)
        cpu = 0.0
        memory = 0.0
        for proc in procs:
            try:
                cpu += proc.cpu_percent(None)
                memory += proc.memory_info().rss / (1024 * 1024)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue  # 子进程刚好退出，跳过

        with self._state_lock:
            runtime.cpu = cpu
            runtime.memory = memory
            if runtime.status is ProjectStatus.STARTING and (
                time.time() - runtime.started_at >= config.STARTING_GRACE
            ):
                runtime.status = ProjectStatus.RUNNING
                repo.save_status(runtime.project_id, ProjectStatus.RUNNING)

    def _refresh_tree(self, runtime: Runtime) -> list[psutil.Process]:
        """维护 pid -> Process 缓存。

        必须复用 Process 对象：``cpu_percent(None)`` 是"距上次调用的平均值"，
        每次 new 一个对象只会得到 0.0。
        """
        root = runtime.proc
        if root is None:
            return []
        try:
            wanted = {root.pid: root}
            for child in root.children(recursive=True):
                wanted[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []

        cache = runtime.tree_cache
        for pid in list(cache):
            if pid not in wanted:
                del cache[pid]
        for pid, proc in wanted.items():
            if pid not in cache:
                try:
                    proc.cpu_percent(None)  # 预热新出现的子进程
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                cache[pid] = proc
        return list(cache.values())

    # ---------------------------------------------------------------- 状态收尾

    def _finalize(self, runtime: Runtime, *, exit_code: int | None, reason: str) -> None:
        """进程结束后的收尾：关句柄、写日志尾、落库、从内存移除。

        幂等：monitor 和 stop() 可能同时走到这里。
        """
        with self._state_lock:
            if runtime.finalized:
                return
            runtime.finalized = True
            runtime.exit_code = exit_code
            intentional = runtime.intentional

        for handle in runtime.handles.values():
            try:
                handle.close()
            except OSError:
                pass
        runtime.handles.clear()
        runtime.tree_cache.clear()

        if intentional or exit_code == 0 or exit_code is None:
            status = ProjectStatus.STOPPED
        else:
            status = ProjectStatus.ERROR

        log_store.append_footer(
            runtime.project_id, runtime.run_id, log_store.build_footer(exit_code, reason)
        )

        with self._state_lock:
            runtime.status = status
            if self._runtimes.get(runtime.project_id) is runtime:
                del self._runtimes[runtime.project_id]

        repo.save_runtime(
            runtime.project_id,
            pid=None,
            create_time=None,
            start_time=None,
            run_id=runtime.run_id,
            status=status,
            exit_code=exit_code,
        )

    def _exit_code(self, runtime: Runtime) -> int | None:
        """尽力取退出码。被接管的进程没有 Popen 句柄，只能返回 None。"""
        if runtime.popen is None:
            return None
        try:
            return runtime.popen.poll()
        except OSError:
            return None

    def _mark_error(self, project_id: int, message: str) -> None:
        """启动失败（还没有进程）时把项目标成 ERROR。"""
        repo.save_runtime(
            project_id,
            pid=None,
            create_time=None,
            start_time=None,
            run_id=None,
            status=ProjectStatus.ERROR,
            exit_code=None,
        )
        log.warning("项目 %s: %s", project_id, message)

    def _live_runtime(self, project_id: int) -> Runtime | None:
        """返回确实还活着的 runtime；发现已死的顺手收尾。"""
        runtime = self._runtimes.get(project_id)
        if runtime is None or runtime.finalized:
            return None
        if runtime.stopping:
            return runtime  # 正在停，仍算"活着"，防止此刻又被启动
        if _is_alive(runtime):
            return runtime
        self._finalize(runtime, exit_code=self._exit_code(runtime), reason="进程退出")
        return None

    def _tail_for_error(self, project_id: int, run_id: str, lines: int = 6) -> str:
        """启动失败时取日志尾部，直接显示在错误提示里。"""
        parts: list[str] = []
        for stream in ("stderr", "stdout"):
            chunk = log_store.read_stream(project_id, run_id, stream)
            text = chunk.text.strip()
            if not text:
                continue
            body = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("=")]
            if body:
                parts.extend(body[-lines:])
        return "\n".join(parts[-lines:])

    # ------------------------------------------------------------ 重新接管进程

    def _adopt_previous(self) -> None:
        """管理器重启后，重新发现仍在运行的项目进程（需求 40 / 41）。

        只有 PID **和** 进程创建时间都匹配才认，因为 Windows 会复用 PID ——
        认错了就会去杀掉一个无关的程序。
        """
        for project in repo.list_projects():
            if project.id is None or not project.last_pid:
                continue
            if project.last_status not in LIVE_STATUSES:
                continue

            pid = project.last_pid
            recorded = project.last_create_time
            try:
                proc = psutil.Process(pid)
                if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                    raise psutil.NoSuchProcess(pid)
                actual = proc.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                self._mark_lost(project)
                continue

            # 创建时间对不上 → 这个 PID 已经被别的程序占用了
            if recorded is None or abs(actual - recorded) > 1.0:
                self._mark_lost(project)
                continue

            try:
                proc.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            runtime = Runtime(
                project_id=project.id,
                pid=pid,
                run_id=project.last_run_id or log_store.new_run_id(),
                started_at=actual,
                create_time=actual,
                status=ProjectStatus.RUNNING,
                popen=None,  # 不是我们这次创建的，拿不到 Popen
                proc=proc,
                adopted=True,
                port=project.port,
            )
            with self._state_lock:
                self._runtimes[project.id] = runtime
            repo.save_status(project.id, ProjectStatus.RUNNING)
            log.info("重新接管项目 %s（PID %s）", project.name, pid)

    def _mark_lost(self, project: Project) -> None:
        """数据库说在跑，实际进程已经没了。"""
        assert project.id is not None
        repo.save_runtime(
            project.id,
            pid=None,
            create_time=None,
            start_time=None,
            run_id=project.last_run_id,
            status=ProjectStatus.STOPPED,
            exit_code=None,
        )

    # ---------------------------------------------------------------- 自动启动

    def _schedule_auto_start(self) -> None:
        """auto_start 放后台线程，别拖慢 Flask 启动（需求 29）。"""
        targets = [p for p in repo.list_projects() if p.auto_start and p.id is not None]
        if not targets:
            return
        thread = threading.Thread(
            target=self._auto_start_worker, args=(targets,), name="ppm-autostart", daemon=True
        )
        thread.start()

    def _auto_start_worker(self, targets: list[Project]) -> None:
        for index, project in enumerate(targets):
            if self._stop_event.is_set():
                return
            if index:
                # 逐个间隔启动，不要瞬间拉起几十个进程
                time.sleep(config.AUTO_START_INTERVAL)
            try:
                if self._live_runtime(project.id or 0) is not None:
                    continue  # 已经接管了，别再启一个
                result = self.start(project)
                level = logging.INFO if result.success else logging.WARNING
                log.log(level, "自动启动 %s：%s", project.name, result.message)
            except Exception:  # noqa: BLE001
                log.exception("自动启动 %s 失败", project.name)

    # -------------------------------------------------------------- 子进程环境

    def _build_env(self, extra: dict[str, str]) -> dict[str, str]:
        """在当前环境基础上叠加项目自定义变量。

        必须继承 os.environ：PATH 没了 ``uv``、``npm`` 这类命令根本找不到。
        """
        env = dict(os.environ)
        env.update(DEFAULT_CHILD_ENV)
        env.update({str(k): str(v) for k, v in extra.items()})
        return env

    def _creation_flags(self) -> int:
        """Windows 进程创建标志。

        * ``CREATE_NEW_PROCESS_GROUP``：让子进程成为进程组组长，这样才能对它
          单独发 CTRL_BREAK_EVENT，而不会把信号也发给管理器自己。
        * ``CREATE_NO_WINDOW``：不弹黑框。
        """
        if not IS_WINDOWS:
            return 0
        return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW


def _is_alive(runtime: Runtime) -> bool:
    """判断进程是否还活着。

    不能只用 ``popen.poll() is None``（需求 14）：进程可能被任务管理器杀掉，
    也可能是管理器重启后接管的、根本没有 Popen 对象。以 psutil 为准。
    """
    if runtime.popen is not None and runtime.popen.poll() is not None:
        return False

    proc = runtime.proc
    if proc is None:
        # 拿不到 psutil 对象时退回 Popen 的判断
        return runtime.popen is not None and runtime.popen.poll() is None

    try:
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        # PID 复用检查：同一个 PID 但创建时间变了，说明已经不是我们的进程
        if runtime.create_time is not None and abs(proc.create_time() - runtime.create_time) > 1.0:
            return False
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False
    return True


def _kill_tree(pid: int, create_time: float | None) -> bool:
    """停止 PID 及其所有子孙进程。

    顺序：CTRL_BREAK（给程序自己清理的机会）→ terminate → kill。
    先子进程后父进程，避免父进程先死导致子进程变孤儿（需求 11）。

    返回 True 表示进程树已确认全部退出。
    """
    try:
        root = psutil.Process(pid)
        # 二次校验，绝对不能误杀复用了 PID 的其他程序（需求 41）
        if create_time is not None and abs(root.create_time() - create_time) > 1.0:
            log.warning("PID %s 的创建时间不匹配，跳过终止以免误杀", pid)
            return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return True  # 已经不在了

    try:
        targets = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        targets = []
    targets.append(root)  # 父进程放最后

    # 第一步：温和地请求退出，让项目有机会跑 finally / atexit
    if IS_WINDOWS:
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
            _, alive = psutil.wait_procs(targets, timeout=config.STOP_GRACEFUL_TIMEOUT)
            if not alive:
                return True
            targets = alive
        except (OSError, psutil.Error):
            pass  # 发不出去就直接进下一步

    # 第二步：terminate
    for proc in targets:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(targets, timeout=config.STOP_TERMINATE_TIMEOUT)
    if not alive:
        return True

    # 第三步：kill
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, still_alive = psutil.wait_procs(alive, timeout=config.STOP_KILL_TIMEOUT)
    if still_alive:
        log.error("以下进程无法终止：%s", [p.pid for p in still_alive])
        return False
    return True


def _scan_listening_ports() -> frozenset[int]:
    """当前所有处于 LISTEN 状态的本地端口。

    每个采样周期算一次，供所有项目共用。Windows 上这个调用不算便宜，
    所以绝不能放在每次 HTTP 状态查询里。
    """
    ports: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr:
                ports.add(conn.laddr.port)
    except (psutil.AccessDenied, OSError, RuntimeError):
        # 权限不足时降级：不显示端口状态，但不影响其他功能
        return frozenset()
    return frozenset(ports)
