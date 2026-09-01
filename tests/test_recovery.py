"""管理器重启后的状态恢复、PID 复用防护、自动启动。"""

from __future__ import annotations

import os
import time

import psutil

from manager import project_manager as repo
from manager.process_manager import ProcessManager
from models.project import ProjectStatus
from tests.conftest import make_payload
from tests.test_processes import LIVE, create, status_of, wait_for


def test_adopt_running_process(client, scripts, app):
    """需求 40：管理器重启后，项目进程应该继续运行并被重新接管。"""
    project_id = create(client, scripts, "sleeper.py")
    client.post(f"/api/projects/{project_id}/start")
    pid = wait_for(client, project_id, LIVE)["pid"]

    # 模拟管理器重启：新建一个 ProcessManager 读同一个数据库
    fresh = ProcessManager()
    fresh._adopt_previous()

    try:
        assert project_id in fresh._runtimes, "重启后没有接管仍在运行的进程"
        runtime = fresh._runtimes[project_id]
        assert runtime.pid == pid
        assert runtime.adopted is True

        project = repo.get_project(project_id)
        assert fresh.status(project)["status"] in LIVE
        assert fresh.status(project)["adopted"] is True
    finally:
        client.post(f"/api/projects/{project_id}/stop")


def test_adopt_rejects_reused_pid(client, scripts):
    """需求 41：PID 相同但创建时间不符时，绝不能认成自己的进程。"""
    project_id = create(client, scripts, "sleeper.py")

    # 伪造一条"运行中"记录，PID 指向当前测试进程，创建时间故意写错
    repo.save_runtime(
        project_id,
        pid=os.getpid(),
        create_time=1.0,          # 1970 年，绝不可能匹配
        start_time="2026-01-01T00:00:00",
        run_id=None,
        status=ProjectStatus.RUNNING,
        exit_code=None,
    )

    fresh = ProcessManager()
    fresh._adopt_previous()

    assert project_id not in fresh._runtimes, "错误地接管了一个 PID 被复用的进程"
    assert repo.get_project(project_id).last_status == ProjectStatus.STOPPED
    # 测试进程本身必须还活着 —— 说明没被误杀
    assert psutil.Process(os.getpid()).is_running()


def test_adopt_handles_dead_pid(client, scripts):
    """数据库说在跑但进程早没了 → 标成已停止。"""
    project_id = create(client, scripts, "sleeper.py")
    repo.save_runtime(
        project_id,
        pid=999_999,              # 不可能存在的 PID
        create_time=time.time(),
        start_time="2026-01-01T00:00:00",
        run_id=None,
        status=ProjectStatus.RUNNING,
        exit_code=None,
    )

    fresh = ProcessManager()
    fresh._adopt_previous()

    assert project_id not in fresh._runtimes
    assert repo.get_project(project_id).last_status == ProjectStatus.STOPPED


def test_status_recovers_when_memory_state_lost(client, scripts):
    """内存里没有 runtime 但数据库写着 RUNNING → 状态查询要报已停止。"""
    project_id = create(client, scripts, "sleeper.py")
    repo.save_runtime(
        project_id,
        pid=999_999,
        create_time=time.time(),
        start_time="2026-01-01T00:00:00",
        run_id=None,
        status=ProjectStatus.RUNNING,
        exit_code=None,
    )
    assert status_of(client, project_id)["status"] == "STOPPED"


def test_auto_start_flag_persists(client, scripts):
    """auto_start 要能存下来（真实的自动拉起由 bootstrap 触发）。"""
    project_id = create(client, scripts, "sleeper.py", auto_start=True)
    assert repo.get_project(project_id).auto_start is True

    client.put(f"/api/projects/{project_id}",
               json=make_payload(scripts, "sleeper.py", auto_start=False))
    assert repo.get_project(project_id).auto_start is False


def test_auto_start_launches_project(client, scripts, monkeypatch):
    """需求 29：bootstrap 时自动拉起 auto_start=true 的项目。"""
    project_id = create(client, scripts, "sleeper.py", auto_start=True)

    fresh = ProcessManager()
    project = repo.get_project(project_id)
    try:
        # 直接调 worker，跳过后台线程和间隔等待，让测试确定性
        fresh._auto_start_worker([project])
        deadline = time.time() + 15
        while time.time() < deadline:
            if fresh.status(repo.get_project(project_id))["status"] in LIVE:
                break
            time.sleep(0.25)
        else:
            raise AssertionError("auto_start 没有拉起项目")
    finally:
        fresh.stop(repo.get_project(project_id))
        fresh.shutdown()


def test_stop_does_not_kill_unrelated_process(client, scripts):
    """停止逻辑必须校验创建时间，不能误杀复用了 PID 的其他程序。"""
    from manager.process_manager import _kill_tree

    # 拿当前测试进程当靶子，创建时间故意给错的
    assert _kill_tree(os.getpid(), create_time=1.0) is True
    assert psutil.Process(os.getpid()).is_running(), "差点杀掉了测试进程自己"
