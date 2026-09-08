"""SQLite 데이터베이스 스키마, WAL 모드, 상태 전이 및 멱등성 단위 테스트 모듈.

R7, R10에 따라:
- WAL 모드 및 busy_timeout 설정 여부를 검증합니다.
- processing -> awaiting_feedback -> completed 및 failed 상태 전이를 테스트합니다.
- UNIQUE(tester_id, client_request_id) 제약 조건 및 충돌 처리를 확인합니다.
"""

import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Generator
import pytest

from app.database import (
    Database,
    FailedRetryError,
    IdempotencyConflictError,
    InProgressConflictError,
)


@pytest.fixture
def temp_db() -> Generator[Database, None, None]:
    """각 테스트 격리를 위해 임시 SQLite 데이터베이스를 생성하는 fixture."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "test_feedback.db"
        test_database = Database(db_path=str(db_file))
        yield test_database


def test_sqlite_wal_mode_and_pragmas(temp_db: Database) -> None:
    """SQLite 연결이 WAL 모드와 foreign_keys, busy_timeout을 올바르게 적용하는지 검증합니다."""
    with temp_db.get_connection() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]

    assert journal_mode.lower() == "wal", f"journal_mode는 wal이어야 합니다 (현재: {journal_mode})"
    assert busy_timeout >= 5000, f"busy_timeout은 5000ms 이상이어야 합니다 (현재: {busy_timeout})"
    assert foreign_keys == 1, "foreign_keys는 ON이어야 합니다"


def test_status_lifecycle_success(temp_db: Database) -> None:
    """요청 선예약 -> Agent 성공 -> 피드백 등록 완료로 이어지는 정상 상태 전이를 검증합니다."""
    tester_id = str(uuid.uuid4())
    client_req_id = str(uuid.uuid4())
    question = "MHC 알람 조치 방법은?"

    # 1. processing 상태로 선예약
    record, is_new = temp_db.reserve_test(tester_id, client_req_id, question)
    assert is_new is True
    assert record.status == "processing"
    assert record.question == question
    assert record.agent_response is None
    assert record.latency_ms is None

    # 2. Agent 성공 응답 반영 -> awaiting_feedback
    updated = temp_db.update_test_success(
        test_id=record.id,
        agent_response="매뉴얼 24페이지를 참조하세요.",
        latency_ms=1200,
    )
    assert updated is True

    fetched = temp_db.get_test_by_id_and_tester(record.id, tester_id)
    assert fetched is not None
    assert fetched.status == "awaiting_feedback"
    assert fetched.agent_response == "매뉴얼 24페이지를 참조하세요."
    assert fetched.latency_ms == 1200

    # 3. 사용자 피드백 제출 -> completed
    feedback_saved = temp_db.save_feedback(
        test_id=record.id,
        tester_id=tester_id,
        expected_response="A/S 접수 경로도 같이 안내해야 함",
    )
    assert feedback_saved is True

    completed_record = temp_db.get_test_by_id_and_tester(record.id, tester_id)
    assert completed_record is not None
    assert completed_record.status == "completed"
    assert completed_record.expected_response == "A/S 접수 경로도 같이 안내해야 함"


def test_status_lifecycle_failure(temp_db: Database) -> None:
    """Agent 호출 실패 시 processing -> failed 상태 전이를 검증합니다."""
    tester_id = str(uuid.uuid4())
    client_req_id = str(uuid.uuid4())
    question = "단종 모델 사양은?"

    # 1. processing 예약
    record, is_new = temp_db.reserve_test(tester_id, client_req_id, question)
    assert is_new is True
    assert record.status == "processing"

    # 2. 실패 업데이트 -> failed
    updated = temp_db.update_test_failure(
        test_id=record.id,
        error_message="Agent Connection Refused",
        latency_ms=500,
    )
    assert updated is True

    failed_record = temp_db.get_test_by_id_and_tester(record.id, tester_id)
    assert failed_record is not None
    assert failed_record.status == "failed"
    assert failed_record.latency_ms == 500
    assert failed_record.metadata_json is not None
    assert "Agent Connection Refused" in failed_record.metadata_json


def test_idempotency_reuse_completed(temp_db: Database) -> None:
    """동일한 요청 식별자 및 질문으로 재호출 시 기존 완료된 결과를 재사용함을 검증합니다."""
    tester_id = str(uuid.uuid4())
    client_req_id = str(uuid.uuid4())
    question = "필터 교체 주기는?"

    record, is_new = temp_db.reserve_test(tester_id, client_req_id, question)
    assert is_new is True
    temp_db.update_test_success(record.id, "6개월마다 교체 권장", 850)

    # 동일 요청 재시도
    reused_record, reused_is_new = temp_db.reserve_test(tester_id, client_req_id, question)
    assert reused_is_new is False
    assert reused_record.id == record.id
    assert reused_record.agent_response == "6개월마다 교체 권장"


def test_idempotency_conflict_different_question(temp_db: Database) -> None:
    """동일 client_request_id에 서로 다른 질문이 들어오면 IdempotencyConflictError가 발생해야 합니다."""
    tester_id = str(uuid.uuid4())
    client_req_id = str(uuid.uuid4())

    temp_db.reserve_test(tester_id, client_req_id, "첫 번째 질문")

    with pytest.raises(IdempotencyConflictError):
        temp_db.reserve_test(tester_id, client_req_id, "두 번째 다른 질문")


def test_idempotency_in_progress_conflict(temp_db: Database) -> None:
    """현재 processing 중인 동일 요청 식별자에 대해 중복 호출 시 InProgressConflictError가 발생해야 합니다."""
    tester_id = str(uuid.uuid4())
    client_req_id = str(uuid.uuid4())
    question = "처리 중인 질문"

    temp_db.reserve_test(tester_id, client_req_id, question)

    with pytest.raises(InProgressConflictError):
        temp_db.reserve_test(tester_id, client_req_id, question)


def test_idempotency_failed_retry_rejected(temp_db: Database) -> None:
    """이전에 failed된 동일 요청 식별자는 자동 재시도되지 않고 FailedRetryError가 발생해야 합니다."""
    tester_id = str(uuid.uuid4())
    client_req_id = str(uuid.uuid4())
    question = "실패할 질문"

    record, _ = temp_db.reserve_test(tester_id, client_req_id, question)
    temp_db.update_test_failure(record.id, "Network timeout")

    with pytest.raises(FailedRetryError):
        temp_db.reserve_test(tester_id, client_req_id, question)
