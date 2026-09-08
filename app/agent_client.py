"""First-AI-Agent HTTP 비동기 통신 및 SSE 스트림 어댑터 모듈.

Feedback Lab은 Agent 내부 코드를 import하지 않고 공개 HTTP API만 사용합니다.
질문은 Agent의 대화형 큐에 등록한 뒤 SSE 이벤트를 구독합니다. 이 경로를 사용하면
다중 접속 시 대기 시간이 일반 요청 제한 시간을 넘더라도 heartbeat로 연결을
유지할 수 있고, 생성 중인 원본 delta를 브라우저까지 그대로 전달할 수 있습니다.
"""

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Optional

import httpx

from app.settings import settings


class AgentClientError(Exception):
    """First-AI-Agent 통신 중 발생하는 기본 예외."""


class AgentConnectionError(AgentClientError):
    """Agent 서버에 연결할 수 없거나 네트워크 장애가 발생했을 때의 예외."""


class AgentTimeoutError(AgentClientError):
    """Agent 연결 또는 요청 본문 전송 제한 시간을 초과했을 때의 예외."""


class AgentHTTPError(AgentClientError):
    """Agent가 기대한 상태 코드 이외의 HTTP 응답을 반환했을 때의 예외."""

    def __init__(self, status_code: int, message: str):
        """상태 코드와 안전하게 제한된 응답 설명을 오류 객체에 보존합니다."""
        super().__init__(f"Agent HTTP 오류 (상태코드: {status_code}): {message}")
        self.status_code = status_code


class AgentContractError(AgentClientError):
    """Agent의 메시지 또는 SSE 응답이 공개 계약을 위반했을 때의 예외."""


class AgentClient:
    """First-AI-Agent의 대화 큐와 SSE 이벤트를 사용하는 비동기 클라이언트."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """Agent 주소와 연결·쓰기 단계 제한 시간을 초기화합니다.

        SSE 읽기는 Agent가 보내는 heartbeat를 따라 완료까지 유지해야 하므로 별도
        read timeout을 두지 않습니다. ``timeout``은 연결, 요청 전송, 커넥션 풀
        획득이 비정상적으로 멈추는 경우에만 적용됩니다.
        """
        self.base_url = (base_url or settings.agent_base_url).rstrip("/")
        self.timeout = timeout or settings.agent_timeout_seconds
        # 테스트에서는 실제 네트워크 없이 Agent HTTP 계약 전체를 검증할 수 있도록
        # MockTransport를 주입합니다. 운영 기본값 None은 httpx 표준 transport입니다.
        self.transport = transport

    def _stream_timeout(self) -> httpx.Timeout:
        """장시간 큐 대기와 생성 스트림을 허용하는 HTTP 제한 시간 설정을 반환합니다."""
        return httpx.Timeout(
            connect=self.timeout,
            read=None,
            write=self.timeout,
            pool=self.timeout,
        )

    @staticmethod
    def _validate_message_payload(data: Any) -> dict[str, Any]:
        """메시지 생성 응답에서 assistant 메시지 객체와 문자열 ID를 검증합니다.

        계약이 다르면 이후 SSE URL을 안전하게 만들 수 없으므로 즉시
        ``AgentContractError``를 발생시킵니다.
        """
        if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
            raise AgentContractError("Agent 메시지 생성 응답에 message 객체가 없습니다.")
        message: dict[str, Any] = data["message"]
        if not isinstance(message.get("id"), str) or not message["id"]:
            raise AgentContractError("Agent 메시지 생성 응답에 문자열 id가 없습니다.")
        return message

    @staticmethod
    def _parse_sse_event(event_type: str, data_lines: list[str]) -> dict[str, Any]:
        """SSE 한 이벤트의 JSON data를 파싱하고 이벤트 종류를 결합합니다.

        잘못된 이벤트를 무시하면 DB가 영구 processing 상태로 남을 수 있으므로
        JSON 객체가 아닌 data는 명시적인 계약 오류로 처리합니다.
        """
        try:
            data: Any = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise AgentContractError("Agent SSE data가 올바른 JSON이 아닙니다.") from exc
        if not isinstance(data, dict):
            raise AgentContractError("Agent SSE data가 JSON 객체가 아닙니다.")
        return {"type": event_type or "message", "data": data}

    async def stream_query(
        self,
        question: str,
        client_message_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """질문을 Agent 큐에 등록하고 완료될 때까지 원본 SSE 이벤트를 반환합니다.

        입력값:
            question: 검증을 마친 단일 턴 질문 원문.
            client_message_id: Agent 큐의 멱등성에 사용할 8~64자 식별자.

        반환값:
            ``queued``, ``started``, ``delta``, ``completed`` 이벤트의 비동기 흐름.
            각 이벤트의 ``data``는 Agent가 제공한 JSON 객체를 그대로 유지합니다.

        예외:
            연결 실패, HTTP 오류, 제한 시간 초과, JSON/SSE 계약 위반을 각각
            ``AgentClientError`` 하위 예외로 변환합니다. Agent의 ``failed`` 이벤트도
            완전한 답변이 아니므로 실패로 처리합니다.
        """
        create_endpoint = f"{self.base_url}/v1/chat/messages"
        payload = {"content": question, "client_message_id": client_message_id}

        try:
            async with httpx.AsyncClient(
                timeout=self._stream_timeout(),
                transport=self.transport,
            ) as client:
                response = await client.post(create_endpoint, json=payload)
                if response.status_code != 202:
                    raise AgentHTTPError(
                        response.status_code,
                        f"메시지 등록 실패 (내용: {response.text[:200]})",
                    )

                message = self._validate_message_payload(response.json())
                message_id = message["id"]
                events_endpoint = f"{self.base_url}/v1/chat/messages/{message_id}/events"
                completed = False

                async with client.stream("GET", events_endpoint) as stream_response:
                    if stream_response.status_code != 200:
                        body = (await stream_response.aread()).decode("utf-8", errors="replace")
                        raise AgentHTTPError(
                            stream_response.status_code,
                            f"SSE 구독 실패 (내용: {body[:200]})",
                        )

                    event_type = ""
                    data_lines: list[str] = []
                    async for line in stream_response.aiter_lines():
                        # 빈 줄은 SSE 이벤트 하나의 끝입니다. heartbeat 주석과 id는
                        # data가 없으므로 전달할 애플리케이션 이벤트를 만들지 않습니다.
                        if line == "":
                            if data_lines:
                                event = self._parse_sse_event(event_type, data_lines)
                                event_name = str(event["type"])
                                if event_name == "failed":
                                    message_text = event["data"].get("message")
                                    raise AgentClientError(
                                        str(message_text or "Agent가 답변 생성을 실패했습니다.")
                                    )
                                yield event
                                if event_name == "completed":
                                    completed = True
                            event_type = ""
                            data_lines = []
                            continue
                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())

                if not completed:
                    raise AgentContractError("Agent SSE 연결이 completed 이벤트 없이 종료되었습니다.")
        except AgentClientError:
            raise
        except httpx.TimeoutException as exc:
            raise AgentTimeoutError(f"Agent 연결 제한 시간 초과 ({self.timeout}초): {exc}") from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise AgentConnectionError(f"Agent 연결 실패: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AgentContractError("Agent 응답 본문을 해석할 수 없습니다.") from exc
        except Exception as exc:
            raise AgentClientError(f"Agent 요청 중 예기치 않은 오류 발생: {exc}") from exc

    async def query(self, question: str) -> tuple[str, int]:
        """SSE를 끝까지 소비해 기존 비스트리밍 API용 ``(answer, latency_ms)``를 반환합니다.

        기존 ``POST /api/test`` 계약은 유지하면서 내부적으로는 Agent 대화 큐를
        사용합니다. 완료 이벤트의 전체 content만 결과로 채택하므로 delta를
        임의로 재조합하면서 원문이 바뀌는 일을 피합니다.
        """
        started_at = time.perf_counter()
        answer: str | None = None
        async for event in self.stream_query(question, str(uuid.uuid4())):
            if event["type"] != "completed":
                continue
            message = event["data"].get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise AgentContractError("Agent completed 이벤트에 문자열 content가 없습니다.")
            answer = message["content"]

        if answer is None:
            raise AgentContractError("Agent가 완성된 답변을 반환하지 않았습니다.")
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        return answer, elapsed_ms


# 실제 HTTP 세션과 Agent 방문자 쿠키는 stream_query 호출마다 분리되어
# 서로 다른 Feedback Lab 테스터의 Agent 대화 상태가 섞이지 않습니다.
agent_client = AgentClient()
