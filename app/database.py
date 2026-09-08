"""SQLite WAL 기반 데이터베이스 계층 및 멱등성 트랜잭션 관리 모듈.

R7, R10에 따라 First-AI-Agent 호출 전 레코드를 'processing' 상태로 선예약하여 동시 중복 호출을 차단하며,
동일 (tester_id, client_request_id) 조합에 대한 엄격한 멱등성 및 상태 전이 규칙을 시행합니다.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.settings import settings

KST = ZoneInfo("Asia/Seoul")


class DatabaseError(Exception):
    """데이터베이스 처리 중 발생하는 기본 예외."""


class IdempotencyConflictError(DatabaseError):
    """동일한 요청 식별자에 다른 질문이 지정되었을 때 발생하는 예외 (HTTP 409)."""


class InProgressConflictError(DatabaseError):
    """동일한 요청 식별자가 현재 Agent 처리 중(processing)일 때 발생하는 예외 (HTTP 409)."""


class FailedRetryError(DatabaseError):
    """이전에 실패한 요청 식별자를 재사용하려 할 때 발생하는 예외 (HTTP 409)."""


@dataclass
class TestRecord:
    """데이터베이스에 저장된 단일 테스트 엔티티 레코드."""

    id: str
    tester_id: str
    client_request_id: str
    created_at: str
    test_date: str
    question: str
    agent_response: Optional[str]
    expected_response: Optional[str]
    status: str
    latency_ms: Optional[int]
    metadata_json: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TestRecord":
        """sqlite3.Row 객체로부터 TestRecord 데이터클래스를 생성합니다."""
        return cls(
            id=row["id"],
            tester_id=row["tester_id"],
            client_request_id=row["client_request_id"],
            created_at=row["created_at"],
            test_date=row["test_date"],
            question=row["question"],
            agent_response=row["agent_response"],
            expected_response=row["expected_response"],
            status=row["status"],
            latency_ms=row["latency_ms"],
            metadata_json=row["metadata_json"],
        )


class Database:
    """SQLite WAL 모드 연결 및 레코드 수명주기를 관리하는 데이터베이스 매니저."""

    def __init__(self, db_path: Optional[str] = None):
        """데이터베이스 경로를 지정하고 스키마를 초기화합니다."""
        self.db_path = db_path or str(settings.resolved_database_path)
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        """WAL 모드와 동시성 보호 PRAGMA가 구성된 새 커넥션을 반환합니다."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,  # busy_timeout 5000ms
            isolation_level=None,  # 자동 트랜잭션 관리
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_db(self) -> None:
        """테이블 스키마 및 인덱스를 생성합니다."""
        with self.get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tests (
                    id TEXT PRIMARY KEY,
                    tester_id TEXT NOT NULL,
                    client_request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    test_date TEXT NOT NULL,
                    question TEXT NOT NULL,
                    agent_response TEXT,
                    expected_response TEXT,
                    status TEXT NOT NULL,
                    latency_ms INTEGER,
                    metadata_json TEXT,
                    UNIQUE(tester_id, client_request_id)
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_tests_tester 
                ON tests(tester_id);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_tests_date_status 
                ON tests(test_date, status);
            """)

    def reserve_test(
        self,
        tester_id: str,
        client_request_id: str,
        question: str,
    ) -> tuple[TestRecord, bool]:
        """First-AI-Agent 호출 전 'processing' 상태로 레코드를 선예약합니다.

        R7 요구사항에 따라:
        - 신규 요청인 경우: 'processing' 상태로 insert하고 (record, True) 반환.
        - 동일 tester + 동일 client_request_id 중복 시:
          - 질문이 다르면: IdempotencyConflictError 발생 (409 Conflict).
          - 질문이 같고 상태가 'processing': InProgressConflictError 발생 (409 Conflict).
          - 질문이 같고 상태가 'failed': FailedRetryError 발생 (409 Conflict, 새 ID 유도).
          - 질문이 같고 상태가 'awaiting_feedback' 또는 'completed': 기존 (record, False) 재사용 반환.

        반환값:
            tuple[TestRecord, bool]: (레코드 객체, 신규 예약 여부)
        """
        now_utc = datetime.now(timezone.utc)
        now_kst = now_utc.astimezone(KST)
        created_at = now_utc.isoformat()
        test_date = now_kst.strftime("%Y-%m-%d")
        new_id = str(uuid.uuid4())

        with self.get_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    """
                    INSERT INTO tests (
                        id, tester_id, client_request_id, created_at,
                        test_date, question, agent_response, expected_response,
                        status, latency_ms, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 'processing', NULL, NULL);
                    """,
                    (new_id, tester_id, client_request_id, created_at, test_date, question),
                )
                conn.execute("COMMIT;")
                cursor = conn.execute("SELECT * FROM tests WHERE id = ?;", (new_id,))
                row = cursor.fetchone()
                if not row:
                    raise DatabaseError("방금 생성된 테스트 레코드를 조회할 수 없습니다.")
                return TestRecord.from_row(row), True

            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK;")
                # 중복 식별자 발생: 기존 레코드 확인
                cursor = conn.execute(
                    """
                    SELECT * FROM tests 
                    WHERE tester_id = ? AND client_request_id = ?;
                    """,
                    (tester_id, client_request_id),
                )
                existing_row = cursor.fetchone()
                if not existing_row:
                    raise DatabaseError("중복 키 충돌 후 기존 레코드 조회 실패")

                existing = TestRecord.from_row(existing_row)

                # 질문 내용 일치 여부 검증
                if existing.question != question:
                    raise IdempotencyConflictError(
                        "동일한 client_request_id에 서로 다른 질문이 지정되었습니다."
                    )

                # 상태별 멱등 분기
                if existing.status == "processing":
                    raise InProgressConflictError(
                        "해당 요청은 현재 AI Agent에서 처리 중입니다. 잠시 후 다시 시도해 주세요."
                    )
                if existing.status == "failed":
                    raise FailedRetryError(
                        "이전에 실패한 요청입니다. 새 테스트를 시작해 주세요."
                    )
                if existing.status in ("awaiting_feedback", "completed"):
                    return existing, False

                raise DatabaseError(f"예상치 못한 테스트 상태입니다: {existing.status}")

    def update_test_success(
        self,
        test_id: str,
        agent_response: str,
        latency_ms: int,
    ) -> bool:
        """First-AI-Agent 성공 응답을 저장하고 상태를 'awaiting_feedback'으로 변경합니다."""
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute(
                """
                UPDATE tests
                SET agent_response = ?,
                    status = 'awaiting_feedback',
                    latency_ms = ?
                WHERE id = ? AND status = 'processing';
                """,
                (agent_response, latency_ms, test_id),
            )
            updated = cursor.rowcount > 0
            conn.execute("COMMIT;")
            return updated

    def update_test_failure(
        self,
        test_id: str,
        error_message: str,
        latency_ms: Optional[int] = None,
    ) -> bool:
        """First-AI-Agent 통신 또는 계약 실패 시 상태를 'failed'로 변경하고 진단 정보를 기록합니다."""
        metadata = json.dumps({"error": error_message}, ensure_ascii=False)
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute(
                """
                UPDATE tests
                SET status = 'failed',
                    latency_ms = ?,
                    metadata_json = ?
                WHERE id = ? AND status = 'processing';
                """,
                (latency_ms, metadata, test_id),
            )
            updated = cursor.rowcount > 0
            conn.execute("COMMIT;")
            return updated

    def save_feedback(
        self,
        test_id: str,
        tester_id: str,
        expected_response: str,
    ) -> bool:
        """사용자 피드백을 저장하고 레코드 상태를 'completed'로 변경합니다.

        R11 요구사항에 따라 현재 요청한 tester_id와 일치하며
        상태가 'awaiting_feedback'인 경우에만 갱신을 허용합니다.
        """
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute(
                """
                UPDATE tests
                SET expected_response = ?,
                    status = 'completed'
                WHERE id = ? AND tester_id = ? AND status = 'awaiting_feedback';
                """,
                (expected_response, test_id, tester_id),
            )
            updated = cursor.rowcount > 0
            conn.execute("COMMIT;")
            return updated

    def get_test_by_id_and_tester(
        self,
        test_id: str,
        tester_id: str,
    ) -> Optional[TestRecord]:
        """지정된 tester_id가 소유한 특정 테스트 레코드를 조회합니다."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM tests WHERE id = ? AND tester_id = ?;",
                (test_id, tester_id),
            )
            row = cursor.fetchone()
            return TestRecord.from_row(row) if row else None

    def get_completed_tests(
        self,
        test_date: Optional[str] = None,
    ) -> list[TestRecord]:
        """JSONL 익스포트 파이프라인을 위해 'completed' 상태의 레코드 목록을 정렬 조회합니다.

        R22 요구사항에 따라 (test_date, created_at, id) 기준 안정 정렬(stable sort)을 수행합니다.
        """
        query = "SELECT * FROM tests WHERE status = 'completed'"
        params: list[Any] = []
        if test_date:
            query += " AND test_date = ?"
            params.append(test_date)
        query += " ORDER BY test_date ASC, created_at ASC, id ASC;"

        with self.get_connection() as conn:
            cursor = conn.execute(query, tuple(params))
            return [TestRecord.from_row(row) for row in cursor.fetchall()]


# 싱글톤 데이터베이스 인스턴스
db = Database()
