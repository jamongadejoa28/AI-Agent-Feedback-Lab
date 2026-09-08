# AI-Agent-Feedback-Lab

`AI-Agent-Feedback-Lab`은 `First-AI-Agent`의 응답 품질을 현업 관점에서 직접 평가하고, 개선 방향(원했던 응답) 피드백을 수집하기 위한 사내 평가 전용 웹 애플리케이션입니다.

---

## 1. 프로젝트 목적 및 역할 분담

본 저장소는 **새로운 AI 에이전트를 개발하는 곳이 아닙니다.**

- **`First-AI-Agent` (../ai-agent)**: 질문 의도 파악, 정책 라우팅, Qdrant 벡터 검색, 1차/2차 지식 검색, 답변 생성 등 에이전트의 모든 지능과 정책을 소유합니다.
- **`AI-Agent-Feedback-Lab` (본 저장소)**:
  - 현장 사용자를 위한 직관적이고 안전한 웹 UI 제공
  - First-AI-Agent 대상 HTTP 프록시 요청 및 응답(`answer` 필드) 추출
  - 익명 테스터 식별자(`feedback_tester_id`) 기반 세션 격리
  - Agent 호출 전 `processing` 선예약 및 멱등성 보장
  - 원했던 답변 및 개선 피드백 수집 및 SQLite WAL 저장
  - 회귀 분석 및 모델 개선을 위한 일자별 JSONL 원자적 익스포트

---

## 2. 시스템 아키텍처 및 런타임 경계

```text
+-------------------------------------------------------------+
| Browser (현장 담당자 / 엔지니어 / 테스터)                     |
+-------------------------------------------------------------+
                            │ HTTP (Cookie: feedback_tester_id)
                            ▼
+-------------------------------------------------------------+
| AI-Agent-Feedback-Lab (FastAPI, SQLite WAL, Vanilla JS)     |
| - 환경: WSL2 Linux                                          |
| - 포트: 8080                                                |
| - 주요역할: 입력검증, 세션격리, processing 선예약, 피드백저장  |
+-------------------------------------------------------------+
                            │ HTTP POST /v1/query {"query": "..."}
                            ▼
+-------------------------------------------------------------+
| First-AI-Agent (FastAPI, LangGraph, Qdrant Client)          |
| - 환경: WSL2 Linux                                          |
| - 포트: 8000                                                |
+-------------------------------------------------------------+
                            │ HTTP REST / vLLM
                            ▼
+-------------------------------------------------------------+
| Windows Host LLM Service (EXAONE-3.5-7.8B-Instruct 등)      |
+-------------------------------------------------------------+
```

> **주의**: Feedback Lab은 First-AI-Agent 내부 코드를 import하지 않고 오직 HTTP 통신만을 사용하며, Agent가 반환한 답변을 임의로 수정하거나 가공하지 않고 그대로 보존합니다.

---

## 3. 저장소 구조

```text
AI-Agent-Feedback-Lab/
├─ AGENTS.md                 # 프로젝트 공통 개발 및 주석/테스트 규칙
├─ README.md                 # 시스템 명세 및 실행 가이드
├─ pyproject.toml            # 패키지 명세 및 의존성 설정
├─ .env.example              # 환경 변수 예시 파일
├─ .gitignore                # DB, 가상환경, 익스포트 파일 제외 설정
│
├─ app/                      # 백엔드 핵심 애플리케이션
│  ├─ __init__.py
│  ├─ settings.py            # 환경 설정 (Pydantic Settings)
│  ├─ schemas.py             # 요청/응답 Pydantic 스키마 및 검증
│  ├─ database.py            # SQLite WAL 연결 및 데이터 수명주기 관리
│  ├─ agent_client.py        # First-AI-Agent HTTP 비동기 통신 클라이언트
│  ├─ policy_reader.py       # dataset_policy.json 안전 조회 어댑터
│  └─ main.py                # FastAPI 앱, 쿠키 미들웨어, 라우터
│
├─ static/                   # 프론트엔드 정적 파일 (Vanilla HTML/CSS/JS)
│  ├─ index.html             # 메인 UI 및 안내/기준 영역
│  ├─ style.css              # 모바일/데스크톱 반응형 스타일
│  └─ app.js                 # 단일 턴 워크플로 및 안전한 DOM 렌더링
│
├─ pipeline/                 # 데이터 파이프라인
│  ├─ __init__.py
│  └─ export_feedback.py     # 완료된 피드백 원자적 JSONL 익스포트 CLI
│
├─ data/                     # 로컬 데이터 디렉토리 (Git 미추적)
│  └─ exports/               # 일자별 JSONL 익스포트 저장 위치
│
└─ tests/                    # 자동화 테스트 스위트
   ├─ test_api.py            # API 엔드포인트 및 검증/쿠키 테스트
   ├─ test_database.py       # SQLite WAL 및 상태 전이/멱등성 테스트
   ├─ test_concurrency.py    # 동시 요청 및 락 회피 테스트
   └─ test_export.py         # JSONL 원자적 익스포트 멱등성 테스트
```

---

## 4. 환경 설정 및 로컬 실행

### 4.1 가상환경 생성 및 의존성 설치

```bash
# 가상환경 생성 (Python 3.11 이상)
uv venv .venv --python python3

# 의존성 및 개발 도구 설치
uv pip install -e ".[dev]"
```

### 4.2 환경 변수 구성

`.env.example`을 복사하여 `.env`를 생성하고 필요한 설정을 조정합니다.

```bash
cp .env.example .env
```

주요 환경 변수:
```env
AGENT_BASE_URL=http://127.0.0.1:8000
DATABASE_PATH=data/feedback.db
AGENT_POLICY_PATH=../ai-agent/data/dataset_policy.json
COOKIE_SECURE=false
HOST=127.0.0.1
PORT=8080
AGENT_TIMEOUT_SECONDS=30.0
```

### 4.3 서버 실행

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

실행 후 웹 브라우저에서 `http://127.0.0.1:8080`에 접속합니다.

---

## 5. API 명세 요약

| Method | Endpoint | 설명 |
| :--- | :--- | :--- |
| `GET` | `/` | 테스터용 싱글 턴 평가 웹 UI 제공 |
| `GET` | `/history` | 등록된 피드백 데이터 모아보기 웹 UI 제공 |
| `GET` | `/api/health` | 서비스 생존 여부, DB 및 정책 파일 상태 점검 |
| `GET` | `/api/policy-info` | First-AI-Agent의 공식 다운로드/수리 접수 URL 및 문의처 제공 |
| `GET` | `/api/feedbacks/stats` | 헤더 알림 배지용 완료 피드백 총 건수 조회 |
| `GET` | `/api/feedbacks` | 테스터들의 다양한 테스트 유도를 위한 완료 피드백 목록 조회 |
| `POST` | `/api/test` | 질문 접수, `processing` 선예약, Agent 호출 및 답변 반환 |
| `POST` | `/api/test/{test_id}/feedback` | 평가 피드백(원했던 응답) 저장 및 `completed` 완료 처리 |

> **데이터 열람 및 격리 원칙**: 테스터들은 `/history`를 통해 등록된 완료 피드백(`status = 'completed'`)을 상호 열람하여 다양한 테스트 아이디어를 얻을 수 있으며, 타인의 진행 중인 테스트 수정이나 세션 탈취를 방지하기 위해 타인의 `test_id`로 피드백을 요청할 경우 `404 Not Found`를 반환합니다.

---

## 6. SQLite 데이터 수명주기 및 멱등성

### 6.1 상태 전이 다이어그램

```text
[요청 수신]
    │
    ▼
(1) SQLite 레코드 생성: status = 'processing'
    UNIQUE(tester_id, client_request_id)
    │
    ├─► First-AI-Agent 호출 성공 ──► status = 'awaiting_feedback'
    │                                          │
    │                                          ▼
    │                                (사용자 피드백 제출)
    │                                          │
    │                                          ▼
    │                                  status = 'completed'
    │
    └─► Agent 호출 실패/계약 위반 ──► status = 'failed'
```

### 6.2 멱등성(Idempotency) 계약

1. **동일 tester_id + 동일 client_request_id + 동일 question**:
   - 기존 상태가 `awaiting_feedback` 또는 `completed`: 기존 테스트 결과(`test_id`, `answer`, `latency_ms`)를 즉시 재사용하여 반환합니다.
   - 기존 상태가 `processing`: 중복 Agent 호출을 방지하기 위해 `409 Conflict` ("요청이 이미 처리 중입니다")를 반환합니다.
   - 기존 상태가 `failed`: 자동 재실행을 방지하며, 사용자는 UI에서 '새 테스트'를 눌러 신규 `client_request_id`로 재시도해야 합니다.
2. **동일 tester_id + 동일 client_request_id + 다른 question**:
   - `409 Conflict`를 반환하고 기존 레코드를 절대 덮어쓰지 않습니다.

---

## 7. 피드백 JSONL 익스포트 파이프라인

완료된 피드백 데이터(`status = 'completed'`)를 일자별 JSONL 형식으로 안전하게 추출합니다.

```bash
# 특정 일자(한국 시간 기준) 익스포트
.venv/bin/python pipeline/export_feedback.py --date 2026-09-08

# 전체 일자 익스포트
.venv/bin/python pipeline/export_feedback.py --all
```

- **저장 위치**: `data/exports/YYYY-MM-DD.jsonl`
- **원자적 교체**: 임시 파일(`.tmp`)에 전체 정렬 데이터를 기록한 뒤 `os.replace`로 원자적으로 교체하므로, 중복 라인이 발생하지 않으며 안전한 멱등 실행이 보장됩니다.

---

## 8. 코드 검증 및 테스트 가이드

코드 수정 시 반드시 **Pyright 정적 분석(0 errors)** 확인 후 **pytest**를 실행합니다.

```bash
# 1. 정적 타입 검사 (반드시 0 errors 확인)
.venv/bin/pyright .

# 2. 자동화 단위/통합 테스트 실행
.venv/bin/pytest tests/ -v
```

---

## 9. 전체 로드맵 (Phase 1~9)

| 단계 | 명칭 | 상태 | 내용 |
| :--- | :--- | :---: | :--- |
| **Phase 1** | **Feedback MVP** | **구현 완료 (현재 범위)** | 단일 턴 테스트 UI, Agent HTTP 프록시, 안전한 답변 렌더링, 피드백 수집 |
| **Phase 2** | **Multi-user / Reliability** | **구현 완료 (현재 범위)** | SQLite WAL 동시성, 세션 쿠키 격리, `processing` 선예약 및 엄격한 멱등성 |
| **Phase 3** | **JSONL Export** | **구현 완료 (현재 범위)** | 일자별 완료 피드백 원자적 익스포트 CLI 구축 |
| Phase 4 | Internal Real-user Testing | 계획 (미구현) | 사내 엔지니어/현장 담당자 대상 실사용 테스트 진행 |
| Phase 5 | Feedback Classification | 계획 (미구현) | 피드백 범주화(매뉴얼, AS, 수치오류, 의도오인 등) 통계화 |
| Phase 6 | Regression Candidates | 계획 (미구현) | 반복 검증을 위한 골든 데이터셋 회귀 테스트 케이스 생성 |
| Phase 7 | AI-assisted Triage | 계획 (미구현) | LLM을 활용한 개선 방향 사전 분석 및 리포팅 보조 |
| Phase 8 | Internal DNS Validation | 계획 (미구현) | 사내 인트라넷 DNS 연결 및 장기 네트워크 안정성 검증 |
| Phase 9 | System Service Automation | 계획 (미구현) | systemd 서비스 등록 및 서버 부팅 시 자동 기동 구성 |

> 본 저장소는 현재 **Phase 1~3 MVP**만 구현되어 있습니다.

---

## 10. 개발 규칙 참조

자세한 코드 스타일, 한글 주석 원칙, 정적 검사 순서 등은 [AGENTS.md](https://github.com/jamongadejoa28/First-AI-Agent/blob/main/AGENTS.md)를 참조하십시오.
