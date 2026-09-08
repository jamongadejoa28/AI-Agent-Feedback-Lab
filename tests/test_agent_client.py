"""First-AI-Agent 대화 큐와 SSE 어댑터의 HTTP 계약 테스트 모듈.

실제 Agent나 네트워크를 사용하지 않고 메시지 등록, 방문자 쿠키 유지, SSE delta,
completed 전체 원문 채택을 함께 검증합니다. 특히 최종 답변이 화면과 DB에서 잘리는
회귀를 막기 위해 delta보다 긴 completed content를 의도적으로 반환합니다.
"""

import httpx
import pytest

from app.agent_client import AgentClient, AgentContractError


@pytest.mark.asyncio
async def test_stream_query_preserves_cookie_and_completed_answer() -> None:
    """등록 응답 쿠키를 SSE 요청에 전달하고 모든 이벤트를 손실 없이 반환합니다."""
    requests: list[httpx.Request] = []
    complete_answer = "첫 조각과 스트림에 없던 마지막 문장까지 포함한 전체 답변"

    def handler(request: httpx.Request) -> httpx.Response:
        """Agent의 메시지 생성과 SSE 두 엔드포인트를 결정적으로 흉내 냅니다."""
        requests.append(request)
        if request.url.path == "/v1/chat/messages":
            return httpx.Response(
                202,
                json={"message": {"id": "assistant-1", "content": "", "status": "queued"}},
                headers={"set-cookie": "mst_chat_visitor=visitor-1; Path=/; HttpOnly"},
            )
        assert request.url.path == "/v1/chat/messages/assistant-1/events"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'event: queued\ndata: {"position": 1}\n\n'
                'event: started\ndata: {}\n\n'
                'event: delta\ndata: {"content": "첫 조각"}\n\n'
                f'event: completed\ndata: {{"message": {{"content": "{complete_answer}"}}}}\n\n'
            ),
        )

    client = AgentClient(
        base_url="http://agent.test",
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )
    events = [event async for event in client.stream_query("질문", "client-message-1")]

    assert [event["type"] for event in events] == ["queued", "started", "delta", "completed"]
    assert events[-1]["data"]["message"]["content"] == complete_answer
    assert len(requests) == 2
    assert "mst_chat_visitor=visitor-1" in requests[1].headers.get("cookie", "")


@pytest.mark.asyncio
async def test_query_uses_completed_full_content() -> None:
    """호환용 비스트리밍 호출도 delta 조합 대신 completed 전체 원문을 반환합니다."""
    complete_answer = "완료 이벤트가 보장하는 잘리지 않은 답변"

    def handler(request: httpx.Request) -> httpx.Response:
        """대기 없이 완료되는 최소 Agent SSE 응답을 제공합니다."""
        if request.url.path == "/v1/chat/messages":
            return httpx.Response(202, json={"message": {"id": "assistant-2"}})
        return httpx.Response(
            200,
            text=(
                'event: delta\ndata: {"content": "일부"}\n\n'
                f'event: completed\ndata: {{"message": {{"content": "{complete_answer}"}}}}\n\n'
            ),
        )

    client = AgentClient(
        base_url="http://agent.test",
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )
    answer, latency_ms = await client.query("질문")

    assert answer == complete_answer
    assert latency_ms >= 0


@pytest.mark.asyncio
async def test_stream_query_rejects_truncated_sse() -> None:
    """completed 없이 끊긴 SSE를 성공으로 오인하지 않고 계약 오류로 처리합니다."""

    def handler(request: httpx.Request) -> httpx.Response:
        """일부 delta 뒤 연결이 끝나는 손상된 Agent 응답을 재현합니다."""
        if request.url.path == "/v1/chat/messages":
            return httpx.Response(202, json={"message": {"id": "assistant-3"}})
        return httpx.Response(
            200,
            text='event: delta\ndata: {"content": "잘린 답변"}\n\n',
        )

    client = AgentClient(
        base_url="http://agent.test",
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AgentContractError, match="completed 이벤트 없이"):
        _ = [event async for event in client.stream_query("질문", "client-message-3")]
