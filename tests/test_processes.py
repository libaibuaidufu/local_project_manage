"""进程启动 / 停止 / 重启 / 状态检测。

这些测试会真的创建 Python 子进程，所以比 CRUD 测试慢一些。
"""

from __future__ import annotations

import time

import psutil
import pytest

from tests.conftest import make_payload

LIVE = {"RUNNING", "STARTING"}


def create(client, scripts, script, **overrides) -> int:
    response = client.post("/api/projects", json=make_payload(scripts, script, **overrides))
    assert response.status_code == 201
    return response.get_json()["data"]["id"]


def status_of(client, project_id) -> dict:
    response = client.get(f"/api/projects/{project_id}/status")
    assert response.status_code == 200
    return response.get_json()["data"]


def wait_for(client, project_id, statuses, timeout=15.0) -> dict:
    """轮询状态直到进入期望状态之一。超时就把最后看到的状态打出来。"""
    deadline = time.time() + timeout
    data = {}
    while time.time() < deadline:
        data = status_of(client, project_id)
        if data["status"] in statuses:
            return data
        time.sleep(0.25)
    pytest.fail(f"等待状态 {statuses} 超时，实际是 {data.get('status')}（{data}）")


def test_start_and_stop(client, scripts):
    project_id = create(client, scripts, "sleeper.py")

    response = client.post(f"/api/projects/{project_id}/start")
    assert response.status_code == 200, response.get_json()
    pid = response.get_json()["data"]["pid"]
    assert pid > 0

    data = wait_for(client, project_id, LIVE)
    assert data["pid"] == pid
    assert psutil.pid_exists(pid)

    response = client.post(f"/api/projects/{project_id}/stop")
    assert response.status_code == 200, response.get_json()
    assert status_of(client, project_id)["status"] == "STOPPED"

    # 进程树必须真的没了
    time.sleep(0.3)
    assert not psutil.pid_exists(pid) or not psutil.Process(pid).is_running()


def test_no_double_start(client, scripts):
    """需求 39：已经在跑就不能再启一个。"""
    project_id = create(client, scripts, "sleeper.py")
    assert client.post(f"/api/projects/{project_id}/start").status_code == 200
    wait_for(client, project_id, LIVE)

    response = client.post(f"/api/projects/{project_id}/start")
    assert response.status_code == 409
    assert "已经在运行" in response.get_json()["message"]

    client.post(f"/api/projects/{project_id}/stop")


def test_stop_when_not_running(client, scripts):
    project_id = create(client, scripts, "sleeper.py")
    response = client.post(f"/api/projects/{project_id}/stop")
    assert response.status_code == 409
    assert "没有在运行" in response.get_json()["message"]


def test_restart_replaces_process(client, scripts):
    """需求 12：重启后必须是一个新进程，旧的要先完全退出。"""
    project_id = create(client, scripts, "sleeper.py")
    client.post(f"/api/projects/{project_id}/start")
    first = wait_for(client, project_id, LIVE)["pid"]

    response = client.post(f"/api/projects/{project_id}/restart")
    assert response.status_code == 200, response.get_json()
    second = wait_for(client, project_id, LIVE)["pid"]

    assert second != first
    assert not psutil.pid_exists(first) or not psutil.Process(first).is_running()

    client.post(f"/api/projects/{project_id}/stop")


def test_restart_from_stopped(client, scripts):
    """没在跑的时候点重启，等价于启动。"""
    project_id = create(client, scripts, "sleeper.py")
    response = client.post(f"/api/projects/{project_id}/restart")
    assert response.status_code == 200
    wait_for(client, project_id, LIVE)
    client.post(f"/api/projects/{project_id}/stop")


def test_missing_working_dir(client, scripts):
    """需求 33：目录不存在要明确报错。"""
    project_id = create(client, scripts, "sleeper.py", working_dir="D:\\no\\such\\dir")
    response = client.post(f"/api/projects/{project_id}/start")
    assert response.status_code == 400
    assert "工作目录不存在" in response.get_json()["message"]
    assert status_of(client, project_id)["status"] == "ERROR"


def test_bad_command_reports_failure(client, scripts):
    """需求 33：命令错误要报失败，而不是先说成功再变红。"""
    project_id = create(client, scripts, "sleeper.py",
                        command="this_command_definitely_does_not_exist_xyz")
    response = client.post(f"/api/projects/{project_id}/start")
    assert response.status_code == 500
    assert "启动命令" in response.get_json()["message"]


def test_crash_becomes_error(client, scripts):
    """需求 33：非 0 退出码 → 异常退出状态。"""
    project_id = create(client, scripts, "crasher.py")
    assert client.post(f"/api/projects/{project_id}/start").status_code == 200

    data = wait_for(client, project_id, {"ERROR"})
    assert data["exit_code"] == 3


def test_external_kill_is_detected(client, scripts):
    """需求 14：从任务管理器杀掉进程后，页面状态必须能恢复。"""
    project_id = create(client, scripts, "sleeper.py")
    client.post(f"/api/projects/{project_id}/start")
    pid = wait_for(client, project_id, LIVE)["pid"]

    # 模拟用户在任务管理器里结束任务
    proc = psutil.Process(pid)
    for child in proc.children(recursive=True):
        child.kill()
    proc.kill()
    proc.wait(timeout=10)

    # 外部杀进程退出码非 0，落到 ERROR；两者都表示"不在运行"
    data = wait_for(client, project_id, {"STOPPED", "ERROR"})
    assert data["pid"] is None


def test_delete_blocked_while_running(client, scripts):
    """需求 25：运行中不允许删除，避免孤儿进程。"""
    project_id = create(client, scripts, "sleeper.py")
    client.post(f"/api/projects/{project_id}/start")
    wait_for(client, project_id, LIVE)

    response = client.delete(f"/api/projects/{project_id}")
    assert response.status_code == 409
    assert "先停止" in response.get_json()["message"]

    client.post(f"/api/projects/{project_id}/stop")
    assert client.delete(f"/api/projects/{project_id}").status_code == 200


def test_edit_running_project_warns(client, scripts):
    """需求 24：运行中改配置不自动重启，但要提示用户。"""
    project_id = create(client, scripts, "sleeper.py")
    client.post(f"/api/projects/{project_id}/start")
    wait_for(client, project_id, LIVE)

    response = client.put(f"/api/projects/{project_id}",
                          json=make_payload(scripts, "sleeper.py", name="新名字"))
    assert response.status_code == 200
    assert "不会自动更新" in response.get_json()["message"]

    client.post(f"/api/projects/{project_id}/stop")


def test_environment_passed_to_child(client, scripts):
    """自定义环境变量必须真的传进子进程。"""
    project_id = create(client, scripts, "env_echo.py", environment={"MY_VAR": "hello-123"})
    client.post(f"/api/projects/{project_id}/start")
    wait_for(client, project_id, LIVE)

    text = read_logs(client, project_id, "MY_VAR=")
    assert "MY_VAR=hello-123" in text

    client.post(f"/api/projects/{project_id}/stop")


def read_logs(client, project_id, marker, timeout=15.0) -> str:
    """轮询日志直到出现 marker。子进程输出有缓冲延迟，必须等。"""
    deadline = time.time() + timeout
    collected = ""
    while time.time() < deadline:
        response = client.get(f"/api/projects/{project_id}/logs")
        assert response.status_code == 200
        data = response.get_json()["data"]
        collected = data["stdout"]["text"] + data["stderr"]["text"]
        if marker in collected:
            return collected
        time.sleep(0.3)
    pytest.fail(f"日志里没等到 {marker!r}，实际内容：{collected!r}")


def test_multiple_projects_run_independently(client, scripts):
    """需求 34：多个项目同时跑，互不影响。"""
    ids = [create(client, scripts, "sleeper.py", name=f"项目{i}") for i in range(3)]
    for project_id in ids:
        assert client.post(f"/api/projects/{project_id}/start").status_code == 200

    pids = [wait_for(client, project_id, LIVE)["pid"] for project_id in ids]
    assert len(set(pids)) == 3

    summary = client.get("/api/projects").get_json()["data"]
    assert summary["running"] == 3
    assert summary["total"] == 3

    # 停掉一个，另外两个不受影响
    client.post(f"/api/projects/{ids[0]}/stop")
    assert status_of(client, ids[0])["status"] == "STOPPED"
    for project_id in ids[1:]:
        assert status_of(client, project_id)["status"] in LIVE

    for project_id in ids[1:]:
        client.post(f"/api/projects/{project_id}/stop")
