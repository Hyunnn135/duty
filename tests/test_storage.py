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
    r = client.post("/api/auth/register",
                    json={"email": email, "password": pw, "name": name, "ward": ward})
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
    nurses = [{"name": "오수아", "team": 1, "seniority_rank": 1, "night_eligible": True,
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
    assert pub["nurses"][0]["name"] == "오수아"
    assert "seniority_rank" not in pub["nurses"][0]
    assert "night_eligible" not in pub["nurses"][0]
    assert not pub["editable"]


# ---- 근무표 발행/조회 ----

def test_schedule_publish_and_view(client):
    master = _reg(client, "m@duty.kr")
    staff = _reg(client, "s@duty.kr", ward="61")
    other = _reg(client, "o@duty.kr", ward="99")  # 다른 병동
    data = {"num_days": 2, "year": 2026, "month": 8,
            "schedules": [{"name": "오수아", "shifts": ["D", "O"], "labels": ["D", "O"],
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
    assert got.json()["data"]["schedules"][0]["name"] == "오수아"
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
