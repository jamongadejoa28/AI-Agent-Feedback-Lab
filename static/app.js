/**
 * AI-Agent-Feedback-Lab 프론트엔드 제어 스크립트.
 *
 * R12~R15, R18을 구현하여:
 * - 단일 턴 테스트 실행 및 피드백 등록 수명주기를 관리합니다.
 * - Agent의 원본 텍스트를 innerHTML 대입 없이 DOM API로 안전하게 렌더링합니다.
 * - 500자 실시간 카운터 및 신규 멱등 식별자(client_request_id)를 생성합니다.
 */

(function () {
    "use strict";

    // 상태 관리 변수
    let currentTestId = null;
    let currentClientRequestId = generateUUID();
    let isSubmitting = false;

    // DOM 요소 캐싱
    const questionInput = document.getElementById("question-input");
    const btnSend = document.getElementById("btn-send");
    const btnNewTest = document.getElementById("btn-new-test");
    const btnNextTest = document.getElementById("btn-next-test");
    const loadingIndicator = document.getElementById("loading-indicator");
    const errorBox = document.getElementById("error-box");
    const errorMessage = document.getElementById("error-message");
    const responseContainer = document.getElementById("agent-response-container");
    const answerContent = document.getElementById("agent-answer-content");
    const latencyBadge = document.getElementById("latency-badge");
    const feedbackSection = document.getElementById("feedback-section");
    const feedbackInput = document.getElementById("feedback-input");
    const feedbackCounter = document.getElementById("feedback-counter");
    const btnSubmitFeedback = document.getElementById("btn-submit-feedback");
    const feedbackSuccessCard = document.getElementById("feedback-success-card");
    const policyInfoBox = document.getElementById("policy-info-box");
    const feedbackCountBadge = document.getElementById("feedback-count-badge");

    /**
     * UUID4 식별자를 생성합니다. (crypto.randomUUID 지원 여부 확인)
     */
    function generateUUID() {
        if (typeof crypto !== "undefined" && crypto.randomUUID) {
            return crypto.randomUUID();
        }
        // 구형 브라우저 대체 구현
        return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
            const r = (Math.random() * 16) | 0;
            const v = c === "x" ? r : (r & 0x3) | 0x8;
            return v.toString(16);
        });
    }

    /**
     * 오류 메시지를 화면에 표시합니다.
     */
    function showError(msg) {
        errorMessage.textContent = msg;
        errorBox.classList.remove("hidden");
    }

    /**
     * 오류 메시지 박스를 숨깁니다.
     */
    function hideError() {
        errorBox.classList.add("hidden");
        errorMessage.textContent = "";
    }

    /**
     * R15. DOM 기반 안전한 Agent 응답 렌더링.
     *
     * innerHTML을 절대 사용하지 않고, 텍스트 노드 생성과 안전한 http/https 링크 DOM 조립을 통해
     * XSS 공격을 완전히 방지합니다.
     *
     * 패턴: [링크이름](https://...)
     */
    function renderAgentAnswerSafely(container, text) {
        // 기존 컨테이너 비우기
        while (container.firstChild) {
            container.removeChild(container.firstChild);
        }

        if (!text) {
            return;
        }

        // 마크다운 링크 정규표현식: [표시명](http://... 또는 https://...)
        const linkRegex = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
        let lastIndex = 0;
        let match;

        while ((match = linkRegex.exec(text)) !== null) {
            const matchStart = match.index;
            const matchEnd = linkRegex.lastIndex;

            // 매치 이전의 일반 텍스트 노드 추가
            if (matchStart > lastIndex) {
                const plainText = text.substring(lastIndex, matchStart);
                container.appendChild(document.createTextNode(plainText));
            }

            const label = match[1];
            const url = match[2];

            // 안전한 <a> 엘리먼트 동적 생성
            const linkElem = document.createElement("a");
            linkElem.href = url;
            linkElem.textContent = label;
            linkElem.target = "_blank";
            linkElem.rel = "noopener noreferrer";
            container.appendChild(linkElem);

            lastIndex = matchEnd;
        }

        // 마지막 매치 이후 잔여 텍스트 노드 추가
        if (lastIndex < text.length) {
            const remainingText = text.substring(lastIndex);
            container.appendChild(document.createTextNode(remainingText));
        }
    }

    /**
     * R13. 단일 턴 테스트 상태 초기화 (New Test).
     */
    function resetTestContext() {
        currentTestId = null;
        currentClientRequestId = generateUUID();
        isSubmitting = false;

        questionInput.value = "";
        questionInput.disabled = false;
        btnSend.disabled = false;

        feedbackInput.value = "";
        feedbackInput.disabled = false;
        feedbackCounter.textContent = "0 / 500";
        btnSubmitFeedback.disabled = false;

        hideError();
        loadingIndicator.classList.add("hidden");
        responseContainer.classList.add("hidden");
        feedbackSection.classList.remove("hidden");
        feedbackSuccessCard.classList.add("hidden");

        while (answerContent.firstChild) {
            answerContent.removeChild(answerContent.firstChild);
        }

        questionInput.focus();
    }

    /**
     * 질문 전송 핸들러 (POST /api/test)
     */
    async function handleSendQuestion() {
        if (isSubmitting) return;

        const question = questionInput.value.trim();
        if (!question) {
            showError("질문 내용을 입력해 주세요.");
            questionInput.focus();
            return;
        }

        if (question.length > 4000) {
            showError("질문 길이는 최대 4000자까지 입력할 수 있습니다.");
            return;
        }

        hideError();
        isSubmitting = true;
        btnSend.disabled = true;
        questionInput.disabled = true;
        loadingIndicator.classList.remove("hidden");
        responseContainer.classList.add("hidden");

        try {
            const response = await fetch("/api/test", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    question: question,
                    client_request_id: currentClientRequestId,
                }),
            });

            const data = await response.json();

            if (!response.ok) {
                const detail = data.detail || "테스트 요청 처리에 실패했습니다.";
                showError(detail);
                questionInput.disabled = false;
                btnSend.disabled = false;
                return;
            }

            // 성공적으로 Agent 응답을 수신한 경우
            currentTestId = data.test_id;
            latencyBadge.textContent = `소요 시간: ${Number(data.latency_ms).toLocaleString()} ms`;

            // R15 안전 렌더링
            renderAgentAnswerSafely(answerContent, data.answer);

            // 피드백 영역 활성화 및 화면 노출
            responseContainer.classList.remove("hidden");
            feedbackSection.classList.remove("hidden");
            feedbackSuccessCard.classList.add("hidden");
            feedbackInput.focus();

        } catch (err) {
            showError("네트워크 오류가 발생했습니다. 서버 상태를 확인해 주세요.");
            questionInput.disabled = false;
            btnSend.disabled = false;
        } finally {
            isSubmitting = false;
            loadingIndicator.classList.add("hidden");
        }
    }

    /**
     * 피드백 제출 핸들러 (POST /api/test/{test_id}/feedback)
     */
    async function handleSubmitFeedback() {
        if (!currentTestId) {
            showError("등록할 테스트 레코드가 없습니다. 새 테스트를 진행해 주세요.");
            return;
        }

        const expected = feedbackInput.value.trim();
        if (!expected) {
            showError("원했던 응답 또는 개선 방향을 1자 이상 입력해 주세요.");
            feedbackInput.focus();
            return;
        }

        if (expected.length > 500) {
            showError("피드백은 최대 500자까지 입력할 수 있습니다.");
            return;
        }

        hideError();
        btnSubmitFeedback.disabled = true;
        feedbackInput.disabled = true;

        try {
            const response = await fetch(`/api/test/${encodeURIComponent(currentTestId)}/feedback`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    expected_response: expected,
                }),
            });

            const data = await response.json();

            if (!response.ok) {
                const detail = data.detail || "피드백 저장에 실패했습니다.";
                showError(detail);
                btnSubmitFeedback.disabled = false;
                feedbackInput.disabled = false;
                return;
            }

            // 피드백 저장 완료 표시
            feedbackSection.classList.add("hidden");
            feedbackSuccessCard.classList.remove("hidden");

            // 헤더 알림 배지 즉시 최신화
            updateFeedbackCountBadge();

        } catch (err) {
            showError("피드백 전송 중 네트워크 오류가 발생했습니다.");
            btnSubmitFeedback.disabled = false;
            feedbackInput.disabled = false;
        }
    }

    /**
     * R18. First-AI-Agent 공식 정책 정보를 로드하여 화면에 안전하게 배치합니다.
     */
    async function loadPolicyInfo() {
        try {
            const response = await fetch("/api/policy-info");
            if (!response.ok) return;

            const data = await response.json();

            while (policyInfoBox.firstChild) {
                policyInfoBox.removeChild(policyInfoBox.firstChild);
            }

            const title = document.createElement("strong");
            title.textContent = "📌 공식 안내 창구:";
            policyInfoBox.appendChild(title);

            const list = document.createElement("ul");
            list.style.marginTop = "6px";
            list.style.paddingLeft = "20px";

            // 매뉴얼 다운로드 링크
            const liManual = document.createElement("li");
            liManual.appendChild(document.createTextNode("제품 매뉴얼 다운로드: "));
            const aManual = document.createElement("a");
            aManual.href = data.manual_url;
            aManual.textContent = data.manual_url;
            aManual.target = "_blank";
            aManual.rel = "noopener noreferrer";
            liManual.appendChild(aManual);
            list.appendChild(liManual);

            // A/S 접수 링크
            const liRepair = document.createElement("li");
            liRepair.appendChild(document.createTextNode("제품 수리 및 A/S 접수: "));
            const aRepair = document.createElement("a");
            aRepair.href = data.repair_url;
            aRepair.textContent = data.repair_url;
            aRepair.target = "_blank";
            aRepair.rel = "noopener noreferrer";
            liRepair.appendChild(aRepair);
            list.appendChild(liRepair);

            // 문의처 목록
            if (data.contacts) {
                for (const [dept, phone] of Object.entries(data.contacts)) {
                    const liContact = document.createElement("li");
                    liContact.appendChild(document.createTextNode(`${dept}: ${phone}`));
                    list.appendChild(liContact);
                }
            }

            policyInfoBox.appendChild(list);
        } catch (err) {
            // 정책 정보 로딩 실패 시 조용히 유지
            policyInfoBox.textContent = "공식 안내 창구: MST 홈페이지(www.msti.co.kr)를 참고해 주시기 바랍니다.";
        }
    }

    // 이벤트 리스너 등록
    btnSend.addEventListener("click", handleSendQuestion);
    btnNewTest.addEventListener("click", resetTestContext);
    btnNextTest.addEventListener("click", resetTestContext);
    btnSubmitFeedback.addEventListener("click", handleSubmitFeedback);

    // Enter 키 전송 (Ctrl/Cmd + Enter)
    questionInput.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
            e.preventDefault();
            handleSendQuestion();
        }
    });

    // R14. 피드백 글자 수 실시간 카운터
    feedbackInput.addEventListener("input", function () {
        const len = feedbackInput.value.length;
        feedbackCounter.textContent = `${len} / 500`;
        if (len > 500) {
            feedbackCounter.style.color = "var(--error-text)";
        } else {
            feedbackCounter.style.color = "var(--text-secondary)";
        }
    });

    /**
     * 헤더에 표시되는 총 피드백 개수 배지를 최신화합니다.
     */
    async function updateFeedbackCountBadge() {
        if (!feedbackCountBadge) return;
        try {
            const res = await fetch("/api/feedbacks/stats");
            if (!res.ok) return;
            const data = await res.json();
            feedbackCountBadge.textContent = Number(data.total_count || 0).toLocaleString();
        } catch {
            // 네트워크 오류 시 조용히 유지
        }
    }

    // 초기화 실행
    loadPolicyInfo();
    updateFeedbackCountBadge();
    questionInput.focus();
})();
