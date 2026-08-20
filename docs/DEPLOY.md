# 배포 가이드 — CI/CD & Cloud Run

간호사 근무표 앱(FastAPI + OR-Tools)을 **GitHub Actions → Google Cloud Run**으로
자동 배포하는 설정이다. 비용은 소규모 사용 시 사실상 무료에 가깝다(PLAN 7.5.2).

- **CI** (`.github/workflows/ci.yml`): 모든 push·PR에서 테스트 실행.
- **CD** (`.github/workflows/deploy.yml`): `main` 병합(또는 수동 실행) 시 테스트 →
  컨테이너 빌드(Cloud Build) → Cloud Run 배포.

---

## 0. 개요 · 아키텍처

```
GitHub push(main) ──▶ GitHub Actions
                         │  (Workload Identity Federation, 키 없음)
                         ▼
                    Cloud Build (Dockerfile 빌드)
                         ▼
                    Cloud Run 서비스 (단일 인스턴스)
                         │  DUTY_DB=/data/duty.db
                         ▼
                    GCS 버킷 볼륨 (/data 마운트, SQLite 영속화)
                    Secret Manager: duty-secret (JWT 서명키)
```

> **DB 영속화 주의**: Cloud Run 컨테이너는 상태가 없어 재시작 시 로컬 파일이 사라진다.
> 그래서 SQLite 파일을 **GCS 버킷 볼륨(/data)** 에 두고 `--max-instances 1`(동시 인스턴스
> 1개, `--min-instances 0` = scale-to-zero로 비용 절감)로 고정한다. 한 병동 규모 MVP에는 충분하다. 사용자가 늘면(여러 병동·동시성)
> **Postgres(Supabase/Cloud SQL)로 이관**을 권장한다(§6).

---

## 1. 사전 준비

- GCP 프로젝트(결제 연결됨) — `PROJECT_ID`
- 로컬에 `gcloud` CLI 설치 & 로그인: `gcloud auth login && gcloud config set project PROJECT_ID`
- 리전 결정(예: `asia-northeast3` 서울)

아래 변수를 셸에 설정해두면 편하다:

```bash
export PROJECT_ID=$(gcloud config get-value project)
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
export REGION=asia-northeast3
export SERVICE=duty
export BUCKET=${PROJECT_ID}-duty-data
export REPO_OWNER_SLASH_NAME=Hyunnn135/duty   # GitHub owner/repo
```

> **⚡ 빠른 길**: §2~6을 한 번에 실행하는 스크립트를 제공한다. `gcloud auth login` +
> `gcloud config set project <ID>` 후 아래를 실행하면 API 활성화·버킷·시크릿·서비스계정·
> WIF까지 만들고, **GitHub에 등록할 값들을 출력**한다. 그다음 §7만 하면 된다.
> ```bash
> bash scripts/gcp_setup.sh          # REGION/SERVICE 등은 환경변수로 덮어쓰기 가능
> ```
> 아래 §2~6은 이 스크립트가 자동으로 하는 일을 단계별로 설명한 것이다(수동 실행/이해용).

## 2. API 활성화

```bash
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  iamcredentials.googleapis.com storage.googleapis.com
```

## 3. SQLite 영속화용 GCS 버킷

```bash
gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access
```

## 4. JWT 서명키 시크릿

```bash
# 강력한 랜덤 키 생성 후 Secret Manager에 저장
python -c "import secrets;print(secrets.token_urlsafe(48))" | \
  gcloud secrets create duty-secret --data-file=-
# 키를 교체할 때: gcloud secrets versions add duty-secret --data-file=-
```

## 5. 배포용 서비스 계정 + 런타임 권한

```bash
# (a) CI가 사용할 배포 서비스 계정
gcloud iam service-accounts create duty-deployer \
  --display-name="Duty CI deployer"
export DEPLOYER=duty-deployer@${PROJECT_ID}.iam.gserviceaccount.com

for ROLE in \
  roles/run.admin \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.admin \
  roles/storage.admin \
  roles/iam.serviceAccountUser ; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOYER}" --role="$ROLE"
done

# (b) Cloud Run 런타임 서비스 계정(기본 compute SA)에
#     시크릿 접근 + 버킷 읽기/쓰기 권한 부여
export RUNTIME=${PROJECT_NUMBER}-compute@developer.gserviceaccount.com
gcloud secrets add-iam-policy-binding duty-secret \
  --member="serviceAccount:${RUNTIME}" --role=roles/secretmanager.secretAccessor
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:${RUNTIME}" --role=roles/storage.objectAdmin
```

## 6. Workload Identity Federation (키리스 인증)

GitHub Actions가 서비스 계정 키 파일 없이 GCP에 인증하도록 연동한다.

```bash
# 풀 생성
gcloud iam workload-identity-pools create github-pool \
  --location=global --display-name="GitHub pool"

export POOL_ID=$(gcloud iam workload-identity-pools describe github-pool \
  --location=global --format='value(name)')

# 공급자 생성 (이 저장소에서 오는 토큰만 허용)
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global --workload-identity-pool=github-pool \
  --display-name="GitHub provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${REPO_OWNER_SLASH_NAME}'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# 배포 SA를 이 저장소가 임시토큰으로 위임할 수 있도록 바인딩
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL_ID}/attribute.repository/${REPO_OWNER_SLASH_NAME}"

# GitHub 시크릿에 넣을 공급자 리소스명 출력
gcloud iam workload-identity-pools providers describe github-provider \
  --location=global --workload-identity-pool=github-pool \
  --format='value(name)'
```

## 7. GitHub 저장소에 변수·시크릿 등록

**Settings → Secrets and variables → Actions**

Repository **secrets**:
| 이름 | 값 |
|------|-----|
| `GCP_WIF_PROVIDER` | §6 마지막 명령이 출력한 공급자 리소스명 (`projects/…/providers/github-provider`) |
| `GCP_SA_EMAIL` | `duty-deployer@PROJECT_ID.iam.gserviceaccount.com` |

Repository **variables**:
| 이름 | 값(예) |
|------|--------|
| `GCP_PROJECT_ID` | `PROJECT_ID` |
| `GCP_REGION` | `asia-northeast3` |
| `SERVICE_NAME` | `duty` |
| `DATA_BUCKET` | `${PROJECT_ID}-duty-data` |

## 7.5 이메일 알림 (선택)

피드백→마스터, 원티드 승인/반려→신청자, (옵트인) 근무표 발행→병동 구성원 메일 알림.
SMTP 미설정 시 앱은 정상 동작하며 메일만 생략된다.

```bash
# 예: Gmail SMTP (2단계 인증 후 '앱 비밀번호' 발급 필요)
printf '앱비밀번호16자리' | gcloud secrets create smtp-password --data-file=-
gcloud secrets add-iam-policy-binding smtp-password \
  --member="serviceAccount:${RUNTIME}" --role=roles/secretmanager.secretAccessor
```

| 환경 변수 | 의미 |
|-----------|------|
| `SMTP_HOST`/`SMTP_PORT` | SMTP 서버·포트(587 STARTTLS, 465 SSL) |
| `SMTP_USER`/`SMTP_PASSWORD` | 로그인 계정·비밀번호(앱 비밀번호). PASSWORD는 Secret Manager |
| `SMTP_FROM` | 발신자 주소(미지정 시 SMTP_USER) |
| `NOTIFY_ON_PUBLISH` | `1`이면 발행 시 병동 구성원에게 메일(기본 꺼짐) |

> 이 값들을 지정하지 않으면 이메일 기능은 자동 비활성(수신함만 동작).

**GitHub Actions CD에 적용** (워크플로 수정 불필요): 배포 명령의 `--set-env-vars`·
`--set-secrets`에 아래 저장소 **변수**가 그대로 이어붙는다. **각 값은 앞에 콤마로 시작**해야
기존 값(`DUTY_DB`·`DUTY_SECRET`)과 병합된다. 비워두면 이메일 없이 배포된다.

| 변수 | 값(예, 맨 앞 콤마 주의) |
|------|------|
| `EXTRA_ENV` | `,SMTP_HOST=smtp.gmail.com,SMTP_PORT=587,SMTP_USER=you@gmail.com,SMTP_FROM=you@gmail.com,NOTIFY_ON_PUBLISH=1` |
| `EXTRA_SECRETS` | `,SMTP_PASSWORD=smtp-password:latest` |

## 7.6 데이터 백업 권한 `DUTY_BACKUP_CLAIM_CODE` (설정하지 않으면 기능 잠김)

`GET /api/admin/backup` 은 병동 **전체 데이터(실명·사번·피드백 원문 포함)** 를 ZIP으로
반출한다. 그래서 `role=="master"` 만으로는 열리지 않는다 — 마스터는 **병동마다** 생기므로
(병동 개설자 = 그 병동의 마스터) **누구나 새 병동을 열면 master가 된다**. 즉 role 조건은
실질 방어가 아니다.

실제로 막는 것은 **`users.backup_owner` 플래그**다. 권한은 **번호가 아니라 계정에 붙고**,
그 플래그를 켜려면 **운영자만 아는 권한 코드**를 앱 화면에서 1회 제출해야 한다(운영자
결정 D-19).

### 왜 환경변수 지정(사번·이메일·uid)을 버렸나 (검수부 침투 재현 결과)

| 예전 방식 | 실제로 뚫린 방법 |
|---|---|
| `DUTY_BACKUP_OWNER` = 사번 문자열 | **선점**: 그 사번이 아직 아무 계정에도 없는 동안, 공격자가 새 병동을 열어 master가 된 뒤 그 사번을 자기 계정에 등록 → 전체 DB 반출 성공 |
| `DUTY_BACKUP_OWNER` = 이메일 문자열 | **유니코드 접힘**: 이메일은 소문자로 저장하는데 비교는 대문자로 해서, 점 없는 `ı`(U+0131)를 쓴 **다른 이메일**이 같은 대문자열로 접혀 통과 → 전체 DB 반출 성공 |
| `DUTY_BACKUP_OWNER_UID` = uid 숫자 | **초기화 상속**: 선점·접힘은 막혔지만, 볼륨을 잃어 **DB가 초기화되면** 새 첫 가입자가 uid 1을 물려받아 환경변수에 적힌 권한까지 그대로 상속했다 → 복구 전에 아무나 먼저 가입하면 반출 성공 |

권한 코드 방식에는 세 경로가 다 없다. **DB가 초기화되면 플래그도 함께 사라져 전원 거부**가
되고, 코드를 모르면 다시 켤 수 없다. 초기화 뒤 재등록이 필요한 것은 결함이 아니라 설계
의도다 — 그것이 이 방식의 존재 이유다.

**예전 변수 `DUTY_BACKUP_OWNER` · `DUTY_BACKUP_OWNER_UID` 는 설정해도 아무 효과가 없다.
남아 있으면 삭제할 것.**

### 설정 순서

1. **권한 코드를 만든다.** 사람이 고른 단어가 아니라 무작위 문자열을 쓴다.
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(24))"
   ```
   - **8자 미만이면 서버가 등록 기능 자체를 끈다**(fail-closed). 위 명령의 길이를 그대로 쓸 것.
   - **이 값을 문서·저장소·이슈·메신저에 적지 않는다.** 배포 플랫폼의 환경변수 화면에만 넣는다.

2. **환경변수로 넣고 재배포한다.**
   ```bash
   # Cloud Run
   gcloud run services update "$SERVICE" --region "$REGION" \
     --update-env-vars DUTY_BACKUP_CLAIM_CODE='<1단계에서 만든 값>'
   ```
   - Railway: 서비스 → **Variables** 탭에 `DUTY_BACKUP_CLAIM_CODE` 추가.
   - GitHub Actions CD를 쓰면 §7.5의 `EXTRA_ENV` 대신 **시크릿**으로 넘긴다(값이 로그·워크플로
     파일에 남지 않게).

3. **백업을 맡을 사람이 가입·로그인한다.** (첫 가입자가 병동을 개설하면 그 병동의 master)

4. **그 사람이 ⚙️ 설정 탭의 🔐 백업 권한 등록 카드에 코드를 입력하고 [등록]을 누른다.**
   성공하면 등록 카드가 사라지고 그 자리에 **💾 데이터 백업** 카드가 나타난다.
   - 코드는 저장되지 않는다. 화면에 남지도 않는다.
   - master가 아닌 계정(admin·staff)에는 등록 카드 자체가 보이지 않고 API도 403이다.

5. **등록이 끝나면 환경변수를 지워도 된다.** 권한은 이미 DB의 계정 행에 들어 있다.
   지워 두면 코드가 서버 환경에 남지 않아 더 안전하다(등록 기능만 꺼진다).
   ```bash
   gcloud run services update "$SERVICE" --region "$REGION" \
     --remove-env-vars DUTY_BACKUP_CLAIM_CODE
   ```
   - 나중에 담당자를 바꾸거나 **DB를 초기화해 재등록해야 할 때** 다시 넣으면 된다.
     그때는 **새 코드를 만들어** 넣는다(이전 코드를 재사용하지 않는다).

### 대입 시도 방어

| 조건 | 결과 |
|------|------|
| `DUTY_BACKUP_CLAIM_CODE` 미설정·빈 값·**8자 미만** | 등록 API가 **전원 403** — 약한 코드로 문이 열리지 않는다 |
| master가 아닌 계정의 등록 시도 | 403 + `backup_log(status='denied')` |
| 코드 오입력 | 403 + `denied` 기록. 비교는 `hmac.compare_digest`(응답 시간으로 앞자리가 새지 않는다) |
| 같은 계정·같은 IP에서 **5회 실패** | 이후 **15분간 429** — 정답을 넣어도 잠금이 우선한다 |

> 잠금 카운터는 **프로세스 메모리**에 있다(IP를 DB·백업 ZIP에 남기지 않기 위해서 —
> 교훈 L-1). 인스턴스가 scale-to-zero로 내려갔다 오면 카운터가 초기화된다. 대입 공격은
> 인스턴스를 계속 깨워 두므로 공격이 진행되는 동안에는 유지되고, 시도 사실 자체는
> `backup_log`의 `denied` 행으로 영구히 남는다.

### 반출 판정 요약

| 조건 | 결과 |
|------|------|
| `users.backup_owner=1` + `role=="master"` | 200 (ZIP) |
| 플래그는 있으나 admin·staff로 강등됨 | 403 |
| 플래그 없는 master(다른 병동 개설자·초기화 후 첫 가입자 포함) | 403 |
| 토큰 없음·만료 | 401 |

**백업 이력**: `backup_log` 테이블에 한 행씩 쌓인다(actor는 `uid:<번호>` 만 — 실명·사번은
남기지 않는다). `status` 값의 뜻은 다음과 같다.

| status | 언제 남는가 |
|---|---|
| `pending` | 내려받기 요청을 받아 파일을 만들기 **직전**. 아직 전달·확인되지 않았다 |
| `ok` | 사용자가 **다운로드 폴더에서 파일을 눈으로 확인하고** [확인했습니다]를 눌렀을 때(`POST /api/admin/backup/confirm`, 받은 바이트 수 대조) |
| `fail` | 스냅샷 구조 손상·시간초과 등으로 만들지 못했을 때(응답은 500) |
| `denied` | 권한 없는 계정이 반출·권한 등록을 시도했을 때. **uid당 하루 1행으로 합쳐서** 남긴다(로그인만 하면 누구나 두드릴 수 있어 합치지 않으면 진짜 시도가 잡음에 묻힌다) |
| `archived` | **백업본 안에만** 나타난다. 스냅샷을 담을 때 그 안의 `ok` 행을 이 값으로 바꾼다 — 복구본이 "이미 백업돼 있음"으로 보이지 않게 하려는 것이다(아래 복구 절차 8단계) |

`pending` 이 `ok` 로 바뀌지 않은 행이 있다면 **그 백업은 완료되지 않았다**는 뜻이다.
다만 **어느 이유였는지는 사후에 구분할 수 없다** — (a) 전송이 중간에 끊겼거나, (b) 브라우저가
저장을 거부했거나, (c) 사용자가 확인 창에서 이탈했거나, (d) 프로세스가 강제 종료된 경우가
모두 똑같은 `pending`으로 남는다. 어느 쪽이든 **경고는 꺼지지 않는다**(예전에는 전송 전에
`ok`를 남겨, 파일이 없는데도 30일간 경고가 꺼졌다).

`GET /api/admin/backup/status` 가 마지막 **성공(`ok`)** 시각과 경고 단계(KST 경과일 기준
`ok`<30일 / `warn` 30~44 / `critical` ≥45일·이력 0건·**경과일이 음수**(서버 시계 역행))를
돌려주고, 화면 팝업·배너가 이 값을 따른다. 최근 30일 `denied` 건수도 함께 내려가 백업
카드에 표시된다. "성공 이력"은 `created_at`이 아니라 **`id`(기록 순서) 기준**으로 고른다 —
시계가 한 번 앞섰을 때 남은 미래 시각 행이 이후의 모든 정상 백업을 영원히 가리지 않게
하기 위해서다.

### 백업본이 비어 있지 않은지 확인하는 법

`DUTY_DB` 경로를 잘못 적거나 볼륨이 마운트되지 않으면, 앱이 **빈 DB에 스키마를 새로
만들어** 준다. 그러면 압축은 정상적으로 만들어지고 화면에도 "백업 완료"로 보이지만
**정작 데이터는 한 건도 들어 있지 않다.** 그래서 두 가지 확인 수단을 넣었다.

1. **README.txt의 건수** — ZIP 안 README 맨 위에 `users … 27건` 처럼 주요 테이블 행수가
   적혀 있다. 0이거나 평소보다 크게 적으면 잘못된 DB를 백업한 것이다.
2. **크기 급감 경고** — 직전 성공 백업의 **1/10 미만**이면 저장 확인 창에 경고가 뜬다.

> `PRAGMA quick_check`도 돌지만 이것이 잡는 것은 **구조 손상(파일이 깨진 것)** 뿐이다.
> **값 오염(내용이 바뀐 것)은 검출 대상이 아니다** — 같은 길이로 in-place 변조된 값은
> quick_check를 그대로 통과한다. 위 두 가지가 실질적인 확인 수단이다.

### 복구 절차 (수작업 — 앱에는 업로드 기능이 없다)

> **누가 하는가**: **백업 ZIP을 가진 운영자 본인이 자기 컴퓨터에서 직접** 올린다
> (운영자 결정 D-20). 파일을 다른 사람에게 넘기지 않는다 — 그 안에는 간호사 실명·사번·
> 피드백 원문과 비밀번호 해시·초대 코드가 들어 있고, 사용자 안내서는 모든 전달 수단
> (메신저·메일·클라우드 공유)을 금지하고 있다.
>
> **먼저 알아둘 것**: 복구하면 **백업 시각 이후에 입력된 데이터는 모두 사라진다**
> (그 뒤 만든 근무표·원티드 신청·가입 계정·피드백). 복구 전에 마스터에게 "언제 시점으로
> 되돌아가는지"를 알리고 동의를 받는다.

#### 준비 (한 번만)

```bash
# (a) Railway CLI — 볼륨 파일을 넣고 뺄 수 있는 유일한 수단(브라우저만으로는 안 된다)
npm i -g @railway/cli
railway --version        # 버전이 찍히면 성공

# (b) sqlite3 CLI — 백업본 검사용
#     macOS: 기본 탑재     Ubuntu/Debian: sudo apt install sqlite3
#     Windows: https://sqlite.org/download.html 의 sqlite-tools zip → 압축 풀고 PATH에 추가
sqlite3 --version        # 버전이 찍히면 성공

railway login            # 브라우저가 열리며 로그인 → 터미널에 계정 이름이 찍히면 성공
railway link             # 프로젝트·서비스 선택 → "Project linked successfully" 가 뜨면 성공
```

#### 1. 자동 배포를 먼저 멈춘다

**이것을 1단계에 두는 이유**: Railway는 `main` 브랜치를 자동 배포한다. 복구 도중 누군가
`main`에 커밋을 넣으면 **멈춰 둔 서비스가 되살아나** 교체 중인 파일을 다시 붙잡는다.

- Railway 대시보드 → 서비스 → **Settings → Source → Automatic Deploys 를 끈다**
  (토글이 꺼진 것을 눈으로 확인한다).
- Cloud Run(GitHub Actions CD)이면 저장소 **Actions → Deploy 워크플로 → ⋯ → Disable workflow**.

#### 2. 백업본이 성한지 확인한다 (교체 전에)

깨진 파일로 교체하면 되돌릴 수 없다. **자기 컴퓨터에서** ZIP을 풀어 `duty.db`만 꺼낸다
(`tables/*.csv`는 사람이 보는 사본이라 복구에 쓰지 않는다. 비밀번호·초대 코드 자리는
`(생략)`으로 가려져 있어 복구에 쓸 수도 없다).

```bash
sqlite3 duty.db "PRAGMA integrity_check;"     # 'ok' 가 아니면 중단하고 다른 백업본을 쓴다
sqlite3 duty.db "SELECT 'users', COUNT(*) FROM users
                 UNION ALL SELECT 'rosters', COUNT(*) FROM rosters
                 UNION ALL SELECT 'schedules', COUNT(*) FROM schedules
                 UNION ALL SELECT 'wanted_requests', COUNT(*) FROM wanted_requests
                 UNION ALL SELECT 'feedback', COUNT(*) FROM feedback;"
```

**행수가 얼마여야 정상인가**: 같은 ZIP 안 **README.txt에 적힌 건수와 정확히 같아야 한다.**
그리고 상식 기준으로 `users`는 **병동 인원수 + 관리자 수**(한 병동이면 보통 20~40),
`rosters`는 **병동 수와 같다**(한 병동이면 1). `users`가 0~1이거나 `rosters`가 0이면
**빈 DB를 백업한 것**이므로 복구에 쓰지 말고 다른 백업본을 찾는다.

> `integrity_check`가 잡는 것은 **구조 손상(파일이 깨진 것)** 뿐이다. **값 오염(내용이 바뀐
> 것)은 검출되지 않는다** — 같은 길이로 변조된 값은 그대로 통과한다. 행수 확인이 필요한
> 이유가 이것이다.

#### 3. 서비스를 멈춘다 (쓰기 중 교체 금지)

살아 있는 앱이 `-wal`/`-shm`을 다시 만들면 새로 올린 파일과 섞여 **손상된다.**

```bash
railway down            # 현재 배포를 내린다. 확인 프롬프트에 y
```

- 대시보드로 할 때: 서비스 → **Deployments** → 실행 중인 배포의 **⋯ → Remove**.
- **성공 확인**: `https://<도메인>/health` 가 응답하지 않아야 한다(연결 실패/502).
  여전히 `{"status":"ok"}` 가 나오면 아직 살아 있는 것이므로 다음 단계로 넘어가지 않는다.
- Cloud Run이면 `gcloud run services update "$SERVICE" --region "$REGION" --max-instances 0`.

#### 4. 볼륨의 기존 파일을 옆으로 치운다 (지우지 않는다)

```bash
railway volume files list /data                                   # 무엇이 있는지 확인
railway volume files download /data/duty.db     ./duty-before-restore.db
railway volume files download /data/duty.db-wal ./duty-before-restore.db-wal   # 없으면 건너뛴다
railway volume files download /data/duty.db-shm ./duty-before-restore.db-shm   # 없으면 건너뛴다
```

- `duty.db`, `duty.db-wal`, `duty.db-shm` **3개를 같이** 받아 둔다. 이 3개가 복구 실패 시의
  **원복 수단**이다. 복구가 확실히 성공할 때까지 지우지 않는다.
- 그런 다음 볼륨에서 `-wal`·`-shm`을 **삭제한다**(`railway volume files delete /data/duty.db-wal`).
  남겨 두면 새 `duty.db`와 섞여 손상된다.
- **볼륨이 여러 개인 프로젝트**라면 어느 볼륨인지 지정해야 한다: `railway volume files list /data -v <볼륨이름>`
  (짧은 옵션 `-v`, 긴 옵션 `--volume`). 볼륨 이름은 `railway volume list` 로 확인한다.
- CLI 버전에 따라 하위 명령 이름이 다를 수 있다. `railway volume --help` 로 확인한다.

#### 5. 백업본을 올린다

```bash
railway volume files upload ./duty.db /data/duty.db
railway volume files list /data      # duty.db 크기가 올린 파일과 같은지 확인
```

- 경로는 `DUTY_DB` 값과 같아야 한다(Railway: `/data/duty.db`, Cloud Run: `/data/duty.db`).
- 대안: `scp ./duty.db <서비스도메인>@ssh.railway.com:/data/duty.db` (Railway SSH).

#### 6. 서비스를 다시 띄우고 확인한다

```bash
railway redeploy         # 또는 대시보드 Deployments → Restart
```

- **성공 확인**: `https://<도메인>/health` 가 `{"status":"ok"}` → 로그인 → 명단·근무표가
  복구 시점 내용으로 보인다.

#### 7. 실패하면 원복

3단계로 서비스를 다시 멈추고, 5에서 올린 파일과 새로 생긴 `-wal`/`-shm`을 지운 뒤,
4에서 받아 둔 3개를 원래 이름으로 다시 올리고 6단계로 띄운다.

#### 8. 복구 직후: 빨간 배너와 백업 권한

- **"데이터를 한 번도 백업하지 않았습니다" 빨간 배너가 뜬다. 정상이다.**
  백업본 안의 성공 이력은 담길 때 `archived`로 바뀌므로 복구본에는 **성공 이력이 0건**이다
  (그렇게 하지 않으면 2회차 이후 백업본으로 복구했을 때 "며칠 전에 백업함"으로 초록 표시가
  되어, 백업이 가장 필요한 순간에 정확히 침묵한다). 마스터에게 안내하고 **즉시 1회 백업을
  받게 한다** — 그러면 사라진다.
- **백업 권한**: 백업본으로 복구했다면 권한 플래그도 그 백업 시점 그대로 살아난다(재등록 불필요).
  그러나 **볼륨을 잃어 빈 DB로 새로 시작한 경우에는 권한이 없다** — §7.6의 순서대로
  `DUTY_BACKUP_CLAIM_CODE`를 **새로 만들어** 넣고 1회 재등록해야 한다. 이것은 설계 의도다.

#### 9. 마무리

- 1단계에서 끈 **자동 배포를 다시 켠다.**
- 자기 컴퓨터에 임시로 꺼내 둔 `duty.db`·원복용 사본을 **삭제한다**(개인정보 잔존 금지).

> 백업은 **`VACUUM INTO`**(잠금·구버전 SQLite로 실패하면 `Connection.backup()`)로 뜬 일관된
> 스냅샷이라 WAL이 반영된 단일 파일이다. 운영 중에도 안전하게 받을 수 있고, 받는 동안 앱은
> 계속 동작한다. 만들어진 스냅샷은 내려주기 전에 `PRAGMA quick_check`로 **구조 손상**을
> 검사하며, 통과하지 못하면 200이 아니라 **500 + `backup_log(status='fail')`** 이 된다
> (깨진 파일을 "성공"으로 내려주지 않는다). 값 오염은 이 검사로 잡히지 않는다.

#### Railway 볼륨 파일 조작에 관한 메모

Railway는 파일 관리자 화면이 없어서 **브라우저만으로는 볼륨의 파일을 바꿀 수 없다.**
위 복구 절차의 CLI 명령이 유일한 경로다. 대화형으로 훑어보고 싶으면
`railway volume browse /` 를 쓸 수 있다(업로드·내려받기·삭제를 화면에서 고른다).

## 8. 첫 배포

- `main`에 병합하면 자동 실행되거나,
- **Actions 탭 → Deploy to Cloud Run → Run workflow** 로 수동 실행.

로컬에서 먼저 확인하고 싶다면:

```bash
gcloud run deploy "$SERVICE" --source . --region "$REGION" \
  --allow-unauthenticated --execution-environment gen2 \
  --cpu 2 --memory 2Gi --concurrency 8 --timeout 300 --min-instances 0 --max-instances 1 \
  --set-env-vars DUTY_DB=/data/duty.db \
  --set-secrets DUTY_SECRET=duty-secret:latest \
  --add-volume name=data,type=cloud-storage,bucket="$BUCKET" \
  --add-volume-mount volume=data,mount-path=/data
```

배포 후 출력된 URL의 `/health`가 `{"status":"ok"}`를 반환하면 성공. 첫 접속 사용자가
자동으로 **마스터**가 된다.

---

## 로컬에서 컨테이너 검증 (선택)

```bash
docker build -t duty:local .
docker run --rm -p 8080:8080 -e DUTY_SECRET=devsecret -e DUTY_DB=/data/duty.db duty:local
# 다른 터미널: curl localhost:8080/health
```

## 운영 메모

- **비용(중요)**: 기본값을 **`--min-instances 0`(scale-to-zero)** 로 둔다. 한 병동이 월 몇 번
  생성하는 저트래픽에서는 안 쓸 때 인스턴스가 0으로 내려가 **사실상 무료**(Cloud Run 무료 티어
  vCPU-s·GiB-s·요청 범위 내). GCS·시크릿·빌드도 무료 티어. 정확한 비용은 GCP 요금 계산기로 확인.
- **콜드스타트 트레이드오프**: scale-to-zero면 오래 안 쓰다 첫 요청 시 OR-Tools 임포트로 **~8~10초**
  대기가 생긴다. 월 단위로 쓰는 도구라 보통 감수 가능. **항상 즉시 응답이 필요하면**
  `--min-instances 1`로 바꾼다(단, 인스턴스 상시 과금 → 월 $30~45 수준). 항상 켤 땐 SQLite
  단일 인스턴스 일관성도 함께 확보된다(scale-to-zero도 `--max-instances 1`이라 동시 쓰기는 없음).
- **동시성·타임아웃**: 근무표 생성은 CPU를 많이 쓰므로 `--concurrency 8`, `--cpu 2`로 제한.
  운영 모드(정확 인원 패턴) 생성은 최대 120초 계산하므로 요청 타임아웃을 `--timeout 300`으로
  여유를 둔다(콜드스타트 ~8초 + 계산 120초 + 여유). 메모리는 OR-Tools 대규모 풀이를 위해 `2Gi`.

## 확장(Postgres 이관) 경로 — 사용자·병동이 늘면

SQLite+단일 인스턴스는 한 병동 MVP용이다. 여러 병동·다수 동시 사용자로 커지면:

1. Supabase(무료 티어) 또는 Cloud SQL(Postgres) 프로비저닝.
2. `app/auth.py`·`app/storage.py`의 `sqlite3` 접근을 Postgres 드라이버로 교체
   (스키마는 이미 `ward`로 테넌트 스코프되어 있어 RLS 적용이 쉽다 — PLAN 7.5.5).
3. `--min/max-instances` 제한을 풀고 자동 확장 활성화, GCS 볼륨 마운트 제거.
4. `DUTY_DB` 대신 `DATABASE_URL` 환경변수로 연결.

이 변경은 라우터·프론트엔드를 건드리지 않고 데이터 접근 계층만 교체하면 된다.

---

## 부록 A. Railway 배포 (GCP 본인확인 대기 등 대안 경로)

GCP 한국 계정 본인확인(신분증·카드 사진, 며칠 소요)이 어려울 때, **브라우저만으로**
Railway(railway.app)에 같은 Docker 이미지를 배포할 수 있다. 월 약 $5(Hobby, 사용량 포함).

1. https://railway.app → **Login with GitHub** → Hobby 플랜 결제 카드 등록(신분증 불필요)
2. **New Project → Deploy from GitHub repo** → `Hyunnn135/duty` 선택(권한 허용)
   → Dockerfile을 자동 감지해 빌드·배포한다 (main 브랜치 기준)
3. 서비스 클릭 → **Variables** 탭:
   - `DUTY_SECRET` = 아무도 모르는 긴 무작위 문장(JWT 서명키 — 절대 공유 금지)
   - (`DUTY_DB`는 Dockerfile 기본값 `/data/duty.db` 사용, `PORT`는 Railway가 자동 주입)
   - `DUTY_BACKUP_CLAIM_CODE` = 백업 권한 코드(§7.6 1단계에서 만든 무작위 문자열).
     지금 넣어도 되고 7번에서 넣어도 된다 — 코드는 계정과 무관하므로 순서를 타지 않는다.
4. 서비스 우클릭(또는 Settings) → **Attach Volume** → mount path `/data`
   (SQLite 영속화 — 볼륨 없이 재시작하면 데이터가 사라진다!)
5. **Settings → Networking → Generate Domain** → 공개 URL 발급
6. `https://<도메인>/health` 가 `{"status":"ok"}` 면 성공. 첫 가입자가 병동 개설(마스터).
7. 백업을 맡을 사람이 **가입한 뒤** 로그인 → **⚙️ 설정 → 🔐 백업 권한 등록** 카드에
   권한 코드를 입력하고 [등록]. 등록 카드가 사라지고 **💾 데이터 백업** 카드가 나타나면 성공.
8. 등록이 끝났으면 **Variables 탭에서 `DUTY_BACKUP_CLAIM_CODE`를 지운다**(§7.6 5단계).
   권한은 이미 계정에 붙어 있어 코드가 서버에 남을 이유가 없다.
   - 볼륨의 파일을 직접 바꿔야 할 때(복구)는 브라우저만으로는 안 되고 Railway CLI가
     필요하다 — §7.6의 "복구 절차" 참고.

- GCP 워크플로(deploy.yml)는 `GCP_PROJECT_ID` 변수가 없으면 자동 스킵되므로 충돌 없음.
- 이후 GCP 승인이 나면: 본 문서 §1~8로 Cloud Run에 띄우고, Railway 볼륨의
  `/data/duty.db` 파일을 GCS 버킷으로 복사하면 데이터 이전 완료.
