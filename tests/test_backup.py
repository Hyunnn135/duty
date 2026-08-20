"""관리자용 데이터 백업 내려받기 + 미백업 경고
(지시서 docs/orders/2026-08-20-백업-내려받기.md, T3 — **보완 지시 반영판**).

지시서의 수용 기준(본문 1~7 + 말미 "보완 지시" 필수 1~5)에서 설계했다.
구현(app/backup.py)을 읽고 역산하지 않았다 — 엔드포인트 경로/요청 본문/상수명 같은
**계약**만 확인했고, 기대값은 전부 지시서에서 왔다.

권한 판정은 **불변 내부 식별자 uid**(`DUTY_BACKUP_OWNER_UID`) 기준이다.
옛 문자열 방식(`DUTY_BACKUP_OWNER`)은 제거됐고, 이 파일은 그것이 **아무 효과가
없음**까지 검증한다(보완 지시 필수 1).

- 날짜는 전부 KST 상대 계산으로 만든다(하드코딩 금지, 교훈 L-4).
- 등장하는 이름·이메일·사번은 전부 **가명/가짜 값**이다(교훈 L-1).
  사번은 실제 사번과 겹치지 않도록 9로 시작하는 임의 값만 쓴다.
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

KST = timezone(timedelta(hours=9))

BACKUP_URL = "/api/admin/backup"
CONFIRM_URL = "/api/admin/backup/confirm"
STATUS_URL = "/api/admin/backup/status"

UID_ENV = "DUTY_BACKUP_OWNER_UID"
LEGACY_ENV = "DUTY_BACKUP_OWNER"  # 제거된 옛 방식 — 설정해도 무효여야 한다

# 지시서 "산출물 구성": CSV는 7개 = 앱 테이블 전량에서 backup_log 제외
#   (backup_log는 복구 정본 duty.db 안에 들어 있으므로 손실 없음 — 사무국 확정 해석)
EXPECTED_CSV_TABLES = {
    "users", "ward_invites", "rosters", "schedules",
    "wanted_requests", "request_windows", "feedback",
}
BOM = b"\xef\xbb\xbf"

# 보완 지시 필수 2 — CSV에서 지워져야 하는 자격증명 (테이블, 컬럼)
CREDENTIAL_COLUMNS = [("users", "pw_hash"), ("users", "salt"), ("ward_invites", "code")]


# ============================ 픽스처 / 헬퍼 ============================

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """DB·서버 임시 디렉터리를 테스트마다 격리한다(임시 파일 잔존 검사를 위해)."""
    db_dir = tmp_path / "dbdir"
    db_dir.mkdir()
    tmp_dir = tmp_path / "srvtmp"
    tmp_dir.mkdir()
    monkeypatch.setenv("DUTY_DB", str(db_dir / "test.db"))
    monkeypatch.setenv("DUTY_SECRET", "test-secret-for-qa-suite-0001")
    monkeypatch.delenv(UID_ENV, raising=False)
    monkeypatch.delenv(LEGACY_ENV, raising=False)
    # 서버가 만드는 임시 파일도 이 디렉터리 안으로 모은다.
    monkeypatch.setenv("TMPDIR", str(tmp_dir))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_dir), raising=False)
    return SimpleNamespace(db_dir=db_dir, tmp_dir=tmp_dir)


@pytest.fixture()
def client(env):
    from app.main import app

    return TestClient(app)


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _db():
    conn = sqlite3.connect(os.environ["DUTY_DB"], timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _invite_code(ward):
    conn = _db()
    try:
        row = conn.execute("SELECT code FROM ward_invites WHERE ward=?", (ward,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _reg(client, *, email=None, empno=None, name="사용자", pw="password123", ward="61"):
    """가입 헬퍼: 병동 미개설이면 개설(master), 이미 있으면 초대 코드로 가입(staff)."""
    body = {"password": pw, "name": name}
    if email:
        body["email"] = email
    if empno:
        body["empno"] = empno
    r = client.post("/api/auth/register", json={**body, "ward": ward})
    if r.status_code == 403:  # 병동 기개설 → 초대 코드로 가입
        r = client.post("/api/auth/register", json={**body, "invite_code": _invite_code(ward)})
    assert r.status_code == 200, r.text
    return r.json()


def _set_role(client, master, *, email, role):
    r = client.post("/api/auth/set-role", json={"email": email, "role": role},
                    headers=_h(master["token"]))
    assert r.status_code == 200 and r.json()["role"] == role, r.text


def _uid(user) -> int:
    """가입/로그인 응답이 알려준 내 계정 번호(uid).

    보완 지시 필수 1-4: 비개발자가 서버 접속 없이 자기 uid를 확인할 수 있어야 한다.
    값이 실제 users.id와 같은지도 여기서 함께 확인한다(틀린 번호를 알려주면
    운영자가 환경변수를 잘못 설정하게 되므로 이것 자체가 결함이다).
    """
    assert "uid" in user, f"로그인/가입 응답에 uid가 없음: {sorted(user)}"
    value = user["uid"]
    assert isinstance(value, int) and value > 0, f"uid가 양의 정수가 아님: {value!r}"
    conn = _db()
    try:
        row = conn.execute("SELECT id FROM users WHERE name=? AND ward=?",
                           (user["name"], user["ward"])).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == value, f"uid가 users.id와 다름: {value} != {row}"
    return value


# ---- 등장인물(전원 가명 · 가짜 사번) ----
OWNER = SimpleNamespace(email="seoyeon@duty.kr", name="김서연", empno="990001", ward="61")
ADMIN = SimpleNamespace(email="jiwoo@duty.kr", name="이지우", empno="990002", ward="61")
STAFF = SimpleNamespace(email="minjun@duty.kr", name="최민준", empno="990003", ward="61")
OTHER = SimpleNamespace(email="haneul@duty.kr", name="박하늘", empno="990004", ward="99")


@pytest.fixture()
def people(client):
    """61병동 master(백업 권한 후보)·admin·staff + 99병동의 다른 master."""
    owner = _reg(client, email=OWNER.email, name=OWNER.name, ward=OWNER.ward)
    assert owner["role"] == "master"
    admin = _reg(client, email=ADMIN.email, name=ADMIN.name, ward=ADMIN.ward)
    staff = _reg(client, email=STAFF.email, name=STAFF.name, ward=STAFF.ward)
    _set_role(client, owner, email=ADMIN.email, role="admin")
    other = _reg(client, email=OTHER.email, name=OTHER.name, ward=OTHER.ward)
    assert other["role"] == "master", "99병동 개설자는 그 병동의 master여야 함"
    # storage 스키마(backup_log 포함)를 만들어 둔다.
    assert client.get("/api/roster", headers=_h(owner["token"])).status_code == 200
    return SimpleNamespace(owner=owner, admin=admin, staff=staff, other=other)


def _allow_uid(monkeypatch, *values):
    """DUTY_BACKUP_OWNER_UID 설정 — 정수 uid나 이미 만들어진 문자열을 그대로 받는다."""
    if len(values) == 1 and isinstance(values[0], str):
        monkeypatch.setenv(UID_ENV, values[0])
    else:
        monkeypatch.setenv(UID_ENV, ",".join(str(v) for v in values))


def _log_rows():
    conn = _db()
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "backup_log" in names, f"backup_log 테이블 없음: {sorted(names)}"
        return [dict(r) for r in conn.execute("SELECT * FROM backup_log ORDER BY id")]
    finally:
        conn.close()


def _zip_of(resp) -> zipfile.ZipFile:
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:400]}"
    body = resp.content
    assert body[:2] == b"PK", f"ZIP 시그니처 아님: {body[:8]!r}"
    return zipfile.ZipFile(io.BytesIO(body))


def _csv_names(zf) -> set[str]:
    return {n.split("/", 1)[1][: -len(".csv")] for n in zf.namelist()
            if n.startswith("tables/") and n.endswith(".csv")}


def _extract_db(zf, dest) -> str:
    path = os.path.join(str(dest), "restored.db")
    with open(path, "wb") as f:
        f.write(zf.read("duty.db"))
    return path


def _feedback(client, user, message):
    r = client.post("/api/feedback", json={"message": message}, headers=_h(user["token"]))
    assert r.status_code == 200, r.text
    return r


def _download(client, user):
    """GET /backup — 응답과 pending 행 번호(X-Backup-Id)를 함께 돌려준다."""
    r = client.get(BACKUP_URL, headers=_h(user["token"]))
    assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
    raw = r.headers.get("x-backup-id")
    assert raw and raw.isdigit(), f"X-Backup-Id 헤더가 없거나 숫자가 아님: {raw!r}"
    return r, int(raw)


def _confirm(client, user, backup_id, size):
    return client.post(CONFIRM_URL, json={"id": backup_id, "bytes": size},
                       headers=_h(user["token"]))


def _full_backup(client, user):
    """지시서가 정의한 '성공한 백업' 1회 = 내려받기 + 확정(confirm)."""
    r, bid = _download(client, user)
    ok = _confirm(client, user, bid, len(r.content))
    assert ok.status_code == 200, f"confirm 실패: {ok.status_code} {ok.text[:300]}"
    return r, bid


def _files_under(root) -> set[str]:
    out = set()
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for f in filenames:
            out.add(os.path.join(dirpath, f))
    return out


def _assert_no_leftovers(env, before_tmp, before_db, *, where=""):
    leftover_tmp = _files_under(env.tmp_dir) - before_tmp
    assert not leftover_tmp, f"임시 디렉터리에 백업 잔여물{where}: {sorted(leftover_tmp)}"
    allowed = {os.environ["DUTY_DB"] + suffix for suffix in ("", "-wal", "-shm", "-journal")}
    leftover_db = {p for p in _files_under(env.db_dir) - before_db if p not in allowed}
    assert not leftover_db, f"DB 디렉터리에 백업 잔여물{where}: {sorted(leftover_db)}"


# ---- KST 상대 시각 ----

def _kst_days_ago(n: int, *, flip_utc_date: bool = False) -> datetime:
    """지금으로부터 **KST 달력 기준** 정확히 n일 전인 시각.

    flip_utc_date=True면 '지금'과 반대편 UTC 날짜 구간(KST 00~09시 / 09~24시)을
    골라, **UTC 달력으로 세면 n이 나오지 않는** 시각을 만든다. 수용 기준 5의
    "UTC로 계산하면 결과가 달라지는 경계 시각" 검증용.
    """
    now_kst = datetime.now(timezone.utc).astimezone(KST)
    d = (now_kst - timedelta(days=n)).date()
    if flip_utc_date:
        # KST 시각이 09시 미만이면 그 시점의 UTC 날짜 = KST 날짜 - 1.
        # '지금'과 반대가 되도록 대상 시각의 시(hour)를 고른다.
        hour = 12 if now_kst.hour < 9 else 3
    else:
        hour = 12
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=KST)


def _utc_date_diff(dt: datetime) -> int:
    """UTC 달력으로 센 경과일(테스트 자기검증용)."""
    now_utc = datetime.now(timezone.utc)
    return (now_utc.date() - dt.astimezone(timezone.utc).date()).days


def _insert_log_row(dt: datetime, *, status: str = "ok", uid: int = 1, size: int = 4096):
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO backup_log (actor, ward, created_at, byte_size, status) "
            "VALUES (?,?,?,?,?)",
            (f"uid:{uid}", OWNER.ward, dt.astimezone(timezone.utc).isoformat(), size, status))
        conn.commit()
    finally:
        conn.close()


def _set_last_backup(dt: datetime | None):
    """backup_log를 원하는 '마지막 성공 시각' 하나만 남긴 상태로 만든다.

    저장은 UTC ISO — 저장소 관례(app.storage._now)와 동일(교훈 L-4).
    actor는 uid 형식만 쓴다(보완 지시: 이력에 사번·이메일 금지).
    """
    conn = _db()
    try:
        conn.execute("DELETE FROM backup_log")
        conn.commit()
    finally:
        conn.close()
    if dt is not None:
        _insert_log_row(dt)


def _status(client, user):
    r = client.get(STATUS_URL, headers=_h(user["token"]))
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    body = r.json()
    for key in ("last_backup_at", "days_since", "level"):
        assert key in body, f"상태 응답에 {key} 없음: {body}"
    return body


# ============================ 수용 기준 1 — 권한 경계 (uid) ============================

def test_ac1_allowlisted_master_gets_zip(client, people, monkeypatch):
    """허가 uid(allowlist ∧ master) → 200 + ZIP 첨부."""
    _allow_uid(monkeypatch, _uid(people.owner))
    r = client.get(BACKUP_URL, headers=_h(people.owner["token"]))
    assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
    assert r.content[:2] == b"PK", "본문이 ZIP이 아님"
    disp = r.headers.get("content-disposition", "")
    assert ".zip" in disp.lower(), f"첨부 파일명(.zip)이 없음: {disp!r}"


def test_ac1_master_of_other_ward_denied(client, people, monkeypatch):
    """allowlist에 없는 master(= 다른 병동 개설자)는 403 — 역할만으로 열리면 안 된다."""
    _allow_uid(monkeypatch, _uid(people.owner))
    r = client.get(BACKUP_URL, headers=_h(people.other["token"]))
    assert r.status_code == 403, f"타 병동 master가 백업을 받음: {r.status_code}"


def test_ac1_admin_and_staff_denied_even_if_allowlisted(client, people, monkeypatch):
    """admin·staff는 allowlist에 uid가 있어도 403 — allowlist ∧ role==master."""
    _allow_uid(monkeypatch, _uid(people.admin), _uid(people.staff), _uid(people.owner))
    assert client.get(BACKUP_URL, headers=_h(people.admin["token"])).status_code == 403
    assert client.get(BACKUP_URL, headers=_h(people.staff["token"])).status_code == 403
    # 대조군: 같은 설정에서 master는 통과해야 판정이 '전부 막기'가 아님이 증명된다.
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200


def test_ac1_unauthenticated_is_401(client, people, monkeypatch):
    _allow_uid(monkeypatch, _uid(people.owner))
    assert client.get(BACKUP_URL).status_code == 401
    assert client.get(BACKUP_URL, headers={"Authorization": "Bearer not-a-real-token"}
                      ).status_code == 401
    assert client.get(STATUS_URL).status_code == 401
    assert client.post(CONFIRM_URL, json={"id": 1, "bytes": 1}).status_code == 401


@pytest.mark.parametrize("value", [
    "",                    # 빈 값
    "   ",                 # 공백만
    "{uid},abc",           # 정수가 아닌 값이 섞임 → 설정 전체 무효
    "abc",                 # 정수가 전혀 없음
    "{uid},0",             # 0이 섞임
    "0",
    "{uid},-1",            # 음수가 섞임
    "-1",
    "{uid}.0",             # 정수 표기가 아님
    "{uid};{uid}",         # 구분자 오기(콤마 아님)
])
def test_ac1_uid_env_is_fail_closed(client, people, monkeypatch, value):
    """미설정·빈 값·정수 아닌 값·0·음수가 섞이면 **설정 전체 무효 = 전원 403**.

    "1은 살리고 abc만 무시"하는 부분 수용은 금지 — 오타 하나로 의도하지 않은
    계정이 열릴 수 있고, 운영자는 설정이 먹혔다고 착각한다(fail-closed).
    """
    uid = _uid(people.owner)
    monkeypatch.setenv(UID_ENV, value.format(uid=uid))
    for who in (people.owner, people.admin, people.staff, people.other):
        r = client.get(BACKUP_URL, headers=_h(who["token"]))
        assert r.status_code == 403, f"{UID_ENV}={value!r} 인데 {r.status_code}"
    assert client.get(STATUS_URL, headers=_h(people.owner["token"])).status_code == 403


def test_ac1_unset_env_denies_everyone(client, people, monkeypatch):
    """DUTY_BACKUP_OWNER_UID 미설정이면 전원 403(기본 개방 금지)."""
    monkeypatch.delenv(UID_ENV, raising=False)
    for who in (people.owner, people.admin, people.staff, people.other):
        assert client.get(BACKUP_URL, headers=_h(who["token"])).status_code == 403


def test_ac1_legacy_string_env_has_no_effect(client, people, monkeypatch):
    """옛 `DUTY_BACKUP_OWNER`(사번/이메일)는 **완전히 제거**됐다.

    (1) 옛 변수만 설정하면 아무도 못 들어온다.
    (2) 옛 변수에 엉뚱한 값이 있어도 uid 허가는 정상 동작한다(간섭 없음).
    """
    monkeypatch.delenv(UID_ENV, raising=False)
    monkeypatch.setenv(LEGACY_ENV, f"{OWNER.email},{OWNER.empno}")
    for who in (people.owner, people.admin, people.staff, people.other):
        r = client.get(BACKUP_URL, headers=_h(who["token"]))
        assert r.status_code == 403, f"제거된 {LEGACY_ENV}로 접근이 열림: {r.status_code}"

    monkeypatch.setenv(LEGACY_ENV, "nobody@example.invalid")
    _allow_uid(monkeypatch, _uid(people.owner))
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200


def test_ac1_uid_allowlist_comma_list_and_whitespace(client, people, monkeypatch):
    """콤마로 여러 uid, 공백이 섞여도 동작한다(배포 환경변수 실수 방지)."""
    owner_uid, other_uid = _uid(people.owner), _uid(people.other)
    monkeypatch.setenv(UID_ENV, f" {other_uid} , {owner_uid} ")
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200
    assert client.get(BACKUP_URL, headers=_h(people.other["token"])).status_code == 200
    # 목록에서 빼면 즉시 막힌다(재시작 없이 환경변수만으로 통제 가능해야 함).
    _allow_uid(monkeypatch, other_uid)
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 403


def test_ac1_denial_body_has_no_personal_info(client, people, monkeypatch):
    """거부 응답 본문에 실명·사번·이메일이 실리지 않는다."""
    _allow_uid(monkeypatch, _uid(people.owner))
    secrets = [p.name for p in (OWNER, ADMIN, STAFF, OTHER)]
    secrets += [p.empno for p in (OWNER, ADMIN, STAFF, OTHER)]
    secrets += [p.email for p in (OWNER, ADMIN, STAFF, OTHER)]
    responses = [
        client.get(BACKUP_URL),
        client.get(BACKUP_URL, headers=_h(people.staff["token"])),
        client.get(BACKUP_URL, headers=_h(people.admin["token"])),
        client.get(BACKUP_URL, headers=_h(people.other["token"])),
        client.get(STATUS_URL, headers=_h(people.staff["token"])),
        client.post(CONFIRM_URL, json={"id": 1, "bytes": 1},
                    headers=_h(people.staff["token"])),
    ]
    for r in responses:
        assert r.status_code in (401, 403), r.status_code
        text = r.text
        for s in secrets:
            assert s not in text, f"거부 본문에 신원 정보 노출({s!r}): {text[:300]}"


def test_ac1_status_endpoint_restricted_to_owner(client, people, monkeypatch):
    """상태 조회도 허가 계정 전용 — staff·admin·타병동 master에게 새어 나가면 안 된다."""
    _allow_uid(monkeypatch, _uid(people.owner))
    assert client.get(STATUS_URL, headers=_h(people.owner["token"])).status_code == 200
    for who in (people.admin, people.staff, people.other):
        assert client.get(STATUS_URL, headers=_h(who["token"])).status_code == 403


def test_ac1_no_store_on_all_three_endpoints(client, people, monkeypatch):
    """보완 지시 필수 4-1: 백업 3개 엔드포인트 응답에 `Cache-Control: no-store`.

    전체 DB 사본이 브라우저·중간 캐시에 남으면 안 된다.
    """
    _allow_uid(monkeypatch, _uid(people.owner))
    dl, bid = _download(client, people.owner)
    conf = _confirm(client, people.owner, bid, len(dl.content))
    assert conf.status_code == 200, conf.text
    st = client.get(STATUS_URL, headers=_h(people.owner["token"]))
    for label, r in (("backup", dl), ("confirm", conf), ("status", st)):
        cc = r.headers.get("cache-control", "")
        assert "no-store" in cc.lower(), f"{label} 응답에 no-store 없음: {cc!r}"


# ==================== 침투 재현 시험 (검수부 ①이 뚫었던 두 경로) ====================

def test_pen_empno_squatting_no_longer_grants_access(client, people, monkeypatch):
    """(a) 선점: 아직 아무 계정에도 묶이지 않은 '허가 문자열'을 가로채기.

    옛 방식에서는 `DUTY_BACKUP_OWNER=990777`을 먼저 설정해 둔 상태에서 누구나
    그 사번으로 새 병동을 열어 master가 되면 전체 DB를 반출할 수 있었다.
    uid 방식에서는 공격자가 자기 users.id를 고를 수 없으므로 막혀야 한다.
    """
    monkeypatch.setenv(LEGACY_ENV, "990777")           # 옛 배포 문서가 안내하던 순서
    _allow_uid(monkeypatch, _uid(people.owner))         # 새 방식의 정상 설정

    attacker = _reg(client, empno="990777", name="침입자가명", ward="77")
    assert attacker["role"] == "master", "새 병동 개설자는 master가 된다(전제)"
    assert _uid(attacker) != _uid(people.owner), "공격자가 허가 uid를 얻으면 안 된다"

    r = client.get(BACKUP_URL, headers=_h(attacker["token"]))
    assert r.status_code == 403, f"사번 선점으로 백업이 열림: {r.status_code}"
    assert client.get(STATUS_URL, headers=_h(attacker["token"])).status_code == 403


def test_pen_unicode_case_folding_lookalike_denied(client, monkeypatch):
    """(b) 유니코드 접힘: 점 없는 ı(U+0131)가 대문자화하면 'I'로 접힌다.

    옛 방식은 저장은 lower(), 비교는 upper()라 `mina@…`와 `mına@…`가 같은
    대문자열로 접혀 allowlist를 통과했다. uid 판정에서는 막혀야 한다.
    """
    victim_email = "mina@duty.kr"
    lookalike_email = "mına@duty.kr"  # 'i' → 'ı'(U+0131)
    assert victim_email != lookalike_email
    assert victim_email.upper() == lookalike_email.upper(), (
        "테스트 자기검증 실패: 대문자 접힘 충돌을 만들지 못함 "
        f"({victim_email.upper()!r} vs {lookalike_email.upper()!r})")

    victim = _reg(client, email=victim_email, name="정다은", ward="61")
    assert victim["role"] == "master"
    attacker = _reg(client, email=lookalike_email, name="유사계정가명", ward="88")
    assert attacker["role"] == "master"

    monkeypatch.setenv(LEGACY_ENV, victim_email)    # 옛 변수가 남아 있어도
    _allow_uid(monkeypatch, _uid(victim))           # 판정은 uid로만

    r = client.get(BACKUP_URL, headers=_h(attacker["token"]))
    assert r.status_code == 403, f"유니코드 접힘으로 백업이 열림: {r.status_code}"
    assert client.get(STATUS_URL, headers=_h(attacker["token"])).status_code == 403
    # 대조군: 진짜 허가 계정은 통과해야 한다.
    assert client.get(BACKUP_URL, headers=_h(victim["token"])).status_code == 200


# ============================ 수용 기준 2 — 일관성 ============================

def test_ac2_row_committed_before_backup_is_in_db_and_csv(client, people, monkeypatch, tmp_path):
    """백업 직전 커밋된 행이 ZIP의 duty.db와 해당 CSV **양쪽**에 있다."""
    _allow_uid(monkeypatch, _uid(people.owner))
    marker = "백업직전-정합성표식-한글확인"
    _feedback(client, people.staff, marker)

    zf = _zip_of(client.get(BACKUP_URL, headers=_h(people.owner["token"])))

    # (1) duty.db 쪽
    path = _extract_db(zf, tmp_path)
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT message, from_name FROM feedback").fetchall()
    finally:
        conn.close()
    assert any(r[0] == marker for r in rows), f"duty.db에 직전 커밋 행 없음: {rows}"
    assert any(r[1] == STAFF.name for r in rows), "duty.db에서 한글 이름이 깨짐"

    # (2) CSV 쪽
    text = zf.read("tables/feedback.csv").decode("utf-8-sig")
    assert marker in text, f"feedback.csv에 직전 커밋 행 없음: {text[:300]}"
    assert STAFF.name in text, "CSV에서 한글 이름이 깨짐"


def test_ac2_original_db_usable_during_and_after_backup(client, people, monkeypatch):
    """백업 중·후에도 원본 DB 읽기·쓰기가 계속 성공한다(WAL 스냅샷이 원본을 막지 않음)."""
    _allow_uid(monkeypatch, _uid(people.owner))
    db = os.environ["DUTY_DB"]

    # 백업이 순식간에 끝나 '동시성'이 검증되지 않는 것을 막기 위해 사전에 덩치를 키운다.
    seed = _db()
    try:
        seed.executemany(
            "INSERT INTO feedback (ward, from_email, from_name, message, created_at) "
            "VALUES (?,?,?,?,?)",
            [("61", STAFF.email, STAFF.name, f"사전적재-{i}-" + "가" * 200,
              datetime.now(timezone.utc).isoformat()) for i in range(20000)])
        seed.commit()
    finally:
        seed.close()

    errors: list[str] = []
    counter = {"writes": 0, "reads": 0}
    stop = threading.Event()

    def worker():
        conn = sqlite3.connect(db, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            while not stop.is_set():
                conn.execute(
                    "INSERT INTO feedback (ward, from_email, from_name, message, created_at) "
                    "VALUES (?,?,?,?,?)",
                    ("61", STAFF.email, STAFF.name, "동시쓰기",
                     datetime.now(timezone.utc).isoformat()))
                conn.commit()
                counter["writes"] += 1
                conn.execute("SELECT COUNT(*) FROM feedback").fetchone()
                counter["reads"] += 1
        except Exception as exc:  # noqa: BLE001 — 실패 원문을 그대로 보고한다
            errors.append(repr(exc))
        finally:
            conn.close()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        before = counter["writes"]
        resp = client.get(BACKUP_URL, headers=_h(people.owner["token"]))
        during = counter["writes"] - before
    finally:
        stop.set()
        t.join(timeout=20)

    assert not errors, f"백업 중 원본 DB 접근 실패: {errors[:3]}"
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    assert during > 0, "백업 요청이 진행되는 동안 원본 DB에 쓴 기록이 0건(동시성 미검증)"

    # 백업 후에도 API 읽기·쓰기가 정상
    _feedback(client, people.staff, "백업후-쓰기")
    got = client.get("/api/feedback", headers=_h(people.owner["token"]))
    assert got.status_code == 200, got.text
    assert client.get("/api/roster", headers=_h(people.owner["token"])).status_code == 200


# ============================ 수용 기준 3 — 산출물 ============================

def test_ac3_zip_contains_db_seven_csv_and_readme(client, people, monkeypatch, tmp_path):
    """ZIP = duty.db 1개 + tables/*.csv 7개 + README.txt, 그 외 없음."""
    _allow_uid(monkeypatch, _uid(people.owner))
    zf = _zip_of(client.get(BACKUP_URL, headers=_h(people.owner["token"])))
    names = set(zf.namelist())

    csv_tables = _csv_names(zf)
    assert len(csv_tables) == 7, f"CSV가 7개가 아님({len(csv_tables)}): {sorted(csv_tables)}"
    assert csv_tables == EXPECTED_CSV_TABLES, f"CSV 대상 테이블 불일치: {sorted(csv_tables)}"
    assert names == {"duty.db", "README.txt"} | {f"tables/{t}.csv" for t in EXPECTED_CSV_TABLES}, \
        f"ZIP 구성 불일치: {sorted(names)}"

    # 복구 정본 duty.db는 열 수 있는 SQLite여야 하고, backup_log까지 들어 있어야 한다
    # (CSV에서 뺀 대신 정본에는 남아야 손실이 없다).
    path = _extract_db(zf, tmp_path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert EXPECTED_CSV_TABLES <= tables, f"duty.db에 누락 테이블: {EXPECTED_CSV_TABLES - tables}"
    assert "backup_log" in tables, "복구 정본 duty.db에 backup_log가 없음"
    # 스키마가 늘었는데 CSV가 따라오지 않으면 알아채야 한다.
    app_tables = {t for t in tables if not t.startswith("sqlite_")} - {"backup_log"}
    assert app_tables == csv_tables, f"CSV로 안 빠진 테이블: {sorted(app_tables - csv_tables)}"


def test_ac3_every_csv_starts_with_utf8_bom_and_keeps_korean(client, people, monkeypatch, tmp_path):
    """CSV 첫 3바이트가 UTF-8 BOM이고, 데이터의 한글이 깨지지 않는다."""
    _allow_uid(monkeypatch, _uid(people.owner))
    _feedback(client, people.staff, "한글 데이터 보존 확인 — 김서연·박하늘")
    nurses = [{"id": "n1", "name": "정다은", "team": 1, "seniority_rank": 1}]
    assert client.put("/api/roster", json={"nurses": nurses},
                      headers=_h(people.owner["token"])).status_code == 200

    zf = _zip_of(client.get(BACKUP_URL, headers=_h(people.owner["token"])))
    path = _extract_db(zf, tmp_path)
    conn = sqlite3.connect(path)

    try:
        for table in sorted(EXPECTED_CSV_TABLES):
            raw = zf.read(f"tables/{table}.csv")
            assert raw[:3] == BOM, f"{table}.csv 첫 3바이트가 BOM이 아님: {raw[:6]!r}"
            text = raw.decode("utf-8-sig")  # 깨졌으면 여기서 예외
            rows = list(csv.reader(io.StringIO(text)))
            assert rows, f"{table}.csv 가 비어 있음(헤더도 없음)"
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            assert rows[0] == cols, f"{table}.csv 헤더가 컬럼명과 다름: {rows[0]} != {cols}"
            db_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert len(rows) - 1 == db_count, \
                f"{table}.csv 행 수 불일치: CSV {len(rows) - 1} != duty.db {db_count}"
    finally:
        conn.close()

    # 한글이 실제로 살아 있는지(모지바케·물음표 치환 없이)
    users_csv = zf.read("tables/users.csv").decode("utf-8-sig")
    for name in (OWNER.name, ADMIN.name, STAFF.name):
        assert name in users_csv, f"users.csv에서 한글 이름 유실: {name}"
    assert "정다은" in zf.read("tables/rosters.csv").decode("utf-8-sig")
    assert "�" not in users_csv, "users.csv에 치환 문자(U+FFFD)가 있음"


def test_ac3_readme_is_korean_with_kst_time_and_privacy_warning(client, people, monkeypatch):
    """README.txt: 한국어 + 백업 시각(KST) + 개인정보/공유 금지 안내.

    표기 형식은 규정하지 않는다(지시서는 "백업 시각(KST)"만 요구). 대신 적힌 시각이
    **UTC가 아니라 KST**인지를 실제 값으로 확인한다.
    """
    _allow_uid(monkeypatch, _uid(people.owner))
    zf = _zip_of(client.get(BACKUP_URL, headers=_h(people.owner["token"])))
    text = zf.read("README.txt").decode("utf-8-sig")
    assert any("가" <= ch <= "힣" for ch in text), f"README.txt가 한국어가 아님: {text[:200]}"

    m = re.search(r"(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})\D{1,4}(\d{1,2}):(\d{2})", text)
    assert m, f"README.txt에서 백업 시각을 찾지 못함: {text[:400]}"
    stamped = datetime(*(int(g) for g in m.groups()), tzinfo=KST)
    now_kst = datetime.now(timezone.utc).astimezone(KST)
    assert abs((now_kst - stamped).total_seconds()) < 300, (
        f"백업 시각이 KST가 아님(UTC로 적힌 듯): README={stamped.isoformat()}, "
        f"현재 KST={now_kst.isoformat()}")

    assert ("KST" in text) or ("한국" in text), "README.txt에 시간대(KST/한국 시간) 표기가 없음"
    assert "개인정보" in text, "README.txt에 개인정보 포함 경고가 없음"
    assert "복구" in text, "README.txt에 복구 요청 방법 안내가 없음"


def test_ac3_no_temp_files_left_after_success(client, people, monkeypatch, env):
    """성공 경로: 요청 종료 후 서버에 백업 임시 파일 잔존 0(개인정보 잔여 금지)."""
    _allow_uid(monkeypatch, _uid(people.owner))
    before_tmp, before_db = _files_under(env.tmp_dir), _files_under(env.db_dir)

    for _ in range(3):  # 반복해도 쌓이지 않아야 한다
        assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200

    _assert_no_leftovers(env, before_tmp, before_db, where="(성공 경로)")


# ============ 보완 지시 필수 2 — 백업 산출물에서 자격증명 제거 ============

def test_ac_mask_no_credentials_anywhere_in_csv(client, people, monkeypatch):
    """ZIP **안 어느 CSV에도** 실제 초대 코드·비밀번호 해시·salt가 없어야 한다.

    검수부 ②는 ward_invites.csv의 유효한 초대 코드로 실제 가입에 성공했다.
    복구 정본은 duty.db이므로 CSV의 이 값들은 이득 0, 위험만 있다.
    """
    _allow_uid(monkeypatch, _uid(people.owner))

    conn = _db()
    try:
        secrets = set()
        for table, column in CREDENTIAL_COLUMNS:
            for row in conn.execute(f"SELECT {column} FROM {table}"):
                if row[0]:
                    secrets.add(str(row[0]))
    finally:
        conn.close()
    assert secrets, "테스트 자기검증 실패: 원본 DB에 자격증명 값이 하나도 없음"
    assert len(secrets) >= 4, f"검사 대상 자격증명이 너무 적음: {len(secrets)}건"

    zf = _zip_of(client.get(BACKUP_URL, headers=_h(people.owner["token"])))
    for name in zf.namelist():
        if not name.endswith(".csv"):
            continue
        text = zf.read(name).decode("utf-8-sig")
        for s in secrets:
            assert s not in text, f"{name} 에 자격증명 평문 노출: {s[:6]}…"

    # 마스킹 대상 컬럼은 '값이 지워졌다'는 것이 눈에 보여야 한다(빈칸이면 복구 오해).
    users_rows = list(csv.DictReader(io.StringIO(
        zf.read("tables/users.csv").decode("utf-8-sig"))))
    assert users_rows, "users.csv에 행이 없음"
    for row in users_rows:
        for column in ("pw_hash", "salt"):
            assert row[column] == "(생략)", f"users.{column} 마스킹 값이 다름: {row[column]!r}"
    invite_rows = list(csv.DictReader(io.StringIO(
        zf.read("tables/ward_invites.csv").decode("utf-8-sig"))))
    assert invite_rows, "ward_invites.csv에 행이 없음"
    for row in invite_rows:
        assert row["code"] == "(생략)", f"ward_invites.code 마스킹 값이 다름: {row['code']!r}"


def test_ac_mask_master_db_keeps_original_values(client, people, monkeypatch, tmp_path):
    """마스킹은 CSV에만 — 복구 정본 duty.db에는 원값이 그대로 보존된다.

    여기가 지워지면 백업으로 복구해도 아무도 로그인할 수 없다.
    """
    _allow_uid(monkeypatch, _uid(people.owner))
    conn = _db()
    try:
        original_users = {r["id"]: (r["pw_hash"], r["salt"])
                          for r in conn.execute("SELECT id, pw_hash, salt FROM users")}
        original_invites = {r["ward"]: r["code"]
                            for r in conn.execute("SELECT ward, code FROM ward_invites")}
    finally:
        conn.close()
    assert original_users and original_invites, "테스트 자기검증 실패: 원본이 비었음"

    zf = _zip_of(client.get(BACKUP_URL, headers=_h(people.owner["token"])))
    restored = sqlite3.connect(_extract_db(zf, tmp_path))
    restored.row_factory = sqlite3.Row
    try:
        got_users = {r["id"]: (r["pw_hash"], r["salt"])
                     for r in restored.execute("SELECT id, pw_hash, salt FROM users")}
        got_invites = {r["ward"]: r["code"]
                       for r in restored.execute("SELECT ward, code FROM ward_invites")}
    finally:
        restored.close()

    assert got_users == original_users, "duty.db 정본의 pw_hash/salt가 훼손됨(복구 불가)"
    assert got_invites == original_invites, "duty.db 정본의 초대 코드가 훼손됨"
    assert "(생략)" not in {v for pair in got_users.values() for v in pair}, \
        "정본 duty.db에 마스킹 값이 들어감"


# ============================ 수용 기준 4 — 이력(2단계 pending → confirm) ============================

def test_ac4_get_creates_pending_row_and_returns_backup_id(client, people, monkeypatch):
    """GET /backup 은 **아직 성공이 아니다** — pending 행 + X-Backup-Id.

    (재설계 근거: 보완 지시 필수 3 — 응답 전에 'ok'를 남기면 다운로드가 끊겨도
    시스템은 백업이 있다고 믿는다.)
    """
    _allow_uid(monkeypatch, _uid(people.owner))
    assert _log_rows() == [], "백업 전인데 이력이 있음"

    r, bid = _download(client, people.owner)
    rows = _log_rows()
    assert len(rows) == 1, f"GET 1회에 이력 {len(rows)}행: {rows}"
    row = rows[0]
    assert row["id"] == bid, f"X-Backup-Id({bid})가 이력 행 번호({row['id']})와 다름"
    assert row["status"] == "pending", f"확정 전인데 status={row['status']!r} (기대 pending)"
    # 크기 불일치 검사(아래 테스트)가 성립하려면 서버가 만든 크기가 기록돼 있어야 한다.
    assert row["byte_size"] == len(r.content), \
        f"기록된 크기가 실제 전달 크기와 다름: {row['byte_size']} != {len(r.content)}"
    parsed = datetime.fromisoformat(str(row["created_at"]))
    assert parsed.tzinfo is not None, f"created_at에 시간대가 없음: {row['created_at']}"
    assert parsed.utcoffset() == timedelta(0), \
        f"created_at이 UTC 저장이 아님(교훈 L-4): {row['created_at']}"


def test_ac4_confirm_marks_ok_and_counts_one_row_per_backup(client, people, monkeypatch):
    """확정(confirm)해야 status='ok'. 성공 1회당 행 1개, 2회면 2개."""
    _allow_uid(monkeypatch, _uid(people.owner))
    r1, bid1 = _download(client, people.owner)
    assert _confirm(client, people.owner, bid1, len(r1.content)).status_code == 200

    rows = _log_rows()
    assert len(rows) == 1, f"성공 1회에 이력 {len(rows)}행: {rows}"
    assert rows[0]["status"] == "ok", f"확정했는데 status={rows[0]['status']!r}"
    assert rows[0]["byte_size"] > 0, f"byte_size가 0 이하: {rows[0]}"

    r2, bid2 = _download(client, people.owner)
    assert bid2 != bid1, "두 번째 요청이 같은 이력 행을 재사용함"
    assert _confirm(client, people.owner, bid2, len(r2.content)).status_code == 200
    rows = _log_rows()
    assert len(rows) == 2, f"성공 2회인데 이력이 {len(rows)}행"
    assert [x["status"] for x in rows] == ["ok", "ok"], rows


def test_ac4_confirm_size_mismatch_is_400_and_stays_pending(client, people, monkeypatch):
    """크기 불일치 → 400, 행은 pending 유지(부분 전달을 성공으로 세지 않는다)."""
    _allow_uid(monkeypatch, _uid(people.owner))
    r, bid = _download(client, people.owner)
    size = len(r.content)

    for wrong in (size - 1, size + 1, 0):
        bad = _confirm(client, people.owner, bid, wrong)
        assert bad.status_code == 400, f"크기 {wrong}(실제 {size})인데 {bad.status_code}"
        rows = _log_rows()
        assert len(rows) == 1 and rows[0]["status"] == "pending", \
            f"크기 불일치 후 상태가 오염됨: {rows}"

    # 올바른 크기로는 확정된다(대조군).
    assert _confirm(client, people.owner, bid, size).status_code == 200
    assert _log_rows()[0]["status"] == "ok"


def test_ac4_confirm_of_other_users_row_is_404(client, people, monkeypatch):
    """남의 pending 행을 confirm하면 404 — 남의 백업을 대신 '성공'시킬 수 없다."""
    owner_uid, other_uid = _uid(people.owner), _uid(people.other)
    _allow_uid(monkeypatch, owner_uid, other_uid)

    r_owner, bid_owner = _download(client, people.owner)
    r_other, bid_other = _download(client, people.other)
    assert bid_owner != bid_other

    stolen = _confirm(client, people.other, bid_owner, len(r_owner.content))
    assert stolen.status_code == 404, f"남의 행 confirm이 {stolen.status_code}"
    by_id = {row["id"]: row for row in _log_rows()}
    assert by_id[bid_owner]["status"] == "pending", "남이 확정해 버림"

    # 존재하지 않는 행도 404
    missing = _confirm(client, people.owner, 999999, 1)
    assert missing.status_code == 404, f"없는 행 confirm이 {missing.status_code}"

    # 본인 행은 정상 확정(대조군)
    assert _confirm(client, people.other, bid_other, len(r_other.content)).status_code == 200
    assert _confirm(client, people.owner, bid_owner, len(r_owner.content)).status_code == 200


def test_ac4_confirm_is_idempotent(client, people, monkeypatch):
    """같은 confirm을 다시 불러도 멱등 — 행이 늘거나 상태가 뒤집히지 않는다."""
    _allow_uid(monkeypatch, _uid(people.owner))
    r, bid = _download(client, people.owner)
    size = len(r.content)

    first = _confirm(client, people.owner, bid, size)
    assert first.status_code == 200, first.text
    second = _confirm(client, people.owner, bid, size)
    assert second.status_code == 200, f"재확정이 {second.status_code}: {second.text[:200]}"

    rows = _log_rows()
    assert len(rows) == 1, f"멱등이어야 하는데 행이 늘어남: {rows}"
    assert rows[0]["status"] == "ok", rows
    assert first.json() == second.json() or (
        first.json()["level"] == second.json()["level"]), "재확정 응답이 달라짐"


def test_ac4_denied_export_attempts_leave_denied_rows(client, people, monkeypatch):
    """반출 거부(GET /backup, POST /confirm)는 `status='denied'` 행을 남긴다.

    (재설계 근거: 보완 지시 필수 4-2 — 침입 시도를 탐지할 수단이 필요하다.
    옛 기준 "403은 행 미추가"는 폐기됐다.)
    """
    _allow_uid(monkeypatch, _uid(people.owner))
    assert _log_rows() == []

    for who in (people.admin, people.staff, people.other):
        assert client.get(BACKUP_URL, headers=_h(who["token"])).status_code == 403
    assert client.post(CONFIRM_URL, json={"id": 1, "bytes": 1},
                       headers=_h(people.staff["token"])).status_code == 403

    rows = _log_rows()
    assert len(rows) == 4, f"거부 4회인데 이력 {len(rows)}행: {rows}"
    assert {r["status"] for r in rows} == {"denied"}, rows

    # 인증조차 없는 요청은 남길 uid가 없으므로 행을 만들지 않는다.
    assert client.get(BACKUP_URL).status_code == 401
    assert len(_log_rows()) == 4, "무인증 401이 이력을 남김"


def test_ac4_denied_status_check_leaves_no_log_row(client, people, monkeypatch):
    """`GET /backup/status` 의 거부는 기록하지 않는다.

    모든 로그인 사용자가 화면 구성용으로 호출하므로, 여기까지 기록하면
    이력이 오염돼 진짜 침입 시도를 찾을 수 없다.
    """
    _allow_uid(monkeypatch, _uid(people.owner))
    for _ in range(3):
        for who in (people.admin, people.staff, people.other):
            assert client.get(STATUS_URL, headers=_h(who["token"])).status_code == 403
    assert _log_rows() == [], f"status 거부가 이력을 남김: {_log_rows()}"


def test_ac4_actor_is_uid_form_and_carries_no_personal_info(client, people, monkeypatch):
    """모든 이력 행의 actor는 `uid:<번호>` — 사번·이메일·실명이 들어가면 안 된다."""
    _allow_uid(monkeypatch, _uid(people.owner))
    _full_backup(client, people.owner)
    assert client.get(BACKUP_URL, headers=_h(people.staff["token"])).status_code == 403

    rows = _log_rows()
    assert len(rows) == 2, rows
    pii = ([p.name for p in (OWNER, ADMIN, STAFF, OTHER)]
           + [p.empno for p in (OWNER, ADMIN, STAFF, OTHER)]
           + [p.email for p in (OWNER, ADMIN, STAFF, OTHER)])
    for row in rows:
        actor = str(row["actor"])
        assert re.fullmatch(r"uid:\d+", actor), f"actor 형식이 uid:<번호>가 아님: {actor!r}"
        blob = " ".join(str(v) for v in row.values())
        for s in pii:
            assert s not in blob, f"이력 행에 신원 정보({s!r}) 노출: {row}"
    assert rows[0]["actor"] == f"uid:{_uid(people.owner)}"
    assert rows[1]["actor"] == f"uid:{_uid(people.staff)}", "거부 행의 actor가 시도자 uid가 아님"


# ============================ 수용 기준 5 — 상태 계산(KST) ============================

def test_ac5_no_history_is_critical(client, people, monkeypatch):
    """이력 0건 → critical."""
    _allow_uid(monkeypatch, _uid(people.owner))
    _set_last_backup(None)
    body = _status(client, people.owner)
    assert body["level"] == "critical", f"이력 0건인데 {body}"
    assert body["last_backup_at"] in (None, ""), f"이력 0건인데 시각이 있음: {body}"


@pytest.mark.parametrize(("days", "level"), [
    (0, "ok"), (1, "ok"), (29, "ok"),
    (30, "warn"), (31, "warn"), (44, "warn"),
    (45, "critical"), (60, "critical"),
])
def test_ac5_level_thresholds_by_kst_elapsed_days(client, people, monkeypatch, days, level):
    """KST 경과 29일→ok / 30일→warn / 45일→critical."""
    _allow_uid(monkeypatch, _uid(people.owner))
    _set_last_backup(_kst_days_ago(days))
    body = _status(client, people.owner)
    assert body["days_since"] == days, f"경과일 계산 오류: {body} (기대 {days}일)"
    assert body["level"] == level, f"{days}일인데 level={body['level']} (기대 {level})"


@pytest.mark.parametrize(("days", "level"), [(29, "ok"), (30, "warn"), (45, "critical")])
def test_ac5_boundary_uses_kst_not_utc(client, people, monkeypatch, days, level):
    """UTC 달력으로 세면 다른 값이 나오는 경계 시각에서도 KST 결과가 나온다."""
    _allow_uid(monkeypatch, _uid(people.owner))
    at = _kst_days_ago(days, flip_utc_date=True)
    utc_days = _utc_date_diff(at)
    assert utc_days != days, (
        "테스트 자기검증 실패: UTC/KST가 갈리는 시각을 못 만듦 "
        f"(KST {days}일, UTC {utc_days}일, 대상 {at.isoformat()})")

    _set_last_backup(at)
    body = _status(client, people.owner)
    assert body["days_since"] == days, (
        f"KST가 아니라 UTC로 계산됨: 응답 {body['days_since']}일, "
        f"KST {days}일 / UTC {utc_days}일 (대상 {at.isoformat()})")
    assert body["level"] == level, f"{body} (KST {days}일 → 기대 {level})"


def test_ac5_status_is_ok_only_after_confirm(client, people, monkeypatch):
    """내려받다 끊긴 백업(pending)은 세지 않는다 — 경고가 계속 떠야 한다."""
    _allow_uid(monkeypatch, _uid(people.owner))
    _set_last_backup(_kst_days_ago(60))
    assert _status(client, people.owner)["level"] == "critical"

    # 1) 내려받기만 하고 확정하지 않음 = 파일이 도착했다는 증거가 없다.
    r, bid = _download(client, people.owner)
    body = _status(client, people.owner)
    assert body["days_since"] == 60, f"pending을 성공으로 셈: {body}"
    assert body["level"] == "critical", f"pending인데 경고가 꺼짐: {body}"

    # 2) 확정하면 그때 경고가 사라진다.
    assert _confirm(client, people.owner, bid, len(r.content)).status_code == 200
    body = _status(client, people.owner)
    assert body["days_since"] == 0, f"확정 직후인데 {body}"
    assert body["level"] == "ok", f"확정 직후인데 {body}"
    assert body["last_backup_at"], "확정 직후인데 last_backup_at이 비어 있음"


@pytest.mark.parametrize("status", ["pending", "fail", "denied"])
def test_ac5_level_counts_ok_rows_only(client, people, monkeypatch, status):
    """`ok`가 아닌 행(pending·fail·denied)은 '마지막 백업'으로 세지 않는다."""
    _allow_uid(monkeypatch, _uid(people.owner))
    _set_last_backup(_kst_days_ago(60))          # 마지막 진짜 성공은 60일 전
    _insert_log_row(_kst_days_ago(0), status=status, uid=_uid(people.owner))

    body = _status(client, people.owner)
    assert body["days_since"] == 60, f"{status} 행을 성공으로 셈: {body}"
    assert body["level"] == "critical", f"{status} 행 때문에 경고가 꺼짐: {body}"


@pytest.mark.parametrize("days", [-1, -30, -3650])
def test_ac5_level_for_negative_days_is_critical(days):
    """시계 되돌림 등으로 경과일이 음수가 되면 경고를 끄지 말고 critical.

    '미래에 백업했다'는 값은 신뢰할 수 없다 — 조용히 ok가 되면 백업이 없는데도
    경고가 사라진다.
    """
    from app.backup import level_for

    assert level_for(days) == "critical", f"level_for({days}) = {level_for(days)!r}"


def test_ac5_level_for_boundaries_and_unknown():
    """level_for 단위 경계 — 29/30/44/45, 이력 없음(None)."""
    from app.backup import level_for

    assert level_for(None) == "critical", "이력 없음(None)은 critical"
    assert level_for(0) == "ok"
    assert level_for(29) == "ok"
    assert level_for(30) == "warn"
    assert level_for(44) == "warn"
    assert level_for(45) == "critical"
    assert level_for(10000) == "critical"


# ==================== 무결성·안정성 (보완 지시 + 수용 기준 3) ====================

CORRUPT_MARKER = "손상표식-품질부-" + "표" * 200


def _corrupt_db(client, user) -> None:
    """운영 DB의 **일부 데이터 페이지만** 실제로 손상시킨다(테스트 픽스처).

    전체를 덮으면 로그인(users 조회)부터 죽어 백업 경로에 도달하지 못한다.
    그래서 feedback 행에 고유 표식을 넣고 그 표식이 실제로 놓인 페이지만 덮는다.
    → 인증·스키마 조회는 성공하고, 스냅샷/무결성 검사에서 손상이 드러난다.
    """
    conn = _db()
    try:
        conn.executemany(
            "INSERT INTO feedback (ward, from_email, from_name, message, created_at) "
            "VALUES (?,?,?,?,?)",
            [("61", STAFF.email, STAFF.name, f"{CORRUPT_MARKER}{i}",
              datetime.now(timezone.utc).isoformat()) for i in range(300)])
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()

    path = os.environ["DUTY_DB"]
    needle = CORRUPT_MARKER.encode()
    with open(path, "rb") as f:
        raw = f.read()
    pages, start = set(), 0
    while True:
        found = raw.find(needle, start)
        if found < 0:
            break
        pages.add(found // page_size)
        start = found + 1
    pages.discard(0)  # 0번(파일 헤더)은 남겨야 SQLite가 열기는 한다
    assert len(pages) >= 5, f"손상시킬 표식 페이지를 찾지 못함: {sorted(pages)}"

    with open(path, "r+b") as f:
        for page in sorted(pages)[:5]:
            f.seek(page * page_size)
            f.write(b"\xde\xad\xbe\xef" * (page_size // 4))
        f.flush()
        os.fsync(f.fileno())


def test_integrity_corrupt_db_returns_500_and_logs_fail(client, people, monkeypatch, env):
    """DB가 손상돼 있으면 200+ZIP이 아니라 **500 + status='fail'**.

    검사 없이 200으로 내려주면 "백업했다"는 기록·화면만 남고 복구는 불가능해진다.
    """
    _allow_uid(monkeypatch, _uid(people.owner))
    # 손상 전에 성공 1회를 만들어 스키마·이력 형태를 확정해 둔다.
    _full_backup(client, people.owner)
    ok_rows_before = len(_log_rows())
    before_tmp, before_db = _files_under(env.tmp_dir), _files_under(env.db_dir)

    _corrupt_db(client, people.owner)
    # 자기검증: 손상은 실재하지만 인증 경로(users)는 살아 있어야 한다.
    probe = sqlite3.connect(os.environ["DUTY_DB"])
    try:
        assert probe.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
        with pytest.raises(sqlite3.DatabaseError):
            probe.execute("PRAGMA quick_check").fetchone()
    finally:
        probe.close()

    r = client.get(BACKUP_URL, headers=_h(people.owner["token"]))
    assert r.status_code == 500, f"손상된 DB인데 {r.status_code} (본문 {r.content[:8]!r})"
    assert r.content[:2] != b"PK", "손상된 DB인데 ZIP을 내려줌"

    rows = _log_rows()
    assert len(rows) == ok_rows_before + 1, f"손상 요청 이력이 1행이 아님: {rows}"
    assert rows[-1]["status"] == "fail", f"손상 실패가 fail로 기록되지 않음: {rows[-1]}"
    _assert_no_leftovers(env, before_tmp, before_db, where="(손상 경로)")


class _NoVacuumIntoConn(sqlite3.Connection):
    """VACUUM INTO가 없는 SQLite 빌드를 흉내 내 **폴백 경로**로 몰아넣는 연결."""

    def execute(self, sql, *args, **kwargs):
        if str(sql).strip().upper().startswith("VACUUM INTO"):
            raise sqlite3.OperationalError('near "INTO": syntax error')
        return super().execute(sql, *args, **kwargs)


class _NoVacuumIntoSqlite:
    def __getattr__(self, name):
        return getattr(sqlite3, name)

    def connect(self, *args, **kwargs):
        kwargs.setdefault("factory", _NoVacuumIntoConn)
        return sqlite3.connect(*args, **kwargs)


def _corrupt_index_pages(count: int = 8) -> None:
    """인덱스 페이지만 손상시킨다 — 표 스캔(CSV 생성)으로는 드러나지 않는 손상.

    폴백(`Connection.backup()`)은 페이지를 그대로 복사하므로 이 손상이 사본에
    그대로 남는다. quick_check가 없으면 200+ZIP으로 나가버린다.
    """
    conn = _db()
    try:
        conn.executemany(
            "INSERT INTO users (email, empno, name, ward, role, pw_hash, salt, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(f"qa{i:05d}@duty.kr", f"99{i:05d}", "가명", "61", "staff",
              "h" * 40, "s" * 16, datetime.now(timezone.utc).isoformat())
             for i in range(3000)])
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()

    path = os.environ["DUTY_DB"]
    with open(path, "rb") as f:
        raw = f.read()
    # SQLite 페이지 첫 바이트: 0x0A = 인덱스 리프(표 리프는 0x0D).
    victims = [p for p in range(1, len(raw) // page_size) if raw[p * page_size] == 0x0A]
    assert len(victims) >= count, f"손상시킬 인덱스 페이지가 부족함: {len(victims)}개"
    with open(path, "r+b") as f:
        for page in victims[:count]:
            f.seek(page * page_size)
            f.write(b"\xde\xad\xbe\xef" * (page_size // 4))
        f.flush()
        os.fsync(f.fileno())


def test_integrity_quick_check_blocks_corrupt_snapshot_on_fallback(
        client, people, monkeypatch, env):
    """폴백 경로(페이지 그대로 복사)에서도 손상본을 내려주지 않는다.

    VACUUM INTO가 없는 빌드에서는 손상이 사본에 그대로 복제된다. 표 스캔만으로는
    드러나지 않는 인덱스 손상이라 **스냅샷 후 quick_check**가 유일한 방어선이다.
    """
    import app.backup as backup

    _allow_uid(monkeypatch, _uid(people.owner))
    monkeypatch.setattr(backup, "sqlite3", _NoVacuumIntoSqlite())
    before_tmp, before_db = _files_under(env.tmp_dir), _files_under(env.db_dir)

    _corrupt_index_pages()

    # 자기검증: 인증 경로는 살아 있고, 손상은 quick_check에만 드러난다.
    probe = sqlite3.connect(os.environ["DUTY_DB"])
    try:
        assert probe.execute("SELECT name FROM users WHERE id=?",
                             (_uid(people.owner),)).fetchone() is not None
        report = str(probe.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        probe.close()
    assert report.lower() != "ok", "테스트 자기검증 실패: 손상이 만들어지지 않음"

    r = client.get(BACKUP_URL, headers=_h(people.owner["token"]))
    assert r.status_code == 500, f"손상 사본인데 {r.status_code} (본문 {r.content[:8]!r})"
    assert r.content[:2] != b"PK", "무결성 검사 없이 손상 ZIP을 내려줌"
    rows = _log_rows()
    assert rows and rows[-1]["status"] == "fail", f"fail로 기록되지 않음: {rows}"
    _assert_no_leftovers(env, before_tmp, before_db, where="(폴백 손상 경로)")


def test_stability_locked_db_hits_deadline_and_leaves_nothing(client, people, monkeypatch, env):
    """잠긴 DB에서 **무한 대기하지 않는다** — 데드라인 초과 시 실패 + 잔여물 0.

    DB 전체가 배타 잠금이면 인증 조회부터 막혀 HTTP 경로로는 스냅샷까지 가지도
    못한다. 그래서 스냅샷 구간(`_build_zip`)을 직접 호출해 데드라인 동작을 본다.
    데드라인 기본값(지시서 30초)은 상수로 확인하고, 실행 시간을 위해 줄여서 잰다.
    """
    import app.backup as backup

    assert backup.SNAPSHOT_TIMEOUT_SEC == 30, (
        f"데드라인 기본값이 지시서(30초)와 다름: {backup.SNAPSHOT_TIMEOUT_SEC}")
    monkeypatch.setattr(backup, "SNAPSHOT_TIMEOUT_SEC", 2)
    monkeypatch.setattr(backup, "SNAPSHOT_BUSY_TIMEOUT_MS", 500)
    before_tmp, before_db = _files_under(env.tmp_dir), _files_under(env.db_dir)

    locker = sqlite3.connect(os.environ["DUTY_DB"], timeout=5)
    try:
        locker.execute("PRAGMA locking_mode=EXCLUSIVE")
        locker.execute("BEGIN IMMEDIATE")
        locker.execute(
            "INSERT INTO feedback (ward, from_email, from_name, message, created_at) "
            "VALUES (?,?,?,?,?)",
            ("61", STAFF.email, STAFF.name, "잠금유지",
             datetime.now(timezone.utc).isoformat()))

        # 별도 스레드로 돌린다 — 데드라인이 없으면 이 호출은 **영원히 돌아오지
        # 않으므로**, 본 스레드에서 부르면 테스트가 실패하는 대신 멈춰버린다.
        result: dict = {}

        def _run():
            started = time.monotonic()
            try:
                backup._build_zip()
                result["returned"] = "성공(ZIP 생성)"
            except Exception as exc:  # noqa: BLE001 — 실패 원문을 그대로 본다
                result["exc"] = exc
            result["elapsed"] = time.monotonic() - started

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=20)
        alive = worker.is_alive()
    finally:
        locker.rollback()
        locker.close()

    assert not alive, "데드라인 2초인데 20초가 지나도 스냅샷이 끝나지 않음(무한 대기)"
    assert "exc" in result, f"잠긴 DB인데 스냅샷이 {result.get('returned')!r}로 끝남"
    assert result["elapsed"] < 20, f"{result['elapsed']:.1f}초 걸림(무한 대기 위험)"
    assert result["elapsed"] >= 2, \
        f"데드라인 전에 끝남({result['elapsed']:.1f}초) — 다른 이유로 실패한 듯"
    assert "초 안에" in str(result["exc"]), f"시간초과가 아닌 다른 실패: {result['exc']!r}"
    _assert_no_leftovers(env, before_tmp, before_db, where="(타임아웃 경로)")


def test_stability_locked_db_request_never_reports_success(client, people, monkeypatch, env):
    """잠금으로 스냅샷이 실패하면 HTTP 응답도 성공이 아니고 이력도 ok가 아니다."""
    import app.backup as backup

    monkeypatch.setattr(backup, "SNAPSHOT_TIMEOUT_SEC", 2)
    monkeypatch.setattr(backup, "SNAPSHOT_BUSY_TIMEOUT_MS", 500)
    _allow_uid(monkeypatch, _uid(people.owner))
    before_tmp, before_db = _files_under(env.tmp_dir), _files_under(env.db_dir)

    real_snapshot = backup._snapshot

    def _locked_snapshot(dest_path):
        """스냅샷 시점에만 DB를 배타 잠금한다(인증은 통과시키기 위해)."""
        locker = sqlite3.connect(os.environ["DUTY_DB"], timeout=5)
        try:
            locker.execute("PRAGMA locking_mode=EXCLUSIVE")
            locker.execute("BEGIN IMMEDIATE")
            locker.execute(
                "INSERT INTO feedback (ward, from_email, from_name, message, created_at) "
                "VALUES (?,?,?,?,?)",
                ("61", STAFF.email, STAFF.name, "잠금유지",
                 datetime.now(timezone.utc).isoformat()))
            return real_snapshot(dest_path)
        finally:
            locker.rollback()
            locker.close()

    monkeypatch.setattr(backup, "_snapshot", _locked_snapshot)

    started = time.monotonic()
    r = client.get(BACKUP_URL, headers=_h(people.owner["token"]))
    elapsed = time.monotonic() - started

    assert r.status_code == 500, f"잠긴 DB인데 {r.status_code} (본문 {r.content[:8]!r})"
    assert r.content[:2] != b"PK", "잠긴 상태에서 ZIP이 나옴"
    assert elapsed < 25, f"데드라인 2초인데 {elapsed:.1f}초 걸림"
    rows = _log_rows()
    assert rows and rows[-1]["status"] == "fail", f"타임아웃이 fail로 기록되지 않음: {rows}"
    assert not any(row["status"] == "ok" for row in rows), f"실패인데 ok 행이 있음: {rows}"
    _assert_no_leftovers(env, before_tmp, before_db, where="(타임아웃 HTTP 경로)")
