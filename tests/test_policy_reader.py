"""정책 파일에서 화면 안내 정보를 추출하는 어댑터의 단위 테스트 모듈.

실제 First-AI-Agent 저장소에 의존하지 않고 임시 정책 파일을 사용하여, 정책별 URL과
기본 연락처가 함께 반환되는 정상 경로를 검증합니다.
"""

import json
from pathlib import Path

from app.policy_reader import DEFAULT_POLICY_INFO, PolicyReader


def test_get_policy_display_info_reads_configured_urls(tmp_path: Path) -> None:
    """유효한 정책 파일의 매뉴얼·수리 URL을 읽고 기본 연락처의 복사본을 반환하는지 검증합니다.

    반환된 연락처를 호출자가 변경해도 모듈의 기본 정책값에 영향을 주지 않아야 하므로,
    값의 일치뿐 아니라 별도 매핑 객체인지도 함께 확인합니다.
    """
    policy_path = tmp_path / "dataset_policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "intent_routes": {
                    "operation_howto": {"url": "https://example.com/manual"},
                    "repair_or_as": {"url": "https://example.com/repair"},
                }
            }
        ),
        encoding="utf-8",
    )

    info = PolicyReader(str(policy_path)).get_policy_display_info()

    assert info["manual_url"] == "https://example.com/manual"
    assert info["repair_url"] == "https://example.com/repair"
    assert info["contacts"] == DEFAULT_POLICY_INFO["contacts"]
    assert info["contacts"] is not DEFAULT_POLICY_INFO["contacts"]
