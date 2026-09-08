/**
 * 피드백 모아보기 화면 제어 스크립트.
 *
 * GET /api/feedbacks에서 데이터를 조회하여 안전한 DOM 렌더링으로 피드백 목록을 표시합니다.
 * 실시간 검색 및 새로고침 기능을 제공합니다.
 */

(function () {
    "use strict";

    let allFeedbacks = [];

    const historyTotalCount = document.getElementById("history-total-count");
    const searchInput = document.getElementById("search-input");
    const btnRefresh = document.getElementById("btn-refresh-history");
    const loadingIndicator = document.getElementById("history-loading");
    const listContainer = document.getElementById("feedback-list-container");
    const emptyStateCard = document.getElementById("empty-state-card");

    /**
     * 마크다운 링크([표시명](http/https))를 안전한 DOM 엘리먼트로 변환하여 부모 노드에 추가합니다.
     */
    function appendSafeContent(container, text) {
        if (!text) return;

        const linkRegex = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
        let lastIndex = 0;
        let match;

        while ((match = linkRegex.exec(text)) !== null) {
            const matchStart = match.index;
            const matchEnd = linkRegex.lastIndex;

            if (matchStart > lastIndex) {
                container.appendChild(document.createTextNode(text.substring(lastIndex, matchStart)));
            }

            const a = document.createElement("a");
            a.href = match[2];
            a.textContent = match[1];
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            container.appendChild(a);

            lastIndex = matchEnd;
        }

        if (lastIndex < text.length) {
            container.appendChild(document.createTextNode(text.substring(lastIndex)));
        }
    }

    /**
     * ISO 8601 타임스탬프를 한국 사용자 친화적인 형식으로 포맷팅합니다.
     */
    function formatDateTime(isoString) {
        if (!isoString) return "";
        try {
            const dt = new Date(isoString);
            return dt.toLocaleString("ko-KR", {
                year: "numeric",
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
            });
        } catch {
            return isoString;
        }
    }

    /**
     * 단일 피드백 항목에 대한 카드 DOM 엘리먼트를 생성합니다.
     */
    function createFeedbackCard(item) {
        const card = document.createElement("article");
        card.className = "card feedback-card";

        // 카드 상단 헤더 (일시, 소요시간)
        const header = document.createElement("div");
        header.className = "feedback-card-header";

        const timeSpan = document.createElement("span");
        timeSpan.className = "feedback-time";
        timeSpan.textContent = `📅 ${formatDateTime(item.created_at)}`;
        header.appendChild(timeSpan);

        if (item.latency_ms !== null && item.latency_ms !== undefined) {
            const latencyBadge = document.createElement("span");
            latencyBadge.className = "badge";
            latencyBadge.textContent = `응답시간: ${Number(item.latency_ms).toLocaleString()} ms`;
            header.appendChild(latencyBadge);
        }
        card.appendChild(header);

        // 1. 질문 섹션
        const questionSec = document.createElement("div");
        questionSec.className = "feedback-card-section question-section";
        const qTitle = document.createElement("div");
        qTitle.className = "section-label";
        qTitle.textContent = "❓ 질문 내용";
        questionSec.appendChild(qTitle);
        const qBody = document.createElement("div");
        qBody.className = "section-body question-body";
        qBody.textContent = item.question;
        questionSec.appendChild(qBody);
        card.appendChild(questionSec);

        // 2. 챗봇 실제 응답 섹션
        const responseSec = document.createElement("div");
        responseSec.className = "feedback-card-section response-section-card";
        const rTitle = document.createElement("div");
        rTitle.className = "section-label";
        rTitle.textContent = "🤖 챗봇 실제 응답";
        responseSec.appendChild(rTitle);
        const rBody = document.createElement("div");
        rBody.className = "section-body answer-body";
        appendSafeContent(rBody, item.agent_response || "(응답 없음)");
        responseSec.appendChild(rBody);
        card.appendChild(responseSec);

        // 3. 사용자 개선 피드백 섹션
        const feedbackSec = document.createElement("div");
        feedbackSec.className = "feedback-card-section expected-section";
        const fTitle = document.createElement("div");
        fTitle.className = "section-label";
        fTitle.textContent = "✍️ 원했던 응답 / 개선 피드백";
        feedbackSec.appendChild(fTitle);
        const fBody = document.createElement("div");
        fBody.className = "section-body feedback-body";
        fBody.textContent = item.expected_response || "(피드백 없음)";
        feedbackSec.appendChild(fBody);
        card.appendChild(feedbackSec);

        return card;
    }

    /**
     * 필터링된 피드백 목록을 화면에 렌더링합니다.
     */
    function renderFeedbacks(items) {
        while (listContainer.firstChild) {
            listContainer.removeChild(listContainer.firstChild);
        }

        if (!items || items.length === 0) {
            emptyStateCard.classList.remove("hidden");
            return;
        }

        emptyStateCard.classList.add("hidden");

        const frag = document.createDocumentFragment();
        items.forEach((item) => {
            frag.appendChild(createFeedbackCard(item));
        });
        listContainer.appendChild(frag);
    }

    /**
     * 검색어 필터링을 수행합니다.
     */
    function filterFeedbacks() {
        const query = (searchInput.value || "").trim().toLowerCase();
        if (!query) {
            renderFeedbacks(allFeedbacks);
            return;
        }

        const filtered = allFeedbacks.filter((item) => {
            const q = (item.question || "").toLowerCase();
            const a = (item.agent_response || "").toLowerCase();
            const e = (item.expected_response || "").toLowerCase();
            return q.includes(query) || a.includes(query) || e.includes(query);
        });

        renderFeedbacks(filtered);
    }

    /**
     * 서버로부터 최신 피드백 목록을 조회합니다.
     */
    async function loadFeedbacks() {
        loadingIndicator.classList.remove("hidden");
        listContainer.classList.add("hidden");
        emptyStateCard.classList.add("hidden");

        try {
            const res = await fetch("/api/feedbacks?limit=200");
            if (!res.ok) throw new Error("피드백 조회 실패");

            const data = await res.json();
            allFeedbacks = data.items || [];
            const total = data.total_count || allFeedbacks.length;

            historyTotalCount.textContent = `${total.toLocaleString()}건`;
            filterFeedbacks();
        } catch (err) {
            historyTotalCount.textContent = "-";
            allFeedbacks = [];
            renderFeedbacks([]);
        } finally {
            loadingIndicator.classList.add("hidden");
            listContainer.classList.remove("hidden");
        }
    }

    // 이벤트 리스너
    btnRefresh.addEventListener("click", loadFeedbacks);
    searchInput.addEventListener("input", filterFeedbacks);

    // 초기 로딩
    loadFeedbacks();
})();
