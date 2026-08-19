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


# ---- 사번 로그인 전환 ----

def test_empno_register_login_duplicate(client):
    """사번으로 가입·로그인, 중복 사번은 409로 명확히 안내."""
    r = client.post("/api/auth/register", json={
        "empno": "100275", "password": "password123", "name": "김간호", "ward": "71"})
    assert r.status_code == 200 and r.json()["empno"] == "100275"
    # 사번 로그인
    ok = client.post("/api/auth/login", json={"login": "100275", "password": "password123"})
    assert ok.status_code == 200 and ok.json()["role"] == "master"
    # /me 에 사번 포함
    me = client.get("/api/auth/me", headers=_auth(ok.json()["token"])).json()
    assert me["empno"] == "100275"
    # 같은 사번으로 재가입 → 409 (사번 안내)
    code = _invite_code("71")
    dup = client.post("/api/auth/register", json={
        "empno": "100275", "password": "password123", "name": "다른사람",
        "invite_code": code})
    assert dup.status_code == 409 and "사번" in dup.json()["detail"]
    # 형식 위반 사번 → 422
    bad = client.post("/api/auth/register", json={
        "empno": "사번한글", "password": "password123", "name": "x", "invite_code": code})
    assert bad.status_code == 422


def test_legacy_email_login_and_set_empno_migration(client):
    """이메일 구계정은 계속 로그인 가능하고, 사번 등록 시 원티드 신청도 이관된다."""
    legacy = _register(client, "boss@duty.kr")  # 이메일 기반 가입(구버전 호환)
    h = _auth(legacy["token"])
    # 이메일 키로 원티드 신청 + 명단 계정 연결 저장
    r = client.post("/api/wanted", json={
        "year": 2026, "month": 9, "start_day": 5, "end_day": 5, "shift": "O"}, headers=h)
    assert r.status_code == 200
    nurses = [{"id": "n1", "name": "김서연", "team": 1, "account_email": "boss@duty.kr"}]
    assert client.put("/api/roster", json={"nurses": nurses}, headers=h).status_code == 200
    # 사번 등록 → 신청·명단 연결이 사번 키로 이관되어 계속 동작한다
    s = client.post("/api/auth/set-empno", json={"empno": "900360"}, headers=h)
    assert s.status_code == 200 and s.json()["empno"] == "900360"
    mine = client.get("/api/wanted/mine?year=2026&month=9", headers=h).json()
    assert len(mine) == 1 and mine[0]["nurse_email"] == "900360"
    roster = client.get("/api/roster", headers=h).json()["nurses"]
    assert roster[0]["account_email"] == "900360"
    assert client.get("/api/me/nurse", headers=h).json()["linked"]
    # 사번 재등록은 409, 타인이 같은 사번 등록도 409
    assert client.post("/api/auth/set-empno", json={"empno": "900361"},
                       headers=h).status_code == 409
    other = _register(client, "s2@duty.kr")
    assert client.post("/api/auth/set-empno", json={"empno": "900360"},
                       headers=_auth(other["token"])).status_code == 409
    # 사번 로그인도 동작
    assert client.post("/api/auth/login", json={
        "login": "900360", "password": "password123"}).status_code == 200


def test_roster_link_by_empno(client):
    """명단 계정 연결(account_email 필드)에 사번을 저장해도 /api/me/nurse 가 연결한다."""
    master = _register(client, "m@duty.kr")
    r = client.post("/api/auth/register", json={
        "empno": "100265", "password": "password123", "name": "박하윤",
        "invite_code": _invite_code("61")})
    assert r.status_code == 200
    staff_tok = r.json()["token"]
    nurses = [{"id": "n1", "name": "박하윤", "team": 1, "account_email": "100265"}]
    assert client.put("/api/roster", json={"nurses": nurses},
                      headers=_auth(master["token"])).status_code == 200
    mn = client.get("/api/me/nurse", headers=_auth(staff_tok)).json()
    assert mn["linked"] and mn["nurse"]["name"] == "박하윤"


def test_set_role_by_empno(client):
    """역할 변경 대상도 사번으로 지정 가능."""
    master = _register(client, "m@duty.kr")
    client.post("/api/auth/register", json={
        "empno": "100266", "password": "password123", "name": "파트장",
        "invite_code": _invite_code("61")})
    r = client.post("/api/auth/set-role", json={"login": "100266", "role": "admin"},
                    headers=_auth(master["token"]))
    assert r.status_code == 200 and r.json()["role"] == "admin"


def test_legacy_db_schema_migrates(tmp_path, monkeypatch):
    """구 스키마(email NOT NULL, empno 없음) DB가 자동 재구축되고 기존 계정이 유지된다."""
    import secrets as sec
    import sqlite3
    from app.auth import _hash_pw
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
        ward TEXT DEFAULT '', pw_hash TEXT NOT NULL, salt TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    salt = sec.token_bytes(16)
    conn.execute(
        "INSERT INTO users (email,name,role,ward,pw_hash,salt) VALUES (?,?,?,?,?,?)",
        ("old@duty.kr", "구계정", "master", "61", _hash_pw("password123", salt), salt.hex()))
    conn.commit(); conn.close()
    monkeypatch.setenv("DUTY_DB", str(db))
    monkeypatch.setenv("DUTY_SECRET", "test-secret")
    from app.main import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"login": "old@duty.kr", "password": "password123"})
    assert r.status_code == 200, r.text
    # 재구축 후 신규 사번 가입도 계속 동작 (초대 코드는 레거시 DB라 API로 첫 발급)
    inv = c.get("/api/auth/invite", headers=_auth(r.json()["token"]))
    assert inv.status_code == 200
    r2 = c.post("/api/auth/register", json={
        "empno": "770001", "password": "password123", "name": "신규",
        "invite_code": inv.json()["code"]})
    assert r2.status_code == 200


def test_empno_case_normalized_no_leak(client):
    """대소문자 변형 사번은 같은 계정으로 취급(409) — 타인 명단 열람 결함 재발 방지."""
    r = client.post("/api/auth/register", json={
        "empno": "abc1", "password": "password123", "name": "가", "ward": "72"})
    assert r.status_code == 200 and r.json()["empno"] == "ABC1"
    dup = client.post("/api/auth/register", json={
        "empno": "ABC1", "password": "password123", "name": "나",
        "invite_code": _invite_code("72")})
    assert dup.status_code == 409
    # 소문자로도 로그인된다(정규화 조회)
    assert client.post("/api/auth/login", json={
        "login": "abc1", "password": "password123"}).status_code == 200


def test_email_login_survives_set_empno_and_cancel_works(client):
    """사번 등록 후에도 이메일 로그인이 유지되고, 이관된 신청을 본인이 취소할 수 있다."""
    legacy = _register(client, "old2@duty.kr")
    h = _auth(legacy["token"])
    w = client.post("/api/wanted", json={
        "year": 2026, "month": 10, "start_day": 3, "end_day": 3, "shift": "O"},
        headers=h).json()
    assert client.post("/api/auth/set-empno", json={"empno": "910001"},
                       headers=h).status_code == 200
    # 이메일 로그인 하위 호환 유지
    assert client.post("/api/auth/login", json={
        "login": "old2@duty.kr", "password": "password123"}).status_code == 200
    # 이관된 신청(키=사번)을 같은 사용자가 취소 가능
    assert client.delete(f"/api/wanted/{w['id']}", headers=h).status_code == 200


def test_ward_users_includes_empno(client):
    """계정 연결 드롭다운이 의존하는 ward-users의 empno 필드."""
    master = _register(client, "m@duty.kr")
    client.post("/api/auth/register", json={
        "empno": "100777", "password": "password123", "name": "부서원",
        "invite_code": _invite_code("61")})
    users = client.get("/api/auth/ward-users", headers=_auth(master["token"])).json()
    assert any(u.get("empno") == "100777" for u in users)


def test_empno_length_boundaries(client):
    """사번 길이 경계: 20자 허용, 21자·2자 거부."""
    ok = client.post("/api/auth/register", json={
        "empno": "A" * 20, "password": "password123", "name": "x", "ward": "73"})
    assert ok.status_code == 200
    code = _invite_code("73")
    too_long = client.post("/api/auth/register", json={
        "empno": "A" * 21, "password": "password123", "name": "y", "invite_code": code})
    assert too_long.status_code == 422
    too_short = client.post("/api/auth/register", json={
        "empno": "AB", "password": "password123", "name": "z", "invite_code": code})
    assert too_short.status_code == 422
