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
    """데이터베이스에 저장된 단일 테스트 엔티티 레코드.

    ``feedback_id``는 개발자가 화면과 CLI에서 사용하는 완료 피드백 순번이고,
    ``id``는 테스터 세션의 상태 전이와 소유권 검증에 사용하는 내부 UUID입니다.
    완료 전에는 순번이 없으므로 None이며, 삭제 후 남은 완료 피드백 순서에 맞춰
    값이 다시 정렬됩니다. 두 식별자를 분리해 관리 번호의 편의성과 기존 API의
    추측 불가능성을 함께 유지합니다.
    """

    feedback_id: Optional[int]
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
            feedback_id=row["feedback_id"],
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
        """테이블 스키마를 생성하고 완료 피드백 숫자 순번을 안전하게 보정합니다.

        이전 버전의 ``tests`` 테이블에는 UUID ``id``만 있으므로 ``feedback_id``
        컬럼을 추가합니다. 이전 구현에서 모든 테스트에 번호를 부여했던 시퀀스와
        트리거는 제거하고, 완료 레코드만 생성 시각 순서대로 1부터 다시 정렬합니다.
        전체 변경은 즉시 쓰기 트랜잭션에서 수행되므로 여러 프로세스가 동시에
        초기화되어도 부분 마이그레이션 상태가 노출되지 않습니다.

        예외:
            SQLite가 스키마 변경 또는 시퀀스 초기화에 실패하면 원래 예외를 전달하며,
            진행 중인 변경은 rollback합니다.
        """
        with self.get_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS tests (
                        feedback_id INTEGER UNIQUE,
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

                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(tests);").fetchall()
                }
                if "feedback_id" not in columns:
                    conn.execute("ALTER TABLE tests ADD COLUMN feedback_id INTEGER;")

                conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_tests_feedback_id
                    ON tests(feedback_id);
                """)
                conn.execute("DROP TRIGGER IF EXISTS trg_tests_assign_feedback_id;")
                conn.execute("DROP TABLE IF EXISTS id_sequences;")
                self._renumber_completed_feedbacks(conn)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tests_tester
                    ON tests(tester_id);
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tests_date_status
                    ON tests(test_date, status);
                """)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    @staticmethod
    def _renumber_completed_feedbacks(conn: sqlite3.Connection) -> None:
        """완료 피드백만 생성 순서대로 1부터 끊김 없이 다시 번호를 부여합니다.

        먼저 완료되지 않은 행의 번호를 비우고, 완료 행을 고유한 음수 rowid로
        임시 이동한 뒤 ``ROW_NUMBER`` 결과를 양수 순번으로 저장합니다. 두 단계로
        갱신하므로 UNIQUE 인덱스가 있는 상태에서도 기존 번호와 새 번호가 중간에
        충돌하지 않습니다. 호출자는 원자성을 위해 쓰기 트랜잭션을 시작해야 합니다.

        부작용:
            완료 피드백의 ``feedback_id``가 현재 생성 순서에 맞춰 변경됩니다.
        """
        conn.execute("UPDATE tests SET feedback_id = NULL WHERE status != 'completed';")
        conn.execute(
            "UPDATE tests SET feedback_id = -rowid WHERE status = 'completed';"
        )
        conn.execute("""
            WITH ranked AS (
                SELECT
                    rowid AS target_rowid,
                    ROW_NUMBER() OVER (ORDER BY created_at ASC, rowid ASC) AS new_id
                FROM tests
                WHERE status = 'completed'
            )
            UPDATE tests
            SET feedback_id = (
                SELECT new_id
                FROM ranked
                WHERE ranked.target_rowid = tests.rowid
            )
            WHERE status = 'completed';
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
          - 질문이 같고 상태가 'failed' 또는 'cancelled': FailedRetryError 발생 (409 Conflict, 새 ID 유도).
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
                if existing.status in ("failed", "cancelled"):
                    raise FailedRetryError(
                        "이미 실패했거나 취소된 요청입니다. 새 테스트를 시작해 주세요."
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

        R11 요구사항에 따라 현재 요청한 tester_id와 일치하며 상태가
        'awaiting_feedback'인 경우에만 갱신을 허용합니다. 쓰기 잠금을 확보한 뒤
        현재 완료 피드백의 마지막 번호 다음 값을 함께 저장하므로, 데이터가 없으면
        항상 1부터 시작하고 동시 제출에도 중복 번호가 발생하지 않습니다.

        반환값:
            피드백과 숫자 순번을 실제로 저장했으면 True, 소유권 또는 상태 조건이
            맞지 않으면 False입니다.
        """
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            max_row = conn.execute(
                "SELECT COALESCE(MAX(feedback_id), 0) AS max_id FROM tests "
                "WHERE status = 'completed';"
            ).fetchone()
            next_feedback_id = (int(max_row["max_id"]) if max_row else 0) + 1
            cursor = conn.execute(
                """
                UPDATE tests
                SET expected_response = ?,
                    status = 'completed',
                    feedback_id = ?
                WHERE id = ? AND tester_id = ? AND status = 'awaiting_feedback';
                """,
                (expected_response, next_feedback_id, test_id, tester_id),
            )
            updated = cursor.rowcount > 0
            conn.execute("COMMIT;")
            return updated

    def cancel_test(self, test_id: str, tester_id: str) -> bool:
        """현재 테스터가 피드백을 제출하지 않기로 한 테스트를 취소합니다.

        Agent 답변을 끝까지 받은 ``awaiting_feedback`` 상태에서만 ``cancelled``로
        전환합니다. tester_id 조건으로 다른 사용자의 테스트 취소를 차단하며,
        이미 완료되거나 취소된 레코드는 변경하지 않습니다.

        반환값:
            실제로 한 레코드가 취소되었으면 True, 조건에 맞는 레코드가 없으면 False.
        """
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.execute(
                """
                UPDATE tests
                SET status = 'cancelled'
                WHERE id = ? AND tester_id = ? AND status = 'awaiting_feedback';
                """,
                (test_id, tester_id),
            )
            updated = cursor.rowcount > 0
            conn.execute("COMMIT;")
            return updated

    @staticmethod
    def _completed_delete_filter(
        *,
        feedback_id: Optional[int],
        test_date: Optional[str],
        delete_all: bool,
    ) -> tuple[str, list[Any]]:
        """숫자 관리 ID·날짜·전체 중 하나인 완료 피드백 삭제 조건을 만듭니다.

        잘못된 호출이 광범위한 삭제로 이어지지 않도록 정확히 한 선택자만 허용하며,
        숫자 ID는 양의 정수만 받습니다.

        예외:
            선택자가 없거나 여러 개이거나 숫자 ID가 1보다 작으면 ValueError를
            발생시킵니다.
        """
        selected = sum((feedback_id is not None, bool(test_date), delete_all))
        if selected != 1:
            raise ValueError("feedback_id, test_date, delete_all 중 정확히 하나를 지정해야 합니다.")
        if feedback_id is not None:
            if feedback_id < 1:
                raise ValueError("feedback_id는 1 이상의 정수여야 합니다.")
            return " AND feedback_id = ?", [feedback_id]
        if test_date:
            return " AND test_date = ?", [test_date]
        return "", []

    def count_completed_feedbacks_for_deletion(
        self,
        *,
        feedback_id: Optional[int] = None,
        test_date: Optional[str] = None,
        delete_all: bool = False,
    ) -> int:
        """CLI 확인 화면에 표시할 선택 범위의 completed 레코드 수를 반환합니다.

        입력값은 숫자 관리 ID, 한국 날짜, 전체 선택 중 하나이며 실제 데이터는
        변경하지 않습니다.
        """
        suffix, params = self._completed_delete_filter(
            feedback_id=feedback_id,
            test_date=test_date,
            delete_all=delete_all,
        )
        with self.get_connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM tests WHERE status = 'completed'{suffix};",
                tuple(params),
            ).fetchone()
            return int(row[0]) if row else 0

    def delete_completed_feedbacks(
        self,
        *,
        feedback_id: Optional[int] = None,
        test_date: Optional[str] = None,
        delete_all: bool = False,
    ) -> int:
        """개발자 CLI가 선택한 완료 피드백 레코드를 트랜잭션으로 삭제합니다.

        화면에 표시되는 숫자 관리 ID, 한국 날짜, 전체 삭제 중 정확히 한 범위만
        허용합니다. processing, awaiting_feedback, failed, cancelled 상태는 어떤
        범위에서도 삭제하지 않아 운영 중 요청과 진단 데이터를 우발적으로 훼손하지
        않습니다.

        반환값:
            실제 삭제된 ``completed`` 레코드 수.

        예외:
            삭제 범위가 없거나 둘 이상이면 ValueError를 발생시킵니다. SQLite 오류가
            발생하면 트랜잭션을 rollback한 뒤 원래 예외를 다시 전달합니다.
        """
        suffix, params = self._completed_delete_filter(
            feedback_id=feedback_id,
            test_date=test_date,
            delete_all=delete_all,
        )
        query = f"DELETE FROM tests WHERE status = 'completed'{suffix};"

        with self.get_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE;")
                cursor = conn.execute(query, tuple(params))
                deleted = max(cursor.rowcount, 0)
                if deleted > 0:
                    # 날짜·전체 삭제에서도 남은 완료 피드백 번호가 1부터 연속되도록 함께 압축합니다.
                    self._renumber_completed_feedbacks(conn)
                conn.execute("COMMIT;")
                return deleted
            except Exception:
                conn.execute("ROLLBACK;")
                raise

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

        R22 요구사항에 따라 (test_date, created_at, feedback_id) 기준 안정 정렬을
        수행하며, 사람이 확인하는 숫자 관리 ID를 최종 동률 해소 기준으로 사용합니다.
        """
        query = "SELECT * FROM tests WHERE status = 'completed'"
        params: list[Any] = []
        if test_date:
            query += " AND test_date = ?"
            params.append(test_date)
        query += " ORDER BY test_date ASC, created_at ASC, feedback_id ASC;"

        with self.get_connection() as conn:
            cursor = conn.execute(query, tuple(params))
            return [TestRecord.from_row(row) for row in cursor.fetchall()]

    @staticmethod
    def _feedback_search(search: Optional[str]) -> tuple[str, list[Any]]:
        """완료 피드백 검색용 SQL 조건과 바인딩 값을 만듭니다.

        사용자가 입력한 ``%``와 ``_``를 LIKE 와일드카드로 해석하지 않고 문자
        그대로 검색합니다. 질문, Agent 원본 응답, 기대 응답 세 필드를 DB에서
        함께 검색하므로 브라우저에 내려받지 않은 과거 페이지도 검색 대상입니다.
        """
        normalized = (search or "").strip()
        if not normalized:
            return "", []
        escaped = (
            normalized.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        clause = """
            AND (
                question LIKE ? ESCAPE '\\'
                OR agent_response LIKE ? ESCAPE '\\'
                OR expected_response LIKE ? ESCAPE '\\'
            )
        """
        return clause, [pattern, pattern, pattern]

    def get_completed_count(self, search: Optional[str] = None) -> int:
        """검색 조건에 맞는 완료 피드백 레코드 수를 반환합니다.

        검색어가 없으면 헤더 배지에서 사용하는 전체 완료 건수이며, 검색어가
        있으면 페이지네이션의 페이지 수 계산에 사용할 필터 결과 건수입니다.
        """
        search_clause, params = self._feedback_search(search)
        query = f"SELECT COUNT(*) FROM tests WHERE status = 'completed' {search_clause};"
        with self.get_connection() as conn:
            cursor = conn.execute(query, tuple(params))
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_feedbacks_list(
        self,
        limit: int = 20,
        offset: int = 0,
        search: Optional[str] = None,
    ) -> list[TestRecord]:
        """검색 조건과 페이지 범위에 맞는 완료 피드백을 최신순으로 조회합니다.

        ``limit``과 ``offset``은 API에서 검증되지만 파이프라인이나 테스트가 직접
        호출해도 음수 범위가 SQL 의미를 바꾸지 않도록 여기서 한 번 더 보정합니다.
        """
        safe_limit = max(1, limit)
        safe_offset = max(0, offset)
        search_clause, params = self._feedback_search(search)
        query = f"""
            SELECT * FROM tests 
            WHERE status = 'completed'
            {search_clause}
            ORDER BY created_at DESC, feedback_id DESC
            LIMIT ? OFFSET ?;
        """
        params.extend([safe_limit, safe_offset])
        with self.get_connection() as conn:
            cursor = conn.execute(query, tuple(params))
            return [TestRecord.from_row(row) for row in cursor.fetchall()]


# 싱글톤 데이터베이스 인스턴스
db = Database()
