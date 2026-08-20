/**
 * 듀티원 — 백업 내려받기 실브라우저 E2E (품질부, T3)
 * =====================================================================
 * 실행 (한 줄):
 *     bash tests/e2e/run_backup_e2e.sh
 *
 * 이 파일만 따로 돌릴 때(서버가 이미 떠 있는 경우):
 *     E2E_BASE=http://127.0.0.1:8861 DUTY_BACKUP_CLAIM_CODE=<코드> \
 *     PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tests/e2e/backup_e2e.js
 *
 * 환경 변수
 *   E2E_BASE                 서버 주소 (기본 http://127.0.0.1:8861)
 *   DUTY_BACKUP_CLAIM_CODE   서버에 설정된 권한 코드 (필수)
 *   E2E_OUT                  스크린샷 저장 폴더 (기본 /tmp/duty-e2e)
 *   PLAYWRIGHT_MODULE        playwright 모듈 경로
 *                            (기본 /opt/node22/lib/node_modules/playwright)
 *
 * 검증하는 것 (지시서 수용 기준 6 + D-19 화면 기준)
 *   1. staff·admin 에게는 백업 관련 UI가 DOM에 **전무**하다
 *   2. 권한 없는 master → 🔐 등록 카드 / 권한 있는 계정 → 💾 백업 카드
 *   3. 🆔 내 계정 번호 카드는 없다
 *   4. 틀린 코드는 카드 안에서 실패로 표시되고 백업 카드가 생기지 않는다
 *   5. critical 배너는 닫는 수단이 없다
 *   6. 배너·모달 버튼 3연타 → 스냅샷 요청은 **1건**
 *   7. 저장 확인 모달: [파일이 없습니다] → 경고 유지 / [확인했습니다] → 경고 소멸
 *   8. 확정이 실패하면 **모달 안에** 사유가 뜨고 다시 시도할 수 있다
 *   9. 0~29일은 조용하고, 30일↑ 팝업 · [오늘은 나중에]는 **KST 당일**만 유효하다
 *  10. 모바일 뷰포트(390×844)에서도 카드·배너가 보인다
 * 테스트 데이터는 전부 가명·가짜 사번(99…)이다 (교훈 L-1).
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || '/opt/node22/lib/node_modules/playwright');

const BASE = (process.env.E2E_BASE || 'http://127.0.0.1:8861').replace(/\/$/, '');
const CODE = process.env.DUTY_BACKUP_CLAIM_CODE || '';
const OUT = process.env.E2E_OUT || '/tmp/duty-e2e';
const PW = 'password123';

const PEOPLE = {
  owner: { empno: '990001', name: '김서연', ward: '61' },
  admin: { empno: '990002', name: '이지우' },
  staff: { empno: '990003', name: '최민준' },
};

const results = [];
function check(ok, label, detail) {
  results.push({ ok: !!ok, label, detail: detail === undefined ? '' : String(detail) });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || detail === undefined ? '' : `  → ${detail}`}`);
}
async function shot(page, name) {
  const file = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
  console.log(`  · 스크린샷 ${file}`);
  return file;
}
// 경과일 시나리오를 만들려면 이력을 직접 심어야 한다(그 용도의 API는 없다).
// 서버가 쓰는 것과 같은 DUTY_DB 파일에 파이썬으로 한 행만 넣는다.
function seedLastBackup(daysAgo) {
  const py = process.env.PY || 'python3';
  const script = [
    'import sqlite3, os, sys, datetime',
    'd = int(sys.argv[1])',
    'ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=d)).isoformat()',
    'c = sqlite3.connect(os.environ["DUTY_DB"])',
    'c.execute("DELETE FROM backup_log")',
    'c.execute("INSERT INTO backup_log (actor,ward,created_at,byte_size,status)'
      + ' VALUES (\'uid:1\',\'61\',?,4096,\'ok\')", (ts,))',
    'c.commit()',
  ].join('\n');
  execFileSync(py, ['-c', script, String(daysAgo)], { env: process.env });
}

async function api(method, url, body, token) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(BASE + url, {
    method, headers, body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) { /* 본문이 JSON이 아닐 수 있다 */ }
  return { status: res.status, body: json, text };
}

async function setup() {
  const owner = await api('POST', '/api/auth/register',
    { empno: PEOPLE.owner.empno, password: PW, name: PEOPLE.owner.name, ward: PEOPLE.owner.ward });
  if (owner.status !== 200) throw new Error(`master 가입 실패: ${owner.status} ${owner.text}`);
  const invite = await api('GET', '/api/auth/invite', null, owner.body.token);
  if (invite.status !== 200) throw new Error(`초대 코드 조회 실패: ${invite.text}`);
  for (const key of ['admin', 'staff']) {
    const r = await api('POST', '/api/auth/register',
      { empno: PEOPLE[key].empno, password: PW, name: PEOPLE[key].name,
        invite_code: invite.body.code });
    if (r.status !== 200) throw new Error(`${key} 가입 실패: ${r.status} ${r.text}`);
  }
  const setRole = await api('POST', '/api/auth/set-role',
    { login: PEOPLE.admin.empno, role: 'admin' }, owner.body.token);
  if (setRole.status !== 200) throw new Error(`admin 역할 부여 실패: ${setRole.text}`);
  return owner.body.token;
}

async function login(context, empno, { mobile = false } = {}) {
  const page = await context.newPage();
  if (mobile) await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.fill('#authEmail', empno);
  await page.fill('#authPw', PW);
  await page.click('#authBtn');
  await page.waitForSelector('#app', { state: 'visible', timeout: 15000 });
  await page.waitForTimeout(1200);  // initBackup(상태 조회) 완료 대기
  await page.evaluate(() => { if (typeof switchTab === 'function') switchTab('setup'); });
  await page.waitForTimeout(300);
  return page;
}

// 새로고침 = "로그인 후 첫 진입"과 같은 경로(initBackup 재실행). 같은 컨텍스트에서
// 다시 login()을 부르면 토큰이 localStorage에 남아 로그인 화면이 뜨지 않는다.
async function refresh(page) {
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#app', { state: 'visible', timeout: 15000 });
  await page.waitForTimeout(1500);
  await page.evaluate(() => { if (typeof switchTab === 'function') switchTab('setup'); });
  await page.waitForTimeout(300);
}

const backupUi = () => ({
  claimCard: !!document.querySelector('#backupClaimBox .card'),
  backupBtn: !!document.getElementById('backupBtn'),
  banner: (document.getElementById('backupBanner') || {}).innerHTML || '',
  modal: (document.getElementById('backupModal') || {}).innerHTML || '',
  bodyText: document.body.innerText,
  // BACKUP 은 페이지 스크립트의 top-level `let` 이라 window 프로퍼티가 아니다.
  level: (typeof BACKUP === 'undefined' || !BACKUP) ? null : BACKUP.level,
});

async function run() {
  fs.mkdirSync(OUT, { recursive: true });
  if (!CODE) throw new Error('DUTY_BACKUP_CLAIM_CODE 가 필요합니다(서버와 같은 값).');
  await setup();

  const browser = await chromium.launch();
  try {
    // ---------- 1. staff·admin: 백업 UI 전무 ----------
    for (const role of ['staff', 'admin']) {
      const ctx = await browser.newContext();
      const page = await login(ctx, PEOPLE[role].empno);
      const ui = await page.evaluate(backupUi);
      check(!ui.claimCard && !ui.backupBtn && !ui.banner.trim() && !ui.modal.trim(),
        `${role}: 백업 카드·등록 카드·배너·팝업이 DOM에 없다`, JSON.stringify(ui).slice(0, 200));
      check(!ui.bodyText.includes('백업') || !ui.bodyText.includes('내려받기'),
        `${role}: 화면 글자에도 백업 기능이 노출되지 않는다`);
      await shot(page, `01-${role}`);
      await ctx.close();
    }

    // ---------- 2. 권한 없는 master: 등록 카드만 ----------
    const ctx = await browser.newContext({ acceptDownloads: true });
    const page = await login(ctx, PEOPLE.owner.empno);
    let ui = await page.evaluate(backupUi);
    check(ui.claimCard && !ui.backupBtn, '권한 없는 master: 🔐 등록 카드만 보인다',
      JSON.stringify(ui).slice(0, 200));
    check(!ui.bodyText.includes('내 계정 번호'), '🆔 내 계정 번호 카드가 제거됐다');
    await shot(page, '02-claim-card');

    // ---------- 3. 틀린 코드 ----------
    await page.fill('#claimCode', 'wrong-code-0000');
    await page.click('#claimBtn');
    await page.waitForTimeout(800);
    const claimMsg = await page.textContent('#claimMsg');
    ui = await page.evaluate(backupUi);
    check(/실패/.test(claimMsg || '') && !ui.backupBtn,
      '틀린 코드: 카드 안에 실패가 표시되고 백업 카드는 생기지 않는다', claimMsg);
    await shot(page, '03-claim-wrong');

    // ---------- 4. 정답 코드 → 백업 카드 등장 ----------
    await page.fill('#claimCode', CODE);
    await page.click('#claimBtn');
    await page.waitForSelector('#backupBtn', { timeout: 10000 });
    ui = await page.evaluate(backupUi);
    check(ui.backupBtn && !ui.claimCard, '정답 코드: 💾 백업 카드로 바뀐다',
      JSON.stringify(ui).slice(0, 200));
    check(ui.level === 'critical', '이력이 없으므로 level=critical', ui.level);
    await shot(page, '04-backup-card');

    // ---------- 5. critical 배너는 닫을 수 없다 ----------
    ui = await page.evaluate(backupUi);
    const bannerText = await page.evaluate(
      () => (document.getElementById('backupBanner') || {}).innerText || '');
    const closers = await page.evaluate(() => Array.from(
      document.querySelectorAll('#backupBanner button')).map(b => b.innerText.trim()));
    check(bannerText.trim().length > 0, 'critical: 상단 고정 배너가 떠 있다', bannerText.slice(0, 60));
    check(!closers.some(t => /닫기|×|✕|나중에/.test(t)),
      '배너에 닫기 수단이 없다', JSON.stringify(closers));
    await shot(page, '05-critical-banner');

    // ---------- 6. 3연타 → 스냅샷 요청 1건 ----------
    const snapshotReqs = [];
    page.on('request', (r) => {
      const u = r.url();
      if (r.method() === 'GET' && /\/api\/admin\/backup(\?|$)/.test(u)) snapshotReqs.push(u);
    });
    const modalBtn = page.locator('#backupModal button', { hasText: '지금 백업하기' });
    const target = (await modalBtn.count()) ? modalBtn.first()
      : page.locator('#backupBtn');
    await target.dispatchEvent('click');
    await target.dispatchEvent('click');
    await target.dispatchEvent('click');
    await page.waitForSelector('text=파일이 저장되었는지 확인해 주세요', { timeout: 30000 });
    await page.waitForTimeout(1500);
    check(snapshotReqs.length === 1,
      '3연타해도 스냅샷 요청은 1건', `요청 ${snapshotReqs.length}건`);
    const modalText = await page.evaluate(
      () => (document.getElementById('backupModal') || {}).innerText || '');
    check(/저장/.test(modalText) && /확인했습니다/.test(modalText),
      '저장 확인 모달이 뜨고 진행/결과 문구가 모달 안에 있다', modalText.slice(0, 80));
    await shot(page, '06-verify-modal');

    // ---------- 7. [파일이 없습니다] → 경고 유지 ----------
    await page.locator('#backupModal button', { hasText: '파일이 없습니다' }).click();
    await page.waitForTimeout(800);
    ui = await page.evaluate(backupUi);
    const status1 = await api('GET', '/api/admin/backup/status', null,
      await page.evaluate(() => localStorage.getItem('duty_token')));
    check(status1.body && status1.body.level === 'critical',
      '[파일이 없습니다]: 서버 이력이 ok가 되지 않는다', JSON.stringify(status1.body));
    check(ui.banner.trim().length > 0, '[파일이 없습니다]: 경고 배너가 그대로 남는다');
    await shot(page, '07-not-saved');

    // ---------- 8. [확인했습니다] → 경고 소멸 ----------
    await page.locator('#backupBtn').dispatchEvent('click');
    await page.waitForSelector('text=파일이 저장되었는지 확인해 주세요', { timeout: 30000 });
    await page.locator('#backupModal button', { hasText: '확인했습니다' }).click();
    await page.waitForTimeout(1500);
    ui = await page.evaluate(backupUi);
    const status2 = await api('GET', '/api/admin/backup/status', null,
      await page.evaluate(() => localStorage.getItem('duty_token')));
    check(status2.body && status2.body.level === 'ok',
      '[확인했습니다]: 서버 이력이 ok가 된다', JSON.stringify(status2.body));
    check(!ui.banner.trim() && !ui.modal.trim(), '확정 후 배너·팝업이 사라진다',
      `banner=${ui.banner.length} modal=${ui.modal.length}`);
    await shot(page, '08-after-confirm');

    // ---------- 9. 확정 실패 → 모달 안에 사유 + 다시 시도 ----------
    await page.route('**/api/admin/backup/confirm', (route) => route.fulfill({
      status: 500, contentType: 'application/json',
      body: JSON.stringify({ detail: '서버 점검 중입니다(E2E 강제 실패)' }),
    }));
    await page.locator('#backupBtn').dispatchEvent('click');
    await page.waitForSelector('text=파일이 저장되었는지 확인해 주세요', { timeout: 30000 });
    await page.locator('#backupModal button', { hasText: '확인했습니다' }).click();
    await page.waitForTimeout(1500);
    const failModal = await page.evaluate(
      () => (document.getElementById('backupModal') || {}).innerText || '');
    check(/기록하지 못했습니다/.test(failModal),
      '확정 실패 사유가 **모달 안에** 표시된다', failModal.slice(0, 120));
    check(/확인했습니다/.test(failModal) && !/오늘은 나중에/.test(failModal),
      '같은 나그 팝업으로 돌아가지 않고 모달에서 다시 시도할 수 있다', failModal.slice(0, 120));
    await shot(page, '09-confirm-failed');
    await page.unroute('**/api/admin/backup/confirm');

    // ---------- (신설) 권한 회수 UI (Q-2) ----------
    // cefa33a가 붙인 새 화면 흐름이라 여기서 실브라우저로 처음 검증한다. 이 흐름이
    // E2E에서 빠져 있으면 회수 버튼이 화면에서 동작하는지 아무도 보지 못한다(P-1 사각지대).
    await refresh(page);  // 실패 모달 등 잔여 상태를 걷어내고 깨끗한 백업 카드에서 시작
    page.on('dialog', (d) => d.accept());  // 회수 confirm() 대화상자 자동 승인
    const hadCard = await page.evaluate(() => !!document.getElementById('backupBtn'));
    check(hadCard, '회수 전: 권한 있는 계정이라 백업 카드가 떠 있다');

    // 접힌 <details>를 펼치고 **틀린 코드**로 회수 시도 → 거부되고 카드는 그대로.
    await page.evaluate(() => {
      const d = document.getElementById('backupRevokeBox'); if (d) d.open = true;
    });
    await page.fill('#revokeCode', 'wrong-revoke-code-0000');
    await page.click('#revokeBtn');
    await page.waitForTimeout(900);
    const rmsg = await page.textContent('#revokeMsg');
    const stillCard = await page.evaluate(() => !!document.getElementById('backupBtn'));
    check(/실패/.test(rmsg || '') && stillCard,
      '틀린 코드 회수: 실패 문구가 뜨고 백업 카드가 그대로다', rmsg);
    await shot(page, '16-revoke-wrong');

    // **정답 코드**로 회수 → 등록 카드로 되돌아가고 회수 안내가 뜬다.
    await page.evaluate(() => {
      const d = document.getElementById('backupRevokeBox'); if (d) d.open = true;
    });
    await page.fill('#revokeCode', CODE);
    await page.click('#revokeBtn');
    await page.waitForSelector('#backupClaimBox .card', { timeout: 10000 });
    ui = await page.evaluate(backupUi);
    check(ui.claimCard && !ui.backupBtn,
      '정답 코드 회수: 💾 백업 카드가 사라지고 🔐 등록 카드로 되돌아간다',
      JSON.stringify(ui).slice(0, 160));
    check(/회수했습니다/.test(ui.bodyText), '회수 후 등록 카드에 회수 안내가 표시된다');

    // 서버에서도 권한이 실제로 꺼졌다: 반출 요청이 403.
    const afterRevoke = await api('GET', '/api/admin/backup', null,
      await page.evaluate(() => localStorage.getItem('duty_token')));
    check(afterRevoke.status === 403,
      '회수 후 서버도 반출을 403으로 막는다', `status=${afterRevoke.status}`);
    await shot(page, '17-revoke-done');

    // 회수는 이 공유 계정(owner)의 플래그를 실제로 껐다. 뒤 시나리오(경과일 팝업·
    // 모바일)가 다시 권한 있는 owner를 전제하므로, 등록 카드에서 정답 코드로 재등록해
    // 권한을 원상복구한다(재등록이 가능하다는 것 자체가 Q-2 기준이기도 하다).
    await page.fill('#claimCode', CODE);
    await page.click('#claimBtn');
    await page.waitForSelector('#backupBtn', { timeout: 10000 });
    await ctx.close();

    // ---------- 10. 경과일별 경고: 10일(조용) → 30일(팝업) → 나중에(당일 무음) ----------
    const ctx2 = await browser.newContext();
    seedLastBackup(10);
    const p2 = await login(ctx2, PEOPLE.owner.empno);
    let u2 = await p2.evaluate(backupUi);
    check(u2.level === 'ok' && !u2.banner.trim() && !u2.modal.trim(),
      '10일 전 백업: 팝업·배너 없이 조용하다', `level=${u2.level}`);
    check(/10일 전/.test(u2.bodyText), '설정 카드에 "마지막 백업: 10일 전"이 표시된다');
    await shot(p2, '12-quiet-10days');

    seedLastBackup(30);
    await refresh(p2);
    u2 = await p2.evaluate(backupUi);
    check(u2.level === 'warn' && /백업이 필요합니다/.test(u2.modal),
      '30일 경과: 로그인 후 첫 진입에 팝업이 뜬다', `level=${u2.level}`);
    check(!u2.banner.trim(), 'warn 단계에서는 고정 배너까지 띄우지 않는다');
    await shot(p2, '13-warn-popup');

    await p2.locator('#backupModal button', { hasText: '오늘은 나중에' }).click();
    await p2.waitForTimeout(300);
    u2 = await p2.evaluate(backupUi);
    check(!u2.modal.trim(), '[오늘은 나중에]: 팝업이 닫힌다');

    await refresh(p2);
    u2 = await p2.evaluate(backupUi);
    check(!u2.modal.trim(), '같은 날 다시 들어와도 팝업이 뜨지 않는다');
    await shot(p2, '14-snoozed-today');

    // 날짜가 바뀐 상황: 저장된 스누즈 날짜를 어제(KST)로 바꾼다.
    await p2.evaluate(() => {
      const y = new Date(Date.now() + 9 * 3600 * 1000 - 86400000)
        .toISOString().slice(0, 10);
      localStorage.setItem('duty_backup_snooze', y);
    });
    await refresh(p2);
    u2 = await p2.evaluate(backupUi);
    check(/백업이 필요합니다/.test(u2.modal),
      '날짜가 바뀌면 팝업이 다시 뜬다(“나중에”는 당일 자정까지)', u2.modal.slice(0, 60));
    await shot(p2, '15-snooze-expired');
    await ctx2.close();

    // ---------- 11. 모바일 뷰포트 ----------
    const mctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const mpage = await login(mctx, PEOPLE.owner.empno, { mobile: true });
    const mui = await mpage.evaluate(backupUi);
    check(mui.backupBtn, '모바일(390×844): 백업 카드가 보인다');
    const overflow = await mpage.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1);
    check(overflow, '모바일: 가로 스크롤이 생기지 않는다');
    await shot(mpage, '10-mobile');
    await mctx.close();
    // staff 는 **새 컨텍스트**로 — 같은 컨텍스트면 master 토큰이 localStorage에 남아
    // 로그인 화면이 뜨지 않는다.
    const sctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const mstaff = await login(sctx, PEOPLE.staff.empno, { mobile: true });
    const msui = await mstaff.evaluate(backupUi);
    check(!msui.backupBtn && !msui.claimCard, '모바일 staff: 백업 UI 전무');
    await shot(mstaff, '11-mobile-staff');
    await sctx.close();
  } finally {
    await browser.close();
  }
}

run().then(() => {
  const failed = results.filter((r) => !r.ok);
  console.log(`\n=== E2E 결과: ${results.length - failed.length}/${results.length} 통과 ` +
    `(스크린샷: ${OUT}) ===`);
  if (failed.length) {
    failed.forEach((f) => console.log(`  FAIL ${f.label} — ${f.detail}`));
    process.exit(1);
  }
}).catch((err) => {
  console.error('E2E 실행 실패:', err);
  process.exit(2);
});
