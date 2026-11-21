# DSH Gemini Agent Bridge (v3.2)

> 🌐 **언어:** [English](../README.md) | [Tiếng Việt](README.vi.md) | **한국어**  
> 📐 **아키텍처, 알고리즘 및 참고 연구:** [English](ARCHITECTURE.en.md) | [Tiếng Việt](ARCHITECTURE.vi.md) | [한국어](ARCHITECTURE.ko.md)

**DSH Gemini Agent Bridge**는 코딩 에이전트(DeepSeek Harness / `pi-ai`, IDE 에이전트, OpenAI SDK 클라이언트)를 **Gemini Web**에 연결하는 모듈형 OpenAI 호환(`/v1/models`, `/v1/chat/completions`, `/v1/responses`) 리버스 프록시 및 멀티 레인 실행 엔진입니다. 실시간 SSE 스트리밍, 다중 계정 페일오버, 브라우저 탭 없이 동작하는 **Profile Vault 자동 쿠키 동기화**, Fail-Closed 도구 호출 검증을 제공합니다.

---

## 주요 기능

- **멀티 레인 프론트엔드 게이트웨이 (`:10000` → `:10001`, `:10002`, ...):** 대화 고정(Sticky Session), 최소 동시 요청(Least-Inflight) 분산, 모델 지원 여부 기반 라우팅, SSE 스트림 시작 전 턴 락 획득을 통한 실제 `HTTP 429` 페일오버, 제로 버퍼 SSE 패스스루를 지원합니다.
- **100% 사용자 선택 모델 & 세션 슬롯 격리:** 계정에 존재하는 모든 Gemini 모델을 하드코딩된 우선순위나 숨김 없이 그대로 노출합니다. 각 `SessionSlot`은 독립적인 모델과 워크스페이스 컨텍스트를 유지하며, `FALLBACK_MODEL`은 명시적으로 설정한 경우에만 동작합니다.
- **자유로운 워크스페이스 경로 지원:** 드라이브(`C:`, `D:`, `E:` 등) 제한 없이 IDE/대화에서 연 프로젝트 폴더를 자동 인식(`X-Workspace-Dir` 또는 프롬프트 컨텍스트)하며, OS 파괴 명령/경로(`System32`, UNC, `format`, `diskpart`, `IWR | IEX` 등)만 차단합니다. `WORKSPACE_SANDBOX_MODE=client|workspace` 모드를 지원합니다.
- **Profile Vault & 헤드리스 자동 쿠키 동기화 (열린 탭 0개):** 비밀번호를 저장하거나 브라우저 탭을 열어 두지 않고도 여러 Google 계정(`acc1`, `acc2`, `work3`, ...)을 관리할 수 있습니다. 격리된 Chrome/Edge 프로필에서 최초 1회만 직접 로그인하면, 이후에는 표준 라이브러리 RFC 6455 CDP WebSocket + `--headless=new`로 2~4초 만에 전체 Google/Gemini 쿠키(`OSID`, `__Secure-OSID`, `SID`, `__Secure-1PSID`, `__Secure-1PSIDTS`, `__Secure-1PSIDCC`, `NID`)를 추출해 워커에 즉시 핫 리로드합니다.
- **Fail-Closed 도구 호출 & 내부 자가 수정:** $O(N)$ 접두사 보류 SSE 스트리머, 인라인 백틱(`` `<tool_call>` ``) 오탐 방지, Gemini Web의 Markdown URL 자동 링크 변환 복원(`[http://...](http://...)` → `http://...`), `json-repair` 기반 결정적 복구, 오류 반환 전 1회 내부 자가 수정(`TOOL_PARSE_SELF_CORRECTION`), 빈 응답 1회 자동 재시도를 제공합니다.
- **장시간 추론(Thinking) 친화적 타임아웃:** `GEMINI_HARD_TIMEOUT=180s`(3분), `GEMINI_CHUNK_IDLE_TIMEOUT=120s`(2분), `GEMINI_STREAM_OVERALL_TIMEOUT=420s`(7분), `2.5s` SSE Keep-Alive 하트비트, `25s` 초기화 데드락 차단(`_safe_init_client`), 병렬 도구 결과 공정 예산 배분(`MAX_TOTAL_TOOL_RESULTS_CHARS=36000`)을 기본 제공합니다.
- **실시간 웹 대시보드 (`/dashboard`):** 레인 제어, 모델 전환, Profile Vault 관리(`1회 로그인`, `완료 및 동기화`, `동기화`, `전체 동기화`), 실시간 메트릭 및 로그 콘솔을 제공합니다.

---

## 디렉터리 구조

```text
dsh-gemini-bridge/
├── app/                     # 코어 런타임, 게이트웨이 라우터, CDP Profile Vault, 도구 파서, 보안
├── docs/                    # 3개 국어(EN/VI/KO) README 및 아키텍처·알고리즘·연구 문서
├── frontend/                # 빌드 불필요 Web Components 대시보드 (/dashboard)
├── scripts/                 # CLI 도구 (cookie_sync.py, live_smoke.py) 및 개별 런처 (launchers/)
├── tests/                   # 100% 오프라인 격리 단위·통합 테스트 230개
├── main.py                  # FastAPI 진입점 (BRIDGE_INSTANCE에 따라 게이트웨이 또는 워커 실행)
├── requirements.txt         # 최소 Python 의존성
├── run_all.bat              # 원클릭 실행 (Gateway :10000 + Workers :10001, :10002)
├── stop_all.bat             # 원클릭 종료 (포트 10000..10002)
└── .env.example             # 전체 환경 변수 가이드
```

---

## 빠른 시작

### 1. 의존성 설치
```powershell
pip install -r requirements.txt
```

### 2. 환경 변수 설정
`.env.example`을 복사해 `.env`(게이트웨이 `:10000`)와 `.env.acc1`, `.env.acc2`(워커 `:10001`, `:10002`)를 생성합니다.
- 모든 `.env*` 파일에 동일한 `LANE_ADMIN_TOKEN`을 설정합니다.
- `.env`에 `PEER_LANES="acc1=http://127.0.0.1:10001,acc2=http://127.0.0.1:10002"`를 지정합니다.

### 3. Google 계정 최초 1회 로그인 (Profile Vault)
```powershell
python scripts/cookie_sync.py login acc1 --lane acc1 --slot 0 --label "Work 1"
python scripts/cookie_sync.py login acc2 --lane acc2 --slot 0 --label "Work 2"
# 백업 계정(슬롯 1..4) 또는 추가 레인 확장:
python scripts/cookie_sync.py login work3 --lane acc1 --slot 1 --label "Backup 1"
```
브라우저 창 없이 전체 계정 쿠키를 백그라운드 동기화:
```powershell
python scripts/cookie_sync.py sync-all
python scripts/cookie_sync.py list
```

### 4. 실행 및 확인
```powershell
.\run_all.bat
# 대시보드: http://127.0.0.1:10000/dashboard
# OpenAI Base URL: http://127.0.0.1:10000/v1
# 전체 종료:
.\stop_all.bat
```

---

## 테스트 및 보안 검증
```powershell
python -B -m unittest discover -s tests
ruff check --no-cache --select F,E9,B main.py app tests scripts
```
- `.env*`, `.cookie_profiles/`, `.gemini_cookies*/`, `logs/`, `.bridge_*.json`은 `.gitignore`로 완벽히 제외됩니다.
- `/v1/profiles/*` 엔드포인트는 Origin 검증과 루프백(`127.0.0.1`) 전용 접근 제어를 강제합니다.
