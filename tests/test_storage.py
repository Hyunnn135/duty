"""서버 영속화(Phase 5) API 테스트: 명단·근무표 발행·원티드 승인·신청기간·피드백."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DUTY_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("DUTY_SECRET", "test-secret")
    from app.main import app

    return TestClient(app)


def _reg(client, email, name="사용자", pw="password123", ward="61"):
    """가입 헬퍼: 병동 미개설이면 개설(master), 이미 있으면 초대 코드로 가입(staff)."""
    r = client.post("/api/auth/register",
                    json={"email": email, "password": pw, "name": name, "ward": ward})
    if r.status_code == 403:  # 병동 기개설 → 초대 코드로 가입
        import os
        import sqlite3
        conn = sqlite3.connect(os.environ["DUTY_DB"])
        try:
            code = conn.execute(
                "SELECT code FROM ward_invites WHERE ward=?", (ward,)).fetchone()[0]
        finally:
            conn.close()
        r = client.post("/api/auth/register",
                        json={"email": email, "password": pw, "name": name,
                              "invite_code": code})
    assert r.status_code == 200, r.text
    return r.json()


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _iso(delta_hours):
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).isoformat()


# ---- 명단 ----

def test_roster_save_and_sensitive_hidden(client):
    master = _reg(client, "m@duty.kr")            # master
    staff = _reg(client, "s@duty.kr", ward="61")  # staff, 같은 병동
    nurses = [{"name": "장현진", "team": 1, "seniority_rank": 1, "night_eligible": True,
               "is_trainee": False, "trainer": ""}]
    # staff는 저장 불가
    assert client.put("/api/roster", json={"nurses": nurses}, headers=_h(staff["token"])).status_code == 403
    # master 저장
    r = client.put("/api/roster", json={"nurses": nurses}, headers=_h(master["token"]))
    assert r.status_code == 200 and r.json()["editable"]
    # master 조회 → 민감 속성 포함
    full = client.get("/api/roster", headers=_h(master["token"])).json()
    assert "seniority_rank" in full["nurses"][0] and full["editable"]
    # staff 조회 → 민감 속성 제거
    pub = client.get("/api/roster", headers=_h(staff["token"])).json()
    assert pub["nurses"][0]["name"] == "장현진"
    assert "seniority_rank" not in pub["nurses"][0]
    assert "night_eligible" not in pub["nurses"][0]
    assert not pub["editable"]


def test_ward_users_and_account_link(client):
    master = _reg(client, "m@duty.kr", name="파트장", ward="61")
    staff = _reg(client, "kim@duty.kr", name="김간호", ward="61")
    _reg(client, "other@duty.kr", name="타병동", ward="99")
    # 병동 사용자 목록: 61병동 2명만, staff는 접근 불가
    assert client.get("/api/auth/ward-users", headers=_h(staff["token"])).status_code == 403
    users = client.get("/api/auth/ward-users", headers=_h(master["token"])).json()
    emails = {u["email"] for u in users}
    assert emails == {"m@duty.kr", "kim@duty.kr"}  # 타병동 제외
    # 명단에 계정 연결
    nurses = [
        {"id": "nur1", "name": "장현진", "team": 1, "seniority_rank": 1, "account_email": "kim@duty.kr"},
        {"id": "nur2", "name": "안현영", "team": 2, "seniority_rank": 1, "account_email": ""},
    ]
    client.put("/api/roster", json={"nurses": nurses}, headers=_h(master["token"]))
    # 연결된 staff는 자기 간호사(장현진)를 확인
    mine = client.get("/api/me/nurse", headers=_h(staff["token"])).json()
    assert mine["linked"] and mine["nurse"]["name"] == "장현진"
    # staff의 명단 조회에는 타인의 account_email이 노출되지 않음
    pub = client.get("/api/roster", headers=_h(staff["token"])).json()
    assert all("account_email" not in n for n in pub["nurses"])
    # 미연결 사용자(master 본인은 명단에 계정연결 안됨)
    unlinked = client.get("/api/me/nurse", headers=_h(master["token"])).json()
    assert unlinked["linked"] is False


# ---- 근무표 발행/조회 ----

def test_schedule_publish_and_view(client):
    master = _reg(client, "m@duty.kr")
    staff = _reg(client, "s@duty.kr", ward="61")
    other = _reg(client, "o@duty.kr", ward="99")  # 다른 병동
    data = {"num_days": 2, "year": 2026, "month": 8,
            "schedules": [{"name": "장현진", "shifts": ["D", "O"], "labels": ["D", "O"],
                           "counts": {"D": 1, "O": 1}}]}
    # staff 발행 불가
    assert client.post("/api/schedule/publish",
                       json={"year": 2026, "month": 8, "data": data},
                       headers=_h(staff["token"])).status_code == 403
    # master 발행
    assert client.post("/api/schedule/publish",
                       json={"year": 2026, "month": 8, "data": data},
                       headers=_h(master["token"])).status_code == 200
    # 같은 병동 staff 조회 가능
    got = client.get("/api/schedule/2026/8", headers=_h(staff["token"]))
    assert got.status_code == 200
    assert got.json()["data"]["schedules"][0]["name"] == "장현진"
    # 다른 병동은 볼 수 없음(404)
    assert client.get("/api/schedule/2026/8", headers=_h(other["token"])).status_code == 404
    # 목록
    lst = client.get("/api/schedules", headers=_h(staff["token"])).json()
    assert len(lst) == 1 and lst[0]["month"] == 8


def test_schedule_publish_upsert(client):
    master = _reg(client, "m@duty.kr")
    d1 = {"schedules": [{"name": "A", "shifts": ["D"], "labels": ["D"], "counts": {"D": 1}}]}
    d2 = {"schedules": [{"name": "A", "shifts": ["N"], "labels": ["N"], "counts": {"N": 1}}]}
    client.post("/api/schedule/publish", json={"year": 2026, "month": 8, "data": d1}, headers=_h(master["token"]))
    client.post("/api/schedule/publish", json={"year": 2026, "month": 8, "data": d2}, headers=_h(master["token"]))
    got = client.get("/api/schedule/2026/8", headers=_h(master["token"])).json()
    assert got["data"]["schedules"][0]["shifts"] == ["N"]  # 덮어쓰기
    assert len(client.get("/api/schedules", headers=_h(master["token"])).json()) == 1


# ---- 원티드 신청 + 승인 ----

def test_wanted_submit_and_approve(client):
    master = _reg(client, "m@duty.kr")
    staff = _reg(client, "s@duty.kr", name="부서원", ward="61")
    body = {"year": 2026, "month": 8, "start_day": 3, "end_day": 5, "shift": "O"}
    r = client.post("/api/wanted", json=body, headers=_h(staff["token"]))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    rid = r.json()["id"]
    # 본인 조회
    mine = client.get("/api/wanted/mine?year=2026&month=8", headers=_h(staff["token"])).json()
    assert len(mine) == 1
    # 관리자 목록 조회
    allw = client.get("/api/wanted?year=2026&month=8", headers=_h(master["token"])).json()
    assert len(allw) == 1 and allw[0]["nurse_name"] == "부서원"
    # staff는 목록 조회 불가
    assert client.get("/api/wanted?year=2026&month=8", headers=_h(staff["token"])).status_code == 403
    # 승인
    dec = client.post(f"/api/wanted/{rid}/decision", json={"status": "approved"}, headers=_h(master["token"]))
    assert dec.status_code == 200 and dec.json()["status"] == "approved"


def test_wanted_max_3_days(client):
    _reg(client, "m@duty.kr")
    staff = _reg(client, "s@duty.kr")
    r = client.post("/api/wanted", json={"year": 2026, "month": 8, "start_day": 1, "end_day": 5, "shift": "O"},
                    headers=_h(staff["token"]))
    assert r.status_code == 422  # 4일 초과


# ---- 신청 기간(윈도우) ----

def test_request_window_blocks_after_close(client):
    master = _reg(client, "m@duty.kr")
    staff = _reg(client, "s@duty.kr", ward="61")
    # 이미 마감된 윈도우 설정 (closes_at = 1시간 전)
    win = {"year": 2026, "month": 9, "opens_at": _iso(-48), "closes_at": _iso(-1), "note": "마감"}
    assert client.put("/api/request-window", json=win, headers=_h(master["token"])).status_code == 200
    # staff 조회 → 닫힘 + 만료 문구
    st = client.get("/api/request-window/2026/9", headers=_h(staff["token"])).json()
    assert st["is_open"] is False
    assert "만료" in st["message"]
    # 신청 시도 → 403
    r = client.post("/api/wanted", json={"year": 2026, "month": 9, "start_day": 1, "end_day": 1, "shift": "O"},
                    headers=_h(staff["token"]))
    assert r.status_code == 403 and "만료" in r.json()["detail"]


def test_request_window_open_allows(client):
    master = _reg(client, "m@duty.kr")
    staff = _reg(client, "s@duty.kr", ward="61")
    win = {"year": 2026, "month": 10, "opens_at": _iso(-1), "closes_at": _iso(48)}
    client.put("/api/request-window", json=win, headers=_h(master["token"]))
    st = client.get("/api/request-window/2026/10", headers=_h(staff["token"])).json()
    assert st["is_open"] is True
    r = client.post("/api/wanted", json={"year": 2026, "month": 10, "start_day": 1, "end_day": 1, "shift": "O"},
                    headers=_h(staff["token"]))
    assert r.status_code == 200


# ---- 피드백 ----

def test_send_email_noop_when_unconfigured(monkeypatch):
    import app.email_notify as en
    for k in ("SMTP_HOST", "SMTP_FROM", "SMTP_USER"):
        monkeypatch.delenv(k, raising=False)
    assert en.is_configured() is False
    assert en.send_email("a@b.c", "제목", "본문") is False  # 네트워크 접근 없이 즉시 False


def test_feedback_emails_master(client, monkeypatch):
    import app.email_notify as en
    sent: list = []
    monkeypatch.setattr(en, "send_email",
                        lambda to, subject, body: sent.append((to, subject, body)) or True)
    master = _reg(client, "boss@duty.kr", name="파트장")
    staff = _reg(client, "kim@duty.kr", name="김간호", ward="61")
    r = client.post("/api/feedback", json={"message": "야간 배분 개선 요청"}, headers=_h(staff["token"]))
    assert r.status_code == 200
    # 백그라운드 태스크가 마스터에게 메일 시도
    assert len(sent) == 1
    recipients, subject, body = sent[0]
    assert "boss@duty.kr" in recipients
    assert "김간호" in subject and "야간 배분" in body


def test_wanted_decision_emails_requester(client, monkeypatch):
    import app.email_notify as en
    sent: list = []
    monkeypatch.setattr(en, "send_email",
                        lambda to, subject, body: sent.append((to, subject, body)) or True)
    master = _reg(client, "boss@duty.kr", name="파트장")
    staff = _reg(client, "kim@duty.kr", name="김간호", ward="61")
    rid = client.post("/api/wanted",
                      json={"year": 2026, "month": 8, "start_day": 3, "end_day": 4, "shift": "O"},
                      headers=_h(staff["token"])).json()["id"]
    sent.clear()
    client.post(f"/api/wanted/{rid}/decision", json={"status": "approved"}, headers=_h(master["token"]))
    assert len(sent) == 1 and "kim@duty.kr" in sent[0][0]
    assert "승인" in sent[0][1]


def test_feedback_to_master(client):
    master = _reg(client, "m@duty.kr")
    staff = _reg(client, "s@duty.kr", name="부서원")
    r = client.post("/api/feedback", json={"message": "야간 배분 개선 부탁드려요"}, headers=_h(staff["token"]))
    assert r.status_code == 200
    # master 수신함
    inbox = client.get("/api/feedback", headers=_h(master["token"]))
    assert inbox.status_code == 200 and len(inbox.json()) == 1
    fb = inbox.json()[0]
    assert fb["from_name"] == "부서원" and fb["read_at"] is None
    # staff는 수신함 접근 불가
    assert client.get("/api/feedback", headers=_h(staff["token"])).status_code == 403
    # 읽음 처리
    rd = client.post(f"/api/feedback/{fb['id']}/read", headers=_h(master["token"]))
    assert rd.status_code == 200 and rd.json()["read_at"] is not None


# ---- 신청 기간 KST 해석 ----

def test_parse_bound_naive_is_kst():
    """시간대 없는 경계값은 한국 시간(KST)으로 해석 — UTC 해석 시 9시간 어긋난다."""
    from app.storage import KST, _parse_bound
    b = _parse_bound("2026-08-10", end_of_day=True)
    assert b == datetime(2026, 8, 10, 23, 59, 59, tzinfo=KST)
    assert b.astimezone(timezone.utc) == datetime(
        2026, 8, 10, 14, 59, 59, tzinfo=timezone.utc)
    o = _parse_bound("2026-08-10T18:00", end_of_day=False)
    assert o == datetime(2026, 8, 10, 18, 0, tzinfo=KST)
    # 시간대가 명시된 값은 그대로 존중한다
    z = _parse_bound("2026-08-10T09:00:00+00:00", end_of_day=False)
    assert z == datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)


def test_set_window_rejects_unparseable_bounds(client):
    """파싱 불가한 경계값은 저장 전 422 — 조용히 '기간 없음'이 되면 마감이 안 닫힌다."""
    admin = _reg(client, "m@duty.kr")
    bad = {"year": 2026, "month": 9, "closes_at": "10/08/2026"}
    assert client.put("/api/request-window", json=bad,
                      headers=_h(admin["token"])).status_code == 422
    ok = {"year": 2026, "month": 9, "closes_at": "2026-09-10"}
    assert client.put("/api/request-window", json=ok,
                      headers=_h(admin["token"])).status_code == 200
