"""FastAPI 엔드포인트 및 쿠키, 검증, 에이전트 연동, 격리 테스트 모듈.

R3~R9, R11에 따라:
- feedback_tester_id 세션 쿠키 발급 및 형식을 검증합니다.
- 질문(1~4000자) 및 피드백(1~500자) 유효성 검사를 테스트합니다.
- Agent 호출 성공 시 'answer' 추출 및 실패 시 failed 상태 전이를 확인합니다.
- 타인 레코드에 대한 피드백 시도 시 404 은닉 반환을 검증합니다.
- 동일 식별자 재호출 멱등성 및 충돌(409) 처리를 테스트합니다.
"""

import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from collections.abc import AsyncIterator, Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.agent_client import AgentConnectionError
from app.database import Database
from app.main import app
import app.main as main_module


class ASGITestClient:
    """Starlette TestClient 버전에 의존하지 않는 동기식 ASGI 테스트 어댑터.

    현재 Starlette는 기존 httpx 기반 TestClient를 deprecated 처리하며 일부 버전
    조합에서 blocking portal 시작이 멈출 수 있습니다. 각 요청을 ASGITransport로
    직접 실행하고 쿠키만 명시적으로 이어서 실제 브라우저 세션 계약을 보존합니다.
    """

    def __init__(self) -> None:
        """테스트 요청 사이에 유지할 쿠키 저장소를 초기화합니다."""
        self.cookies = httpx.Cookies()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """한 ASGI 요청을 독립 이벤트 루프에서 실행하고 응답 쿠키를 보존합니다."""

        async def send() -> httpx.Response:
            """FastAPI 앱을 네트워크 소켓 없이 호출하여 완성된 응답을 반환합니다."""
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                cookies=self.cookies,
            ) as client:
                response = await client.request(method, url, **kwargs)
                self.cookies.update(client.cookies)
                return response

        return asyncio.run(send())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET 요청을 실행합니다."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        """POST 요청을 실행합니다."""
        return self.request("POST", url, **kwargs)


@pytest.fixture
def test_client() -> Generator[ASGITestClient, None, None]:
    """테스트용 임시 DB를 주입한 FastAPI TestClient fixture."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "api_test.db"
        test_db = Database(db_path=str(db_file))
        with patch.object(main_module, "db", test_db):
            yield ASGITestClient()


def test_cookie_generation_on_first_visit(test_client: ASGITestClient) -> None:
    """R5. 첫 방문 시 feedback_tester_id UUID4 쿠키가 HttpOnly, SameSite=Lax로 발급되는지 검증합니다."""
    response = test_client.get("/api/health")
    assert response.status_code == 200

    set_cookie_header = response.headers.get("set-cookie", "")
    assert "feedback_tester_id=" in set_cookie_header
    assert "HttpOnly" in set_cookie_header
    assert "samesite=lax" in set_cookie_header.lower()

    # 발급된 쿠키 값이 유효한 UUID4인지 검증
    tester_cookie = test_client.cookies.get("feedback_tester_id")
    assert tester_cookie is not None
    parsed_uuid = uuid.UUID(tester_cookie, version=4)
    assert str(parsed_uuid) == tester_cookie


def test_malformed_cookie_is_regenerated(test_client: ASGITestClient) -> None:
    """R5. 클라이언트가 임의의 잘못된 형식의 쿠키를 전달하면 새 UUID4로 재발급되어야 합니다."""
    response = test_client.get(
        "/api/health",
        headers={"Cookie": "feedback_tester_id=invalid-malformed-cookie"},
    )
    assert response.status_code == 200

    set_cookie = response.headers.get("set-cookie", "")
    assert "feedback_tester_id=" in set_cookie
    assert "invalid-malformed-cookie" not in set_cookie

    # 발급된 쿠키가 유효한 UUID4인지 검증
    cookie_val = ""
    for part in set_cookie.split(";"):
        if "feedback_tester_id=" in part:
            cookie_val = part.split("feedback_tester_id=")[1].strip()
            break

    assert cookie_val != ""
    assert cookie_val != "invalid-malformed-cookie"
    assert uuid.UUID(cookie_val, version=4)


def test_question_validation_length_and_whitespace(test_client: ASGITestClient) -> None:
    """R6. 질문 길이 제한(1~4000자) 및 공백 제거 검증을 수행합니다."""
    # 1. 빈 문자열 거부 (422)
    res_empty = test_client.post(
        "/api/test",
        json={"question": "   ", "client_request_id": str(uuid.uuid4())},
    )
    assert res_empty.status_code == 422

    # 2. 4000자 초과 거부 (422)
    over_4000 = "가" * 4001
    res_over = test_client.post(
        "/api/test",
        json={"question": over_4000, "client_request_id": str(uuid.uuid4())},
    )
    assert res_over.status_code == 422


@patch("app.main.agent_client.query", new_callable=AsyncMock)
def test_test_flow_agent_success(mock_query: AsyncMock, test_client: ASGITestClient) -> None:
    """R3, R8. Agent 정상 응답 시 answer 필드 추출 및 awaiting_feedback 상태 저장을 검증합니다."""
    mock_query.return_value = ("공식 매뉴얼 10페이지를 참고하세요.", 1234)

    client_req_id = str(uuid.uuid4())
    response = test_client.post(
        "/api/test",
        json={
            "question": "  MHC 모델 설정 방법은?  ",
            "client_request_id": client_req_id,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert "test_id" in data
    assert data["answer"] == "공식 매뉴얼 10페이지를 참고하세요."
    assert data["latency_ms"] == 1234

    # DB에 저장된 상태가 awaiting_feedback인지 확인
    test_record = main_module.db.get_test_by_id_and_tester(
        data["test_id"],
        test_client.cookies.get("feedback_tester_id") or "",
    )
    assert test_record is not None
    assert test_record.status == "awaiting_feedback"
    assert test_record.question == "MHC 모델 설정 방법은?"  # trim 검증


@patch("app.main.agent_client.query", new_callable=AsyncMock)
def test_test_flow_agent_failure_updates_status_failed(
    mock_query: AsyncMock,
    test_client: ASGITestClient,
) -> None:
    """R9. Agent 연결 또는 계약 실패 시 failed 상태로 갱신되고 502/504 에러를 반환해야 합니다."""
    mock_query.side_effect = AgentConnectionError("연결 거부됨")

    client_req_id = str(uuid.uuid4())
    response = test_client.post(
        "/api/test",
        json={
            "question": "서버 장애 테스트 질문",
            "client_request_id": client_req_id,
        },
    )

    assert response.status_code == 502
    assert "AI Agent에 연결할 수 없거나" in response.json()["detail"]

    # DB에서 해당 레코드가 failed 상태로 갱신되었는지 확인
    tester_id = test_client.cookies.get("feedback_tester_id") or ""
    with main_module.db.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tests WHERE tester_id = ? AND client_request_id = ?;",
            (tester_id, client_req_id),
        ).fetchone()
        assert row is not None
        assert row["status"] == "failed"
        assert "연결 거부됨" in row["metadata_json"]


@patch("app.main.agent_client.query", new_callable=AsyncMock)
def test_feedback_submission_and_length_limit(
    mock_query: AsyncMock,
    test_client: ASGITestClient,
) -> None:
    """R11. 피드백 500자 제한 및 정상 등록 시 completed 상태 전이를 검증합니다."""
    mock_query.return_value = ("기본 응답", 500)

    # 1. 테스트 실행
    res_test = test_client.post(
        "/api/test",
        json={"question": "정상 질문", "client_request_id": str(uuid.uuid4())},
    )
    test_id = res_test.json()["test_id"]

    # 2. 500자 초과 피드백 거부 (422)
    over_500 = "개선방향" * 150
    res_over = test_client.post(
        f"/api/test/{test_id}/feedback",
        json={"expected_response": over_500},
    )
    assert res_over.status_code == 422

    # 3. 정상 피드백 등록 (1~500자)
    res_feedback = test_client.post(
        f"/api/test/{test_id}/feedback",
        json={"expected_response": "수리 접수처 안내가 누락되어 추가 필요합니다."},
    )
    assert res_feedback.status_code == 200
    assert res_feedback.json()["status"] == "completed"

    # DB 레코드 상태 검증
    tester_id = test_client.cookies.get("feedback_tester_id") or ""
    record = main_module.db.get_test_by_id_and_tester(test_id, tester_id)
    assert record is not None
    assert record.status == "completed"
    assert record.expected_response == "수리 접수처 안내가 누락되어 추가 필요합니다."


@patch("app.main.agent_client.query", new_callable=AsyncMock)
def test_cross_user_feedback_isolation_returns_404(
    mock_query: AsyncMock,
    test_client: ASGITestClient,
) -> None:
    """R11. 다른 사용자의 test_id에 피드백을 등록하려 할 경우 존재 여부 은닉을 위해 404를 반환합니다."""
    mock_query.return_value = ("테스터 A의 답변", 600)

    # 1. 테스터 A가 테스트 실행
    res_a = test_client.post(
        "/api/test",
        json={"question": "테스터 A의 질문", "client_request_id": str(uuid.uuid4())},
    )
    test_id_a = res_a.json()["test_id"]

    # 2. 테스터 B의 세션 쿠키로 교체
    tester_b_id = str(uuid.uuid4())
    test_client.cookies.set("feedback_tester_id", tester_b_id)

    # 3. 테스터 B가 테스터 A의 test_id로 피드백 제출 시도 -> 404 Not Found
    res_b = test_client.post(
        f"/api/test/{test_id_a}/feedback",
        json={"expected_response": "테스터 B가 침범하려는 피드백"},
    )
    assert res_b.status_code == 404
    assert "찾을 수 없거나" in res_b.json()["detail"]


@patch("app.main.agent_client.query", new_callable=AsyncMock)
def test_idempotency_api_behavior(
    mock_query: AsyncMock,
    test_client: ASGITestClient,
) -> None:
    """R7. API 수준에서의 멱등성 재호출(동일 질문) 및 409 Conflict(다른 질문)를 검증합니다."""
    mock_query.return_value = ("최초 답변", 700)
    req_id = str(uuid.uuid4())

    # 1. 최초 요청
    res1 = test_client.post(
        "/api/test",
        json={"question": "동일 질문 내용", "client_request_id": req_id},
    )
    assert res1.status_code == 200
    test_id1 = res1.json()["test_id"]

    # 2. 동일 식별자 + 동일 질문 재호출 -> 기존 결과 재사용 (Agent 호출 추가 없음)
    res2 = test_client.post(
        "/api/test",
        json={"question": "동일 질문 내용", "client_request_id": req_id},
    )
    assert res2.status_code == 200
    assert res2.json()["test_id"] == test_id1
    assert res2.json()["answer"] == "최초 답변"
    assert mock_query.call_count == 1  # Agent 추가 호출 없음

    # 3. 동일 식별자 + 다른 질문 -> 409 Conflict
    res3 = test_client.post(
        "/api/test",
        json={"question": "완전히 다른 질문 내용", "client_request_id": req_id},
    )
    assert res3.status_code == 409
    assert "서로 다른 질문" in res3.json()["detail"]


def test_history_page_endpoint(test_client: ASGITestClient) -> None:
    """history 라우터가 실제 모아보기 HTML 파일을 반환하는지 검증합니다.

    설치된 httpx ASGITransport는 Starlette FileResponse의 스트림 종료를 기다리며
    멈추는 버전 호환 문제가 있어, 이 정적 파일 경로만 라우터 반환 객체와 실제
    파일 내용을 직접 확인합니다. JSON 및 NDJSON API는 계속 ASGI로 검증합니다.
    """
    del test_client  # fixture의 임시 DB 수명은 다른 API 테스트와 동일하게 유지합니다.
    response = asyncio.run(main_module.history_page())
    assert response.status_code == 200
    assert "피드백 모아보기" in Path(response.path).read_text(encoding="utf-8")


@patch("app.main.agent_client.query", new_callable=AsyncMock)
def test_feedback_stats_and_list_endpoints(
    mock_query: AsyncMock,
    test_client: ASGITestClient,
) -> None:
    """GET /api/feedbacks/stats 및 GET /api/feedbacks 엔드포인트 동작을 검증합니다."""
    mock_query.return_value = ("챗봇 답변", 500)

    # 1. 초기 상태 카운트는 0건이어야 함
    stats_res = test_client.get("/api/feedbacks/stats")
    assert stats_res.status_code == 200
    assert stats_res.json()["total_count"] == 0

    list_res = test_client.get("/api/feedbacks")
    assert list_res.status_code == 200
    assert list_res.json()["total_count"] == 0
    assert len(list_res.json()["items"]) == 0

    # 2. 피드백 1건 완료 생성
    test_res = test_client.post(
        "/api/test",
        json={"question": "구매문의 방법은?", "client_request_id": str(uuid.uuid4())},
    )
    test_id = test_res.json()["test_id"]

    test_client.post(
        f"/api/test/{test_id}/feedback",
        json={"expected_response": "마케팅부서(070-8666-4272) 안내 필요"},
    )

    # 3. 카운트 1건으로 갱신 확인
    stats_res2 = test_client.get("/api/feedbacks/stats")
    assert stats_res2.status_code == 200
    assert stats_res2.json()["total_count"] == 1

    # 4. 목록 조회 확인
    list_res2 = test_client.get("/api/feedbacks")
    assert list_res2.status_code == 200
    assert list_res2.json()["total_count"] == 1
    items = list_res2.json()["items"]
    assert len(items) == 1
    assert items[0]["feedback_id"] == 1
    assert items[0]["question"] == "구매문의 방법은?"
    assert items[0]["agent_response"] == "챗봇 답변"
    assert items[0]["expected_response"] == "마케팅부서(070-8666-4272) 안내 필요"


def test_stream_test_uses_completed_full_answer(test_client: ASGITestClient) -> None:
    """실시간 delta를 전달하되 DB와 완료 응답에는 Agent 전체 원문을 저장합니다."""
    complete_answer = "첫 조각 뒤에 마지막 문장까지 포함된 잘리지 않은 전체 답변"

    async def fake_stream_query(
        question: str,
        client_message_id: str,
    ) -> AsyncIterator[dict[str, object]]:
        """대기·생성·일부 delta·전체 완료로 이어지는 Agent 이벤트를 재현합니다."""
        assert question == "스트리밍 질문"
        assert uuid.UUID(client_message_id)
        yield {"type": "queued", "data": {"position": 1}}
        yield {"type": "started", "data": {}}
        yield {"type": "delta", "data": {"content": "첫 조각"}}
        yield {
            "type": "completed",
            "data": {"message": {"content": complete_answer}},
        }

    with patch.object(main_module.agent_client, "stream_query", new=fake_stream_query):
        response = test_client.post(
            "/api/test/stream",
            json={"question": "스트리밍 질문", "client_request_id": "stream-request-1"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line]
    assert [event["type"] for event in events] == [
        "accepted",
        "queued",
        "started",
        "delta",
        "completed",
    ]
    assert events[-1]["answer"] == complete_answer

    tester_id = test_client.cookies.get("feedback_tester_id") or ""
    record = main_module.db.get_test_by_id_and_tester(events[-1]["test_id"], tester_id)
    assert record is not None
    assert record.status == "awaiting_feedback"
    assert record.agent_response == complete_answer


def test_feedback_list_server_search_and_pagination(test_client: ASGITestClient) -> None:
    """200건 고정 로딩 없이 모든 완료 레코드를 페이지 이동하고 DB에서 검색합니다."""
    for index in range(25):
        tester_id = str(uuid.uuid4())
        record, _ = main_module.db.reserve_test(
            tester_id,
            f"pagination-request-{index}",
            f"페이지 질문 {index}",
        )
        answer = "DB 전체 검색 표식" if index == 24 else f"답변 {index}"
        main_module.db.update_test_success(record.id, answer, index)
        main_module.db.save_feedback(record.id, tester_id, f"피드백 {index}")

    second_page = test_client.get("/api/feedbacks?page=2&page_size=10")
    assert second_page.status_code == 200
    page_data = second_page.json()
    assert page_data["total_count"] == 25
    assert page_data["page"] == 2
    assert page_data["page_size"] == 10
    assert page_data["total_pages"] == 3
    assert len(page_data["items"]) == 10

    searched = test_client.get(
        "/api/feedbacks",
        params={"page": 1, "page_size": 20, "q": "DB 전체 검색 표식"},
    )
    assert searched.status_code == 200
    search_data = searched.json()
    assert search_data["total_count"] == 1
    assert search_data["total_pages"] == 1
    assert search_data["items"][0]["agent_response"] == "DB 전체 검색 표식"

    assert test_client.get("/api/feedbacks?page=0").status_code == 422
    assert test_client.get("/api/feedbacks?page_size=101").status_code == 422


@patch("app.main.agent_client.query", new_callable=AsyncMock)
def test_cancel_feedback_transitions_owned_test(
    mock_query: AsyncMock,
    test_client: ASGITestClient,
) -> None:
    """취소 API가 현재 테스터의 피드백 대기 테스트만 cancelled로 전환합니다."""
    mock_query.return_value = ("취소 전 Agent 답변", 300)
    response = test_client.post(
        "/api/test",
        json={"question": "피드백을 취소할 질문", "client_request_id": str(uuid.uuid4())},
    )
    test_id = response.json()["test_id"]

    cancelled = test_client.post(f"/api/test/{test_id}/cancel")
    assert cancelled.status_code == 204

    tester_id = test_client.cookies.get("feedback_tester_id") or ""
    record = main_module.db.get_test_by_id_and_tester(test_id, tester_id)
    assert record is not None
    assert record.status == "cancelled"
    assert test_client.post(f"/api/test/{test_id}/cancel").status_code == 404
    assert test_client.post(
        f"/api/test/{test_id}/feedback",
        json={"expected_response": "취소 후 제출할 수 없는 피드백"},
    ).status_code == 404
