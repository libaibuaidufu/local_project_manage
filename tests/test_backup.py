"""数据备份：导出 / 导入。

导入是合并语义（按名称匹配，不删除现有项目）——这套测试就是
为了钉死这个行为，谁改坏了谁负责。
"""

from __future__ import annotations

import json

from manager.db import connect
from tests.conftest import make_payload


def _create(client, scripts, name, **overrides):
    response = client.post("/api/projects", json=make_payload(scripts, "sleeper.py", name=name, **overrides))
    assert response.status_code == 201
    return response.get_json()["data"]


# ------------------------------------------------------------------ 导出

def test_export_contains_all_configs(client, scripts):
    _create(client, scripts, "甲", port=8801, environment={"KEY": "值"}, auto_start=True)
    _create(client, scripts, "乙")

    response = client.get("/api/projects/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    assert "projects-backup-" in response.headers["Content-Disposition"]

    payload = json.loads(response.data)
    assert payload["format"] == 1
    assert payload["count"] == 2

    entries = {p["name"]: p for p in payload["projects"]}
    assert entries["甲"]["port"] == 8801
    assert entries["甲"]["environment"] == {"KEY": "值"}
    assert entries["甲"]["auto_start"] is True
    # 运行时状态（PID 等）不进备份
    allowed = {"name", "working_dir", "command", "port", "environment",
               "auto_start", "created_at"}
    assert set(entries["甲"]) <= allowed


def test_export_empty_db(client):
    response = client.get("/api/projects/export")
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["count"] == 0
    assert payload["projects"] == []


# ------------------------------------------------------------------ 导入

def test_import_restores_after_data_loss(client, scripts):
    """导出 → 数据全丢 → 导入 → 配置回来。这正是做备份功能的原因。"""
    _create(client, scripts, "甲", port=8801, environment={"KEY": "值"})
    backup = json.loads(client.get("/api/projects/export").data)

    # 模拟数据丢失
    with connect() as conn:
        conn.execute("DELETE FROM projects")
    assert client.get("/api/projects").get_json()["data"]["total"] == 0

    response = client.post("/api/projects/import", json=backup)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["created"] == 1
    assert data["updated"] == 0
    assert data["skipped"] == 0

    listed = client.get("/api/projects").get_json()["data"]["projects"]
    assert len(listed) == 1
    assert listed[0]["name"] == "甲"
    assert listed[0]["port"] == 8801
    assert listed[0]["environment"] == {"KEY": "值"}


def test_import_merge_updates_existing(client, scripts):
    _create(client, scripts, "甲", port=8801)
    backup = {
        "format": 1,
        "projects": [
            make_payload(scripts, "sleeper.py", name="甲", port=9999),  # 同名 → 更新
            make_payload(scripts, "sleeper.py", name="乙"),              # 新名 → 新增
        ],
    }

    response = client.post("/api/projects/import", json=backup)
    data = response.get_json()["data"]
    assert data["created"] == 1
    assert data["updated"] == 1
    assert data["skipped"] == 0

    projects = {p["name"]: p for p in client.get("/api/projects").get_json()["data"]["projects"]}
    assert projects["甲"]["port"] == 9999
    assert "乙" in projects


def test_import_never_deletes_existing(client, scripts):
    """合并语义的核心保证：库里多出来的项目绝不能被导入删掉。"""
    _create(client, scripts, "库里的独有项目")
    backup = {"projects": [make_payload(scripts, "sleeper.py", name="乙")]}

    client.post("/api/projects/import", json=backup)

    names = {p["name"] for p in client.get("/api/projects").get_json()["data"]["projects"]}
    assert "库里的独有项目" in names
    assert "乙" in names


def test_import_skips_invalid_entries(client, scripts):
    backup = {
        "projects": [
            make_payload(scripts, "sleeper.py", name="好的"),
            {"name": "缺目录"},                                   # 缺 working_dir / command
            make_payload(scripts, "sleeper.py", name="端口错", port=99999),
            "不是对象",
        ]
    }

    response = client.post("/api/projects/import", json=backup)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["created"] == 1
    assert data["skipped"] == 3
    assert len(data["errors"]) == 3
    # 好的那条确实进去了
    assert any(p["name"] == "好的" for p in client.get("/api/projects").get_json()["data"]["projects"])


def test_import_accepts_bare_array(client, scripts):
    """手工编辑过的裸数组也能导（宽容输入，严格校验）。"""
    response = client.post(
        "/api/projects/import",
        json=[make_payload(scripts, "sleeper.py", name="甲")],
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["created"] == 1


def test_import_rejects_bad_payloads(client):
    cases = [
        {"projects": "不是列表"},
        {"projects": []},
        {"没有projects键": 1},
        "一个字符串",
    ]
    for payload in cases:
        response = client.post("/api/projects/import", json=payload)
        assert response.status_code == 400, payload

    # 完全不是 JSON
    response = client.post("/api/projects/import", data="not json", content_type="text/plain")
    assert response.status_code == 400


def test_import_rejects_oversized_batch(client, scripts):
    items = [make_payload(scripts, "sleeper.py", name=f"项目{i}") for i in range(1001)]
    response = client.post("/api/projects/import", json=items)
    assert response.status_code == 400


def test_import_into_form_modal_flow_is_idempotent(client, scripts):
    """同一份备份导入两次：第一次新增，第二次全部更新，数量不翻倍。"""
    backup = {"projects": [make_payload(scripts, "sleeper.py", name="甲", port=1)]}
    client.post("/api/projects/import", json=backup)
    response = client.post("/api/projects/import", json=backup)

    data = response.get_json()["data"]
    assert data["created"] == 0
    assert data["updated"] == 1
    assert client.get("/api/projects").get_json()["data"]["total"] == 1
