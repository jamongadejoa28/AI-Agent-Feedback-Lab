"""FastAPI 엔드포인트 및 쿠키, 검증, 에이전트 연동, 격리 테스트 모듈.

R3~R9, R11에 따라:
- feedback_tester_id 세션 쿠키 발급 및 형식을 검증합니다.
- 질문(1~4000자) 및 피드백(1~500자) 유효성 검사를 테스트합니다.
- Agent 호출 성공 시 'answer' 추출 및 실패 시 failed 상태 전이를 확인합니다.
- 타인 레코드에 대한 피드백 시도 시 404 은닉 반환을 검증합니다.
- 동일 식별자 재호출 멱등성 및 충돌(409) 처리를 테스트합니다.
"""

import tempfile
import uuid
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.agent_client import AgentConnectionError, AgentContractError, AgentTimeoutError
from app.database import Database
from app.main import app
import app.main as main_module


@pytest.fixture
def test_client() -> Generator[TestClient, None, None]:
    """테스트용 임시 DB를 주입한 FastAPI TestClient fixture."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "api_test.db"
        test_db = Database(db_path=str(db_file))
        with patch.object(main_module, "db", test_db):
            with TestClient(app) as client:
                yield client


def test_cookie_generation_on_first_visit(test_client: TestClient) -> None:
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


def test_malformed_cookie_is_regenerated(test_client: TestClient) -> None:
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


def test_question_validation_length_and_whitespace(test_client: TestClient) -> None:
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
def test_test_flow_agent_success(mock_query: AsyncMock, test_client: TestClient) -> None:
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
    test_client: TestClient,
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
    test_client: TestClient,
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
    test_client: TestClient,
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
    test_client: TestClient,
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
