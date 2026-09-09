"""동시성 요청 및 SQLite WAL 모드 락 회피, 중복 호출 차단 테스트 모듈.

R7, R10, R23에 따라:
- 다중 테스터의 동시 요청 시 SQLite WAL 모드 하에서 DB 락 충돌 없이 안정적으로 처리되는지 검증합니다.
- 동일한 (tester_id, client_request_id)가 동시에 유입될 때 단 1건만 처리되고 중복 Agent 호출이 차단되는지 확인합니다.
"""

import concurrent.futures
import tempfile
import uuid
from pathlib import Path
from typing import Generator
import pytest

from app.database import Database, InProgressConflictError


@pytest.fixture
def temp_db() -> Generator[Database, None, None]:
    """임시 SQLite DB fixture."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "concurrent_test.db"
        yield Database(db_path=str(db_file))


def test_concurrent_multi_tester_reservations(temp_db: Database) -> None:
    """수십 명의 서로 다른 테스터가 동시에 요청할 때 SQLite WAL 모드에서 락 에러 없이 정상 처리되는지 검증합니다."""
    num_requests = 30

    def task(i: int) -> tuple[str, bool]:
        t_id = f"tester_{i}"
        req_id = str(uuid.uuid4())
        q = f"동시 요청 질문 {i}"
        record, is_new = temp_db.reserve_test(t_id, req_id, q)
        # 성공 시뮬레이션
        temp_db.update_test_success(record.id, f"답변 {i}", 100)
        return record.id, is_new

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(task, i) for i in range(num_requests)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert len(results) == num_requests
    assert all(is_new for _, is_new in results)

    # update_test_success는 awaiting_feedback 상태이므로 완료 목록에는 포함되지 않아야 함
    completed = temp_db.get_completed_tests()
    assert completed == []

    # 동시에 생성한 테스트 레코드는 상태와 관계없이 모두 저장되어야 함
    with temp_db.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM tests;").fetchone()[0]
        assert count == num_requests


def test_concurrent_duplicate_client_request_blocks_second_agent_call(temp_db: Database) -> None:
    """동일한 (tester_id, client_request_id)가 동시에 유입될 때, 1건만 선예약에 성공하고 다른 요청은 InProgressConflictError로 차단되어야 합니다."""
    tester_id = str(uuid.uuid4())
    client_req_id = str(uuid.uuid4())
    question = "동시 중복 테스트 질문"

    success_count = 0
    in_progress_count = 0

    def attempt_reservation() -> str:
        try:
            _, is_new = temp_db.reserve_test(tester_id, client_req_id, question)
            return "new" if is_new else "reused"
        except InProgressConflictError:
            return "in_progress"

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(attempt_reservation) for _ in range(5)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    for res in results:
        if res == "new":
            success_count += 1
        elif res == "in_progress":
            in_progress_count += 1

    # 최초 1개만 신규 생성(new)되고, 나머지는 processing 충돌(in_progress)이어야 함
    assert success_count == 1, f"신규 예약은 정확히 1건이어야 합니다 (실제: {success_count})"
    assert in_progress_count == 4, f"나머지 4건은 in_progress 충돌이어야 합니다 (실제: {in_progress_count})"

    # DB에 단 하나의 레코드만 존재해야 함
    with temp_db.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tests WHERE tester_id = ? AND client_request_id = ?;",
            (tester_id, client_req_id),
        ).fetchall()
        assert len(rows) == 1
