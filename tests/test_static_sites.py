"""静态页面项目：校验、访问、目录列表、路径穿越防护。"""

from __future__ import annotations

import pytest


@pytest.fixture
def site(tmp_path):
    """一个带 index.html 和子目录资源的静态站点目录。"""
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text("<h1>首页</h1>", encoding="utf-8")
    (root / "about.html").write_text("<h1>关于</h1>", encoding="utf-8")
    assets = root / "assets"
    assets.mkdir()
    (assets / "style.css").write_text("body{color:red}", encoding="utf-8")
    (root / "secret.txt").write_text("不是网页也照样能读", encoding="utf-8")
    return root


@pytest.fixture
def bare(tmp_path):
    """没有 index.html 的目录，用来验证目录列表兜底。"""
    root = tmp_path / "bare"
    root.mkdir()
    (root / "report.html").write_text("<p>报表</p>", encoding="utf-8")
    (root / "chart.html").write_text("<p>图表</p>", encoding="utf-8")
    return root


def create_site(client, directory, **overrides):
    payload = {"name": "静态站", "kind": "static", "working_dir": str(directory)}
    payload.update(overrides)
    return client.post("/api/projects", json=payload)


def test_create_static_without_command(client, site):
    """静态项目不需要 command —— 这是它和进程项目最大的区别。"""
    response = create_site(client, site)
    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data["kind"] == "static"
    assert data["command"] == ""
    assert data["status"] == "STATIC"
    assert data["url"] == f"/sites/{data['id']}/"
    assert data["resolved_entry"] == "index.html"


def test_static_never_reports_running(client, site):
    """静态项目没有进程，状态恒为 STATIC，不参与运行计数。"""
    create_site(client, site)
    listed = client.get("/api/projects").get_json()["data"]
    assert listed["total"] == 1
    assert listed["running"] == 0
    assert listed["projects"][0]["status"] == "STATIC"


def test_missing_dir_warns_but_saves(client, tmp_path):
    """目录不存在只提醒不拦 —— 和进程项目一致（用户可能打算稍后再建）。"""
    response = create_site(client, tmp_path / "并不存在")
    assert response.status_code == 201
    assert "不存在" in response.get_json()["message"]


def test_auto_start_forced_off(client, site):
    """静态项目无从"启动"，auto_start 必须被强制关掉。"""
    data = create_site(client, site, auto_start=True).get_json()["data"]
    assert data["auto_start"] is False


def test_serves_index_by_default(client, site):
    project_id = create_site(client, site).get_json()["data"]["id"]
    response = client.get(f"/sites/{project_id}/")
    assert response.status_code == 200
    assert "首页" in response.get_data(as_text=True)


def test_serves_nested_asset_with_mime(client, site):
    project_id = create_site(client, site).get_json()["data"]["id"]
    response = client.get(f"/sites/{project_id}/assets/style.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["Content-Type"]


def test_custom_entry_file(client, site):
    data = create_site(client, site, entry_file="about.html").get_json()["data"]
    assert data["resolved_entry"] == "about.html"
    body = client.get(f"/sites/{data['id']}/").get_data(as_text=True)
    assert "关于" in body


def test_directory_listing_when_no_index(client, bare):
    """没有 index.html 时列出 HTML 文件，而不是直接 404。"""
    data = create_site(client, bare).get_json()["data"]
    assert data["resolved_entry"] is None
    body = client.get(f"/sites/{data['id']}/").get_data(as_text=True)
    assert "report.html" in body
    assert "chart.html" in body


def test_missing_file_returns_404(client, site):
    project_id = create_site(client, site).get_json()["data"]["id"]
    assert client.get(f"/sites/{project_id}/nope.html").status_code == 404


@pytest.mark.parametrize("attack", [
    "../../../../Windows/win.ini",
    "..%2f..%2f..%2fWindows/win.ini",
    "....//....//Windows/win.ini",
    "/etc/passwd",
    "..\\..\\..\\Windows\\win.ini",
])
def test_path_traversal_blocked(client, site, attack):
    """working_dir 之外的文件一律读不到 —— 这个工具只监听本机，但仍不能当文件服务器用。

    只断言"没吐出目录外的内容"，不锁具体状态码：403（判定非法）、404（拼出来
    的路径不存在）、308（多余斜杠被 Werkzeug 规范化）都算拦住了。
    """
    project_id = create_site(client, site).get_json()["data"]["id"]
    response = client.get(f"/sites/{project_id}/{attack}",
                          follow_redirects=True)
    assert response.status_code in (403, 404)
    # win.ini 里必然有的小节名；出现即说明真读到了系统文件
    assert "[fonts]" not in response.get_data(as_text=True).lower()


def test_sibling_dir_escape_blocked(client, site, tmp_path):
    """兄弟目录同样越界 —— 前缀相同不代表在目录内。"""
    outside = tmp_path / "site_backup"
    outside.mkdir()
    (outside / "leak.html").write_text("机密内容", encoding="utf-8")
    project_id = create_site(client, site).get_json()["data"]["id"]
    response = client.get(f"/sites/{project_id}/../site_backup/leak.html",
                          follow_redirects=True)
    assert response.status_code in (403, 404)
    assert "机密内容" not in response.get_data(as_text=True)


def test_unknown_project_404(client):
    assert client.get("/sites/9999/").status_code == 404


def test_process_project_not_servable(client, scripts):
    """进程项目不能走静态通道，否则等于把源码目录挂上去了。"""
    from tests.conftest import make_payload

    project_id = client.post(
        "/api/projects", json=make_payload(scripts, "sleeper.py")
    ).get_json()["data"]["id"]
    assert client.get(f"/sites/{project_id}/").status_code == 404


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_process_actions_rejected(client, site, action):
    """对静态项目调启停接口要明确拒绝，而不是假装成功。"""
    project_id = create_site(client, site).get_json()["data"]["id"]
    response = client.post(f"/api/projects/{project_id}/{action}")
    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_logs_rejected(client, site):
    project_id = create_site(client, site).get_json()["data"]["id"]
    assert client.get(f"/api/projects/{project_id}/logs").status_code == 400


def test_status_still_readable(client, site):
    """status 是只读查询，前端每张卡片都会轮询，不能被一起拒掉。"""
    project_id = create_site(client, site).get_json()["data"]["id"]
    response = client.get(f"/api/projects/{project_id}/status")
    assert response.status_code == 200
    assert response.get_json()["data"]["status"] == "STATIC"


def test_convert_process_to_static(client, scripts, site):
    """改 kind 时要把 command 清掉，免得留着一条永远不会执行的命令。"""
    from tests.conftest import make_payload

    project_id = client.post(
        "/api/projects", json=make_payload(scripts, "sleeper.py")
    ).get_json()["data"]["id"]

    # PUT 是整体替换，字段要给全
    response = client.put(f"/api/projects/{project_id}",
                          json={"name": "转成静态", "kind": "static",
                                "working_dir": str(site)})
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["kind"] == "static"
    assert data["command"] == ""
    assert data["status"] == "STATIC"
    assert client.get(f"/sites/{project_id}/").status_code == 200
