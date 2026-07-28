"""로그인/권한(RBAC) + 대원칙 검사 API 테스트."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DUTY_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("DUTY_SECRET", "test-secret")
    from app.main import app

    return TestClient(app)


def _register(client, email, name="사용자", pw="password123", ward="61"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": pw, "name": name, "ward": ward})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


SCHED_PAYLOAD = {
    "num_days": 7,
    "nurses": [{"id": f"n{i}", "name": f"간호사{i}"} for i in range(7)],
}


def test_first_user_is_master_then_staff(client):
    first = _register(client, "master@duty.kr", name="마스터")
    assert first["role"] == "master"
    second = _register(client, "staff@duty.kr", name="부서원")
    assert second["role"] == "staff"


def test_duplicate_email_rejected(client):
    _register(client, "a@duty.kr")
    r = client.post("/api/auth/register",
                    json={"email": "a@duty.kr", "password": "password123", "name": "b"})
    assert r.status_code == 409


def test_login_and_me(client):
    _register(client, "m@duty.kr", name="마스터")
    r = client.post("/api/auth/login",
                    json={"email": "m@duty.kr", "password": "password123"})
    assert r.status_code == 200
    token = r.json()["token"]
    me = client.get("/api/auth/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["email"] == "m@duty.kr"
    bad = client.post("/api/auth/login",
                      json={"email": "m@duty.kr", "password": "wrongpass1"})
    assert bad.status_code == 401


def test_schedule_requires_admin_or_master(client):
    master = _register(client, "m@duty.kr")
    staff = _register(client, "s@duty.kr")
    # 미로그인 → 401
    assert client.post("/api/schedule", json=SCHED_PAYLOAD).status_code == 401
    # 일반 부서원 → 403
    assert client.post("/api/schedule", json=SCHED_PAYLOAD,
                       headers=_auth(staff["token"])).status_code == 403
    # 마스터 → 200
    r = client.post("/api/schedule", json=SCHED_PAYLOAD, headers=_auth(master["token"]))
    assert r.status_code == 200 and r.json()["feasible"]


def test_set_role_promotes_to_admin(client):
    master = _register(client, "m@duty.kr")
    staff = _register(client, "partjang@duty.kr", name="파트장")
    # 부서원은 승격 불가
    assert client.post("/api/auth/set-role",
                       json={"email": "m@duty.kr", "role": "staff"},
                       headers=_auth(staff["token"])).status_code == 403
    # 마스터가 파트장을 admin으로 승격
    r = client.post("/api/auth/set-role",
                    json={"email": "partjang@duty.kr", "role": "admin"},
                    headers=_auth(master["token"]))
    assert r.status_code == 200 and r.json()["role"] == "admin"
    # 승격 후 재로그인하면 근무표 생성 가능
    login = client.post("/api/auth/login",
                        json={"email": "partjang@duty.kr", "password": "password123"})
    r = client.post("/api/schedule", json=SCHED_PAYLOAD,
                    headers=_auth(login.json()["token"]))
    assert r.status_code == 200


def test_validate_endpoint_reports_violations(client):
    user = _register(client, "u@duty.kr")
    body = {
        "schedules": {
            "간호사A": ["N", "D", "O", "O", "O", "O", "O"],      # P1: N→D
            "간호사B": ["E", "D", "O", "O", "O", "O", "O"],      # P3: E→D
            "간호사C": ["N", "N", "O", "O", "D", "O", "O"],      # 정상 (N블록2+오프2)
            "간호사D": ["N", "O", "D", "O", "O", "O", "O"],      # P1: N-OFF-D
            "간호사E": ["D", "D", "D", "D", "D", "D", "O"],      # F: 연속 6일
        }
    }
    r = client.post("/api/validate", json=body, headers=_auth(user["token"]))
    assert r.status_code == 200
    data = r.json()
    assert not data["ok"]
    principles = {v["principle"] for v in data["violations"]}
    assert {"P1", "P3", "F"} <= principles
    # 간호사C(N-N-O-O-D)는 위반 없음
    assert all(v["nurse"] != "간호사C" for v in data["violations"])


def test_validate_carry_over_boundary(client):
    user = _register(client, "u@duty.kr")
    body = {
        "schedules": {"간호사A": ["D", "O", "O", "O", "O", "O", "O"]},
        "carry_over": {"간호사A": ["N", "N"]},  # 전월 말 나이트 → 1일차 D는 P1 위반
    }
    r = client.post("/api/validate", json=body, headers=_auth(user["token"]))
    data = r.json()
    assert not data["ok"]
    assert any(v["principle"] == "P1" and v["day"] == 1 for v in data["violations"])


def test_candidates_endpoint(client):
    master = _register(client, "m@duty.kr")
    staff = _register(client, "s@duty.kr")
    # 일반 부서원 → 403
    assert client.post("/api/schedule/candidates", json=SCHED_PAYLOAD,
                       headers=_auth(staff["token"])).status_code == 403
    # 마스터 → 동일 품질 후보 여러 개
    r = client.post("/api/schedule/candidates?count=3", json=SCHED_PAYLOAD,
                    headers=_auth(master["token"]))
    assert r.status_code == 200
    data = r.json()
    assert data["feasible"] and data["count"] >= 1
    assert all(c["feasible"] for c in data["candidates"])
