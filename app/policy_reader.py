"""First-AI-Agent 정책 파일(dataset_policy.json) 조회 어댑터 모듈.

R18에 따라 에이전트의 공식 정책 정의 파일을 안전하게 읽어 화면 안내용 URL 및 문의처를 추출합니다.
정책 파일이 존재하지 않거나 읽을 수 없더라도 기본 안전값(fallback)을 제공하여 앱 전체의 다운을 방지합니다.
"""

import json
import logging
from pathlib import Path
from typing import TypedDict

from app.settings import settings

logger = logging.getLogger(__name__)


class PolicyDisplayInfo(TypedDict):
    """화면 안내 API에 전달하는 정책 정보의 고정 구조.

    정책 JSON에서 추출한 두 URL과 기본 연락처 매핑을 명시하여, 잘못된 키나 값 타입이
    런타임 예외 처리에 가려지기 전에 정적 분석에서 발견되도록 합니다.
    """

    manual_url: str
    repair_url: str
    contacts: dict[str, str]


# 파일 부재 시 사용할 기본 안전값 (Fallback)
DEFAULT_POLICY_INFO: PolicyDisplayInfo = {
    "manual_url": "https://www.msti.co.kr/download/02/",
    "repair_url": "https://www.msti.co.kr/customer/05/",
    "contacts": {
        "기술연구소 (기술문의)": "070-8666-3005",
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

    def get_policy_display_info(self) -> PolicyDisplayInfo:
        """정책 파일에서 UI에 표시할 매뉴얼 URL, A/S URL 및 연락처를 추출합니다.

        반환값:
            PolicyDisplayInfo: 정책 파일의 URL과 호출자가 안전하게 사용할 연락처 매핑.

        예외 및 부작용:
            파일 부재 또는 JSON 파싱 오류를 외부로 전파하지 않고 기본 안내값을 반환하며,
            원인을 로그에 남깁니다. 유효한 파일에서도 연락처는 기본값의 복사본을 반환합니다.
        """
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

            manual_url = operation_howto.get("url") or DEFAULT_POLICY_INFO["manual_url"]
            repair_url = repair_or_as.get("url") or DEFAULT_POLICY_INFO["repair_url"]

            # 연락처는 문자열만 담는 1단계 매핑이므로 얕은 복사로도 기본값의 변경을 방지할 수 있습니다.
            contacts = dict(DEFAULT_POLICY_INFO["contacts"])

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
