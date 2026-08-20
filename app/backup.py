"""관리자용 데이터 백업 내려받기 (T3 — D-15 B안).

허가된 **계정**만 병동 전체 데이터를 ZIP 한 개로 내려받는다.

권한 판정은 **`users.backup_owner` 플래그**로만 한다(운영자 결정 D-19). 권한은
번호가 아니라 **계정에 붙고**, 그 플래그를 켜려면 운영자만 아는 **권한 코드**
(`DUTY_BACKUP_CLAIM_CODE`)를 제출해야 한다.

앞선 두 설계가 실제로 뚫렸다(검수부 침투 재현):

1. **문자열(사번·이메일) 지정** — 환경변수에 적힌 사번이 아직 어느 계정에도 묶이지
   않았으면 아무나 새 병동을 열어 master가 된 뒤 그 사번을 등록해 반출했다. 이메일은
   대소문자 접힘(점 없는 ı)으로 **다른 이메일**이 통과했다.
2. **환경변수 uid 목록** — 선점·접힘은 막혔지만, **DB가 초기화되면** 첫 가입자가
   uid 1을 물려받아 환경변수에 적힌 권한을 **상속**했다. 볼륨을 잃어버린 뒤 복구
   전에 아무나 먼저 가입하면 전체 DB를 가져갈 수 있었다.

플래그 방식에는 두 경로가 다 없다. 초기화되면 플래그도 함께 사라지고(전원 거부),
코드를 모르면 다시 켤 수 없다. **DB 초기화 후 재등록이 필요한 것은 결함이 아니라
설계 의도다** — 그것이 이 전환의 존재 이유다.

`role=="master"` 결합 조건은 유지한다. 실질 방어선은 플래그이고, role은 강등된
계정이 권한을 계속 들고 있지 않게 하는 보조 조건이다.

일관성: 운영 DB는 WAL 모드라 파일 직접 복사(`shutil.copy`)는 -wal 미반영·손상
위험이 있다. `VACUUM INTO`(잠금·구버전으로 실패하면 `Connection.backup()`)로
**스냅샷 사본**을 만들고, CSV도 그 사본에서 읽어 duty.db와 시점이 어긋나지 않게 한다.
스냅샷은 내려주기 전에 `PRAGMA quick_check`로 **구조 손상(파일 깨짐)** 을 검사한다 —
깨진 DB를 "성공"으로 내려주면 사고 당일에야 복구 불가를 알게 된다. **값 오염(내용이
바뀐 것)은 검사 대상이 아니다**: 같은 길이로 in-place 변조된 값은 quick_check를
그대로 통과한다. 그래서 README에 주요 테이블 **행수**를 함께 적어, 사람이 "이 백업이
비어 있지 않은지"를 눈으로 대조할 수 있게 한다.

권한은 코드로 **켜고(claim) 끌 수 있다(revoke)**. 회수는 계정을 고르지 않고 전원의
플래그를 0으로 되돌린다 — 담당자 교체·퇴사·코드 유출 때 필요한 것은 "누가 들고
있는지 모르는 상태를 한 번에 정리하는 것"이기 때문이다. 정리한 뒤 정당한 운영자가
코드로 다시 등록한다.

환경 변수: DUTY_BACKUP_CLAIM_CODE (권한 코드 — 배포 환경에서만 설정). 등록이 끝나면
지워도 되지만 **지운 상태에서는 회수도 할 수 없다**. 담당자 교체·유출 때는 새 코드를
넣어 배포한 뒤 회수 → 재등록 순서로 한다(DEPLOY §7.6).
"""
from __future__ import annotations

import csv
import hmac
import io
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .auth import (
    CLAIM_BAD,
    CLAIM_LOCKED,
    CLAIM_LOCKED_GLOBAL,
    CLAIM_OK,
    UserInfo,
    get_current_user,
    run_claim_transaction,
)
from .storage import KST, _conn, _db_path, _now

# 경고 단계 경계 (KST 기준 경과일) — 마스터 승인 규칙
WARN_DAYS = 30
CRITICAL_DAYS = 45

# CSV로 함께 내보내는 데이터 테이블(전량). backup_log는 백업 자체의 이력
# (병동 데이터가 아님)이라 CSV에서는 제외한다 — duty.db 사본에는 그대로 들어간다.
CSV_EXCLUDED_TABLES = ("backup_log",)

# CSV에서 값을 가리는 **테이블.컬럼** 명시 목록.
#
# 근거: 복구 정본은 `duty.db`이고 CSV는 사람이 눈으로 보는 사본일 뿐이다. 아래
# 값들은 CSV에 실려도 복구에 아무 도움이 되지 않는 반면(이득 0), 파일이 한 번
# 새면 그대로 침입 도구가 된다 — 검수부가 백업본의 초대 코드로 실제 가입에
# 성공했다. duty.db 안의 값은 그대로 두므로 복구 능력은 줄지 않는다.
MASKED_COLUMNS: dict[str, frozenset[str]] = {
    "users": frozenset({"pw_hash", "salt"}),  # 오프라인 비밀번호 대입 공격의 재료
    "ward_invites": frozenset({"code"}),      # 유효한 병동 가입 코드(그 자체로 통행증)
}
MASK_TEXT = "(생략)"

# 명시 목록 뒤의 **2차 그물**(fail-closed). 명시 목록만 두면 컬럼 축에서 열린 채로
# 샌다 — 검수부가 `users`에 `reset_token` 컬럼을 추가하자 평문 그대로 CSV에 실려
# 나갔다. 이름이 자격증명 냄새를 풍기면 **등록하지 않아도 일단 가린다**.
# 가리면 안 되는 컬럼은 아래 MASK_EXEMPT_COLUMNS에 명시적으로 적는다.
SENSITIVE_COLUMN_RE = re.compile(
    r"pw|hash|salt|secret|token|key|code|credential", re.IGNORECASE)

# 2차 그물의 **명시적 예외**. 여기 적지 않으면 가려진다(뒤집힌 기본값).
#
# 현재 스키마(users·rosters·schedules·wanted_requests·request_windows·feedback·
# ward_invites)의 전체 컬럼을 위 정규식에 대조한 결과 걸리는 것은
# `users.pw_hash`·`users.salt`·`ward_invites.code` 셋뿐이고, 셋 다 **가려야 하는**
# 값이라 예외로 뺄 것이 없다. 그래서 이 표는 의도적으로 비어 있다.
# 나중에 예외를 추가할 때는 "왜 사람이 눈으로 봐야 하는 값인지"를 줄마다 적을 것 —
# 근거 없는 예외 한 줄이 곧 2차 그물의 구멍이다.
MASK_EXEMPT_COLUMNS: dict[str, frozenset[str]] = {}

# CSV 셀 하나의 길이 상한(문자). 행이 많은 축은 이미 안전하지만(100만 행/256MB DB →
# 피크 +20.5MB) **한 칸이 큰** 축은 스트리밍으로도 못 묶는다 — 62.7MB 셀 하나가
# RSS를 +467MB 올렸고 512MiB 컨테이너에서는 셀 ~50MB부터 OOM이다. OOM은 요청
# 하나가 아니라 프로세스 전체를 죽인다. 값은 **SQL의 substr로 잘라서** 읽어오므로
# 큰 칸이 통째로 파이썬 메모리에 올라오지 않는다.
MAX_CELL_CHARS = 100_000
CELL_TRUNCATED_SUFFIX = "…(생략, duty.db 참조)"

# 기계용 JSON 컬럼은 CSV에 값을 싣지 않는다. CSV는 "사람이 눈으로 보는 사본"이고
# 한 칸에 통째로 든 JSON은 사람이 읽을 수 없다(README도 "앱 화면을 쓰라"고 안내).
# 반출 이득은 0인데 셀 크기 폭발의 주범이라 값 대신 안내 문구를 쓴다.
# **정본 duty.db에는 그대로 들어 있으므로 복구 능력은 줄지 않는다.**
JSON_COLUMNS: dict[str, frozenset[str]] = {
    "rosters": frozenset({"data"}),
    "schedules": frozenset({"data"}),
}
JSON_TEXT = "(JSON — duty.db 참조)"

# CSV 수식 인젝션 차단 — 부서원이 피드백에 쓴 `=HYPERLINK(...)`가 운영자 엑셀에서
# 실행돼 데이터를 외부로 보내는 것이 실제로 재현됐다. README가 "엑셀로 열립니다"라고
# 안내하므로 지시대로 열면 그대로 실행된다. 앞에 작은따옴표를 붙여 **텍스트로**
# 고정한다(정본 duty.db의 값은 건드리지 않는다).
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# 거부 응답에는 어떤 신원 정보도 담지 않는다(개인정보 — 교훈 L-1).
DENIED_MSG = "백업 내려받기 권한이 없습니다."
BUILD_FAIL_MSG = (
    "백업 파일을 만들지 못했습니다. 잠시 후 다시 시도하고, 계속 실패하면 "
    "운영 담당자에게 알려 주세요."
)

# 폴백 백업의 데드라인(초). CPython의 Connection.backup()은 SQLITE_BUSY에서
# **횟수 제한 없이** 0.25초씩 재시도하므로 그대로 두면 요청 스레드가 영구
# 점유되고, finally가 영원히 실행되지 않아 개인정보가 든 임시 파일이 남는다.
#
# **실제 상한은 이 값이 아니다.** 데드라인은 backup()의 진행 콜백에서만 검사되므로,
#   (1) 먼저 시도하는 VACUUM INTO가 busy_timeout(아래)을 통째로 소진할 수 있고,
#   (2) 데드라인을 넘긴 뒤에도 진행 중인 step 하나가 최대 busy_timeout만큼 블록한다.
# 최악의 실측 상한 ≈ SNAPSHOT_TIMEOUT_SEC + 2 × SNAPSHOT_BUSY_TIMEOUT_MS
# (설정 3초로 실측했을 때 10.0초 → 운영 30초 설정에서는 약 40초). 요청 타임아웃을
# 잡을 때는 30초가 아니라 이 상한을 기준으로 볼 것.
SNAPSHOT_TIMEOUT_SEC = 30
# 스냅샷 전용 연결의 잠금 대기(ms) — storage._conn()과 같은 값.
SNAPSHOT_BUSY_TIMEOUT_MS = 5000

# 권한 코드(claim) 설정 — 운영자만 아는 문자열. 짧은 코드는 대입으로 뚫리므로
# 최소 길이 미만이면 **등록 기능 자체를 끈다**(fail-closed).
CLAIM_CODE_ENV = "DUTY_BACKUP_CLAIM_CODE"
CLAIM_CODE_MIN_LEN = 8
# 실패 시도 제한 — **계정당**. 20자 코드라도 무제한 시도는 허용하지 않는다.
CLAIM_MAX_FAILS = 5
CLAIM_LOCK_SEC = 15 * 60
# 전역 실패 총량 상한. 계정 축만 잠그면 공격자가 병동을 새로 열어 master 계정을
# 만들 때마다 5회씩 무한히 시도할 수 있다(가입은 값싸다). 잠금 창이 아직 살아 있는
# 계정들의 실패 합계가 이 값에 닿으면 등록·회수 경로를 통째로 잠근다.
#
# 값(30)의 근거: 계정 6개분이다. 정상 운영자가 혼자 밟을 수 있는 수(5)의 6배라
# 오타로는 닿지 않고, 자동 대입은 15분당 30회 = 하루 2,880회로 묶인다 — 최소 길이
# 8자 무작위 코드(≈10^14)에도 턱없이 부족한 속도다. 대신 **공격 중에는 정상
# 운영자의 등록도 최대 15분 막힌다**: 등록은 배포 직후·DB 초기화 후에만 하는 1회성
# 작업이고 15분 뒤 저절로 풀리므로, 무한 대입을 열어 두는 쪽보다 낫다고 판단했다.
# (창은 CLAIM_LOCK_SEC과 같다 — 실패마다 미는 잠금 시각이 곧 창의 경계다.)
CLAIM_GLOBAL_MAX_FAILS = 30
CLAIM_DISABLED_MSG = (
    "백업 권한 등록이 준비되지 않았습니다. 운영 담당자에게 문의하세요."
)
CLAIM_BAD_CODE_MSG = "권한 코드가 올바르지 않습니다."
CLAIM_LOCKED_MSG = (
    "권한 코드를 여러 번 잘못 입력했습니다. 15분 뒤에 다시 시도하세요."
)
CLAIM_GLOBAL_LOCKED_MSG = (
    "권한 코드를 잘못 입력한 시도가 최근에 지나치게 많았습니다. "
    "15분 뒤에 다시 시도하고, 짐작 가는 바가 없으면 코드를 새로 바꾸세요."
)
CLAIM_NOT_MASTER_MSG = "병동 마스터 계정만 백업 권한을 등록할 수 있습니다."
REVOKE_NOT_MASTER_MSG = "병동 마스터 계정만 백업 권한을 회수할 수 있습니다."


class BackupError(RuntimeError):
    """백업본을 신뢰할 수 없어 내려주면 안 되는 상황(손상·시간초과 등)."""


# ---- 권한 ----

def is_backup_owner(user: UserInfo) -> bool:
    """허가 계정 여부 — `users.backup_owner` 플래그 ∧ role==master.

    플래그가 실질 방어선이다. 플래그는 권한 코드를 제출한 계정에만 켜지고, DB가
    초기화되면 함께 사라진다(전원 거부). role 조건은 보조 — 강등된 계정이 권한을
    계속 들고 있지 않게 한다.

    **환경변수 기반 판정은 남기지 않는다.** 하위 호환으로 한 줄만 남겨도 그 줄이
    곧 취약점이다(직전 두 라운드에서 같은 실수를 반복했다).
    """
    return user.role == "master" and bool(user.backup_owner)


def _claim_code() -> str:
    return os.environ.get(CLAIM_CODE_ENV, "").strip()


def claim_enabled() -> bool:
    """권한 등록을 받을 수 있는 상태인지 — 미설정·빈 값·짧은 코드면 False."""
    return len(_claim_code()) >= CLAIM_CODE_MIN_LEN


# 실패 시도 카운터는 **계정 행(users.claim_fails·claim_locked_until)** 에 있고, 잠금
# 재판정·코드 대조·실패/성공 기록은 auth.run_claim_transaction이 한 트랜잭션으로 묶는다.
#
# 축을 계정 하나로 줄인 이유: 예전에는 출처 IP도 키에 넣었는데, Railway처럼 모든
# 요청이 같은 프록시를 거치는 환경에서는 전원이 같은 IP로 보여 **아무나 5회 틀리면
# 정상 운영자도 15분간 등록하지 못하는** 사실상의 전역 잠금이 됐다. IP를 DB에
# 남기는 것은 백업 ZIP으로 실려 나가므로 애초에 선택지가 아니다(교훈 L-1).
#
# DB에 둔 이유: 프로세스 메모리에 두면 scale-to-zero로 인스턴스가 내려갈 때 잠금이
# 통째로 풀린다(직전 라운드의 자진 신고 한계). 계정 축만 남기면 공격자가 계정을
# 새로 만들며 우회할 수 있으므로 전역 실패 총량 상한을 함께 둔다.

def _raise_if_gate_closed(outcome: str) -> None:
    """직렬화 트랜잭션이 잠금/전역상한으로 막았으면 그에 맞는 429를 낸다.

    잠금·코드 대조·실패 기록은 run_claim_transaction 안에서 한 트랜잭션으로 끝난다
    (①결함1의 검사-후-행동 경합을 없앤다). 여기서는 그 결과 코드를 HTTP로 옮길 뿐이다.
    """
    if outcome == CLAIM_LOCKED:
        raise HTTPException(429, CLAIM_LOCKED_MSG)
    if outcome == CLAIM_LOCKED_GLOBAL:
        raise HTTPException(429, CLAIM_GLOBAL_LOCKED_MSG)


def _clear_denied_for(user: UserInfo) -> None:
    """등록에 성공한 계정의 denied 이력을 지운다 (③ denied 오탐, FIX-4).

    denied_last_30d는 '권한 없는 계정의 침입 시도'를 세는 지표인데, 예전에는 운영자
    본인이 설정 전에 코드를 잘못 넣거나(오타) 코드 미설정기에 두드린 시도까지 여기
    섞여 30일 빨간 경보로 남았다. 코드를 결국 맞혀 **등록에 성공**한 계정의 과거 거부는
    침입이 아니라 본인의 설정 과정이므로, 성공 시점에 그 계정(actor=uid)의 denied 행을
    지운다. 다른 계정(uid≠본인)의 거부 이력은 그대로 둔다 — 그쪽이 진짜 탐지 대상이다.
    """
    conn = _conn()
    try:
        conn.execute("DELETE FROM backup_log WHERE status='denied' AND actor=?",
                     (_actor(user),))
        conn.commit()
    finally:
        conn.close()


def require_backup_owner(
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> UserInfo:
    """허가 계정만 통과 — 무인증은 get_current_user가 먼저 401을 낸다.

    이쪽은 이력을 남기지 않는다. 화면이 "백업 카드를 그릴지" 판단하려고 모든
    로그인 사용자가 /backup/status 를 한 번씩 호출하므로, 여기서 거부를 기록하면
    정상 이용이 전부 '침입 시도'로 쌓여 로그가 무의미해진다.
    """
    if not is_backup_owner(user):
        raise HTTPException(403, DENIED_MSG)
    return user


def require_backup_owner_audited(
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> UserInfo:
    """반출 경로 전용 — 거부되면 `status='denied'` 로 남긴다.

    남기는 것은 uid와 시각뿐이다(실명·사번·이메일 금지 — 교훈 L-1).
    """
    if not is_backup_owner(user):
        _record_denied(_actor(user))
        raise HTTPException(403, DENIED_MSG)
    return user


# ---- 스냅샷 ----

def _backup_with_deadline(src: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    """`Connection.backup()` 폴백 — 데드라인을 넘기면 예외로 빠져나온다.

    pages=1024로 잘라 실행해야 진행 콜백이 매 단계 호출된다(기본값 -1은 한 번에
    끝내므로 콜백이 한 번뿐이라 시간 검사를 걸 수 없다). 콜백에서 올린 예외는
    backup()이 그대로 전파하므로, 호출부의 finally가 반드시 실행된다.
    """
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SEC

    def _tick(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise BackupError(
                f"백업 스냅샷이 {SNAPSHOT_TIMEOUT_SEC}초 안에 끝나지 않았습니다."
            )

    src.backup(dest, pages=1024, progress=_tick)


def _verify_snapshot(path: str) -> None:
    """스냅샷 무결성 확인 — `quick_check`가 'ok'가 아니면 내려주지 않는다.

    폴백 경로(`Connection.backup()`)는 페이지를 그대로 복사하므로 원본이 손상돼
    있으면 손상까지 충실히 복제한다. 검사 없이 200으로 내려주면 "백업했다"는
    기록·화면 표시만 남고 정작 복구는 불가능해진다.
    """
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    if not row or str(row[0]).lower() != "ok":
        raise BackupError(f"백업 스냅샷 무결성 검사 실패: {row[0] if row else '결과 없음'}")


def _snapshot(dest_path: str) -> None:
    """운영 DB의 일관된 사본을 dest_path에 만든다 (파일 직접 복사 금지).

    VACUUM INTO는 대상 파일이 이미 있으면 실패하므로 dest_path는 비어 있어야 한다.
    폴백은 **잠금·구버전(OperationalError)** 일 때만 허용한다. `sqlite3.Error`를
    통째로 삼키면 손상(DatabaseError: database disk image is malformed)까지 폴백으로
    흘러가 손상본을 그대로 복제하게 된다 — 손상은 그대로 전파시켜야 한다.
    """
    src = sqlite3.connect(_db_path())
    try:
        src.execute(f"PRAGMA busy_timeout={SNAPSHOT_BUSY_TIMEOUT_MS}")
        try:
            src.execute("VACUUM INTO ?", (dest_path,))
        except sqlite3.OperationalError as exc:
            # 디스크가 꽉 찬 것("database or disk is full")은 잠금이 아니다. 폴백으로
            # 전체 복사를 한 번 더 시도하면 이미 꽉 찬 디스크에 I/O만 두 배로 든다.
            # 재시도해도 결과가 같은 오류는 즉시 전파한다.
            if "disk is full" in str(exc).lower():
                raise
            if os.path.exists(dest_path):  # 실패로 남은 부분 파일 제거 후 폴백
                os.remove(dest_path)
            dest = sqlite3.connect(dest_path)
            try:
                _backup_with_deadline(src, dest)
            finally:
                dest.close()
    finally:
        src.close()
    _verify_snapshot(dest_path)


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in CSV_EXCLUDED_TABLES]


def masked_columns(table: str, cols: list[str]) -> set[str]:
    """이 테이블에서 값을 가릴 컬럼 집합 — 명시 목록 + 이름 기반 2차 그물.

    2차 그물이 fail-closed의 핵심이다. 명시 목록만 보면 새 컬럼(`reset_token` 등)이
    등록될 때까지 평문으로 나간다. 이름이 자격증명 냄새를 풍기면 등록 여부와 무관하게
    가리고, 가리면 안 되는 것만 MASK_EXEMPT_COLUMNS에 근거와 함께 적는다.
    """
    explicit = MASKED_COLUMNS.get(table, frozenset())
    exempt = MASK_EXEMPT_COLUMNS.get(table, frozenset())
    out = set(c for c in cols if c in explicit)
    for c in cols:
        if c not in exempt and SENSITIVE_COLUMN_RE.search(c):
            out.add(c)
    return out


def _sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _csv_query(conn: sqlite3.Connection, table: str) -> tuple[list[str], str]:
    """CSV용 SELECT 문을 만든다 — 가릴 값·JSON·긴 값은 **SQL 단계에서** 처리한다.

    파이썬에서 잘라내면 큰 칸이 이미 메모리에 올라온 뒤라 늦다. 리터럴로 대체하거나
    substr로 잘라서 읽으면 애초에 올라오지 않는다.
    """
    cols = [d[0] for d in conn.execute(f'SELECT * FROM "{table}" LIMIT 0').description]
    masked = masked_columns(table, cols)
    jsons = JSON_COLUMNS.get(table, frozenset())
    parts = []
    for c in cols:
        q = f'"{c}"'
        if c in masked:
            parts.append(f"{_sql_literal(MASK_TEXT)} AS {q}")
        elif c in jsons:
            parts.append(f"{_sql_literal(JSON_TEXT)} AS {q}")
        else:
            # 상한+1자만 읽는다 — 넘치면 잘렸다는 것을 파이썬에서 알 수 있다.
            parts.append(f"substr({q}, 1, {MAX_CELL_CHARS + 1}) AS {q}")
    return cols, f'SELECT {", ".join(parts)} FROM "{table}"'


def _csv_cell(v: object) -> str:
    """DB 값 하나 → CSV에 쓸 안전한 문자열."""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return f"(바이너리 {len(v)}바이트 — duty.db 참조)"
    s = v if isinstance(v, str) else str(v)
    if len(s) > MAX_CELL_CHARS:
        s = s[:MAX_CELL_CHARS] + CELL_TRUNCATED_SUFFIX
    if s[:1] in FORMULA_PREFIXES:
        s = "'" + s  # 엑셀이 수식으로 해석하지 않게 텍스트로 고정
    return s


def _write_table_csv(
    zf: zipfile.ZipFile, conn: sqlite3.Connection, table: str
) -> None:
    """테이블 1개를 CSV(UTF-8 BOM)로 ZIP에 **흘려 쓴다** — 엑셀 한글 깨짐 방지.

    전량을 StringIO에 str로 쌓고 다시 encode하면 DB 크기의 4~5배가 메모리에
    올라간다(실측: 214MB DB에서 피크 RSS 998MB). 512MiB 컨테이너에서는 DB가
    100MB만 돼도 OOM이고, OOM은 요청 하나가 아니라 프로세스 전체를 죽인다.
    행 단위로 인코딩해 넘기면 피크가 한 행 수준으로 내려가고, **한 칸**이 큰
    경우는 _csv_query의 substr가 상한으로 묶는다.
    """
    cols, sql = _csv_query(conn, table)
    cur = conn.execute(sql)
    with zf.open(f"tables/{table}.csv", "w") as raw:
        out = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        try:
            w = csv.writer(out)
            w.writerow(cols)
            for row in cur:
                w.writerow([_csv_cell(v) for v in row])
            out.flush()
        finally:
            out.detach()  # TextIOWrapper가 ZIP 항목을 닫지 않게 한다


def _readme(taken_at_kst: datetime, counts: list[tuple[str, int]]) -> bytes:
    """운영자용 안내문. 비개발자가 **유일하게 실제로 읽는 파일**이다.

    구형 Windows 메모장은 BOM 없는 UTF-8을 한글로 못 읽고 LF만 있는 파일을 한 줄로
    붙여 보여준다. 그래서 CSV와 같은 규칙(BOM + CRLF)으로 쓴다.
    """
    rows = "".join(f"- {name:<16} : {n:,}건\n" for name, n in counts)
    text = (
        "듀티원 데이터 백업\n"
        "==================\n\n"
        f"백업 시각: {taken_at_kst:%Y년 %m월 %d일 %H:%M} (한국 시간)\n\n"
        "이 파일에 들어 있는 것\n"
        "- duty.db      : 복구에 사용하는 정본 파일입니다. 열어보지 말고 그대로 보관하세요.\n"
        "- tables/*.csv : 사람이 확인할 수 있게 표로 뽑은 사본입니다(엑셀로 열립니다).\n"
        "- README.txt   : 이 안내문입니다.\n\n"
        "이번 백업에 담긴 건수 (★ 반드시 확인하세요)\n"
        f"{rows}"
        "- 위 숫자가 **0이거나 평소보다 크게 적다면 잘못된 데이터베이스를 백업한 것**입니다.\n"
        "  (서버 설정이 잘못돼 빈 파일을 백업하면 압축은 정상적으로 만들어지고 화면에도\n"
        "  '백업 완료'로 보입니다. 숫자만이 진짜 확인 수단입니다.)\n"
        "  숫자가 이상하면 이 파일을 지우지 말고 운영 담당자에게 알려 주세요.\n\n"
        "CSV에 담기는 내용(전부)\n"
        "- users        : 가입 계정 목록 — 이름·사번·이메일·역할·병동, 그리고 백업\n"
        "                 권한 상태(backup_owner=1이면 반출 권한 보유)와 권한 코드\n"
        "                 실패 잠금(claim_fails·claim_locked_until)\n"
        "- rosters      : 간호사 명단(실명·팀·경력순 등)\n"
        "- schedules    : 만들어 둔 근무표와 발행 이력\n"
        "- wanted_requests : 부서원이 낸 원티드 신청과 승인 여부\n"
        "- request_windows : 원티드 신청 기간 설정\n"
        "- feedback     : 수신함에 들어온 **피드백 원문**(쓴 사람이 누구인지 포함)\n"
        "- ward_invites : 병동별 초대 코드 표\n\n"
        "CSV를 볼 때 알아둘 것\n"
        "- 비밀번호(pw_hash·salt)와 초대 코드(code) 자리에는 값 대신 (생략)이 적혀 있습니다.\n"
        "  새어 나갔을 때 그대로 침입에 쓰이는 값이라 일부러 가린 것이며, 복구에는\n"
        "  쓰이지 않습니다(복구는 duty.db로 합니다).\n"
        "- 빈칸은 '값이 없음'과 '빈 글자'를 구분하지 않습니다. 둘 다 빈칸으로 보입니다.\n"
        "- rosters·schedules의 data 열에는 값 대신 (JSON — duty.db 참조)라고만 적혀 있습니다.\n"
        "  프로그램이 쓰는 형식이 한 칸에 통째로 든 자리라 사람이 읽을 수 없고, 그대로\n"
        "  실으면 파일이 지나치게 커집니다. 눈으로 확인하실 때는 앱 화면을 쓰세요.\n"
        "  **정본 duty.db에는 그대로 들어 있으므로 복구에는 아무 지장이 없습니다.**\n"
        f"- 한 칸이 너무 길면 {MAX_CELL_CHARS:,}자까지만 싣고 뒤에 '{CELL_TRUNCATED_SUFFIX}'를 붙입니다.\n"
        "- 값이 =, +, -, @ 로 시작하면 앞에 작은따옴표(')를 붙여 둡니다. 엑셀이 그 값을\n"
        "  수식으로 실행하지 않게 하려는 것입니다(글자 자체는 그대로입니다).\n\n"
        "복구가 필요할 때\n"
        "- 이 ZIP 파일은 **다른 사람에게 넘기지 말고 본인이 직접** 서버에 올립니다.\n"
        "  절차는 배포 문서(DEPLOY §7.6 '복구 절차')에 번호 순서로 적혀 있습니다.\n"
        "  (duty.db 파일이 있어야 복구할 수 있습니다. 압축을 풀어 편집하지 마세요.)\n"
        "- 복구하면 백업 권한도 함께 되돌아갑니다. 권한 코드를 다시 넣어 1회 재등록하세요.\n\n"
        "개인정보 주의\n"
        "- 간호사 실명·사번·근무 이력, 그리고 피드백에 쓴 글 원문까지 들어 있습니다.\n"
        "- **duty.db 안에는 비밀번호 해시와 초대 코드가 그대로 들어 있습니다.**\n"
        "  CSV에서 가린 것은 눈에 띄는 사고를 줄이려는 것일 뿐, 이 ZIP 자체는 안전하지\n"
        "  않습니다. **ZIP 파일 자체를 비밀번호와 같은 급으로 다루세요.**\n"
        "- 잃어버렸다면 즉시 앱에서 초대 코드를 재발급하세요.\n"
        "- 메신저·메일·공용 폴더에 올리거나 다른 사람에게 전달하지 마세요.\n"
        "- OneDrive·iCloud 같은 자동 동기화가 켜진 폴더(바탕화면·문서·다운로드)에\n"
        "  두지 마세요. 그대로 인터넷에 올라갑니다.\n"
        "- 본인 기기의 안전한 위치에 보관하고, 복구에 쓰고 나면 파일을 삭제하세요.\n"
    )
    # BOM + CRLF — 구형 Windows 메모장에서도 한글이 깨지지 않고 줄이 나뉜다.
    return text.replace("\n", "\r\n").encode("utf-8-sig")


def _row_counts(conn: sqlite3.Connection, tables: list[str]) -> list[tuple[str, int]]:
    """주요 테이블 행수 — README에 실어 "빈 DB를 백업했는지"를 사람이 알게 한다."""
    counts: list[tuple[str, int]] = []
    for t in tables:
        try:
            n = int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        except sqlite3.Error:
            n = -1
        counts.append((t, n))
    return counts


def _archive_snapshot_history(snap_path: str) -> None:
    """스냅샷 안의 성공 이력을 'archived'로 바꾼다 — 복구본은 성공 이력 0건.

    이걸 하지 않으면 **2회차 이후 백업본으로 복구했을 때 경고가 아예 뜨지 않는다.**
    N회차 스냅샷에는 N-1회차의 확정된 'ok' 행이 들어 있어서, 복구된 서버가
    "10일 전에 백업했음(level=ok)"이라고 초록으로 표시한다. 복구 직후가 백업이 가장
    필요한 순간인데 그때 정확히 침묵하게 된다.

    행 자체는 지우지 않는다 — 그 백업이 언제 만들어졌는지는 계속 알 수 있어야 한다.
    """
    conn = sqlite3.connect(snap_path)
    try:
        conn.execute("UPDATE backup_log SET status='archived' WHERE status='ok'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # backup_log가 아직 없는 DB(첫 실행) — 되돌릴 성공 이력도 없다
    finally:
        conn.close()


def _build_zip() -> bytes:
    """스냅샷 → ZIP(bytes). 임시 파일은 성공·실패 무관하게 삭제한다."""
    tmpdir = tempfile.mkdtemp(prefix="duty-backup-")
    try:
        snap = os.path.join(tmpdir, "duty.db")
        _snapshot(snap)
        _archive_snapshot_history(snap)
        taken_at = datetime.now(KST)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, "duty.db")
            conn = sqlite3.connect(snap)  # CSV도 같은 스냅샷에서 — 시점 일치
            try:
                tables = _table_names(conn)
                counts = _row_counts(conn, tables)
                for table in tables:
                    _write_table_csv(zf, conn, table)
            finally:
                conn.close()
            zf.writestr("README.txt", _readme(taken_at, counts))
        return buf.getvalue()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)  # 개인정보 잔존 금지


# ---- 이력·상태 ----
#
# 이력은 **2단계**로 남긴다.
#   1) 요청을 받으면 스냅샷을 뜨기 **전에** status='pending' 행을 넣는다.
#   2) 브라우저가 파일을 끝까지 받은 뒤 /backup/confirm 을 호출하면 'ok'로 바꾼다.
#
# 왜 이렇게 하나:
#  - (a) 예전처럼 응답을 보내기 전에 'ok'를 남기면, 다운로드가 끊기거나 사용자가
#    저장을 취소해도 성공으로 기록돼 경고가 30일간 꺼진다. 파일은 없는데 시스템만
#    있다고 믿는 상태가 가장 위험하다. 브라우저는 **응답 본문을 전부 받은 뒤에만**
#    confirm을 호출하므로(중간에 끊기면 blob 읽기가 실패한다), 'ok'는 실제 전달의
#    증거가 된다. 크기까지 대조해 부분 전달을 걸러낸다.
#  - (b) pending 행을 스냅샷 **전에** 넣는 덕분에 그 행이 백업본 안에도 들어간다.
#    복구본에서 "언제 뜬 백업인지"를 알 수 있다(예전에는 backup_log가 항상 비어
#    있었다).
#  - 실패(스냅샷 손상·시간초과)는 'fail'로 남겨 사후 추적이 가능하게 한다.
#
# 복구본의 경고: 스냅샷을 담을 때 그 안의 'ok' 행을 **'archived'로 바꿔서** 넣는다
# (_archive_snapshot_history). 그러지 않으면 2회차 이후 백업본에는 직전 회차의 확정된
# 'ok' 행이 들어 있어, 복구된 서버가 "며칠 전에 백업했음(ok)"이라고 초록으로 표시한다.
# 복구 직후가 백업이 가장 필요한 순간이므로 그때는 반드시 critical이어야 한다.


def _actor(user: UserInfo) -> str:
    """이력에 남기는 행위자 표기 — uid만. 실명·사번·이메일은 남기지 않는다."""
    return f"uid:{user.uid}"


def _insert_log(actor: str, ward: str, byte_size: int, status: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO backup_log (actor, ward, created_at, byte_size, status) "
            "VALUES (?,?,?,?,?)",
            (actor, ward, _now(), byte_size, status),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _update_log(entry_id: int, *, byte_size: int | None = None,
                status: str | None = None) -> None:
    sets, args = [], []
    if byte_size is not None:
        sets.append("byte_size=?")
        args.append(byte_size)
    if status is not None:
        sets.append("status=?")
        args.append(status)
    if not sets:
        return
    args.append(entry_id)
    conn = _conn()
    try:
        conn.execute(f"UPDATE backup_log SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
    finally:
        conn.close()


def days_since_kst(created_at: str | None) -> int | None:
    """저장된 UTC ISO 시각 → **KST 달력 날짜** 기준 경과일 (교훈 L-4).

    사용자가 "며칠 지났다"고 인식하는 기준이 KST이므로 UTC로 계산하면 자정
    부근에서 하루가 어긋난다. 값이 없거나 깨졌으면 None.
    """
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(KST).date() - dt.astimezone(KST).date()).days


def level_for(days: int | None) -> str:
    """경고 단계 — 서버가 계산해 프런트에 내려준다(이력 0건이면 critical).

    음수는 마지막 백업 시각이 미래라는 뜻이다(서버 시계 역행·수동 조정·조작된 행).
    "0일 전이니 안전"으로 읽으면 경고가 꺼지므로 신뢰할 수 없는 값으로 보고
    critical 취급한다.
    """
    if days is None or days < 0 or days >= CRITICAL_DAYS:
        return "critical"
    if days >= WARN_DAYS:
        return "warn"
    return "ok"


class BackupStatus(BaseModel):
    last_backup_at: str | None = None  # 저장 형식 그대로(UTC ISO)
    days_since: int | None = None      # KST 달력 기준 경과일
    level: str = "critical"            # ok | warn | critical
    # 최근 30일(KST) 거부 시도 건수 — uid당 하루 1건으로 합쳐 센다. 기록만 쌓이고
    # 읽을 방법이 없으면 "탐지 수단"이 아니므로 화면까지 올린다.
    denied_last_30d: int = 0


class BackupConfirm(BaseModel):
    id: int          # 내려받기 응답의 X-Backup-Id
    bytes: int = 0   # 브라우저가 실제로 받은 바이트 수


class BackupClaimRequest(BaseModel):
    code: str = Field("", max_length=200, description="운영자에게 받은 백업 권한 코드")


class BackupRevokeRequest(BaseModel):
    code: str = Field("", max_length=200, description="백업 권한 코드(회수에도 필요)")


class BackupRevokeResult(BaseModel):
    # 실제로 꺼진 계정 수. 1보다 크면 **나 말고도 권한을 들고 있던 계정이 있었다**는
    # 뜻이라 운영자가 그 자리에서 알아야 할 정보다(유출 정황).
    revoked: int = 0


def _kst_day_start_utc(days_ago: int) -> str:
    """KST 기준 '오늘 - days_ago일'의 자정을 저장 형식(UTC ISO)으로."""
    day = (datetime.now(KST) - timedelta(days=days_ago)).date()
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    return start.astimezone(timezone.utc).isoformat()


def _record_denied(actor: str) -> None:
    """거부 이력을 남긴다 — **uid당 KST 하루 1행**으로 합친다.

    합치는 이유: 로그인만 하면 누구나 반출 경로를 두드릴 수 있어 400건을 1.1초에
    쌓을 수 있었다. 그러면 진짜 시도가 잡음에 묻히고 볼륨만 먹는다(시간당 ~73MB).
    "누가 언제 시도했나"는 하루 단위면 충분하고, 그것이 사람이 실제로 읽을 수 있는
    해상도다.
    """
    since = _kst_day_start_utc(0)
    conn = _conn()
    try:
        dup = conn.execute(
            "SELECT 1 FROM backup_log WHERE status='denied' AND actor=? "
            "AND created_at >= ? LIMIT 1",
            (actor, since),
        ).fetchone()
        if dup is not None:
            return  # 오늘 이미 남겼다
        conn.execute(
            "INSERT INTO backup_log (actor, ward, created_at, byte_size, status) "
            "VALUES (?,?,?,?,?)",
            (actor, "", _now(), 0, "denied"),
        )
        conn.commit()
    finally:
        conn.close()


def _record_granted(user: UserInfo) -> None:
    """권한 **부여**를 남긴다 — 거부만 남기면 사후 확인이 반쪽이다.

    코드가 새어 제3의 master가 몰래 등록해도, 예전에는 `denied` 행만 있고 "언제 누가
    권한을 얻었는지"는 어디에도 없었다. 등록은 드문 사건이므로 합치지 않고 매번
    남긴다(회수 후 재등록도 각각 한 줄로 보인다).

    이 행은 **성공한 백업이 아니다** — level 판정은 `status='ok'` 행만 세므로
    `archived`·`pending`·`fail`·`denied`와 같은 취급이다.
    """
    _insert_log(_actor(user), user.ward, 0, "granted")


def _record_revoked(revoked: list[tuple[int, str]]) -> None:
    """권한 **회수**를 회수된 계정마다 한 줄씩 남긴다.

    행위자가 아니라 **권한을 잃은 계정**을 actor로 적는다. `granted`와 짝이 맞아
    (granted uid:5 → revoked uid:5) 계정별 권한 이력이 그대로 읽히고, 유출 상황에서
    정작 알아야 할 "그때 누가 권한을 들고 있었나"가 남는다. 몇 건이 회수됐는지는
    **행 수가 곧 건수**라 byte_size 같은 다른 자리를 빌릴 필요가 없다.
    """
    for uid, ward in revoked:
        _insert_log(f"uid:{uid}", ward, 0, "revoked")


def _last_ok_byte_size() -> int:
    """직전 성공 백업의 크기(바이트). 없으면 0."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT byte_size FROM backup_log WHERE status='ok' AND byte_size>0 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return int(row["byte_size"]) if row else 0


def _current_status() -> BackupStatus:
    conn = _conn()
    try:
        # ORDER BY id DESC — **기록 순서**로 본다. created_at 정렬은 서버 시계가 한 번
        # 앞섰을 때 남은 미래 시각 행이 이후의 모든 정상 백업을 영원히 가린다(정상
        # 백업을 해도 days_since가 음수인 채로 critical이 유지되고, 배너는 닫을 수
        # 없으니 화면에서 빠져나갈 방법이 없다). id는 조작할 수 없는 기록 순서다.
        row = conn.execute(
            "SELECT created_at FROM backup_log WHERE status='ok' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        denied = conn.execute(
            "SELECT COUNT(*) AS c FROM backup_log WHERE status='denied' "
            "AND created_at >= ?",
            (_kst_day_start_utc(29),),
        ).fetchone()["c"]
    finally:
        conn.close()
    last = row["created_at"] if row else None
    days = days_since_kst(last)
    return BackupStatus(last_backup_at=last, days_since=days, level=level_for(days),
                        denied_last_30d=int(denied))


# ---- 라우터 ----

router = APIRouter(prefix="/api/admin", tags=["backup"])

# 전체 DB 사본·경고 상태가 브라우저·중간 캐시에 남지 않게 한다.
NO_STORE = {"Cache-Control": "no-store"}


@router.get("/backup")
def download_backup(
    user: Annotated[UserInfo, Depends(require_backup_owner_audited)],
) -> Response:
    """전체 데이터 백업 ZIP 내려받기 (허가 계정 전용).

    여기서는 'ok'를 남기지 않는다 — 아직 파일이 전달되지 않았다. pending 행의
    번호를 `X-Backup-Id` 헤더로 알려주고, 받는 쪽이 /backup/confirm 으로 확정한다.
    """
    prev_bytes = _last_ok_byte_size()
    _conn().close()  # 스냅샷 전에 스키마 생성 보장(첫 실행 시 테이블 누락 방지)
    entry_id = _insert_log(_actor(user), user.ward, 0, "pending")
    try:
        data = _build_zip()
    except Exception as exc:
        # 손상·시간초과 등은 'fail'로 남긴다 — 실패가 어디에도 안 남으면 사후 추적이
        # 불가능하다. 원인은 서버 로그에 남기고(from exc) 사용자에게는 알리지 않는다.
        _update_log(entry_id, status="fail")
        raise HTTPException(500, BUILD_FAIL_MSG) from exc
    _update_log(entry_id, byte_size=len(data))
    fname = f"duty_backup_{datetime.now(KST):%Y%m%d_%H%M}.zip"
    # 크기 급감 경고: DUTY_DB 경로 오타·볼륨 미마운트면 빈 DB에 스키마만 새로 생겨
    # **항상 "유효한 백업"** 이 만들어진다(실측 4,251바이트 ZIP이 level=ok로 기록됐다).
    # 진짜 데이터가 든 볼륨은 한 번도 백업되지 않았는데 30일간 아무도 모른다.
    # 직전 성공 백업의 1/10 미만이면 화면에 경고를 띄운다(README의 행수와 함께 본다).
    shrunk = prev_bytes > 0 and len(data) * 10 < prev_bytes
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Backup-Id": str(entry_id),
            "X-Backup-Bytes": str(len(data)),
            "X-Backup-Prev-Bytes": str(prev_bytes),
            "X-Backup-Shrink": "1" if shrunk else "0",
            **NO_STORE,
        },
    )


@router.post("/backup/confirm", response_model=BackupStatus)
def confirm_backup(
    body: BackupConfirm,
    user: Annotated[UserInfo, Depends(require_backup_owner_audited)],
    response: Response,
) -> BackupStatus:
    """내려받기 완료 확정 — 이 호출이 있어야 성공 이력('ok')이 된다.

    본인이 만든 pending 행이어야 하고, 받은 바이트 수가 서버가 만든 크기와
    정확히 같아야 한다(부분 전달 차단).
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT actor, byte_size, status FROM backup_log WHERE id=?",
            (body.id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["actor"] != _actor(user):
        raise HTTPException(404, "확정할 백업 기록을 찾을 수 없습니다.")
    if row["status"] == "ok":
        response.headers.update(NO_STORE)
        return _current_status()  # 재호출은 조용히 통과(멱등)
    if row["status"] != "pending":
        raise HTTPException(409, "이미 실패로 처리된 백업입니다.")
    if row["byte_size"] <= 0 or body.bytes != row["byte_size"]:
        raise HTTPException(400, "받은 파일 크기가 서버 기록과 다릅니다.")
    _update_log(body.id, status="ok")
    response.headers.update(NO_STORE)
    return _current_status()


@router.post("/backup/claim", response_model=BackupStatus)
def claim_backup_owner(
    body: BackupClaimRequest,
    user: Annotated[UserInfo, Depends(get_current_user)],
    response: Response,
) -> BackupStatus:
    """운영자 권한 코드로 **내 계정에** 백업 반출 권한을 등록한다 (D-19).

    이 경로가 권한이 생기는 유일한 통로다. 코드를 모르면 어떤 계정도(첫 가입자여도,
    DB가 초기화된 직후여도) 반출할 수 없다.
    """
    if user.role != "master":
        _record_denied(_actor(user))
        raise HTTPException(403, CLAIM_NOT_MASTER_MSG)
    if not claim_enabled():
        # 코드 미설정·빈 값·8자 미만 → 등록 기능 자체가 꺼진 상태(fail-closed).
        # 설정 미비이지만 **두드린 사실은 남긴다** — 코드를 넣기 전(배포 직후)이
        # 가장 취약한 시기인데 그 기간의 시도가 통째로 안 남으면 사후에 알 길이 없다.
        _record_denied(_actor(user))
        raise HTTPException(403, CLAIM_DISABLED_MSG)
    # 잠금 재판정·코드 대조·실패/성공 기록을 한 트랜잭션으로 직렬화한다(①결함1).
    # compare_digest — 앞자리부터 몇 글자가 맞는지 응답 시간으로 새어 나가지 않게 한다.
    outcome, data = run_claim_transaction(
        user.uid,
        action="grant",
        max_fails=CLAIM_MAX_FAILS,
        global_max_fails=CLAIM_GLOBAL_MAX_FAILS,
        lock_sec=CLAIM_LOCK_SEC,
        code_ok=lambda: hmac.compare_digest(
            body.code.strip().encode("utf-8"), _claim_code().encode("utf-8")),
    )
    _raise_if_gate_closed(outcome)
    if outcome == CLAIM_BAD:
        _record_denied(_actor(user))
        raise HTTPException(403, CLAIM_BAD_CODE_MSG)
    if data == "missing":
        raise HTTPException(401, "계정을 찾을 수 없습니다. 다시 로그인해 주세요.")
    if data == "granted":
        # 새로 켜졌을 때만 감사 행을 남긴다 — 이미 소유자의 재-claim은 감사 중복
        # 적재로 유출 조사 신호를 흐리므로 재기록을 생략한다(③ granted 중복, FIX-5).
        _record_granted(user)
    _clear_denied_for(user)  # 본인 오타·미설정기 시도가 침입 경보로 남지 않게(FIX-4)
    response.headers.update(NO_STORE)
    return _current_status()


@router.post("/backup/revoke", response_model=BackupRevokeResult)
def revoke_backup_owners(
    body: BackupRevokeRequest,
    user: Annotated[UserInfo, Depends(get_current_user)],
    response: Response,
) -> BackupRevokeResult:
    """권한 코드로 **모든 계정의** 백업 반출 권한을 회수한다.

    없어서는 안 되는 짝이다 — 등록만 있고 회수가 없으면 퇴사·담당자 교체·코드 유출
    때 전체 DB 반출 권한이 그대로 남는다("코드를 다시 넣으면 담당자가 바뀐다"는 것은
    사실이 아니었다. 새 계정에 플래그가 하나 더 붙을 뿐이었다).

    계정을 고르게 하지 않는 이유: 코드가 샌 상황에서는 "누가 몰래 등록했는지 모른다"는
    것이 문제다. 전원을 0으로 되돌린 뒤 정당한 운영자가 코드로 다시 등록하면 된다.
    (코드도 함께 바꿔 배포한 뒤 회수하는 순서를 문서에 적어 뒀다 — DEPLOY §7.6.)

    등록과 같은 문(코드·마스터·잠금)을 쓴다. 회수 쪽만 무르면 그쪽이 구멍이 된다.
    """
    if user.role != "master":
        _record_denied(_actor(user))
        raise HTTPException(403, REVOKE_NOT_MASTER_MSG)
    if not claim_enabled():
        _record_denied(_actor(user))
        raise HTTPException(403, CLAIM_DISABLED_MSG)
    # 등록과 같은 문(코드·마스터·잠금)을 쓴다. 회수의 플래그 끄기·회수 목록 확보를
    # **같은 트랜잭션 안에서** 처리해, SELECT-후-UPDATE 사이에 grant가 끼어들어 플래그는
    # 꺼지되 revoked 이력이 안 남는 경합(②D-2)을 없앤다. 잠금·직렬화는 claim과 동일(①).
    outcome, revoked = run_claim_transaction(
        user.uid,
        action="revoke",
        max_fails=CLAIM_MAX_FAILS,
        global_max_fails=CLAIM_GLOBAL_MAX_FAILS,
        lock_sec=CLAIM_LOCK_SEC,
        code_ok=lambda: hmac.compare_digest(
            body.code.strip().encode("utf-8"), _claim_code().encode("utf-8")),
    )
    _raise_if_gate_closed(outcome)
    if outcome == CLAIM_BAD:
        _record_denied(_actor(user))
        raise HTTPException(403, CLAIM_BAD_CODE_MSG)
    _record_revoked(revoked)
    response.headers.update(NO_STORE)
    # 회수한 본인도 권한을 잃었으므로 백업 상태(BackupStatus)는 돌려주지 않는다.
    return BackupRevokeResult(revoked=len(revoked))


@router.get("/backup/status", response_model=BackupStatus)
def backup_status(
    _user: Annotated[UserInfo, Depends(require_backup_owner)],
    response: Response,
) -> BackupStatus:
    """마지막 백업 시각과 경고 단계 (허가 계정 전용)."""
    response.headers.update(NO_STORE)
    return _current_status()
