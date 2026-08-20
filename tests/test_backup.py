"""관리자용 데이터 백업 내려받기 + 미백업 경고
(지시서 docs/orders/2026-08-20-백업-내려받기.md, T3).

지시서의 **수용 기준 1~5·7**에서 설계한 서버 테스트다. 구현(app/backup.py)을 읽고
역산하지 않았다 — 픽스처/헬퍼 관례만 tests/test_storage.py·tests/test_wanted_blocking.py
에서 가져왔다.

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
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

KST = timezone(timedelta(hours=9))

BACKUP_URL = "/api/admin/backup"
STATUS_URL = "/api/admin/backup/status"

# 지시서 "산출물 구성": CSV는 7개 = 앱 테이블 전량에서 backup_log 제외
#   (backup_log는 복구 정본 duty.db 안에 들어 있으므로 손실 없음 — 사무국 확정 해석)
EXPECTED_CSV_TABLES = {
    "users", "ward_invites", "rosters", "schedules",
    "wanted_requests", "request_windows", "feedback",
}
BOM = b"\xef\xbb\xbf"


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
    monkeypatch.delenv("DUTY_BACKUP_OWNER", raising=False)
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


def _allow(monkeypatch, value):
    monkeypatch.setenv("DUTY_BACKUP_OWNER", value)


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


def _set_last_backup(dt: datetime | None):
    """backup_log를 원하는 '마지막 성공 시각' 하나만 남긴 상태로 만든다.

    저장은 UTC ISO — 저장소 관례(app.storage._now)와 동일(교훈 L-4).
    """
    conn = _db()
    try:
        conn.execute("DELETE FROM backup_log")
        if dt is not None:
            conn.execute(
                "INSERT INTO backup_log (actor, ward, created_at, byte_size, status) "
                "VALUES (?,?,?,?,?)",
                (OWNER.empno, OWNER.ward, dt.astimezone(timezone.utc).isoformat(), 4096, "ok"))
        conn.commit()
    finally:
        conn.close()


def _status(client, user):
    r = client.get(STATUS_URL, headers=_h(user["token"]))
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    body = r.json()
    for key in ("last_backup_at", "days_since", "level"):
        assert key in body, f"상태 응답에 {key} 없음: {body}"
    return body


# ============================ 수용 기준 1 — 권한 경계 ============================

def test_ac1_allowlisted_master_gets_zip(client, people, monkeypatch):
    """허가 계정(allowlist ∧ master) → 200 + ZIP 첨부."""
    _allow(monkeypatch, OWNER.email)
    r = client.get(BACKUP_URL, headers=_h(people.owner["token"]))
    assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
    assert r.content[:2] == b"PK", "본문이 ZIP이 아님"
    disp = r.headers.get("content-disposition", "")
    assert ".zip" in disp.lower(), f"첨부 파일명(.zip)이 없음: {disp!r}"


def test_ac1_master_of_other_ward_denied(client, people, monkeypatch):
    """allowlist에 없는 master(= 다른 병동 개설자)는 403 — 역할만으로 열리면 안 된다."""
    _allow(monkeypatch, OWNER.email)
    r = client.get(BACKUP_URL, headers=_h(people.other["token"]))
    assert r.status_code == 403, f"타 병동 master가 백업을 받음: {r.status_code}"


def test_ac1_admin_and_staff_denied_even_if_allowlisted(client, people, monkeypatch):
    """admin·staff는 allowlist에 있어도 403 — allowlist ∧ role==master 동시 충족."""
    _allow(monkeypatch, f"{ADMIN.email},{STAFF.email},{OWNER.email}")
    assert client.get(BACKUP_URL, headers=_h(people.admin["token"])).status_code == 403
    assert client.get(BACKUP_URL, headers=_h(people.staff["token"])).status_code == 403
    # 대조군: 같은 설정에서 master는 통과해야 판정이 '전부 막기'가 아님이 증명된다.
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200


def test_ac1_unauthenticated_is_401(client, people, monkeypatch):
    _allow(monkeypatch, OWNER.email)
    assert client.get(BACKUP_URL).status_code == 401
    assert client.get(BACKUP_URL, headers={"Authorization": "Bearer not-a-real-token"}
                      ).status_code == 401
    assert client.get(STATUS_URL).status_code == 401


def test_ac1_unset_env_denies_everyone(client, people, monkeypatch):
    """DUTY_BACKUP_OWNER 미설정·빈 값이면 전원 403(기본 개방 금지)."""
    monkeypatch.delenv("DUTY_BACKUP_OWNER", raising=False)
    for who in (people.owner, people.admin, people.staff, people.other):
        assert client.get(BACKUP_URL, headers=_h(who["token"])).status_code == 403
    _allow(monkeypatch, "")
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 403
    _allow(monkeypatch, "   ")
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 403


def test_ac1_allowlist_accepts_empno_and_comma_list(client, monkeypatch):
    """allowlist 값은 사번도 되고, 콤마로 여러 개도 된다."""
    master = _reg(client, empno=OWNER.empno, name=OWNER.name, ward=OWNER.ward)
    assert master["role"] == "master"
    _allow(monkeypatch, f"{OTHER.email},{OWNER.empno}")
    assert client.get(BACKUP_URL, headers=_h(master["token"])).status_code == 200
    # 목록에서 빼면 즉시 막힌다(재시작 없이 환경변수만으로 통제 가능해야 함).
    _allow(monkeypatch, OTHER.email)
    assert client.get(BACKUP_URL, headers=_h(master["token"])).status_code == 403


def test_ac1_allowlist_tolerates_whitespace(client, people, monkeypatch):
    """콤마 구분 목록에 공백이 섞여도 동작한다(배포 환경변수 실수 방지)."""
    _allow(monkeypatch, f" {OTHER.email} , {OWNER.email} ")
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200


def test_ac1_denial_body_has_no_personal_info(client, people, monkeypatch):
    """거부 응답 본문에 실명·사번·이메일이 실리지 않는다."""
    _allow(monkeypatch, OWNER.email)
    secrets = [p.name for p in (OWNER, ADMIN, STAFF, OTHER)]
    secrets += [p.empno for p in (OWNER, ADMIN, STAFF, OTHER)]
    secrets += [p.email for p in (OWNER, ADMIN, STAFF, OTHER)]
    responses = [
        client.get(BACKUP_URL),
        client.get(BACKUP_URL, headers=_h(people.staff["token"])),
        client.get(BACKUP_URL, headers=_h(people.admin["token"])),
        client.get(BACKUP_URL, headers=_h(people.other["token"])),
        client.get(STATUS_URL, headers=_h(people.staff["token"])),
    ]
    for r in responses:
        assert r.status_code in (401, 403), r.status_code
        text = r.text
        for s in secrets:
            assert s not in text, f"거부 본문에 신원 정보 노출({s!r}): {text[:300]}"


def test_ac1_status_endpoint_restricted_to_owner(client, people, monkeypatch):
    """상태 조회도 허가 계정 전용 — staff·admin·타병동 master에게 새어 나가면 안 된다."""
    _allow(monkeypatch, OWNER.email)
    assert client.get(STATUS_URL, headers=_h(people.owner["token"])).status_code == 200
    for who in (people.admin, people.staff, people.other):
        assert client.get(STATUS_URL, headers=_h(who["token"])).status_code == 403


# ============================ 수용 기준 2 — 일관성 ============================

def test_ac2_row_committed_before_backup_is_in_db_and_csv(client, people, monkeypatch, tmp_path):
    """백업 직전 커밋된 행이 ZIP의 duty.db와 해당 CSV **양쪽**에 있다."""
    _allow(monkeypatch, OWNER.email)
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
    _allow(monkeypatch, OWNER.email)
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
    _allow(monkeypatch, OWNER.email)
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
    _allow(monkeypatch, OWNER.email)
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
    _allow(monkeypatch, OWNER.email)
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


def test_ac3_no_temp_files_left_after_request(client, people, monkeypatch, env):
    """요청 종료 후 서버에 백업 임시 파일 잔존 0(개인정보 잔여 금지)."""
    _allow(monkeypatch, OWNER.email)

    def snapshot(root):
        out = set()
        for dirpath, _dirnames, filenames in os.walk(root):
            for f in filenames:
                out.add(os.path.join(dirpath, f))
        return out

    before_tmp = snapshot(env.tmp_dir)
    before_db = snapshot(env.db_dir)

    for _ in range(3):  # 반복해도 쌓이지 않아야 한다
        assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200

    leftover_tmp = snapshot(env.tmp_dir) - before_tmp
    assert not leftover_tmp, f"임시 디렉터리에 백업 잔여물: {sorted(leftover_tmp)}"

    allowed = {os.environ["DUTY_DB"] + suffix for suffix in ("", "-wal", "-shm", "-journal")}
    leftover_db = {p for p in snapshot(env.db_dir) - before_db if p not in allowed}
    assert not leftover_db, f"DB 디렉터리에 백업 잔여물: {sorted(leftover_db)}"


# ============================ 수용 기준 4 — 이력 ============================

def test_ac4_one_log_row_per_successful_backup(client, people, monkeypatch):
    """성공 1회당 backup_log 1행(status='ok', byte_size>0)."""
    _allow(monkeypatch, OWNER.email)
    assert _log_rows() == [], "백업 전인데 이력이 있음"

    r1 = client.get(BACKUP_URL, headers=_h(people.owner["token"]))
    assert r1.status_code == 200
    rows = _log_rows()
    assert len(rows) == 1, f"성공 1회에 이력 {len(rows)}행: {rows}"
    row = rows[0]
    assert row["status"] == "ok", f"status가 'ok'가 아님: {row}"
    assert row["byte_size"] > 0, f"byte_size가 0 이하: {row}"
    assert (row["actor"] or "").strip(), f"actor가 비어 있음: {row}"
    parsed = datetime.fromisoformat(str(row["created_at"]))
    assert parsed.tzinfo is not None, f"created_at에 시간대가 없음: {row['created_at']}"
    assert parsed.utcoffset() == timedelta(0), \
        f"created_at이 UTC 저장이 아님(교훈 L-4): {row['created_at']}"

    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200
    assert len(_log_rows()) == 2, "성공 2회인데 이력이 2행이 아님"


def test_ac4_denied_requests_leave_no_log_row(client, people, monkeypatch):
    """403·401 요청은 backup_log에 행을 남기지 않는다."""
    _allow(monkeypatch, OWNER.email)
    for who in (people.admin, people.staff, people.other):
        assert client.get(BACKUP_URL, headers=_h(who["token"])).status_code == 403
    assert client.get(BACKUP_URL).status_code == 401
    monkeypatch.delenv("DUTY_BACKUP_OWNER", raising=False)
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 403
    assert _log_rows() == [], f"거부 요청이 이력을 남김: {_log_rows()}"


# ============================ 수용 기준 5 — 상태 계산(KST) ============================

def test_ac5_no_history_is_critical(client, people, monkeypatch):
    """이력 0건 → critical."""
    _allow(monkeypatch, OWNER.email)
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
    _allow(monkeypatch, OWNER.email)
    _set_last_backup(_kst_days_ago(days))
    body = _status(client, people.owner)
    assert body["days_since"] == days, f"경과일 계산 오류: {body} (기대 {days}일)"
    assert body["level"] == level, f"{days}일인데 level={body['level']} (기대 {level})"


@pytest.mark.parametrize(("days", "level"), [(29, "ok"), (30, "warn"), (45, "critical")])
def test_ac5_boundary_uses_kst_not_utc(client, people, monkeypatch, days, level):
    """UTC 달력으로 세면 다른 값이 나오는 경계 시각에서도 KST 결과가 나온다."""
    _allow(monkeypatch, OWNER.email)
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


def test_ac5_status_is_ok_right_after_a_real_backup(client, people, monkeypatch):
    """실제 백업 성공 직후에는 경과 0일·ok — 경고가 즉시 사라져야 한다(수용 기준 6의 서버 측)."""
    _allow(monkeypatch, OWNER.email)
    _set_last_backup(_kst_days_ago(60))
    assert _status(client, people.owner)["level"] == "critical"
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200
    body = _status(client, people.owner)
    assert body["days_since"] == 0, f"백업 직후인데 {body}"
    assert body["level"] == "ok", f"백업 직후인데 {body}"
    assert body["last_backup_at"], "백업 직후인데 last_backup_at이 비어 있음"
