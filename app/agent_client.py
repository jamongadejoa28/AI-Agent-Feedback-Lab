"""First-AI-Agent HTTP 비동기 통신 클라이언트 모듈.

R2, R3, R9에 따라 First-AI-Agent의 /v1/query 엔드포인트를 호출하고,
반환된 JSON에서 'answer' 필드를 엄격하게 검증하여 추출합니다.
Agent 내부 코드를 절대 import하지 않으며 오직 HTTP 프로토콜만을 사용합니다.
"""

import time
from typing import Any, Optional
import httpx

from app.settings import settings


class AgentClientError(Exception):
    """First-AI-Agent 통신 중 발생하는 기본 예외."""


class AgentConnectionError(AgentClientError):
    """Agent 서버에 연결할 수 없거나 네트워크 장애 발생 시 예외."""


class AgentTimeoutError(AgentClientError):
    """Agent 응답 시간 초과 시 예외."""


class AgentHTTPError(AgentClientError):
    """Agent가 2xx 이외의 HTTP 상태 코드를 반환했을 때의 예외."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"Agent HTTP 오류 (상태코드: {status_code}): {message}")
        self.status_code = status_code


class AgentContractError(AgentClientError):
    """Agent 응답이 JSON이 아니거나 필수 필드 'answer'가 누락/비문자열일 때의 계약 위반 예외."""


class AgentClient:
    """First-AI-Agent와 HTTP로 통신하는 비동기 클라이언트."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        """기본 엔드포인트 URL 및 타임아웃을 초기화합니다."""
        self.base_url = (base_url or settings.agent_base_url).rstrip("/")
        self.timeout = timeout or settings.agent_timeout_seconds

    async def query(self, question: str) -> tuple[str, int]:
        """First-AI-Agent의 POST /v1/query를 호출하고 (answer, latency_ms)를 반환합니다.

        R3 검증 절차:
        1. HTTP 요청 성공 (상태코드 200)
        2. 응답 본문이 유효한 JSON
        3. JSON 루트가 객체(dict)
        4. 'answer' 키 존재
        5. 'answer' 값이 문자열(str)

        예외:
            AgentConnectionError: 연결 실패 또는 DNS 오류
            AgentTimeoutError: 응답 지연 시간 초과
            AgentHTTPError: 비정상 상태 코드 반환
            AgentContractError: 응답 JSON 계약 위반
        """
        endpoint = f"{self.base_url}/v1/query"
        payload = {"query": question}

        start_time = time.perf_counter()

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(endpoint, json=payload)
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            raise AgentConnectionError(f"Agent 연결 실패: {exc}") from exc
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException) as exc:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            raise AgentTimeoutError(f"Agent 응답 시간 초과 ({self.timeout}초): {exc}") from exc
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            raise AgentClientError(f"Agent 요청 중 예기치 않은 오류 발생: {exc}") from exc

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)

        # 1. HTTP 상태 코드 검증
        if response.status_code != 200:
            raise AgentHTTPError(
                response.status_code,
                f"예상치 못한 응답 상태입니다 (내용: {response.text[:200]})",
            )

        # 2. JSON 파싱 검증
        try:
            data: Any = response.json()
        except Exception as exc:
            raise AgentContractError("Agent 응답 본문이 올바른 JSON 형식이 아닙니다.") from exc

        # 3. 루트 타입 검증 (dict)
        if not isinstance(data, dict):
            raise AgentContractError("Agent 응답 루트가 JSON 객체(dict) 형태가 아닙니다.")

        # 4. 'answer' 필드 존재 여부 검증
        if "answer" not in data:
            raise AgentContractError("Agent 응답 JSON에 필수 필드 'answer'가 존재하지 않습니다.")

        answer = data["answer"]

        # 5. 'answer' 값의 타입 검증 (str)
        if not isinstance(answer, str):
            raise AgentContractError(
                f"Agent 응답의 'answer' 필드가 문자열이 아닙니다 (실제 타입: {type(answer).__name__})."
            )

        return answer, elapsed_ms


# 싱글톤 에이전트 클라이언트 인스턴스
agent_client = AgentClient()
