"""API 요청 및 응답 Pydantic 스키마 정의 모듈.

클라이언트로부터 전달받는 질문과 피드백 데이터의 유효성을 엄격히 검증하며,
길이 제한, 공백 제거, 필수 필드 존재 여부를 확인합니다.
"""

from typing import Optional
from pydantic import BaseModel, Field, field_validator


class TestCreateRequest(BaseModel):
    """신규 테스트 질문 등록 요청 모델.

    R6 요구사항에 따라 질문 문자열의 앞뒤 공백을 제거한 후 1~4000자 범위 내에 있는지 검증하며,
    클라이언트가 생성한 멱등 식별자(client_request_id)를 필수로 전달받습니다.
    """

    question: str = Field(
        ...,
        description="테스터가 First-AI-Agent에 질의할 내용 (1~4000자)",
    )
    client_request_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="클라이언트가 발급한 단일 테스트 멱등 식별자 (UUID 등)",
    )

    @field_validator("question")
    @classmethod
    def validate_and_trim_question(cls, v: str) -> str:
        """질문 앞뒤 공백을 제거하고 길이가 1~4000자 사이인지 검증합니다."""
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("질문 내용은 공백만으로 구성될 수 없습니다.")
        if len(trimmed) > 4000:
            raise ValueError(f"질문 길이는 최대 4000자까지 허용됩니다. (현재: {len(trimmed)}자)")
        return trimmed


class TestResponse(BaseModel):
    """테스트 실행 성공 응답 모델.

    First-AI-Agent로부터 전달받은 원본 답변과 측정된 지연 시간, 발급된 test_id를 반환합니다.
    """

    test_id: str = Field(..., description="생성된 테스트 레코드 고유 ID")
    answer: str = Field(..., description="First-AI-Agent가 생성한 원본 응답 본문")
    latency_ms: int = Field(..., description="Agent 응답 소요 시간 (밀리초)")


class FeedbackCreateRequest(BaseModel):
    """원했던 응답 및 피드백 등록 요청 모델.

    R11 요구사항에 따라 1~500자 범위 내의 피드백을 전달받아 유효성을 검증합니다.
    """

    expected_response: str = Field(
        ...,
        description="사용자가 기대했던 올바른 응답 또는 개선 방향 (1~500자)",
    )

    @field_validator("expected_response")
    @classmethod
    def validate_and_trim_feedback(cls, v: str) -> str:
        """피드백 앞뒤 공백을 제거하고 길이가 1~500자 사이인지 검증합니다."""
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("피드백 내용은 공백만으로 구성될 수 없습니다.")
        if len(trimmed) > 500:
            raise ValueError(f"피드백 길이는 최대 500자까지 허용됩니다. (현재: {len(trimmed)}자)")
        return trimmed


class FeedbackResponse(BaseModel):
    """피드백 등록 완료 응답 모델."""

    status: str = Field(default="completed", description="처리 상태")
    message: str = Field(default="피드백이 성공적으로 등록되었습니다.", description="결과 안내 메시지")


class HealthResponse(BaseModel):
    """서비스 헬스체크 응답 모델."""

    status: str = Field(..., description="Feedback Lab 전체 상태 ('ok' 또는 'warning')")
    database: str = Field(..., description="SQLite 데이터베이스 상태 ('ok' 또는 'error')")
    policy: str = Field(..., description="정책 정의 파일 상태 ('ok' 또는 'missing')")
    agent_base_url: str = Field(..., description="설정된 First-AI-Agent 기본 엔드포인트 URL")


class PolicyInfoResponse(BaseModel):
    """화면 안내용 공식 정책 정보 응답 모델.

    dataset_policy.json에서 추출한 공식 매뉴얼 다운로드 링크, A/S 링크, 부서별 연락처를 포함합니다.
    """

    manual_url: str = Field(..., description="공식 제품 매뉴얼 다운로드 URL")
    repair_url: str = Field(..., description="공식 제품 수리 및 A/S 접수 URL")
    contacts: dict[str, str] = Field(..., description="주요 부서별 공식 문의 전화번호 매핑")


class FeedbackItemResponse(BaseModel):
    """피드백 열람용 개별 레코드 응답 모델."""

    id: str = Field(..., description="테스트 레코드 ID")
    test_date: str = Field(..., description="테스트 실행 일자 (Asia/Seoul)")
    created_at: str = Field(..., description="테스트 생성 시각 (ISO 8601)")
    question: str = Field(..., description="사용자 질문 내용")
    agent_response: Optional[str] = Field(default=None, description="AI Agent 응답 내용")
    expected_response: Optional[str] = Field(default=None, description="원했던 응답 및 피드백 내용")
    latency_ms: Optional[int] = Field(default=None, description="소요 시간 (밀리초)")


class FeedbackListResponse(BaseModel):
    """DB 전체 검색과 페이지 이동 정보를 포함하는 피드백 목록 응답 모델."""

    total_count: int = Field(..., description="현재 검색 조건에 맞는 완료 피드백 건수")
    page: int = Field(..., description="현재 페이지 번호 (1부터 시작)")
    page_size: int = Field(..., description="페이지당 피드백 건수")
    total_pages: int = Field(..., description="현재 검색 조건의 전체 페이지 수")
    items: list[FeedbackItemResponse] = Field(default_factory=list, description="피드백 레코드 목록")


class FeedbackStatsResponse(BaseModel):
    """헤더 알림 배지용 피드백 수량 요약 모델."""

    total_count: int = Field(..., description="완료된 총 피드백 건수")
