"""완료된 피드백 레코드를 선택적으로 삭제하는 개발자용 CLI.

ID 한 건, 한국 날짜별, 전체 완료 피드백 중 하나의 범위를 명시해야 하며 기본적으로
터미널 확인 문구를 요구합니다. 자동화 환경에서는 ``--yes``로 확인 단계를 생략할
수 있습니다. 진행 중이거나 피드백 대기·실패·취소 상태인 레코드는 삭제하지 않습니다.
"""

import argparse
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Optional

# 파일 경로로 직접 실행하는 경우에도 프로젝트 패키지를 찾을 수 있게 합니다.
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.database import Database, db


def validate_date(value: str) -> str:
    """YYYY-MM-DD 형식의 실제 달력 날짜만 argparse 값으로 허용합니다."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc
    return parsed.strftime("%Y-%m-%d")


def count_records(
    database: Database,
    *,
    record_id: Optional[str],
    target_date: Optional[str],
    delete_all: bool,
) -> int:
    """삭제 확인 전에 범위에 포함되는 completed 레코드 수만 DB에서 조회합니다.

    전체 삭제를 선택해도 Agent 응답 본문을 포함한 모든 행을 메모리에 적재하지 않고
    ``COUNT(*)`` 결과만 가져오므로, 피드백이 누적된 운영 DB에서도 확인 단계의
    메모리 사용량이 데이터 건수에 비례해 증가하지 않습니다.
    """
    return database.count_completed_feedbacks_for_deletion(
        test_id=record_id,
        test_date=target_date,
        delete_all=delete_all,
    )


def run_delete(
    *,
    record_id: Optional[str] = None,
    target_date: Optional[str] = None,
    delete_all: bool = False,
    assume_yes: bool = False,
    database: Optional[Database] = None,
    input_func: Callable[[str], str] = input,
) -> int:
    """범위를 미리 보여주고 확인된 완료 피드백을 트랜잭션으로 삭제합니다.

    반환값:
        실제 삭제된 레코드 수. 대상이 없거나 사용자가 확인하지 않으면 0입니다.

    부작용:
        ``assume_yes``가 False이면 표준 입력을 기다립니다. 확인 후 SQLite의 완료
        레코드를 영구 삭제하므로 복구가 필요하면 실행 전에 DB를 백업해야 합니다.
    """
    active_db = database or db
    record_count = count_records(
        active_db,
        record_id=record_id,
        target_date=target_date,
        delete_all=delete_all,
    )
    if record_count == 0:
        print("[-] 조건에 맞는 완료 피드백이 없습니다.")
        return 0

    if record_id:
        scope = f"ID {record_id}"
    elif target_date:
        scope = f"날짜 {target_date}"
    else:
        scope = "전체 날짜"
    print(f"[!] 삭제 범위: {scope}, 완료 피드백 {record_count}건")

    if not assume_yes:
        confirmation = input_func("영구 삭제하려면 '삭제'를 입력하세요: ").strip()
        if confirmation != "삭제":
            print("[-] 삭제를 취소했습니다.")
            return 0

    deleted = active_db.delete_completed_feedbacks(
        test_id=record_id,
        test_date=target_date,
        delete_all=delete_all,
    )
    print(f"[+] 완료 피드백 {deleted}건을 삭제했습니다.")
    return deleted


def main() -> None:
    """명령행 옵션을 검증하고 선택 범위의 피드백 삭제를 실행합니다."""
    parser = argparse.ArgumentParser(description="완료된 Feedback Lab 피드백 삭제 도구")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--id", dest="record_id", help="삭제할 단일 테스트 레코드 ID")
    scope.add_argument(
        "--date",
        dest="target_date",
        type=validate_date,
        help="삭제할 한국 기준 테스트 날짜 (YYYY-MM-DD)",
    )
    scope.add_argument(
        "--all",
        dest="delete_all",
        action="store_true",
        help="모든 날짜의 완료 피드백 삭제",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="터미널 확인 문구 없이 삭제 (자동화 용도)",
    )
    args = parser.parse_args()
    run_delete(
        record_id=args.record_id,
        target_date=args.target_date,
        delete_all=args.delete_all,
        assume_yes=args.yes,
    )


if __name__ == "__main__":
    main()
