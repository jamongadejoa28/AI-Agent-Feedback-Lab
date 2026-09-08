"""FastAPI 메인 애플리케이션 및 라우터 모듈.

R4, R5, R6, R7, R8, R9, R11에 따라:
- 익명 세션 쿠키(feedback_tester_id)를 미들웨어에서 자동 발급 및 검증합니다.
- 단일 턴 테스트 실행(POST /api/test) 및 피드백 제출(POST /api/test/{test_id}/feedback)을 처리합니다.
- 헬스체크 및 정책 정보 조회 API를 제공합니다.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.agent_client import (
    AgentClientError,
    AgentConnectionError,
    AgentContractError,
    AgentHTTPError,
    AgentTimeoutError,
    agent_client,
)
from app.database import (
    DatabaseError,
    FailedRetryError,
    IdempotencyConflictError,
    InProgressConflictError,
    db,
)
from app.policy_reader import policy_reader
from app.schemas import (
    FeedbackCreateRequest,
    FeedbackItemResponse,
    FeedbackListResponse,
    FeedbackResponse,
    FeedbackStatsResponse,
    HealthResponse,
    PolicyInfoResponse,
    TestCreateRequest,
    TestResponse,
)
from app.settings import settings

COOKIE_NAME = "feedback_tester_id"


def is_valid_uuid(val: str) -> bool:
    """주어진 문자열이 유효한 UUID4 형식인지 검증합니다."""
    try:
        u = uuid.UUID(val, version=4)
        return str(u) == val
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """애플리케이션 시작 및 종료 시 필요한 리소스를 초기화합니다."""
    # 데이터베이스 디렉터리 및 export 디렉터리 보장
    settings.resolved_database_path
    Path("data/exports").mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="AI-Agent-Feedback-Lab",
    description="First-AI-Agent 응답 품질 평가 및 피드백 수집 시스템",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def feedback_cookie_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """R5 요구사항에 따른 익명 세션 쿠키(feedback_tester_id) 관리 미들웨어.

    요청 쿠키에서 feedback_tester_id를 검사하고, 누락되었거나 UUID 형식이 아니면
    신규 UUID4를 발급하여 request.state에 설정하고 나가는 응답에 HttpOnly 쿠키로 첨부합니다.
    """
    raw_tester_id = request.cookies.get(COOKIE_NAME)
    is_new_cookie = False

    if raw_tester_id and is_valid_uuid(raw_tester_id):
        tester_id = raw_tester_id
    else:
        tester_id = str(uuid.uuid4())
        is_new_cookie = True

    # 하위 라우터에서 안전하게 접근할 수 있도록 request.state에 저장
    request.state.tester_id = tester_id

    response = await call_next(request)

    if is_new_cookie:
        response.set_cookie(
            key=COOKIE_NAME,
            value=tester_id,
            httponly=True,
            samesite="lax",
            path="/",
            secure=settings.cookie_secure,
        )

    return response


# 정적 파일 마운트 (정적 자산 제공)
static_path = Path("static")
static_path.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", summary="메인 테스트 UI 화면")
async def index() -> FileResponse:
    """테스터용 메인 웹 인터페이스(index.html)를 반환합니다."""
    index_file = static_path / "index.html"
    if not index_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="index.html 파일을 찾을 수 없습니다.",
        )
    return FileResponse(index_file)


@app.get("/history", summary="피드백 모아보기 화면")
async def history_page() -> FileResponse:
    """피드백 모아보기 웹 인터페이스(history.html)를 반환합니다."""
    history_file = static_path / "history.html"
    if not history_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="history.html 파일을 찾을 수 없습니다.",
        )
    return FileResponse(history_file)


@app.get("/api/feedbacks/stats", response_model=FeedbackStatsResponse, summary="피드백 총 건수 통계 조회")
async def get_feedback_stats() -> FeedbackStatsResponse:
    """헤더 배지 알림 카운트 및 데이터 현황 확인을 위해 완료된 피드백 총 건수를 반환합니다."""
    count = db.get_completed_count()
    return FeedbackStatsResponse(total_count=count)


@app.get("/api/feedbacks", response_model=FeedbackListResponse, summary="완료된 피드백 목록 조회")
async def get_feedbacks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str = Query(default="", max_length=200),
) -> FeedbackListResponse:
    """완료 피드백을 DB 전체 검색하고 요청한 페이지 범위만 반환합니다.

    검색어는 질문, Agent 원본 응답, 기대 응답에 적용됩니다. 서버 측 검색과
    페이지네이션을 함께 수행하여 데이터가 200건을 넘어도 모든 레코드에 접근할
    수 있으며, 한 요청이 과도한 데이터를 브라우저로 전송하지 않도록 제한합니다.
    """
    search = q.strip()
    total = db.get_completed_count(search=search)
    total_pages = (total + page_size - 1) // page_size
    offset = (page - 1) * page_size
    records = db.get_feedbacks_list(limit=page_size, offset=offset, search=search)
    items = [
        FeedbackItemResponse(
            id=r.id,
            test_date=r.test_date,
            created_at=r.created_at,
            question=r.question,
            agent_response=r.agent_response,
            expected_response=r.expected_response,
            latency_ms=r.latency_ms,
        )
        for r in records
    ]
    return FeedbackListResponse(
        total_count=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        items=items,
    )


@app.get("/api/health", response_model=HealthResponse, summary="서비스 헬스체크")
async def health_check() -> HealthResponse:
    """R24 요구사항에 따른 서비스 상태 점검 엔드포인트.

    Feedback Lab 자체의 생존 여부, SQLite 데이터베이스 및 정책 파일 상태를 보고합니다.
    """
    db_status = "ok"
    try:
        with db.get_connection() as conn:
            conn.execute("SELECT 1;").fetchone()
    except Exception:
        db_status = "error"

    policy_status = "ok" if policy_reader.is_available() else "missing"
    overall_status = "ok" if db_status == "ok" else "warning"

    return HealthResponse(
        status=overall_status,
        database=db_status,
        policy=policy_status,
        agent_base_url=settings.agent_base_url,
    )


@app.get("/api/policy-info", response_model=PolicyInfoResponse, summary="공식 정책 정보 조회")
async def get_policy_info() -> PolicyInfoResponse:
    """R18 요구사항에 따른 화면 안내용 공식 매뉴얼/수리 링크 및 문의처를 반환합니다."""
    info = policy_reader.get_policy_display_info()
    return PolicyInfoResponse(
        manual_url=info["manual_url"],
        repair_url=info["repair_url"],
        contacts=info["contacts"],
    )


@app.post("/api/test", response_model=TestResponse, summary="단일 턴 테스트 실행 및 Agent 답변 수신")
async def run_test(
    payload: TestCreateRequest,
    request: Request,
) -> TestResponse:
    """R6~R9 요구사항에 따른 테스트 실행 흐름을 처리합니다.

    1. tester_id 식별
    2. DB에 'processing' 상태로 선예약 (동시 중복 호출 차단)
    3. First-AI-Agent HTTP 호출 및 응답('answer') 검증
    4. 성공 시 'awaiting_feedback'으로 상태 갱신 후 결과 반환
    5. 실패 시 'failed'로 상태 갱신 후 사용자 오류 메시지 반환
    """
    tester_id: str = getattr(request.state, "tester_id", None) or str(uuid.uuid4())

    # 1. DB 레코드 선예약 (멱등성 검사)
    try:
        record, is_new = db.reserve_test(
            tester_id=tester_id,
            client_request_id=payload.client_request_id,
            question=payload.question,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except InProgressConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except FailedRetryError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except DatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"데이터베이스 예약 오류: {exc}",
        ) from exc

    # 동일 요청 재호출인 경우: 기존 결과 재사용 반환
    if not is_new:
        if record.agent_response is not None:
            return TestResponse(
                test_id=record.id,
                answer=record.agent_response,
                latency_ms=record.latency_ms or 0,
            )

    # 2. First-AI-Agent HTTP 호출
    try:
        answer, latency_ms = await agent_client.query(payload.question)
    except AgentTimeoutError as exc:
        db.update_test_failure(record.id, error_message=str(exc))
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI Agent 응답 시간이 초과되었습니다. 잠시 후 새 테스트로 다시 시도해 주세요.",
        ) from exc
    except (AgentConnectionError, AgentHTTPError, AgentContractError, AgentClientError) as exc:
        db.update_test_failure(record.id, error_message=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI Agent에 연결할 수 없거나 응답 형식이 올바르지 않습니다. 잠시 후 새 테스트로 다시 시도해 주세요.",
        ) from exc

    # 3. DB 성공 상태 갱신 (awaiting_feedback)
    db.update_test_success(
        test_id=record.id,
        agent_response=answer,
        latency_ms=latency_ms,
    )

    return TestResponse(
        test_id=record.id,
        answer=answer,
        latency_ms=latency_ms,
    )


def _ndjson_event(event_type: str, **data: object) -> str:
    """브라우저 스트림에 전달할 한 이벤트를 UTF-8 NDJSON 한 줄로 직렬화합니다.

    JSON 문자열 안의 줄바꿈은 이스케이프되므로 실제 줄바꿈 하나가 곧 이벤트
    경계가 됩니다. 이 계약 덕분에 fetch ReadableStream이 청크를 임의 위치에서
    나누더라도 브라우저가 버퍼에 이어 붙여 안전하게 복원할 수 있습니다.
    """
    return json.dumps({"type": event_type, **data}, ensure_ascii=False) + "\n"


@app.post(
    "/api/test/stream",
    summary="단일 턴 테스트 실행 및 Agent 답변 실시간 스트리밍",
)
async def stream_test(
    payload: TestCreateRequest,
    request: Request,
) -> StreamingResponse:
    """Agent의 큐·SSE API를 중계하고 최종 원본 답변을 DB에 저장합니다.

    Agent 호출 전 processing 상태를 선예약하는 멱등성 계약은 기존 API와
    동일합니다. 예약 이후에는 즉시 NDJSON 응답을 시작하고, Agent의 delta를
    순서대로 전달합니다. 최종 저장과 완료 이벤트에는 delta 조합값이 아닌 Agent
    ``completed`` 이벤트의 전체 content를 사용하여 마지막 청크 누락이나 중복으로
    답변이 잘리는 일을 방지합니다.
    """
    tester_id: str = getattr(request.state, "tester_id", None) or str(uuid.uuid4())
    try:
        record, is_new = db.reserve_test(
            tester_id=tester_id,
            client_request_id=payload.client_request_id,
            question=payload.question,
        )
    except (IdempotencyConflictError, InProgressConflictError, FailedRetryError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"데이터베이스 예약 오류: {exc}",
        ) from exc

    async def generate_events() -> AsyncIterator[str]:
        """예약 결과와 Agent 이벤트를 NDJSON으로 변환하고 상태 전이를 확정합니다."""
        # 일부 Windows·모바일 중간 경로는 첫 수 KB를 모은 뒤 브라우저에 전달합니다.
        # 의미 없는 별도 청크 대신 accepted JSON에 무시 가능한 여백을 포함하여 대기
        # 상태가 즉시 표시되도록 하며, 이후 이벤트의 원본 content에는 영향이 없습니다.
        yield _ndjson_event("accepted", test_id=record.id, padding=" " * 2048)

        # 완료되었거나 피드백 대기 중인 멱등 재호출은 Agent를 다시 호출하지 않고
        # DB에 보존된 원문을 하나의 완료 이벤트로 반환합니다.
        if not is_new and record.agent_response is not None:
            yield _ndjson_event(
                "completed",
                test_id=record.id,
                answer=record.agent_response,
                latency_ms=record.latency_ms or 0,
                reused=True,
            )
            return

        started_at = time.perf_counter()
        completed = False
        try:
            async for event in agent_client.stream_query(
                payload.question,
                record.id,
            ):
                event_type = str(event.get("type", ""))
                event_data = event.get("data")
                if not isinstance(event_data, dict):
                    raise AgentContractError("Agent SSE 이벤트 data가 객체가 아닙니다.")

                if event_type == "queued":
                    yield _ndjson_event("queued", position=event_data.get("position"))
                elif event_type == "started":
                    yield _ndjson_event("started")
                elif event_type == "delta":
                    delta = event_data.get("content")
                    if not isinstance(delta, str):
                        raise AgentContractError("Agent delta 이벤트에 문자열 content가 없습니다.")
                    yield _ndjson_event("delta", content=delta)
                elif event_type == "completed":
                    message = event_data.get("message")
                    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                        raise AgentContractError(
                            "Agent completed 이벤트에 문자열 content가 없습니다."
                        )
                    answer = message["content"]
                    latency_ms = int((time.perf_counter() - started_at) * 1000)
                    if not db.update_test_success(record.id, answer, latency_ms):
                        raise DatabaseError("Agent 성공 결과의 상태 전이를 적용할 수 없습니다.")
                    completed = True
                    yield _ndjson_event(
                        "completed",
                        test_id=record.id,
                        answer=answer,
                        latency_ms=latency_ms,
                        reused=False,
                    )
                    return

            if not completed:
                raise AgentContractError("Agent 스트림이 완료 답변 없이 종료되었습니다.")
        except asyncio.CancelledError:
            # 브라우저가 연결을 끊으면 이 요청에서는 더 이상 완성 답변을 받을 수
            # 없습니다. processing 레코드를 남기지 않도록 실패로 종결합니다.
            db.update_test_failure(record.id, "브라우저 스트림 연결이 종료되었습니다.")
            raise
        except AgentTimeoutError as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            db.update_test_failure(record.id, str(exc), latency_ms)
            yield _ndjson_event(
                "error",
                message="AI Agent 연결 시간이 초과되었습니다. 새 테스트로 다시 시도해 주세요.",
            )
        except (AgentConnectionError, AgentHTTPError, AgentContractError, AgentClientError) as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            db.update_test_failure(record.id, str(exc), latency_ms)
            yield _ndjson_event(
                "error",
                message="AI Agent 응답을 끝까지 받지 못했습니다. 새 테스트로 다시 시도해 주세요.",
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            db.update_test_failure(record.id, str(exc), latency_ms)
            yield _ndjson_event(
                "error",
                message="테스트 결과를 저장하는 중 오류가 발생했습니다. 새 테스트로 다시 시도해 주세요.",
            )

    return StreamingResponse(
        generate_events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/api/test/{test_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="피드백 입력 취소 및 테스트 종료",
)
async def cancel_test(test_id: str, request: Request) -> Response:
    """현재 테스터가 피드백을 남기지 않기로 한 테스트를 취소합니다.

    소유자가 일치하는 awaiting_feedback 레코드만 cancelled로 전환합니다. 타인의
    레코드나 이미 완료된 레코드는 존재 여부와 상태를 구분하지 않고 404로 반환해
    세션 격리와 피드백 불변성을 유지합니다.
    """
    tester_id: str = getattr(request.state, "tester_id", None) or ""
    if not db.cancel_test(test_id=test_id, tester_id=tester_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="취소할 테스트 레코드를 찾을 수 없거나 이미 종료된 상태입니다.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/api/test/{test_id}/feedback",
    response_model=FeedbackResponse,
    summary="원했던 응답/개선 방향 피드백 제출",
)
async def submit_feedback(
    test_id: str,
    payload: FeedbackCreateRequest,
    request: Request,
) -> FeedbackResponse:
    """R11 요구사항에 따른 피드백 저장 및 'completed' 상태 전이를 처리합니다.

    타인의 test_id에 대한 접근이나 존재하지 않는 레코드에 대해서는
    존재 여부 자체를 은닉하기 위해 404 Not Found를 반환합니다.
    """
    tester_id: str = getattr(request.state, "tester_id", None) or ""

    success = db.save_feedback(
        test_id=test_id,
        tester_id=tester_id,
        expected_response=payload.expected_response,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 테스트 레코드를 찾을 수 없거나 이미 피드백이 등록된 상태입니다.",
        )

    return FeedbackResponse()
