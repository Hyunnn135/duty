#!/usr/bin/env bash
# 듀티원 백업 기능 실브라우저 E2E — 한 줄 실행 스크립트 (품질부)
# =====================================================================
#   bash tests/e2e/run_backup_e2e.sh
#
# 하는 일: 임시 DB로 서버를 띄우고 → Playwright(Chromium)로 화면을 실제로 조작하고
#          → 스크린샷을 남기고 → 서버를 내린다. 저장소 데이터는 건드리지 않는다.
#
# 준비물
#   - 파이썬 의존성이 설치된 인터프리터. 기본은 python3이며 다른 것을 쓰려면:
#         PY=/path/to/venv/bin/python bash tests/e2e/run_backup_e2e.sh
#     (이 컨테이너의 시스템 파이썬은 cryptography가 깨져 있어 venv가 필요하다 — 교훈 L-8)
#   - Playwright + Chromium. 이 컨테이너에는 이미 설치돼 있다:
#         PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
#         모듈 경로 /opt/node22/lib/node_modules/playwright
#     (`npx playwright install` 은 프록시가 막으므로 실행하지 말 것)
#
# 환경 변수
#   PY        파이썬 실행기 (기본 python3)
#   E2E_PORT  서버 포트 (기본 8861)
#   E2E_OUT   스크린샷 폴더 (기본 <임시폴더>/shots — 실행 끝에 경로를 출력한다)
#
# 종료 코드: 0=전 항목 통과, 1=검증 실패, 2=실행 실패(서버 기동 등)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PY:-python3}"
PORT="${E2E_PORT:-8861}"
NODE_BIN="${NODE_BIN:-node}"
WORK="$(mktemp -d)"
OUT="${E2E_OUT:-$WORK/shots}"
mkdir -p "$OUT"

# 권한 코드는 **매 실행마다 새로 만든다** — 저장소에 실값을 남기지 않는다.
CODE="$("$PY" - <<'PYCODE'
import secrets
print("e2e" + secrets.token_urlsafe(18).replace("-", "x").replace("_", "y"))
PYCODE
)"

export DUTY_DB="$WORK/e2e.db"
export DUTY_SECRET="e2e-secret-$(date +%s)-$RANDOM"
export DUTY_BACKUP_CLAIM_CODE="$CODE"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}"
export PY   # node 쪽에서 경과일 시나리오를 심을 때 같은 인터프리터를 쓴다

cd "$ROOT"
echo "· 서버 기동: 127.0.0.1:$PORT (DB $DUTY_DB)"
setsid nohup "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
  < /dev/null > "$WORK/server.log" 2>&1 &

cleanup() {
  local pid
  pid="$(pgrep -f "port $PORT" | head -1 || true)"
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then break; fi
  sleep 0.5
done
if ! curl -fsS "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
  echo "서버가 뜨지 않았습니다. 로그:"; tail -30 "$WORK/server.log"; exit 2
fi

status=0
E2E_BASE="http://127.0.0.1:$PORT" E2E_OUT="$OUT" DUTY_BACKUP_CLAIM_CODE="$CODE" \
  "$NODE_BIN" "$ROOT/tests/e2e/backup_e2e.js" || status=$?

echo "· 스크린샷: $OUT"
echo "· 서버 로그: $WORK/server.log"
exit $status
