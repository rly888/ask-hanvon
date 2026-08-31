"""API 层测试：健康 / 图书 / 搜索 / 推荐 / 会话 / MCP / 鉴权 / 限流。

口令策略：测试口令运行期用 secrets 生成，不在源码中写入凭据字面量。
"""
import secrets

import pytest
from fastapi.testclient import TestClient

from askhanvon.server.app import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _test_password() -> str:
    return "tp-" + secrets.token_hex(8)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok"
    assert d["chunks"] > 0


def test_books_and_detail(client, sample_book):
    r = client.get("/api/books")
    assert r.status_code == 200
    assert r.json()["books"]
    r2 = client.get("/api/books/" + sample_book)
    assert r2.status_code == 200
    assert r2.json()["chapters"]


def test_book_content(client, sample_book):
    r = client.get("/api/books/" + sample_book + "/content?chapter_no=1")
    assert r.status_code == 200
    assert r.json()["paragraphs"]


def test_search_endpoint(client):
    r = client.get("/api/search?q=大闹天宫")
    assert r.status_code == 200
    assert r.json()["ok"]


def test_recommend_endpoint(client, sample_book):
    r = client.get("/api/recommend?top_k=4")
    assert r.status_code == 200
    assert r.json()["items"]


def test_chat_nonstream_qa(client, sample_book):
    r = client.post("/api/chat", json={"message": "《西游记》里孙悟空为什么被压五行山下？",
                                       "stream": False})
    assert r.status_code == 200
    d = r.json()
    assert d["intent"] == "qa"
    assert d["text"]


def test_chat_stream_sse(client):
    r = client.post("/api/chat", json={"message": "你好", "stream": True})
    assert r.status_code == 200
    body = r.text
    assert "data:" in body


def test_auth_flow_and_rbac(client, sample_book):
    username = "apitester_" + secrets.token_hex(3)
    password = _test_password()
    headers = {}
    # 注册（失败则登录）
    r1 = client.post("/api/auth/register", json={"username": username,
                                                 "password": password})
    token = r1.json().get("token")
    if not token:
        r1 = client.post("/api/auth/login", json={"username": username,
                                                  "password": password})
        token = r1.json()["token"]
    headers = {"Authorization": "Bearer " + token}

    # 匿名购买被拒
    r0 = client.post("/api/tools/purchase_init", json={"arguments": {"book_title": "x"}})
    assert r0.json()["ok"] is False

    books = client.get("/api/books").json()["books"]
    title = books[0]["title"]
    r2 = client.post("/api/tools/purchase_init", headers=headers,
                     json={"arguments": {"book_title": title}})
    assert r2.json()["ok"] is True
    order = r2.json()["data"]
    r3 = client.post("/api/tools/purchase_confirm", headers=headers,
                     json={"arguments": {"order_id": order["order_id"],
                                         "confirm_token": order["confirm_token"]}})
    assert r3.json()["ok"] is True
    assert r3.json()["data"]["status"] == "paid"
    # 幂等：重复确认返回已支付
    r4 = client.post("/api/tools/purchase_confirm", headers=headers,
                     json={"arguments": {"order_id": order["order_id"],
                                         "confirm_token": order["confirm_token"]}})
    assert r4.json()["data"].get("already_paid") is True


def test_admin_requires_admin(client):
    r = client.get("/api/admin/overview")
    assert r.status_code == 401
    r2 = client.post("/api/auth/login", json={"username": "admin",
                                              "password": _test_password()})
    assert r2.status_code == 401  # 随机口令必然登录失败，未泄露凭据


def test_mcp_endpoints(client):
    r = client.get("/api/mcp/tools/list")
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["tools"]]
    assert "book_qa" in names
    r2 = client.post("/api/mcp/tools/call", json={"name": "book_search",
                                                  "arguments": {"query": "推荐"}})
    assert r2.status_code == 200


def test_events_endpoint(client, sample_book):
    r = client.post("/api/events", json=[{"event_type": "click",
                                          "book_id": sample_book, "props": {}}])
    assert r.status_code == 200
    assert r.json()["accepted"] == 1
    r2 = client.post("/api/events", json=[{"event_type": "bad_type"}])
    assert r2.json()["accepted"] == 0


def test_metrics_prometheus(client):
    r = client.get("/api/metrics")
    assert r.status_code == 200
    assert "http_requests_total" in r.text
