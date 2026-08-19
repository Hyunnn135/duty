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


def _invite_code(ward):
    """테스트 DB에서 병동 초대 코드를 직접 조회 (가입 헬퍼용)."""
    import os
    import sqlite3
    conn = sqlite3.connect(os.environ["DUTY_DB"])
    try:
        row = conn.execute(
            "SELECT code FROM ward_invites WHERE ward=?", (ward,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _register(client, email, name="사용자", pw="password123", ward="61"):
    """가입 헬퍼: 병동 미개설이면 개설(master), 이미 있으면 초대 코드로 가입(staff)."""
    r = client.post("/api/auth/register",
                    json={"email": email, "password": pw, "name": name, "ward": ward})
    if r.status_code == 403:  # 병동 기개설 → 초대 코드로 가입
        r = client.post("/api/auth/register",
                        json={"email": email, "password": pw, "name": name,
                              "invite_code": _invite_code(ward)})
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
                    json={"email": "a@duty.kr", "password": "password123", "name": "b",
                          "invite_code": _invite_code("61")})
    assert r.status_code == 409


def test_register_requires_invite_for_existing_ward(client):
    """개설된 병동은 초대 코드 없이(또는 틀린 코드로) 가입 불가."""
    _register(client, "boss@duty.kr")  # 61 개설(master)
    no_code = client.post("/api/auth/register", json={
        "email": "x@duty.kr", "password": "password123", "name": "x", "ward": "61"})
    assert no_code.status_code == 403
    bad_code = client.post("/api/auth/register", json={
        "email": "x@duty.kr", "password": "password123", "name": "x",
        "invite_code": "WRONGCOD"})
    assert bad_code.status_code == 403
    ok = client.post("/api/auth/register", json={
        "email": "x@duty.kr", "password": "password123", "name": "x",
        "invite_code": _invite_code("61")})
    assert ok.status_code == 200 and ok.json()["ward"] == "61"


def test_invite_endpoints_and_rotate(client):
    master = _register(client, "boss@duty.kr")
    staff = _register(client, "s@duty.kr")
    # staff는 초대 코드 조회 불가
    assert client.get("/api/auth/invite", headers=_auth(staff["token"])).status_code == 403
    inv = client.get("/api/auth/invite", headers=_auth(master["token"]))
    assert inv.status_code == 200 and inv.json()["ward"] == "61"
    old = inv.json()["code"]
    # 재발급하면 이전 코드는 무효
    new = client.post("/api/auth/invite/rotate", headers=_auth(master["token"])).json()["code"]
    assert new != old
    rejected = client.post("/api/auth/register", json={
        "email": "y@duty.kr", "password": "password123", "name": "y", "invite_code": old})
    assert rejected.status_code == 403
    accepted = client.post("/api/auth/register", json={
        "email": "y@duty.kr", "password": "password123", "name": "y", "invite_code": new})
    assert accepted.status_code == 200


def test_role_change_effective_immediately(client):
    """강등/승격이 토큰 만료를 기다리지 않고 즉시 반영된다(DB 기준 권한)."""
    master = _register(client, "boss@duty.kr")
    staff = _register(client, "s@duty.kr")
    # 승격 후, '기존' staff 토큰으로도 즉시 admin 권한 동작
    client.post("/api/auth/set-role", json={"email": "s@duty.kr", "role": "admin"},
                headers=_auth(master["token"]))
    assert client.get("/api/auth/me", headers=_auth(staff["token"])).json()["role"] == "admin"
    r = client.post("/api/schedule", json=SCHED_PAYLOAD, headers=_auth(staff["token"]))
    assert r.status_code == 200
    # 강등 후, 같은 토큰으로 즉시 403
    client.post("/api/auth/set-role", json={"email": "s@duty.kr", "role": "staff"},
                headers=_auth(master["token"]))
    assert client.post("/api/schedule", json=SCHED_PAYLOAD,
                       headers=_auth(staff["token"])).status_code == 403


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


def test_set_role_scoped_to_own_ward(client):
    """타 병동 마스터는 다른 병동 사용자의 역할을 바꿀 수 없다(테넌트 경계).

    회귀 가드: set-role의 UPDATE가 ward로 스코프되지 않으면, 아무나 새 병동을
    개설(master)한 뒤 타 병동 계정을 승격/강등할 수 있었다.
    """
    _register(client, "boss61@duty.kr", ward="61")           # 61 개설(master)
    victim = _register(client, "v@duty.kr", ward="61")       # 61 staff
    attacker = _register(client, "boss99@duty.kr", ward="99")  # 99 개설(master)
    r = client.post("/api/auth/set-role",
                    json={"email": "v@duty.kr", "role": "master"},
                    headers=_auth(attacker["token"]))
    assert r.status_code == 404  # 타 병동 계정은 존재 여부조차 구분하지 않는다
    assert client.get("/api/auth/me",
                      headers=_auth(victim["token"])).json()["role"] == "staff"


def test_register_empty_ward_rejected(client):
    """초대 코드도 병동명도 없는 가입은 422 — 빈 병동('') 테넌트 생성 차단."""
    r = client.post("/api/auth/register", json={
        "email": "e@duty.kr", "password": "password123", "name": "e", "ward": "  "})
    assert r.status_code == 422
