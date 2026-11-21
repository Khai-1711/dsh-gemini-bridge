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

## 핵심 기술 보고서 (요약)

### 1. 해결 과제 및 주요 개선 사항 (v1 → v3.2)

| 영역 | 기존 웹 래퍼의 한계 | DSH Bridge v3.2의 핵심 개선 |
|---|---|---|
| **세션 및 인증** | DevTools 수동 복사 필요; `osid=1` 리다이렉트로 `1PSID+1PSIDTS` 단독 전송 거부; `gemini-webapi` HTTP 400 재귀 `_lock` 데드락 | **Profile Vault + 헤드리스 CDP 동기화** (`app/cookie_sync.py`): 1회 실브라우저 로그인, 2~4초 헤드리스 전체 쿠키(`OSID`, `SID`, `1PSID`, `1PSIDTS`, `1PSIDCC`, `NID`) 자동 추출, 열린 탭 0개, `25s` 데드락 방지 `_safe_init_client` |
| **모델 및 세션 격리** | 하드코딩된 모델 우선순위; 병렬 에이전트 간 모델·세션 오염 | **100% 사용자 선택 모델 & `SessionSlot` 풀** (`app/session_pool.py`): 대화별 `(cid/rid/rcid, model, workspace, FactLedger)` 완전 격리; `FALLBACK_MODEL` 명시적 선택(Opt-in) |
| **도구 호출 신뢰성** | Gemini Web의 URL 자동 링크 변환(`[http://...](http://...)`), 손상된 JSON 및 `<tool_call>` 잘림 | **Fail-Closed 게이트 + 자가 수정** (`app/streaming.py`, `app/tool_parser.py`): 백틱 안전 $O(N)$ FSM 스트리머, 재귀적 URL 복원, `json-repair`, 1회 내부 자가 수정 및 빈 응답 1회 재시도 |
| **멀티 에이전트 동시성** | 락 획득 전 SSE `200` 응답(페일오버 불가); 병렬 도구 대량 결과 시 추론 타임아웃 | **사전 SSE 턴 락(`429` 실제 페일오버)** + **Sticky/Least-Inflight 게이트웨이** (`app/gateway_router.py`) + **36k 공정 도구 예산** + **180s/120s/420s** 완주 우선 타임아웃 |
| **워크스페이스 보안** | 특정 드라이브(`D:\ABC`) 고정으로 일반 프로젝트 차단 | **동적 워크스페이스 개방** (`app/security.py`): 모든 드라이브의 IDE 작업 폴더를 자동 추적하며 OS 파괴 명령/경로만 차단 |

### 2. 적용된 핵심 알고리즘
- **단일 패스 SSE 억제 FSM (`app/streaming.py`):** $O(N)$ 유한 상태 기계 + 접두사 룩어헤드 + 인라인 백틱 추적으로 일반 텍스트 내 `` `<tool_call>` `` 언급은 유지하고 실제 호출 태그만 분리.
- **결정적 파싱 및 제한된 자가 수정 (`app/tool_parser.py`, `app/openai_adapter.py`):** 문자열 인식 괄호 스캐너, 외부 후행 쉼표 제거, `json-repair`, JSON Schema 검증, 1회 진단 피드백 재시도.
- **중요도 보존 절단 (`app/prompt_builder.py`):** 앞(2/3) + 뒤(1/3) + 중간 오류/스택트레이스 추출 + 병렬 도구 결과 공정 예산 분할 + 세션 복구용 `Context Bootstrap`.
- **Sticky Least-Inflight 라우팅 및 서킷 브레이커 (`app/gateway_router.py`):** 대화 고정(`3600s`), 최소 부하 워커 선택, 모델 지원 필터링, `15s` 장애 쿨다운.
- **무의존성 RFC 6455 CDP 클라이언트 (`app/cookie_sync.py`):** 순수 Python `asyncio` WebSocket 프레이밍(7/16/64비트, 분할 프레임 재조립, Ping/Pong)과 `asyncio.Semaphore(1)` 순차 실행.

### 3. 참고 연구 논문 및 기술 표준
- **ReAct** — Yao et al. (ICLR 2023), *Synergizing Reasoning and Acting in Language Models*.
- **Toolformer** — Schick et al. (NeurIPS 2023), *Language Models Can Teach Themselves to Use Tools*.
- **Reflexion & Self-Refine** — Shinn et al. (NeurIPS 2023) & Madaan et al. (NeurIPS 2023).
- **Lost in the Middle** — Liu et al. (TACL 2024), *How Language Models Use Long Contexts*.
- **Load Balancing & Resilience** — Mitzenmacher (2001), *The Power of Two Choices*; Nygard (2018), *Release It!* (Circuit Breaker & Bulkhead).
- **표준 프로토콜:** RFC 6455 (WebSocket), RFC 8259 & JSON Schema Draft 2020-12, Chrome DevTools Protocol (CDP), WHATWG SSE.

### 4. 활용 오픈소스 저장소 및 개선 기여

| 오픈소스 저장소 | 프로젝트 내 역할 | 본 프로젝트의 고도화·확장 내역 |
|---|---|---|
| [`HanaokaYuzu/Gemini-API`](https://github.com/HanaokaYuzu/Gemini-API) (`gemini-webapi`) | Gemini Web `ChatSession`, `batchexecute`, `RotateCookies` | HTTP 400 데드락 방지 `_safe_init_client`, Chrome CDP 전체 쿠키 주입(`osid=1` 해결), 워커별 `GEMINI_COOKIE_PATH` 격리 |
| [`mangiucugna/json_repair`](https://github.com/mangiucugna/json_repair) | 비정상 JSON 복구 | 문자열 인식 스캐너, URL 언링크, 인자 별칭 정규화 및 엄격한 JSON Schema Fail-Closed 게이트 결합 |
| [`fastapi/fastapi`](https://github.com/fastapi/fastapi) · [`encode/uvicorn`](https://github.com/encode/uvicorn) · [`encode/httpx`](https://github.com/encode/httpx) | 비동기 OpenAI 호환 서버 및 게이트웨이 프록시 | 사전 SSE 턴 락, 제로 버퍼 스트리밍(`read=None`), `2.5s` 하트비트, 루프백·Origin 보안 미들웨어 |
| [`lexiforest/curl_cffi`](https://github.com/lexiforest/curl_cffi) | 브라우저 TLS/HTTP2 모방 (`gemini-webapi` 내부) | 장시간 추론 모델을 위한 `420s` 스트림 / `180s` 워치독 타임아웃 최적화 |
| **DeepSeek Harness (`pi-ai`) & OpenAI SDK 클라이언트** | 대상 코딩 에이전트 생태계 | `/v1/chat/completions`·`/v1/responses` 완전 호환, `<5ms` 로컬 제목 생성, 다중 서브에이전트 병렬 분산 |

> 📄 상세 아키텍처 다이어그램 및 복잡도 분석: [docs/ARCHITECTURE.ko.md](ARCHITECTURE.ko.md)

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
