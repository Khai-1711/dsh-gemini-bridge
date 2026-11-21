# 시스템 아키텍처, 알고리즘 및 참고 연구 논문 (v3.2)

> 🌐 **언어:** [English](ARCHITECTURE.en.md) | [Tiếng Việt](ARCHITECTURE.vi.md) | **한국어**  
> 📘 **README:** [English](../README.md) | [Tiếng Việt](README.vi.md) | [한국어](README.ko.md)

---

## 1. 엔드투엔드 요청 생명주기

```text
[코딩 에이전트 클라이언트 (DeepSeek Harness / pi-ai / OpenAI SDK)]
        │  POST /v1/chat/completions 또는 /v1/responses (+ 선택적 X-Workspace-Dir)
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 프론트엔드 게이트웨이 (:10000) — main.py + app/gateway_router.py         │
│  ├─ 보안 미들웨어: DNS 리바인딩 차단, API_KEY, X-Lane-Token 검증         │
│  ├─ 로컬 고속 경로: app/title_generation.py (<5ms 세션 제목 생성)        │
│  ├─ 라우팅: Sticky Affinity (3600s) + Least-Inflight + 모델 지원 필터링  │
│  ├─ 사전 SSE 페일오버: X-Gateway-Failover-Ready: 1 (2초 빠른 실패->429)  │
│  └─ Profile Vault 제어 평면: /v1/profiles/* (루프백 127.0.0.1 전용)      │
└──────────────┬───────────────────────────────────────┬──────────────────┘
               │ acc1 (:10001)                         │ acc2 (:10002)
               ▼                                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 워커 OpenAI 어댑터 — app/openai_adapter.py                               │
│  ├─ 턴 락 획득 전 읽기 전용 모델 해석                                    │
│  ├─ StreamingResponse(200) 반환 전 턴 락 선획득 -> 실제 HTTP 429 재시도  │
│  ├─ 격리된 SessionSlot 활성화 (model, workspace_root, FactLedger)        │
│  ├─ app/prompt_builder.py: 델타 프롬프트, Context Bootstrap, 병렬 도구   │
│  │  결과 공정 예산 배분(36k), Head+Tail+Error 중요도 절단                │
│  └─ StreamingResponse + 2.5초 주기 ": keep-alive\n\n" SSE 하트비트       │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 워커 실행 코어 — app/gemini_runtime.py + app/account_pool.py             │
│  ├─ 데드락 방지 _safe_init_client (초기화 25초 제한 / 스트림 420초 유지) │
│  ├─ Auto Cookie Sync: UNAUTHENTICATED 감지 시 헤드리스 CDP 자동 복구     │
│  ├─ 추론 친화적 타임아웃: 첫 청크 180초 / 유휴 120초 / 전체 420초        │
│  └─ 빈 응답 1회 자동 재시도 + 1097 오류 시 Context Bootstrap 세션 복구   │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Fail-Closed 프로토콜 게이트 — app/streaming.py + app/tool_parser.py      │
│  ├─ O(N) FSM ToolCallAwareStreamer (인라인 백틱 `<tool_call>` 오탐 방지) │
│  ├─ Gemini Web 정제: 자동 링크 URL 복원([url](url)->url), <Image> 제거   │
│  ├─ 결정적 json-repair + JSON Schema 엄격 검증 (최대 500k 문자)          │
│  ├─ invalid_tool_call 반환 전 1회 내부 자가 수정 루프                    │
│  └─ 워크스페이스 및 OS 파괴 명령 보호 (app/security.py)                  │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
               [클라이언트가 활성 워크스페이스에서 로컬 도구 실행]
```

---

## 2. 핵심 서브시스템

### 2.1. 게이트웨이 라우터 (`app/gateway_router.py`)
- `PEER_LANES`가 설정되고 `GATEWAY_MODE=auto|on`일 때 자동 활성화됩니다.
- **Sticky Session Affinity** (`3600s`, 최대 `128`개), **Least-Inflight**, **모델 지원 여부 필터링**, **쿨다운 서킷 브레이커**(`429`/`503`/`ConnectError` 시 `15s` 휴식)를 결합합니다.
- 워커가 `StreamingResponse(200)`를 열기 *전에* 턴 락을 획득(`X-Gateway-Failover-Ready: 1` 헤더 시 `2.0s` 빠른 실패)하므로 실제 SSE 페일오버가 보장됩니다.

### 2.2. 슬롯 단위 세션 격리 (`app/session_pool.py`, `app/gemini_runtime.py`)
- 각 대화 키는 독립된 `SessionSlot`(`16`개, TTL `3600s`)에 매핑되어 `cid/rid/rcid`, `model`, `workspace_root`, `tool_header_hash`, `FactLedger`를 개별 보관합니다.
- 동일 워커에서 서로 다른 모델을 쓰는 여러 에이전트가 번갈아 실행되어도 `default_model`이나 다른 슬롯 상태를 오염시키지 않습니다.

### 2.3. Profile Vault & 헤드리스 CDP 쿠키 동기화 (`app/cookie_sync.py`)
- 무제한 Google 계정(`acc1`, `acc2`, `work3`, ...)을 `.cookie_profiles/` 하위의 독립된 `--user-data-dir` 폴더로 격리합니다(개인 Chrome 프로필 비접촉, 비밀번호 미저장).
- **최초 1회 로그인:** 자동화 플래그 없이 순정 Chrome/Edge를 열어 Google 2FA/Passkey 로그인을 정상 완료합니다.
- **헤드리스 동기화:** `--headless=new --remote-debugging-port=0 --remote-debugging-address=127.0.0.1`로 실행한 뒤 외부 의존성 없는 **표준 라이브러리 RFC 6455 WebSocket** 클라이언트(`CdpConnection`)로 `.google.com` + `gemini.google.com` 전체 쿠키(`OSID`, `__Secure-OSID`, `SID`, `__Secure-1PSID`, `__Secure-1PSIDTS`, `__Secure-1PSIDCC`, `NID` — `accounts.google.com`의 `LSID`/`__Host-GAPS`는 제외)를 추출해 `.gemini_cookies_<lane>/.cached_cookies_<PSID>.json`과 `.env.<lane>`에 원자적으로 저장한 후 `Browser.close`로 종료합니다.

---

## 3. 적용된 알고리즘 및 자료구조

| 구성 요소 | 알고리즘 / 설계 패턴 | 시간·공간 복잡도 | 소스 파일 |
|---|---|---|---|
| **SSE `<tool_call>` 억제 스트리머** | 부분 접두사 룩어헤드 및 인라인 백틱 컨텍스트 추적 기반 단일 패스 유한 상태 기계(FSM) | $O(N)$ 시간, $O(1)$ 버퍼 | `app/streaming.py` |
| **도구 호출 추출 및 복구** | 문자열 인식 괄호/따옴표 스캐너, 문자열 외부 후행 쉼표 제거, `json-repair`, 재귀적 URL 언링크, JSON Schema 검증 | $O(N)$ 단일 패스 | `app/tool_parser.py` |
| **제한된 내부 자가 수정** | 파싱/스키마 오류 진단 메시지를 모델에 피드백하는 1회 자가 수정 루프 | $O(1)$ 제한 반복 | `app/openai_adapter.py` |
| **스마트 중요도 절단 (Smart Truncate)** | 앞부분(2/3) + 뒷부분(1/3) + 중간 오류/스택트레이스 라인 보존 + 병렬 도구 결과 공정 예산 배분 | $O(L)$ (라인 수) | `app/prompt_builder.py` |
| **게이트웨이 부하 분산** | Sticky 해시 매핑 + Least-Inflight 최소 선택 + 시간 기반 서킷 브레이커 | $O(P)$ ($P$ = 피어 수) | `app/gateway_router.py` |
| **세션 슬롯 풀** | 크기 제한 LRU/TTL 해시 맵(`16`슬롯) + Task 소유 재진입 `asyncio.Lock` | $O(1)$ 조회, $O(S)$ 축출 | `app/session_pool.py` |
| **로컬 CDP 전송 계층** | 순수 stdlib RFC 6455 WebSocket 프레이밍(7/16/64비트 길이, 분할 프레임 재조립, Ping/Pong) + 이벤트 링 버퍼 | $O(B)$ (바이트 수) | `app/cookie_sync.py` |
| **데드락 방지 초기화** | 초기화 타임아웃(`25s`)과 장시간 스트림/추론 타임아웃(`420s`)을 분리하는 `asyncio.wait_for` 가드 | $O(1)$ 래퍼 | `app/account_pool.py` |
| **원자적 설정 파일 저장** | 임시 파일 기록 후 원자적 `os.replace()` (`.env*`, `vault.json`, 쿠키 캐시) | $O(F)$ (파일 크기) | `app/env_update.py`, `app/cookie_sync.py` |

---

## 4. 참고 연구 논문 및 기술 표준

1. **ReAct:** Yao et al. (2023), *ReAct: Synergizing Reasoning and Acting in Language Models* (ICLR 2023). — 추론 추적(`thoughts_delta`), 구조화된 도구 호출, 환경 관찰 결과를 교차 실행하는 에이전트 루프 설계 기반.
2. **Toolformer:** Schick et al. (2023), *Toolformer: Language Models Can Teach Themselves to Use Tools* (NeurIPS 2023). — 네이티브 Function Calling이 없는 웹 모델에서 텍스트 내 구분 태그(`<tool_call>...</tool_call>`)로 도구를 호출하는 프로토콜의 이론적 기반.
3. **Reflexion & Self-Refine:** Shinn et al. (2023), *Reflexion: Language Agents with Verbal Reinforcement Learning* (NeurIPS 2023); Madaan et al. (2023), *Self-Refine: Iterative Refinement with Self-Feedback* (NeurIPS 2023). — 잘못된 도구 호출에 대한 1회 내부 진단·자가 수정 루프 설계에 반영.
4. **Lost in the Middle:** Liu et al. (2024), *Lost in the Middle: How Language Models Use Long Contexts* (TACL 2024). — 단순 말단 절단 대신 앞·뒤 컨텍스트와 중간 핵심 오류 라인을 보존하는 `smart_truncate()` 설계의 근거.
5. **부하 분산 및 복원력 공학:** Mitzenmacher (2001), *The Power of Two Choices in Randomized Load Balancing* (IEEE TPDS); Nygard (2018), *Release It!* (Circuit Breaker & Bulkhead 패턴). — Least-Inflight 라우팅, 레인별 쿨다운, 워커 프로세스 격리에 적용.
6. **프로토콜 및 표준:**
   - **RFC 6455** — *The WebSocket Protocol* (`app/cookie_sync.py`에 외부 의존성 없이 직접 구현).
   - **RFC 8259 / JSON Schema Draft 2020-12** — 도구 인자 엄격 검증 (`app/tool_parser.py`).
   - **Chrome DevTools Protocol (CDP)** — `Storage.getCookies`, `Target.createTarget`, `Page.loadEventFired`, `Runtime.evaluate`, `Browser.close`.
   - **WHATWG Server-Sent Events (SSE)** — `: keep-alive\n\n` 하트비트 포함 OpenAI 호환 스트리밍.
