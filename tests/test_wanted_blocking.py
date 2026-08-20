"""발행된 달·지난 달 원티드 신청 차단 (지시서 2026-08-20-원티드-발행달-차단).

지시서 수용 기준 1~5에 대응하는 API 테스트. 구현 코드가 아니라 수용 기준에서
설계했다. 날짜는 KST 기준 상대 계산(지난 달/이번 달/다음 달)으로 만들어 실행
시점에 따라 깨지지 않게 한다. 테스트 데이터는 전부 가명(교훈 L-1).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

KST = timezone(timedelta(hours=9))


# ---- 픽스처 / 헬퍼 (tests/test_storage.py 관례를 따름) ----

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


def _ward_pair(client):
    """같은 병동(61)의 부서장(master)·부서원(staff) 한 쌍. 이름은 가명."""
    master = _reg(client, "seoyeon@duty.kr", name="김서연", ward="61")
    staff = _reg(client, "jiwoo@duty.kr", name="이지우", ward="61")
    return master, staff


# ---- KST 기준 상대 연월 ----

def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _ym(delta_months: int) -> tuple[int, int]:
    """KST 현재 연월에서 delta개월 이동한 (연, 월). delta=-1이면 지난 달."""
    now = datetime.now(KST)
    return _shift_month(now.year, now.month, delta_months)


# ---- 상태 만들기 ----

def _publish(client, master, year, month):
    """해당 연월 근무표를 발행 상태로 만든다."""
    data = {"num_days": 2, "year": year, "month": month,
            "schedules": [{"name": "박하늘", "shifts": ["D", "O"],
                           "labels": ["D", "O"], "counts": {"D": 1, "O": 1}}]}
    r = client.post("/api/schedule/publish",
                    json={"year": year, "month": month, "data": data},
                    headers=_h(master["token"]))
    assert r.status_code == 200, f"발행 준비 실패: {r.status_code} {r.text}"


def _open_window(client, master, year, month):
    """해당 연월 신청 기간을 명시적으로 '열림'으로 설정한다."""
    win = {"year": year, "month": month, "opens_at": _iso(-48),
           "closes_at": _iso(48), "note": "접수 중"}
    r = client.put("/api/request-window", json=win, headers=_h(master["token"]))
    assert r.status_code == 200, f"기간 설정 실패: {r.status_code} {r.text}"


def _close_window(client, master, year, month):
    """해당 연월 신청 기간을 이미 마감된 상태로 설정한다."""
    win = {"year": year, "month": month, "opens_at": _iso(-48),
           "closes_at": _iso(-1), "note": "마감"}
    r = client.put("/api/request-window", json=win, headers=_h(master["token"]))
    assert r.status_code == 200, f"기간 설정 실패: {r.status_code} {r.text}"


def _apply(client, staff, year, month, start_day=1, end_day=1, shift="O"):
    return client.post("/api/wanted",
                       json={"year": year, "month": month, "start_day": start_day,
                             "end_day": end_day, "shift": shift},
                       headers=_h(staff["token"]))


# ---- 검증 헬퍼 ----

def _db_wanted_count() -> int:
    """wanted_requests 테이블 전체 행 수 (테스트마다 새 DB이므로 전체 카운트로 충분)."""
    conn = sqlite3.connect(os.environ["DUTY_DB"])
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "wanted_requests" in names, f"wanted_requests 테이블 없음: {sorted(names)}"
        return conn.execute("SELECT COUNT(*) FROM wanted_requests").fetchone()[0]
    finally:
        conn.close()


def _assert_not_stored(client, master, staff, year, month):
    """DB에도, 본인 조회에도, 부서장 목록에도 남지 않아야 한다."""
    assert _db_wanted_count() == 0, "차단된 신청이 wanted_requests에 저장됨"
    mine = client.get(f"/api/wanted/mine?year={year}&month={month}",
                      headers=_h(staff["token"]))
    assert mine.status_code == 200, mine.text
    assert mine.json() == [], f"차단된 신청이 본인 조회에 남음: {mine.json()}"
    allw = client.get(f"/api/wanted?year={year}&month={month}",
                      headers=_h(master["token"]))
    assert allw.status_code == 200, allw.text
    assert allw.json() == [], f"차단된 신청이 부서장 목록에 남음: {allw.json()}"


def _is_korean(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


def _assert_korean_guidance(detail: str):
    assert detail and _is_korean(detail), f"한국어 안내가 아님: {detail!r}"
    assert "부서장" in detail, f"'부서장 문의' 안내가 없음: {detail!r}"


PAST_REASON_WORDS = ("지난", "지나", "과거", "이전", "종료된")


# =====================================================================
# 수용 기준 1 — 발행된 달 신청 차단
# =====================================================================

def test_ac1_published_month_wanted_is_blocked(client):
    """AC1: 발행된 달 POST /api/wanted → 403, '발행'·'부서장' 포함 한국어 안내."""
    master, staff = _ward_pair(client)
    year, month = _ym(+1)          # 미래 달이라 '지난 달' 규칙과 무관 → 발행만이 사유
    _publish(client, master, year, month)

    r = _apply(client, staff, year, month)
    assert r.status_code == 403, f"발행된 달인데 차단되지 않음: {r.status_code} {r.text}"
    detail = r.json()["detail"]
    _assert_korean_guidance(detail)
    assert "발행" in detail, f"차단 사유에 '발행'이 없음: {detail!r}"


def test_ac1_published_month_request_is_not_persisted(client):
    """AC1: 차단된 신청은 DB·본인 조회·부서장 목록 어디에도 남지 않는다."""
    master, staff = _ward_pair(client)
    year, month = _ym(+1)
    _publish(client, master, year, month)

    assert _apply(client, staff, year, month).status_code == 403
    _assert_not_stored(client, master, staff, year, month)


# =====================================================================
# 수용 기준 2 — KST 기준 지난 달 신청 차단
# =====================================================================

def test_ac2_past_month_wanted_is_blocked(client):
    """AC2: 발행되지 않은 지난 달도 403 + 한국어 안내, 저장 안 됨."""
    master, staff = _ward_pair(client)
    year, month = _ym(-1)          # 발행하지 않음 → 사유는 '지난 달'뿐

    r = _apply(client, staff, year, month)
    assert r.status_code == 403, f"지난 달인데 차단되지 않음: {r.status_code} {r.text}"
    _assert_korean_guidance(r.json()["detail"])
    _assert_not_stored(client, master, staff, year, month)


def test_ac2_past_month_blocked_regardless_of_publication(client):
    """AC2: 지난 달이면서 발행까지 된 달도 동일하게 403 + 저장 안 됨."""
    master, staff = _ward_pair(client)
    year, month = _ym(-1)
    _publish(client, master, year, month)

    r = _apply(client, staff, year, month)
    assert r.status_code == 403, f"발행된 지난 달인데 차단되지 않음: {r.status_code} {r.text}"
    _assert_korean_guidance(r.json()["detail"])
    _assert_not_stored(client, master, staff, year, month)


def test_ac2_far_past_month_is_blocked(client):
    """AC2: 여러 달 전(예: 3개월 전)도 동일하게 차단된다."""
    master, staff = _ward_pair(client)
    year, month = _ym(-3)

    r = _apply(client, staff, year, month)
    assert r.status_code == 403, f"3개월 전인데 차단되지 않음: {r.status_code} {r.text}"
    _assert_korean_guidance(r.json()["detail"])
    _assert_not_stored(client, master, staff, year, month)


# =====================================================================
# 수용 기준 3 — 신청 기간이 열려 있어도 차단이 우선
# =====================================================================

def test_ac3_open_window_does_not_override_published_block(client):
    """AC3: 신청 기간을 명시적으로 열어도 발행된 달은 차단이 우선한다."""
    master, staff = _ward_pair(client)
    year, month = _ym(+1)
    _open_window(client, master, year, month)
    _publish(client, master, year, month)

    r = _apply(client, staff, year, month)
    assert r.status_code == 403, (
        f"기간이 열려 있다는 이유로 발행된 달 신청이 통과함: {r.status_code} {r.text}")
    detail = r.json()["detail"]
    _assert_korean_guidance(detail)
    assert "발행" in detail, f"차단 사유에 '발행'이 없음: {detail!r}"
    _assert_not_stored(client, master, staff, year, month)


def test_ac3_open_window_does_not_override_past_month_block(client):
    """AC3: 신청 기간을 명시적으로 열어도 지난 달은 차단이 우선한다."""
    master, staff = _ward_pair(client)
    year, month = _ym(-1)
    _open_window(client, master, year, month)

    r = _apply(client, staff, year, month)
    assert r.status_code == 403, (
        f"기간이 열려 있다는 이유로 지난 달 신청이 통과함: {r.status_code} {r.text}")
    _assert_korean_guidance(r.json()["detail"])
    _assert_not_stored(client, master, staff, year, month)


# =====================================================================
# 수용 기준 4 — GET /api/request-window 가 닫힘 + 사유를 알린다
# =====================================================================

def test_ac4_window_status_closed_for_published_month(client):
    """AC4: 발행된 달의 신청 기간 조회 → is_open=false + '발행' 사유 message."""
    master, staff = _ward_pair(client)
    year, month = _ym(+1)
    _open_window(client, master, year, month)   # 기간은 열려 있지만
    _publish(client, master, year, month)       # 발행되었으므로 닫힘이어야 한다

    st = client.get(f"/api/request-window/{year}/{month}", headers=_h(staff["token"]))
    assert st.status_code == 200, st.text
    body = st.json()
    assert body["is_open"] is False, f"발행된 달인데 is_open이 True: {body}"
    msg = body.get("message") or ""
    _assert_korean_guidance(msg)
    assert "발행" in msg, f"사유 message에 '발행'이 없음: {msg!r}"


def test_ac4_window_status_closed_for_past_month(client):
    """AC4: 지난 달의 신청 기간 조회 → is_open=false + 지난 달임을 알리는 message."""
    master, staff = _ward_pair(client)
    year, month = _ym(-1)
    _open_window(client, master, year, month)   # 기간을 열어도 닫힘이어야 한다

    st = client.get(f"/api/request-window/{year}/{month}", headers=_h(staff["token"]))
    assert st.status_code == 200, st.text
    body = st.json()
    assert body["is_open"] is False, f"지난 달인데 is_open이 True: {body}"
    msg = body.get("message") or ""
    _assert_korean_guidance(msg)
    assert any(w in msg for w in PAST_REASON_WORDS), (
        f"사유 message에 지난 달임을 알리는 표현이 없음{PAST_REASON_WORDS}: {msg!r}")


# =====================================================================
# 수용 기준 5 — 회귀: 발행되지 않은 현재·미래 달은 기존 동작 유지
# =====================================================================

def test_ac5_future_unpublished_month_still_accepts(client):
    """AC5(회귀): 발행되지 않은 미래 달 + 기간 열림 → 기존대로 200 접수."""
    master, staff = _ward_pair(client)
    year, month = _ym(+1)
    _open_window(client, master, year, month)

    st = client.get(f"/api/request-window/{year}/{month}", headers=_h(staff["token"])).json()
    assert st["is_open"] is True, f"발행 전 미래 달인데 닫힘: {st}"

    r = _apply(client, staff, year, month)
    assert r.status_code == 200, f"미래 달 신청이 거부됨: {r.status_code} {r.text}"
    assert r.json()["status"] == "pending"
    mine = client.get(f"/api/wanted/mine?year={year}&month={month}",
                      headers=_h(staff["token"])).json()
    assert len(mine) == 1


def test_ac5_future_month_without_window_still_accepts(client):
    """AC5(회귀): 기간 미설정(기본 열림)인 미래 달도 기존대로 200."""
    _master, staff = _ward_pair(client)
    year, month = _ym(+2)

    r = _apply(client, staff, year, month)
    assert r.status_code == 200, f"기간 미설정 미래 달 신청이 거부됨: {r.status_code} {r.text}"
    assert r.json()["status"] == "pending"


def test_ac5_current_month_unpublished_still_accepts(client):
    """AC5(회귀): 이번 달은 지난 달이 아니므로 발행 전이면 기존대로 접수된다."""
    _master, staff = _ward_pair(client)
    year, month = _ym(0)

    r = _apply(client, staff, year, month)
    assert r.status_code == 200, f"발행 전 이번 달 신청이 거부됨: {r.status_code} {r.text}"
    assert r.json()["status"] == "pending"


def test_ac5_future_month_closed_window_keeps_existing_message(client):
    """AC5(회귀): 미래 달 기간 마감 시 기존 '만료' 403 메시지가 그대로 유지된다."""
    master, staff = _ward_pair(client)
    year, month = _ym(+1)
    _close_window(client, master, year, month)

    st = client.get(f"/api/request-window/{year}/{month}", headers=_h(staff["token"])).json()
    assert st["is_open"] is False
    assert "만료" in (st.get("message") or ""), f"기존 마감 문구가 바뀜: {st}"

    r = _apply(client, staff, year, month)
    assert r.status_code == 403, f"{r.status_code} {r.text}"
    assert "만료" in r.json()["detail"], f"기존 마감 문구가 바뀜: {r.json()['detail']!r}"


# =====================================================================
# 수용 기준 1 보강 — 차단 판정은 '해당 병동'의 발행만 본다
# =====================================================================

def test_ac1_other_ward_publication_does_not_block(client):
    """AC1: 다른 병동에서 발행되었다고 우리 병동 신청까지 막히면 안 된다."""
    _master61, staff61 = _ward_pair(client)
    master99 = _reg(client, "haneul@duty.kr", name="박하늘", ward="99")
    year, month = _ym(+1)
    _publish(client, master99, year, month)      # 99병동만 발행

    r = _apply(client, staff61, year, month)
    assert r.status_code == 200, (
        f"타 병동 발행 때문에 61병동 신청이 막힘: {r.status_code} {r.text}")


# =====================================================================
# 지시서 '범위 제외' 보호 — 발행된 달의 기존 신청 취소는 계속 허용
# =====================================================================

def test_scope_cancel_still_allowed_after_publication(client):
    """지시서 범위 제외: 발행된 달이라도 본인 신청 취소(DELETE)는 막히지 않는다."""
    master, staff = _ward_pair(client)
    year, month = _ym(+1)

    submitted = _apply(client, staff, year, month)
    assert submitted.status_code == 200, submitted.text
    rid = submitted.json()["id"]

    _publish(client, master, year, month)        # 발행 이후에도

    dele = client.delete(f"/api/wanted/{rid}", headers=_h(staff["token"]))
    assert dele.status_code == 200, f"발행된 달의 본인 신청 취소가 막힘: {dele.status_code} {dele.text}"
    mine = client.get(f"/api/wanted/mine?year={year}&month={month}",
                      headers=_h(staff["token"])).json()
    assert mine == [], f"취소했는데 신청이 남음: {mine}"


# =====================================================================
# 수용 기준 4 보강 — 기간 저장(PUT) 응답도 실제 차단 상태와 일치해야 한다
#   부서장이 발행된 달/지난 달에 기간을 저장했을 때 응답이 is_open=true면
#   "열어뒀다"고 오해하지만 부서원 신청은 실제로 막힌다 (상태 불일치).
# =====================================================================

def test_ac4_put_window_response_reflects_published_block(client):
    """AC4: 발행된 달에 기간을 저장해도 응답은 is_open=false + '발행' 사유."""
    master, _staff = _ward_pair(client)
    year, month = _ym(+1)
    _publish(client, master, year, month)

    r = client.put("/api/request-window",
                   json={"year": year, "month": month, "opens_at": _iso(-48),
                         "closes_at": _iso(48), "note": "접수 중"},
                   headers=_h(master["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_open"] is False, (
        f"발행된 달에 기간 저장 응답이 열림으로 표시됨(실제 신청은 차단): {body}")
    msg = body.get("message") or ""
    _assert_korean_guidance(msg)
    assert "발행" in msg, f"저장 응답 message에 '발행' 사유가 없음: {msg!r}"


def test_ac4_put_window_response_reflects_past_month_block(client):
    """AC4: 지난 달에 기간을 저장해도 응답은 is_open=false + 지난 달 사유."""
    master, _staff = _ward_pair(client)
    year, month = _ym(-1)

    r = client.put("/api/request-window",
                   json={"year": year, "month": month, "opens_at": _iso(-48),
                         "closes_at": _iso(48), "note": "접수 중"},
                   headers=_h(master["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_open"] is False, (
        f"지난 달에 기간 저장 응답이 열림으로 표시됨(실제 신청은 차단): {body}")
    msg = body.get("message") or ""
    _assert_korean_guidance(msg)
    assert any(w in msg for w in PAST_REASON_WORDS), (
        f"저장 응답 message에 지난 달 사유가 없음{PAST_REASON_WORDS}: {msg!r}")


def test_ac4_put_window_response_matches_get_status(client):
    """AC4: 저장(PUT) 응답과 조회(GET) 응답의 is_open·message가 서로 어긋나지 않는다."""
    master, staff = _ward_pair(client)
    year, month = _ym(+1)
    _publish(client, master, year, month)

    put_body = client.put("/api/request-window",
                          json={"year": year, "month": month, "opens_at": _iso(-48),
                                "closes_at": _iso(48)},
                          headers=_h(master["token"])).json()
    get_body = client.get(f"/api/request-window/{year}/{month}",
                          headers=_h(staff["token"])).json()
    assert put_body["is_open"] == get_body["is_open"], (
        f"저장 응답과 조회 응답의 is_open 불일치: PUT={put_body}, GET={get_body}")
    assert (put_body.get("message") or "") == (get_body.get("message") or ""), (
        f"저장 응답과 조회 응답의 message 불일치: "
        f"PUT={put_body.get('message')!r}, GET={get_body.get('message')!r}")


def test_ac5_put_window_response_open_for_future_unpublished(client):
    """AC5(회귀): 미발행 미래 달에 기간을 저장하면 기존대로 is_open=true."""
    master, _staff = _ward_pair(client)
    year, month = _ym(+1)

    r = client.put("/api/request-window",
                   json={"year": year, "month": month, "opens_at": _iso(-48),
                         "closes_at": _iso(48), "note": "접수 중"},
                   headers=_h(master["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_open"] is True, f"미발행 미래 달 기간 저장이 열림으로 표시되지 않음: {body}"
