"""项目 CRUD 与校验。"""

from __future__ import annotations

from tests.conftest import make_payload


def test_empty_list(client):
    response = client.get("/api/projects")
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["data"] == {"projects": [], "running": 0, "total": 0}


def test_create_and_read_back(client, scripts):
    payload = make_payload(scripts, "sleeper.py", name="PC TTS", port=8774,
                           environment={"MODEL_PATH": "D:\\Models\\tts"})
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201
    created = response.get_json()["data"]
    assert created["name"] == "PC TTS"
    assert created["port"] == 8774
    assert created["environment"] == {"MODEL_PATH": "D:\\Models\\tts"}

    listed = client.get("/api/projects").get_json()["data"]
    assert listed["total"] == 1
    assert listed["projects"][0]["status"] == "STOPPED"


def test_update_project(client, scripts):
    project_id = client.post("/api/projects", json=make_payload(scripts, "sleeper.py")).get_json()["data"]["id"]

    response = client.put(f"/api/projects/{project_id}",
                          json=make_payload(scripts, "sleeper.py", name="改名了", port=9001))
    assert response.status_code == 200
    assert response.get_json()["data"]["name"] == "改名了"
    assert response.get_json()["data"]["port"] == 9001


def test_delete_project(client, scripts):
    project_id = client.post("/api/projects", json=make_payload(scripts, "sleeper.py")).get_json()["data"]["id"]
    assert client.delete(f"/api/projects/{project_id}").status_code == 200
    assert client.get(f"/api/projects/{project_id}").status_code == 404


def test_missing_project_returns_404(client):
    assert client.get("/api/projects/999").status_code == 404
    assert client.put("/api/projects/999", json={}).status_code == 404
    assert client.delete("/api/projects/999").status_code == 404
    assert client.post("/api/projects/999/start").status_code == 404


def test_validation_errors(client, scripts):
    cases = [
        ({}, "名称"),
        (make_payload(scripts, "sleeper.py", name=""), "名称"),
        (make_payload(scripts, "sleeper.py", working_dir=""), "工作目录"),
        (make_payload(scripts, "sleeper.py", command=""), "启动命令"),
        (make_payload(scripts, "sleeper.py", port=99999), "端口"),
        (make_payload(scripts, "sleeper.py", port="abc"), "端口"),
        (make_payload(scripts, "sleeper.py", environment={"BAD KEY": "x"}), "环境变量"),
    ]
    for payload, keyword in cases:
        response = client.post("/api/projects", json=payload)
        assert response.status_code == 400, payload
        assert keyword in response.get_json()["message"]


def test_nonexistent_dir_saves_with_warning(client, scripts):
    """目录不存在允许保存，但要提示（用户可能打算稍后再创建）。"""
    payload = make_payload(scripts, "sleeper.py", working_dir="D:\\definitely\\not\\here")
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201
    assert "不存在" in response.get_json()["message"]


def test_port_optional(client, scripts):
    response = client.post("/api/projects", json=make_payload(scripts, "sleeper.py", port=""))
    assert response.status_code == 201
    assert response.get_json()["data"]["port"] is None


def test_projects_ordered_by_creation(client, scripts):
    for name in ("第一个", "第二个", "第三个"):
        client.post("/api/projects", json=make_payload(scripts, "sleeper.py", name=name))
    projects = client.get("/api/projects").get_json()["data"]["projects"]
    assert [p["name"] for p in projects] == ["第一个", "第二个", "第三个"]
