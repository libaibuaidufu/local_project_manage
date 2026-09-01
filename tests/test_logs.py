"""日志：中文编码、stdout/stderr 区分、增量读取、历史记录。"""

from __future__ import annotations

import time

import psutil

from manager import logger as log_store
from tests.conftest import make_payload
from tests.test_processes import LIVE, create, read_logs, wait_for


def test_chinese_stdout_and_stderr(client, scripts):
    """需求 19：中文日志不能导致崩溃；需求 18：要能区分 stdout 和 stderr。"""
    project_id = create(client, scripts, "talker.py")
    client.post(f"/api/projects/{project_id}/start")
    wait_for(client, project_id, LIVE)

    read_logs(client, project_id, "标准错误")
    data = client.get(f"/api/projects/{project_id}/logs").get_json()["data"]

    # 两个流分开返回，各自的内容不会串台
    assert "标准输出：你好" in data["stdout"]["text"]
    assert "标准错误：出错了" in data["stderr"]["text"]
    assert "标准错误" not in data["stdout"]["text"]

    client.post(f"/api/projects/{project_id}/stop")


def test_incremental_read(client, scripts):
    """带 offset 再请求，不应该重复返回已经读过的内容。"""
    project_id = create(client, scripts, "talker.py")
    client.post(f"/api/projects/{project_id}/start")
    wait_for(client, project_id, LIVE)
    read_logs(client, project_id, "标准输出")

    first = client.get(f"/api/projects/{project_id}/logs").get_json()["data"]
    offset = first["stdout"]["offset"]
    assert offset > 0

    second = client.get(
        f"/api/projects/{project_id}/logs?stdout_at={offset}&stderr_at={first['stderr']['offset']}"
    ).get_json()["data"]
    assert second["stdout"]["text"] == ""      # 没有新内容
    assert second["stdout"]["offset"] == offset

    client.post(f"/api/projects/{project_id}/stop")


def test_log_survives_project_stop(client, scripts):
    """需求：项目停止后仍然可以查看本次运行日志。"""
    project_id = create(client, scripts, "talker.py")
    client.post(f"/api/projects/{project_id}/start")
    wait_for(client, project_id, LIVE)
    read_logs(client, project_id, "标准输出")
    client.post(f"/api/projects/{project_id}/stop")

    data = client.get(f"/api/projects/{project_id}/logs").get_json()["data"]
    assert "标准输出：你好" in data["stdout"]["text"]
    # 收尾信息也写进了日志
    assert "退出码" in data["stdout"]["text"]


def test_each_run_gets_its_own_file(client, scripts):
    """需求 43：每次启动一个独立日志文件。"""
    project_id = create(client, scripts, "talker.py")
    for _ in range(2):
        client.post(f"/api/projects/{project_id}/start")
        wait_for(client, project_id, LIVE)
        client.post(f"/api/projects/{project_id}/stop")
        time.sleep(0.1)

    runs = log_store.list_runs(project_id)
    assert len(runs) == 2
    assert runs[0]["run_id"] > runs[1]["run_id"]   # 最新的在前

    data = client.get(f"/api/projects/{project_id}/logs").get_json()["data"]
    assert len(data["runs"]) == 2


def test_can_read_old_run(client, scripts):
    """能指定 run_id 查看历史运行的日志。"""
    project_id = create(client, scripts, "talker.py")
    client.post(f"/api/projects/{project_id}/start")
    wait_for(client, project_id, LIVE)
    read_logs(client, project_id, "标准输出")
    client.post(f"/api/projects/{project_id}/stop")
    old_run = log_store.list_runs(project_id)[0]["run_id"]

    time.sleep(0.1)
    client.post(f"/api/projects/{project_id}/start")
    wait_for(client, project_id, LIVE)
    client.post(f"/api/projects/{project_id}/stop")

    data = client.get(f"/api/projects/{project_id}/logs?run_id={old_run}").get_json()["data"]
    assert data["run_id"] == old_run
    assert "标准输出：你好" in data["stdout"]["text"]


def test_path_traversal_rejected(client, scripts):
    """run_id 不能用来穿越目录读任意文件。"""
    project_id = create(client, scripts, "sleeper.py")
    for evil in ["../../../etc/passwd", "..\\..\\secret", "abc"]:
        response = client.get(f"/api/projects/{project_id}/logs?run_id={evil}")
        assert response.status_code == 400


def test_bad_offset_rejected(client, scripts):
    project_id = create(client, scripts, "sleeper.py")
    response = client.get(f"/api/projects/{project_id}/logs?stdout_at=abc")
    assert response.status_code == 400


def test_logs_empty_before_first_run(client, scripts):
    project_id = create(client, scripts, "sleeper.py")
    data = client.get(f"/api/projects/{project_id}/logs").get_json()["data"]
    assert data["run_id"] is None
    assert data["stdout"]["text"] == ""


def test_gbk_output_does_not_crash(client, scripts, tmp_path):
    """项目输出 GBK 编码时不能让管理器崩溃（需求 19）。"""
    script = scripts / "gbk_out.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdout.buffer.write('中文GBK输出\\n'.encode('gbk'))\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    project_id = create(client, scripts, "gbk_out.py",
                        environment={"PYTHONIOENCODING": "gbk"})
    client.post(f"/api/projects/{project_id}/start")
    wait_for(client, project_id, LIVE)

    text = read_logs(client, project_id, "中文GBK输出")
    assert "中文GBK输出" in text   # GBK 也能正确解码出来

    client.post(f"/api/projects/{project_id}/stop")


def test_log_header_records_config(client, scripts):
    """日志头要记下这次是用什么配置跑的，方便事后排查。"""
    project_id = create(client, scripts, "talker.py", port=8123,
                        environment={"MY_VAR": "abc"})
    client.post(f"/api/projects/{project_id}/start")
    wait_for(client, project_id, LIVE)

    data = client.get(f"/api/projects/{project_id}/logs").get_json()["data"]
    header = data["stdout"]["text"]
    assert "工作目录" in header
    assert "8123" in header
    assert "MY_VAR=abc" in header

    client.post(f"/api/projects/{project_id}/stop")


def test_decode_fallbacks():
    """解码器：UTF-8 优先，GBK 兜底，最后 replace 保证不抛异常。"""
    assert log_store.decode("你好".encode("utf-8")) == "你好"
    assert log_store.decode("你好".encode("gbk")) == "你好"
    # 无效字节序列不能抛异常
    assert log_store.decode(b"\xff\xfe\x00bad") != ""
