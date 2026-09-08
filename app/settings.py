"""환경 변수 및 애플리케이션 기본 설정 모듈.

pydantic-settings를 사용하여 .env 파일과 시스템 환경 변수로부터 설정을 로드하며,
Agent 연결 정보, 데이터베이스 경로, 보안 쿠키 옵션 등을 중앙 집중 관리합니다.
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Feedback Lab 애플리케이션 전체 설정 클래스.

    모든 설정값은 환경 변수 또는 .env 파일에서 덮어쓸 수 있습니다.
    """

    # First-AI-Agent HTTP 엔드포인트 URL
    agent_base_url: str = "http://0.0.0.0:8000"

    # SQLite 데이터베이스 파일 경로
    database_path: str = "data/feedback.db"

    # First-AI-Agent 정책 정의 파일 경로
    agent_policy_path: str = "../ai-agent/data/dataset_policy.json"

    # 세션 쿠키의 Secure 플래그 설정 (HTTPS 적용 시 True)
    cookie_secure: bool = False

    # First-AI-Agent 호출 타임아웃 (초 단위)
    agent_timeout_seconds: float = 30.0

    # 서버 바인딩 호스트 및 포트
    host: str = "0.0.0.0"
    port: int = 8001
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def resolved_database_path(self) -> Path:
        """데이터베이스 경로를 Path 객체로 반환하며 상위 디렉터리 존재를 보장합니다."""
        p = Path(self.database_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


# 싱글톤 설정 인스턴스
settings = Settings()
