"""완료된 사용자 피드백을 일자별 JSONL로 원자적 익스포트하는 CLI 스크립트.

R22 요구사항에 따라:
- 'completed' 상태의 레코드만 선별하여 추출합니다 (processing, awaiting_feedback, failed 제외).
- 일자별(test_date, Asia/Seoul 기준)로 분할하여 data/exports/YYYY-MM-DD.jsonl 파일로 저장합니다.
- 안정 정렬(stable sort) 후 임시 파일 작성 및 os.replace 원자적 교체를 수행하여 멱등 실행을 보장합니다.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# 프로젝트 루트 경로 추가 (CLI 직접 실행 지원)
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.database import TestRecord, db
from app.settings import settings

KST = ZoneInfo("Asia/Seoul")


def export_records_to_jsonl(
    records: list[TestRecord],
    exports_dir: Path,
) -> dict[str, int]:
    """레코드 목록을 일자별로 그룹화하고 임시 파일을 거쳐 원자적으로 대상 JSONL을 교체합니다.

    매개변수:
        records: 익스포트할 'completed' 상태의 레코드 목록
        exports_dir: JSONL 파일이 저장될 출력 디렉터리

    반환값:
        dict[str, int]: 일자별 익스포트된 레코드 수 매핑 (예: {"2026-09-08": 15})
    """
    exports_dir.mkdir(parents=True, exist_ok=True)

    # 일자별 그룹화 (안정 정렬 보장: created_at, id 순)
    grouped: dict[str, list[TestRecord]] = defaultdict(list)
    for r in records:
        grouped[r.test_date].append(r)

    results: dict[str, int] = {}

    for date_str, items in grouped.items():
        # 안정 정렬: 생성 시각 및 레코드 ID 기준
        items.sort(key=lambda x: (x.created_at, x.id))

        target_file = exports_dir / f"{date_str}.jsonl"
        tmp_file = exports_dir / f".{date_str}.jsonl.tmp"

        # 임시 파일에 정렬된 JSONL 라인 기록
        with open(tmp_file, "w", encoding="utf-8") as f:
            for item in items:
                record_dict = {
                    "id": item.id,
                    "tester_id": item.tester_id,
                    "client_request_id": item.client_request_id,
                    "created_at": item.created_at,
                    "test_date": item.test_date,
                    "question": item.question,
                    "agent_response": item.agent_response,
                    "expected_response": item.expected_response,
                    "latency_ms": item.latency_ms,
                }
                f.write(json.dumps(record_dict, ensure_ascii=False) + "\n")

        # 원자적 파일 교체 (Atomic replace)
        os.replace(tmp_file, target_file)
        results[date_str] = len(items)

    return results


def run_export(
    target_date: Optional[str] = None,
    export_all: bool = False,
    output_dir: Optional[str] = None,
) -> int:
    """피드백 데이터 익스포트를 총괄 실행합니다.

    반환값:
        int: 총 익스포트된 레코드 건수
    """
    exports_path = Path(output_dir or "data/exports")

    # 옵션이 전혀 지정되지 않은 경우: 오늘 일자(Asia/Seoul)를 기본값으로 사용
    if not target_date and not export_all:
        today_kst = datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d")
        print(f"[*] 날짜 옵션이 지정되지 않아 오늘 일자({today_kst})를 기본값으로 익스포트합니다.")
        target_date = today_kst

    query_date = None if export_all else target_date
    records = db.get_completed_tests(test_date=query_date)

    if not records:
        print(f"[-] 익스포트 대상이 되는 완료된 피드백 레코드가 없습니다. (조건: date={query_date})")
        return 0

    counts = export_records_to_jsonl(records, exports_path)
    total = sum(counts.values())

    print(f"[+] 총 {total}건의 완료된 피드백 레코드가 익스포트되었습니다.")
    for dt, cnt in sorted(counts.items()):
        print(f"    - {dt}.jsonl : {cnt}건")

    return total


def main() -> None:
    """CLI 진입점 함수."""
    parser = argparse.ArgumentParser(
        description="First-AI-Agent 완료된 피드백 데이터 일자별 JSONL 익스포트 도구"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--date",
        type=str,
        help="익스포트할 특정 일자 (YYYY-MM-DD, Asia/Seoul 기준)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="데이터베이스 내 완료된 모든 일자의 피드백을 각각의 JSONL 파일로 익스포트",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/exports",
        help="JSONL 출력 디렉터리 경로 (기본값: data/exports)",
    )

    args = parser.parse_args()
    run_export(target_date=args.date, export_all=args.all, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
