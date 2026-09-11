"""개발자용 피드백 삭제 CLI의 확인 절차와 삭제 범위 테스트 모듈."""

import argparse
import tempfile
import uuid
from pathlib import Path
from typing import Generator

import pytest

from app.database import Database
from pipeline.delete_feedback import run_delete, validate_date, validate_record_id


@pytest.fixture
def temp_db() -> Generator[Database, None, None]:
    """실제 운영 DB와 분리된 임시 SQLite 데이터베이스를 제공합니다."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Database(db_path=str(Path(tmpdir) / "delete_test.db"))


def create_completed_feedback(database: Database, question: str) -> int:
    """삭제 테스트용 completed 레코드를 만들고 화면과 CLI가 쓰는 숫자 ID를 반환합니다."""
    tester_id = str(uuid.uuid4())
    record, _ = database.reserve_test(tester_id, str(uuid.uuid4()), question)
    database.update_test_success(record.id, f"{question} 답변", 100)
    database.save_feedback(record.id, tester_id, f"{question} 피드백")
    completed = database.get_test_by_id_and_tester(record.id, tester_id)
    assert completed is not None
    assert completed.feedback_id is not None
    return completed.feedback_id


def test_run_delete_requires_confirmation(temp_db: Database) -> None:
    """확인 문구가 정확하지 않으면 데이터를 삭제하지 않습니다."""
    record_id = create_completed_feedback(temp_db, "확인 취소")

    deleted = run_delete(
        record_id=record_id,
        database=temp_db,
        input_func=lambda _prompt: "아니오",
    )

    assert deleted == 0
    assert temp_db.get_completed_count() == 1


def test_run_delete_by_id_and_all(temp_db: Database) -> None:
    """ID 삭제와 --yes 전체 삭제가 선택한 완료 피드백 수를 정확히 반환합니다."""
    first_id = create_completed_feedback(temp_db, "첫 번째")
    create_completed_feedback(temp_db, "두 번째")

    assert run_delete(
        record_id=first_id,
        database=temp_db,
        input_func=lambda _prompt: "삭제",
    ) == 1
    assert temp_db.get_completed_count() == 1
    assert run_delete(delete_all=True, assume_yes=True, database=temp_db) == 1
    assert temp_db.get_completed_count() == 0


def test_run_delete_by_korean_test_date(temp_db: Database) -> None:
    """날짜 선택 삭제가 해당 test_date의 completed 레코드만 제거하는지 검증합니다."""
    target_id = create_completed_feedback(temp_db, "날짜 삭제 대상")
    preserved_id = create_completed_feedback(temp_db, "다른 날짜 보존 대상")

    # 생성 시각에 의존하지 않고 날짜 선택자의 경계를 재현하도록 테스트 전용 날짜를 지정합니다.
    with temp_db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute(
            "UPDATE tests SET test_date = ? WHERE feedback_id = ?;",
            ("2026-09-08", target_id),
        )
        conn.execute(
            "UPDATE tests SET test_date = ? WHERE feedback_id = ?;",
            ("2026-09-09", preserved_id),
        )
        conn.execute("COMMIT;")

    assert run_delete(
        target_date="2026-09-08",
        assume_yes=True,
        database=temp_db,
    ) == 1
    assert temp_db.get_completed_count() == 1
    assert temp_db.count_completed_feedbacks_for_deletion(feedback_id=preserved_id) == 0
    assert temp_db.count_completed_feedbacks_for_deletion(feedback_id=1) == 1


def test_validate_date_rejects_invalid_calendar_date() -> None:
    """날짜 선택자가 형식뿐 아니라 실제 존재하는 날짜인지 검증합니다."""
    assert validate_date("2026-09-08") == "2026-09-08"
    with pytest.raises(argparse.ArgumentTypeError):
        validate_date("2026-02-30")


def test_validate_record_id_accepts_only_positive_integer() -> None:
    """삭제 CLI가 카드의 양의 숫자 ID만 받고 UUID나 0 이하 값은 거부하는지 검증합니다."""
    assert validate_record_id("12") == 12
    for invalid in ("0", "-1", "550e8400-e29b-41d4-a716-446655440000"):
        with pytest.raises(argparse.ArgumentTypeError):
            validate_record_id(invalid)
