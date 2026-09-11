"""JSONL 피드백 익스포트 파이프라인 단위 테스트 모듈.

R22, R23에 따라:
- 'completed' 상태의 레코드만 추출되고 processing, awaiting_feedback, failed는 제외되는지 검증합니다.
- Asia/Seoul 일자별(YYYY-MM-DD.jsonl) 분할 저장을 확인합니다.
- 반복 실행 시 멱등성이 보장되고 원자적 교체가 수행되는지 테스트합니다.
"""

import json
import tempfile
import uuid
from pathlib import Path
from typing import Generator
import pytest

from app.database import Database
from pipeline.export_feedback import export_records_to_jsonl


@pytest.fixture
def temp_db() -> Generator[Database, None, None]:
    """임시 SQLite DB fixture."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "export_test.db"
        yield Database(db_path=str(db_file))


def test_export_completed_records_only(temp_db: Database) -> None:
    """오직 'completed' 상태의 레코드만 익스포트되고 다른 상태(processing, awaiting_feedback, failed)는 제외되는지 검증합니다."""
    # 1. 다양한 상태의 레코드 생성
    t_id = str(uuid.uuid4())

    # completed 레코드 2건
    r1, _ = temp_db.reserve_test(t_id, str(uuid.uuid4()), "완료 질문 1")
    temp_db.update_test_success(r1.id, "답변 1", 100)
    temp_db.save_feedback(r1.id, t_id, "원했던 답변 1")

    r2, _ = temp_db.reserve_test(t_id, str(uuid.uuid4()), "완료 질문 2")
    temp_db.update_test_success(r2.id, "답변 2", 200)
    temp_db.save_feedback(r2.id, t_id, "원했던 답변 2")

    # awaiting_feedback 레코드 1건 (피드백 미입력)
    r3, _ = temp_db.reserve_test(t_id, str(uuid.uuid4()), "대기 질문")
    temp_db.update_test_success(r3.id, "답변 3", 150)

    # failed 레코드 1건
    r4, _ = temp_db.reserve_test(t_id, str(uuid.uuid4()), "실패 질문")
    temp_db.update_test_failure(r4.id, "Agent 오류")

    # processing 레코드 1건
    temp_db.reserve_test(t_id, str(uuid.uuid4()), "진행 중 질문")

    # 2. 익스포트 대상 조회
    completed_records = temp_db.get_completed_tests()
    assert len(completed_records) == 2
    assert {rec.id for rec in completed_records} == {r1.id, r2.id}

    # 3. 파일 작성 검증
    with tempfile.TemporaryDirectory() as export_dir:
        out_path = Path(export_dir)
        counts = export_records_to_jsonl(completed_records, out_path)

        date_key = r1.test_date
        assert counts[date_key] == 2

        target_jsonl = out_path / f"{date_key}.jsonl"
        assert target_jsonl.exists()

        with open(target_jsonl, "r", encoding="utf-8") as f:
            lines = f.readlines()
            assert len(lines) == 2

            parsed_1 = json.loads(lines[0])
            parsed_2 = json.loads(lines[1])

            # 필수 필드 보존 검증
            for p in (parsed_1, parsed_2):
                assert "feedback_id" in p
                assert "id" in p
                assert "tester_id" in p
                assert "created_at" in p
                assert "test_date" in p
                assert "question" in p
                assert "agent_response" in p
                assert "expected_response" in p


def test_export_rerun_is_idempotent_and_atomic(temp_db: Database) -> None:
    """반복 실행 시 라인이 중복 추가되지 않고 동일한 결과로 원자적 교체되는지 검증합니다."""
    t_id = str(uuid.uuid4())
    r, _ = temp_db.reserve_test(t_id, str(uuid.uuid4()), "멱등성 검증 질문")
    temp_db.update_test_success(r.id, "답변", 100)
    temp_db.save_feedback(r.id, t_id, "개선방향")

    completed_records = temp_db.get_completed_tests()

    with tempfile.TemporaryDirectory() as export_dir:
        out_path = Path(export_dir)
        target_jsonl = out_path / f"{r.test_date}.jsonl"

        # 1차 실행
        export_records_to_jsonl(completed_records, out_path)
        with open(target_jsonl, "r", encoding="utf-8") as f:
            lines_first = f.readlines()
        assert len(lines_first) == 1

        # 2차 재실행 (동일 데이터)
        export_records_to_jsonl(completed_records, out_path)
        with open(target_jsonl, "r", encoding="utf-8") as f:
            lines_second = f.readlines()

        # 라인이 2개로 증가하지 않고 정확히 1개여야 함
        assert len(lines_second) == 1
        assert lines_first == lines_second
