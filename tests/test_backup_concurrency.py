"""백업 권한 코드(claim/revoke)의 **동시성** 검증 — 게이트의 사각지대(품질부 4차).

왜 별도 모듈인가
----------------
기존 test_backup.py 는 전부 **동기 TestClient**(요청을 한 줄로 세워 보낸다)라,
검수부 ①이 재현한 **동시 요청**(TOCTOU + lost update)을 전혀 밟지 못한다. 순차
테스트가 초록이어도 실제 잠금·상한이 동시 버스트에서 뚫리면 게이트는 무력하다.
이 모듈은 **진짜 병렬 요청**으로 그 사각지대를 메운다.

방법(운으로 통과하지 않게)
--------------------------
- 실제 uvicorn 서버를 띄우고(동기 라우트는 Starlette 스레드풀에서 **겹쳐** 실행돼
  각자 자기 SQLite 연결로 BEGIN IMMEDIATE 락을 경합한다), ThreadPoolExecutor로
  버스트를 쏜다. TestClient 내부 직렬화에 기대지 않는다.
- 기대값은 **직렬화가 보장하는 결정적 수**다(운이 아니라 불변식):
  한 계정은 최대 5회만 코드 대조에 도달하고(6번째부터 429), claim_fails는 정확히 5.
  전역 실패 합계는 정확히 30에서 멈춘다. 이 수가 어긋나면(과다·과소·유출) 결함이다.

수용 기준 출처: 지시서 D-19/Q-3(계정 단위 잠금·전역 상한 30) + 품질부 4차 지시 A.
기대값은 전부 기준에서 왔고, 구현에서는 엔드포인트·환경변수 계약만 확인했다.

가명·가짜 사번(99…)만 사용한다(교훈 L-1). 권한 코드는 테스트가 생성한 임시값이다.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import socket
import sqlite3
import subprocess
import sys
import secrets
import threading
import time

import httpx
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CLAIM_URL = "/api/admin/backup/claim"
REVOKE_URL = "/api/admin/backup/revoke"
BACKUP_URL = "/api/admin/backup"
REGISTER_URL = "/api/auth/register"

# run_claim_transaction 의 불변식 파라미터(구현과 같은 값 — 계약 확인용).
PER_ACCOUNT_MAX = 5      # CLAIM_MAX_FAILS
GLOBAL_MAX = 30          # CLAIM_GLOBAL_MAX_FAILS
BAD_MSG = "권한 코드가 올바르지 않습니다."   # CLAIM_BAD_CODE_MSG


# ----------------------------------------------------------------------------
# 서버 하니스 — 8961번대 포트, 스크래치패드 venv(sys.executable)로 실제 기동
# ----------------------------------------------------------------------------

def _free_port(lo: int = 8961, hi: int = 8998) -> int:
    for p in range(lo, hi + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("8961번대에 빈 포트가 없습니다")


def _new_code(length: int = 24) -> str:
    raw = secrets.token_urlsafe(length * 2).replace("-", "x").replace("_", "y")
    return raw[:length]


class Server:
    def __init__(self, base_url: str, db_path: str, code: str, proc):
        self.base_url = base_url
        self.db_path = db_path
        self.code = code
        self._proc = proc

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def uid_of(self, ward: str, name: str = "가명운영자") -> int:
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT id FROM users WHERE ward=? AND name=?", (ward, name)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, f"계정을 찾지 못함: {ward}/{name}"
        return int(row[0])

    def flag_of(self, ward: str, name: str = "가명운영자") -> int:
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT backup_owner FROM users WHERE ward=? AND name=?",
                (ward, name)).fetchone()
        finally:
            conn.close()
        return int(row[0])

    def fails_of(self, ward: str, name: str = "가명운영자") -> int:
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT claim_fails FROM users WHERE ward=? AND name=?",
                (ward, name)).fetchone()
        finally:
            conn.close()
        return int(row[0])

    def live_fail_sum(self) -> int:
        """지금 잠금 창이 살아 있는 계정들의 claim_fails 합계 — 전역 상한의 관측값."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(claim_fails),0) AS n FROM users "
                "WHERE claim_locked_until IS NOT NULL AND claim_locked_until > ?",
                (now,)).fetchone()
        finally:
            conn.close()
        return int(row["n"])

    def register_master(self, ward: str, empno: str, name: str = "가명운영자") -> str:
        """새 병동을 열어 그 병동 master가 되는 계정 → 토큰 반환."""
        with httpx.Client(base_url=self.base_url, timeout=30) as c:
            r = c.post(REGISTER_URL, json={
                "empno": empno, "password": "password123",
                "name": name, "ward": ward})
        assert r.status_code == 200, f"가입 실패: {r.status_code} {r.text[:200]}"
        body = r.json()
        assert body["role"] == "master", f"새 병동 개설자가 master가 아님: {body}"
        return body["token"]


def _wait_ready(base_url: str, proc, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"서버가 조기 종료됨(exit={proc.returncode})")
        try:
            r = httpx.get(base_url + "/api/health", timeout=2)
            if r.status_code < 500:
                return
        except Exception as exc:  # 아직 안 뜸
            last = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"서버가 {timeout}s 안에 준비되지 않음: {last}")


@pytest.fixture()
def server(tmp_path):
    """격리 DB + 유효 권한 코드로 실제 uvicorn 서버를 띄운다(테스트마다 새로)."""
    db_path = str(tmp_path / "conc.db")
    code = _new_code()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "DUTY_DB": db_path,
        "DUTY_SECRET": "conc-test-secret-000000000000",
        "DUTY_BACKUP_CLAIM_CODE": code,
        "TMPDIR": str(tmp_path),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        # /api/health 가 없으면 아래가 실패하므로, 없는 경우 404<500 로 준비 판정된다.
        _wait_ready(base_url, proc)
        yield Server(base_url, db_path, code, proc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# ----------------------------------------------------------------------------
# 버스트 헬퍼 — 각 스레드가 자기 httpx.Client 로 요청(클라이언트 공유 경합 배제)
# ----------------------------------------------------------------------------

def _post(base_url: str, url: str, token: str, code: str) -> tuple[int, str]:
    with httpx.Client(base_url=base_url, timeout=30) as c:
        r = c.post(url, json={"code": code},
                   headers={"Authorization": f"Bearer {token}"})
    return r.status_code, _detail(r)


def _detail(r) -> str:
    try:
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json().get("detail", "")
    except Exception:
        pass
    return ""


def _burst(base_url: str, url: str, jobs: list[tuple[str, str]],
           workers: int | None = None) -> list[tuple[int, str]]:
    """jobs = [(token, code), ...] 를 **동시에** 쏜다 → [(status, detail), ...].

    barrier로 모든 워커가 클라이언트·TCP 연결을 미리 만든 뒤 **같은 순간에** send
    하게 한다 — 요청별 연결 오버헤드가 도착을 어긋나게 해서 경합 창을 놓치는 것을
    막는다(정상 구현은 직렬화라 언제 쏘든 결정적이므로, 조여도 기대값은 안 바뀐다).
    """
    n = len(jobs)
    workers = workers or n
    barrier = threading.Barrier(n)
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def _job(token: str, code: str) -> None:
        # 연결을 먼저 열어(핸드셰이크·keep-alive) 배리어 뒤 send만 남긴다.
        with httpx.Client(base_url=base_url, timeout=30) as c:
            try:
                c.get("/api/health")  # 커넥션 워밍업(응답 코드는 무시)
            except Exception:
                pass
            barrier.wait()
            r = c.post(url, json={"code": code},
                       headers={"Authorization": f"Bearer {token}"})
        with lock:
            results.append((r.status_code, _detail(r)))

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_job, tok, code) for tok, code in jobs]
        for f in cf.as_completed(futs):
            f.result()
    return results


# ============================================================================
# A-1. 계정당 상한 — 한 계정에 틀린 코드 50건 동시
# ============================================================================

def test_concurrent_wrong_claims_capped_at_five_per_account(server):
    """틀린 코드 50건을 **동시에** 한 계정에 → 코드 대조 도달은 정확히 5회,
    나머지는 잠금(429). claim_fails 정확히 5(과다·과소 아님), 권한 미부여.

    직렬화(BEGIN IMMEDIATE)+원자적 증가가 없으면 다수가 잠금 전에 가드를 통과해
    403(코드 불일치)이 5건을 넘고 claim_fails가 5에서 어긋난다(검수부 ① 재현).
    """
    tok = server.register_master(ward="61", empno="990001")
    N = 50
    res = _burst(server.base_url, CLAIM_URL, [(tok, _new_code()) for _ in range(N)])

    bad = [s for s, _ in res if s == 403]
    locked = [s for s, _ in res if s == 429]
    granted = [s for s, _ in res if s == 200]

    assert granted == [], f"틀린 코드 버스트에서 권한이 부여됨: {res}"
    assert server.flag_of("61") == 0, "권한 플래그가 켜졌다"
    # 코드 대조에 도달한 요청(=403 bad code)은 잠금 전 5회뿐이어야 한다.
    assert len(bad) == PER_ACCOUNT_MAX, (
        f"코드 대조 도달이 {len(bad)}회 — 5회여야 함(직렬화 실패로 가드 누수): {res}")
    assert len(locked) == N - PER_ACCOUNT_MAX, (
        f"잠금(429)이 {len(locked)}건 — {N-PER_ACCOUNT_MAX}건이어야 함: {res}")
    # 카운터가 정확히 5(원자적 증가의 lost update가 없어야 함).
    assert server.fails_of("61") == PER_ACCOUNT_MAX, (
        f"claim_fails={server.fails_of('61')} — 정확히 5여야 함(과다/과소 = lost update)")


# ============================================================================
# A-2. 정답 코드가 잠금을 못 넘는다 — 잠긴 계정에 정답을 버스트로 섞어도 429
# ============================================================================

def test_correct_code_cannot_beat_lock_under_burst(server):
    """계정을 먼저 잠근 뒤(5회 실패) 정답 코드를 **동시 버스트로 여러 개** 섞어도
    200/권한부여가 없어야 한다(전부 429). TOCTOU로 정답이 잠금을 앞질러선 안 된다.
    """
    tok = server.register_master(ward="61", empno="990001")
    # 잠글 때까지 순차로 5회 실패(결정적으로 잠금 상태를 만든다).
    for _ in range(PER_ACCOUNT_MAX):
        _post(server.base_url, CLAIM_URL, tok, _new_code())
    assert server.fails_of("61") == PER_ACCOUNT_MAX
    # 이제 정답 코드 20개를 동시에 — 잠금 창이 살아 있으므로 전부 429여야 한다.
    res = _burst(server.base_url, CLAIM_URL, [(tok, server.code) for _ in range(20)])
    assert all(s == 429 for s, _ in res), (
        f"잠긴 계정에 정답 코드가 통과함(TOCTOU): {sorted({s for s,_ in res})} {res[:5]}")
    assert server.flag_of("61") == 0, "잠금 중인데 정답 코드로 권한이 켜졌다"


# ============================================================================
# A-3. 전역 상한 — 여러 계정 동시 버스트로 실패 합계가 정확히 30에서 정지
# ============================================================================

def test_global_cap_stops_exactly_at_thirty_under_burst(server):
    """여러 계정이 동시에 틀린 코드를 퍼부어도 전역 실패 합계는 **정확히 30**에서
    멈추고(각 계정 ≤5), 그 뒤 **한 번도 안 틀린 새 계정의 정답 코드조차 429**.

    직렬화가 없으면 전역 SUM 판정이 lost update로 새어 30을 넘기거나, 정답이
    상한 중에 통과한다(검수부 ①: 정답을 버스트에 섞어 전역 30을 뚫음).
    """
    # 10개 병동 master × 각 8건 틀림 = 80 동시 요청 → 최소 30 증가분 확보.
    tokens = [server.register_master(ward=f"7{i}", empno=f"9911{i:02d}")
              for i in range(10)]
    jobs = [(tok, _new_code()) for tok in tokens for _ in range(8)]
    res = _burst(server.base_url, CLAIM_URL, jobs)

    assert all(s in (403, 429) for s, _ in res), f"예상 밖 응답: {sorted({s for s,_ in res})}"
    # 어떤 계정도 5를 넘지 않는다.
    for i in range(10):
        f = server.fails_of(f"7{i}")
        assert f <= PER_ACCOUNT_MAX, f"7{i} 계정 claim_fails={f} > 5 (per-account 누수)"
    # 살아 있는 잠금 계정들의 실패 합계는 정확히 상한 30 (초과 통과 없음).
    assert server.live_fail_sum() == GLOBAL_MAX, (
        f"전역 실패 합계={server.live_fail_sum()} — 정확히 30이어야 함(초과=lost update)")
    # 상한 도달 상태에서 완전히 새 계정의 **정답 코드**도 막힌다(429).
    victim = server.register_master(ward="88", empno="991288")
    s, _ = _post(server.base_url, CLAIM_URL, victim, server.code)
    assert s == 429, f"전역 상한 30인데 새 계정 정답 코드가 통과: {s}"
    assert server.flag_of("88") == 0, "전역 잠금 중인데 권한이 켜졌다"


# ============================================================================
# A-4. revoke 도 같은 경로 — 동시 revoke 버스트에서도 상한·직렬화 유지
# ============================================================================

def test_concurrent_revoke_burst_respects_lock(server):
    """revoke 는 claim 과 같은 문(코드·마스터·잠금·직렬화)을 쓴다. 틀린 코드
    revoke 50건 동시 → 코드 대조 도달 5회·나머지 429, 플래그는 안 꺼진다.
    이어 정답 revoke 를 동시에 섞어도 잠금 창이 살아 있으면 429(회수 안 됨).
    """
    # 두 병동 master 에 권한을 정당히 부여해 둔다(회수 대상 존재).
    owner = server.register_master(ward="61", empno="990001")
    other = server.register_master(ward="99", empno="990004")
    for tok in (owner, other):
        s, _ = _post(server.base_url, CLAIM_URL, tok, server.code)
        assert s == 200, f"사전 권한 부여 실패: {s}"
    assert server.flag_of("61") == 1 and server.flag_of("99") == 1

    # 틀린 코드 revoke 50건 동시 — owner 계정 축으로 잠금이 걸린다.
    res = _burst(server.base_url, REVOKE_URL, [(owner, _new_code()) for _ in range(50)])
    bad = [s for s, _ in res if s == 403]
    locked = [s for s, _ in res if s == 429]
    ok = [s for s, _ in res if s == 200]
    assert ok == [], f"틀린 코드 revoke 가 성공함: {res}"
    assert len(bad) == PER_ACCOUNT_MAX, (
        f"revoke 코드 대조 도달 {len(bad)}회 — 5회여야 함: {res}")
    assert len(locked) == 50 - PER_ACCOUNT_MAX, f"revoke 잠금(429) 수 이상: {res}"
    assert server.fails_of("61") == PER_ACCOUNT_MAX, "revoke 실패 카운터가 5가 아님"
    # 틀린 코드 revoke 는 플래그를 건드리지 않는다.
    assert server.flag_of("61") == 1 and server.flag_of("99") == 1, "틀린 코드 revoke 가 회수함"

    # 잠긴 상태에서 정답 revoke 를 동시에 섞어도 전부 429(회수 안 됨).
    res2 = _burst(server.base_url, REVOKE_URL, [(owner, server.code) for _ in range(10)])
    assert all(s == 429 for s, _ in res2), f"잠금 중 정답 revoke 가 통과: {res2}"
    assert server.flag_of("61") == 1 and server.flag_of("99") == 1, "잠금 중 정답 revoke 가 회수함"


# ============================================================================
# A-5. (FIX-2) 동시 첫 요청의 레거시 마이그레이션 — duplicate column 500 없이 기동
# ============================================================================

def test_concurrent_first_requests_on_legacy_db_boot_without_500(tmp_path):
    """`backup_owner` 등이 없는 **옛 DB**에 여러 요청이 **동시에 처음** 붙어도
    duplicate column 500 이 나지 않는다(_add_user_column 가드, FIX-2/D-1).

    자기 서버를 직접 띄운다 — 레거시 DB를 미리 만들어 두고, 준비되자마자 동시
    요청을 퍼붓는다.
    """
    import hashlib
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    try:  # backup_owner·claim_fails·claim_locked_until 이 **없는** 구 스키마
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
            ("990900", "legacy@duty.kr", "가명옛계정", "master", "61",
             pw_hash, salt.hex()))
        conn.execute("INSERT INTO ward_invites (ward, code) VALUES ('61','LEGACY01')")
        conn.commit()
    finally:
        conn.close()

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "DUTY_DB": db_path,
        "DUTY_SECRET": "conc-test-secret-000000000000",
        "DUTY_BACKUP_CLAIM_CODE": _new_code(),
        "TMPDIR": str(tmp_path),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        _wait_ready(base_url, proc)

        def _login():
            with httpx.Client(base_url=base_url, timeout=30) as c:
                r = c.post("/api/auth/login",
                           json={"login": "990900", "password": "password123"})
            return r.status_code

        # 준비되자마자 동시 로그인 20건 — 마이그레이션 경합 창을 때린다.
        with cf.ThreadPoolExecutor(max_workers=20) as ex:
            codes = list(ex.map(lambda _: _login(), range(20)))
        assert all(s == 200 for s in codes), (
            f"동시 첫 요청에서 500(duplicate column) 발생: {sorted(set(codes))}")
        # 기동만으로 컬럼이 올라왔다.
        cols = {r[1] for r in sqlite3.connect(db_path).execute(
            "PRAGMA table_info(users)")}
        assert {"backup_owner", "claim_fails", "claim_locked_until"} <= cols, (
            f"마이그레이션 미완료: {sorted(cols)}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
