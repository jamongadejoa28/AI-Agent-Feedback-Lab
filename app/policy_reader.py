"""First-AI-Agent 정책 파일(dataset_policy.json) 조회 어댑터 모듈.

R18에 따라 에이전트의 공식 정책 정의 파일을 안전하게 읽어 화면 안내용 URL 및 문의처를 추출합니다.
정책 파일이 존재하지 않거나 읽을 수 없더라도 기본 안전값(fallback)을 제공하여 앱 전체의 다운을 방지합니다.
"""

import json
import logging
from pathlib import Path
from typing import Any

from app.settings import settings

logger = logging.getLogger(__name__)

# 파일 부재 시 사용할 기본 안전값 (Fallback)
DEFAULT_POLICY_INFO: dict[str, Any] = {
    "manual_url": "https://www.msti.co.kr/download/02/",
    "repair_url": "https://www.msti.co.kr/customer/05/",
    "contacts": {
        "기술연구소 (기술문의)": "070-8666-3069",
        "마케팅 (매뉴얼/구매)": "070-8666-4272",
    },
}


class PolicyReader:
    """First-AI-Agent 정책 데이터를 파싱하는 리더 클래스."""

    def __init__(self, policy_path: str | None = None):
        """정책 파일 경로를 설정합니다."""
        self.policy_path = Path(policy_path or settings.agent_policy_path)

    def is_available(self) -> bool:
        """정책 파일이 실제로 존재하고 읽기 가능한지 확인합니다."""
        return self.policy_path.is_file()

    def get_policy_display_info(self) -> dict[str, Any]:
        """UI에 표시할 매뉴얼 URL, A/S 접수 URL 및 주요 연락처를 추출합니다."""
        if not self.is_available():
            logger.warning(
                "정책 파일이 존재하지 않습니다 (%s). 기본 안내값을 사용합니다.",
                self.policy_path,
            )
            return DEFAULT_POLICY_INFO

        try:
            with open(self.policy_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            intent_routes = data.get("intent_routes", {})
            operation_howto = intent_routes.get("operation_howto", {})
            repair_or_as = intent_routes.get("repair_or_as", {})
            manual_and_repair = intent_routes.get("manual_and_repair", {})

            manual_url = operation_howto.get("url") or DEFAULT_POLICY_INFO["manual_url"]
            repair_url = repair_or_as.get("url") or DEFAULT_POLICY_INFO["repair_url"]

            contacts: dict[str, str] = {
                "기술연구소 (기술문의)": "070-8666-3069",
                "마케팅 (매뉴얼/구매)": "070-8666-4272",
            }

            return {
                "manual_url": manual_url,
                "repair_url": repair_url,
                "contacts": contacts,
            }
        except Exception as exc:
            logger.error("정책 파일 파싱 실패 (%s): %s", self.policy_path, exc)
            return DEFAULT_POLICY_INFO


# 싱글톤 정책 리더 인스턴스
policy_reader = PolicyReader()
