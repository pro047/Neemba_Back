/* 번역 모니터 — TypeScript 소스.
 *
 * app.js는 이 파일의 tsc 빌드 산출물이다 — app.js를 직접 수정하지 말 것.
 * 수정 후 이 디렉터리에서 `npm run build` (검사만: `npm run check`).
 *
 * 시각은 ISO8601 문자열. 모든 외부 텍스트는 textContent로만 렌더(XSS 방지).
 */

// ====================================================================
// 백엔드 계약 타입
//   GET /api/monitor/sessions?limit=&offset=            -> SessionsResponse
//   GET /api/monitor/sessions/{id}/translations?cursor= -> HistoryResponse
//   GET /api/monitor/translations?lang=&from=&to=&q=    -> SearchResponse
//   WS  /ws/monitor?sessionId=<id>                      -> WsMessage
// ====================================================================

interface SessionItem {
  sessionId: string;
  startedAt: string;
  endedAt: string | null;
  sourceLang: string | null;
  targetLang: string | null;
  translationCount: number;
  /** MAX(created_at) — WU2 추가, STALE 배지용(WU4). 번역 0건이면 null. */
  lastTranslationAt: string | null;
  live: boolean;
}

interface SessionsResponse {
  items: SessionItem[];
  limit: number;
  offset: number;
  nextOffset: number | null;
}

interface TranslationPair {
  id: number;
  segmentId: number | null;
  sequence: number | null;
  sourceText: string | null;
  translatedText: string | null;
  sourceLang: string | null;
  targetLang: string | null;
  confidence: number | null;
  createdAt: string;
}

interface HistoryResponse {
  items: TranslationPair[];
  limit: number;
  nextCursor: number | null; // 정수 id 키셋
}

type SearchItem = TranslationPair & { sessionId: string };

interface SearchResponse {
  items: SearchItem[];
  limit: number;
  nextCursor: string | null; // opaque 문자열
}

interface WsTranslation {
  type: "translation";
  sessionId: string;
  segmentId: number | null;
  sequence: number | null;
  sourceText: string | null;
  translatedText: string | null;
  sourceLang: string | null;
  targetLang: string | null;
  confidence: number | null;
  /** WU2: DB insert 후 broadcast. insert 실패·pool 부재 시 null (안정 shape). */
  id: number | null;
  createdAt: string | null;
}

interface WsSessionClosed {
  type: "session_closed";
  sessionId: string;
  translationCount: number | null;
}

type WsMessage = WsTranslation | WsSessionClosed;

/** pairRow가 렌더에 실제로 쓰는 필드 (이력 행·라이브 메시지 공용). */
interface PairLike {
  sequence: number | null;
  sourceText: string | null;
  translatedText: string | null;
  createdAt: string | null;
}

(() => {
  "use strict";

  const API = "/api/monitor";

  // ---- DOM 헬퍼 (textContent 전용, innerHTML 사용 안 함) ----------------
  function el<K extends keyof HTMLElementTagNameMap>(
    tag: K,
    opts?: { className?: string; text?: unknown }
  ): HTMLElementTagNameMap[K] {
    const node = document.createElement(tag);
    if (opts) {
      if (opts.className) node.className = opts.className;
      if (opts.text != null) node.textContent = String(opts.text);
    }
    return node;
  }

  function clear(node: HTMLElement): void {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function $<T extends HTMLElement = HTMLElement>(id: string): T {
    const node = document.getElementById(id);
    if (!node) throw new Error("missing element: #" + id);
    return node as T;
  }

  function fmtTime(iso: string | null | undefined): string {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso); // 파싱 실패 시 원본 그대로
    const p = (n: number): string => (n < 10 ? "0" : "") + n;
    return (
      `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
      ` ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
    );
  }

  function langPair(src: string | null | undefined, tgt: string | null | undefined): string {
    return `${src || "?"} → ${tgt || "?"}`;
  }

  async function fetchJson<T>(url: string): Promise<T> {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status} — ${url}`);
    }
    return res.json() as Promise<T>;
  }

  // ====================================================================
  // 탭 전환
  // ====================================================================
  function showView(name: "sessions" | "search"): void {
    const isSessions = name === "sessions";
    $("view-sessions").hidden = !isSessions;
    $("view-search").hidden = isSessions;
    $("tab-sessions").classList.toggle("active", isSessions);
    $("tab-search").classList.toggle("active", !isSessions);
  }
  $("tab-sessions").addEventListener("click", () => showView("sessions"));
  $("tab-search").addEventListener("click", () => showView("search"));

  // ====================================================================
  // 세션 목록
  // ====================================================================
  const sessionsState = {
    offset: 0,
    nextOffset: null as number | null,
    loading: false,
    selectedId: null as string | null,
  };

  function sessionRow(s: SessionItem): HTMLLIElement {
    const li = el("li");
    const btn = el("button", { className: "session-item" });
    btn.type = "button";
    btn.dataset.sessionId = s.sessionId;

    const sid = el("div", { className: "sid", text: s.sessionId });
    const badge = el("span", {
      className: "badge " + (s.live ? "live" : "ended"),
      text: s.live ? "LIVE" : "종료",
    });
    sid.appendChild(badge);

    const sub = el("div", { className: "sub" });
    sub.textContent =
      "시작 " + fmtTime(s.startedAt) +
      "  ·  " + langPair(s.sourceLang, s.targetLang) +
      "  ·  " + (s.translationCount != null ? s.translationCount : 0) + "건";

    btn.appendChild(sid);
    btn.appendChild(sub);
    btn.addEventListener("click", () => selectSession(s, btn));
    li.appendChild(btn);
    return li;
  }

  function loadSessions(reset: boolean): void {
    if (sessionsState.loading) return;
    sessionsState.loading = true;
    if (reset) {
      sessionsState.offset = 0;
      clear($("session-list"));
    }
    $("sessions-status").textContent = "불러오는 중…";
    const url = `${API}/sessions?limit=50&offset=${sessionsState.offset}`;
    fetchJson<SessionsResponse>(url)
      .then((data) => {
        const list = $("session-list");
        (data.items ?? []).forEach((s) => {
          list.appendChild(sessionRow(s));
        });
        sessionsState.nextOffset = data.nextOffset != null ? data.nextOffset : null;
        $("sessions-more").hidden = sessionsState.nextOffset == null;
        $("sessions-status").textContent =
          list.children.length + "개 세션" +
          (sessionsState.nextOffset != null ? " (더 있음)" : "");
      })
      .catch((err: Error) => {
        $("sessions-status").textContent = "오류: " + err.message;
      })
      .finally(() => {
        sessionsState.loading = false;
      });
  }

  $("sessions-refresh").addEventListener("click", () => loadSessions(true));
  $("sessions-more").addEventListener("click", () => {
    if (sessionsState.nextOffset != null) {
      sessionsState.offset = sessionsState.nextOffset;
      loadSessions(false);
    }
  });

  // ====================================================================
  // 세션 상세 (이력 / 라이브)
  // ====================================================================
  const detail = {
    session: null as SessionItem | null,
    cursor: null as number | null, // 이력 페이지네이션 (정수 id)
    loading: false,
    ws: null as WebSocket | null, // 라이브 WebSocket
    liveOn: false,
    seenIds: new Set<number>(), // 이력 행 중복 방지
  };

  function closeLive(): void {
    if (detail.ws) {
      try { detail.ws.close(); } catch { /* noop */ }
      detail.ws = null;
    }
    detail.liveOn = false;
  }

  function pairRow(p: PairLike, opts?: { flash?: boolean }): HTMLTableRowElement {
    const tr = el("tr");
    if (opts && opts.flash) tr.className = "new-row";
    const seq = el("td", { className: "col-seq" });
    seq.textContent = p.sequence != null ? String(p.sequence) : "·"; // NULL sequence 허용
    const src = el("td", { className: "col-src", text: p.sourceText || "" });
    const tgt = el("td", { className: "col-tgt", text: p.translatedText || "" });
    const time = el("td", { className: "col-time", text: fmtTime(p.createdAt) });
    tr.appendChild(seq);
    tr.appendChild(src);
    tr.appendChild(tgt);
    tr.appendChild(time);
    return tr;
  }

  function selectSession(s: SessionItem, btnEl: HTMLButtonElement): void {
    detail.session = s;
    detail.cursor = null;
    detail.seenIds = new Set();
    closeLive();

    sessionsState.selectedId = s.sessionId;
    document.querySelectorAll<HTMLButtonElement>(".session-item").forEach((b) => {
      b.classList.toggle("selected", b === btnEl);
    });

    $("detail-title").textContent = s.sessionId;
    $("detail-controls").hidden = false;
    $("detail-meta").textContent =
      langPair(s.sourceLang, s.targetLang) +
      "  ·  시작 " + fmtTime(s.startedAt) +
      (s.endedAt ? "  ·  종료 " + fmtTime(s.endedAt) : "  ·  진행중") +
      "  ·  " + (s.translationCount != null ? s.translationCount : 0) + "건";

    // live(ended_at IS NULL) 세션이면 라이브 토글 노출, 종료 세션이면 이력만.
    const liveBtn = $("live-toggle");
    liveBtn.hidden = !s.live;
    liveBtn.textContent = "라이브 시작";
    $("detail-mode-label").textContent = "이력";
    $("detail-mode-label").className = "mode-label";

    clear($("detail-rows"));
    loadHistory(true);
  }

  function loadHistory(reset: boolean): void {
    if (!detail.session || detail.loading) return;
    detail.loading = true;
    if (reset) {
      detail.cursor = null;
      clear($("detail-rows"));
      detail.seenIds = new Set();
    }
    $("detail-status").textContent = "이력 불러오는 중…";
    let url =
      `${API}/sessions/${encodeURIComponent(detail.session.sessionId)}` +
      "/translations?limit=100";
    if (detail.cursor != null) url += "&cursor=" + encodeURIComponent(detail.cursor);
    fetchJson<HistoryResponse>(url)
      .then((data) => {
        const body = $("detail-rows");
        (data.items ?? []).forEach((p) => {
          if (detail.seenIds.has(p.id)) return;
          detail.seenIds.add(p.id);
          body.appendChild(pairRow(p));
        });
        detail.cursor = data.nextCursor != null ? data.nextCursor : null;
        $("detail-more").hidden = detail.cursor == null || detail.liveOn;
        $("detail-status").textContent =
          body.children.length + "행" +
          (detail.cursor != null ? " (더 있음)" : "");
      })
      .catch((err: Error) => {
        $("detail-status").textContent = "오류: " + err.message;
      })
      .finally(() => {
        detail.loading = false;
      });
  }

  $("detail-more").addEventListener("click", () => {
    if (detail.cursor != null) loadHistory(false);
  });

  // ---- 라이브 (WebSocket) --------------------------------------------
  function startLive(): void {
    if (!detail.session || detail.ws) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url =
      `${proto}//${location.host}` +
      `/ws/monitor?sessionId=${encodeURIComponent(detail.session.sessionId)}`;

    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      $("detail-status").textContent = "라이브 연결 실패: " + (e as Error).message;
      return;
    }
    detail.ws = ws;
    detail.liveOn = true;
    $("detail-more").hidden = true;
    $("live-toggle").textContent = "라이브 중지";
    $("detail-mode-label").textContent = "라이브";
    $("detail-mode-label").className = "mode-label live";
    $("detail-status").textContent = "라이브 연결 중…";

    ws.onopen = () => {
      $("detail-status").textContent = "라이브 수신 중";
    };
    ws.onmessage = (ev: MessageEvent) => {
      let msg: unknown;
      try { msg = JSON.parse(String(ev.data)); } catch { return; }
      if (!msg || typeof msg !== "object") return;
      const m = msg as WsMessage;
      if (m.type === "session_closed") {
        $("detail-status").textContent =
          "세션 종료됨 (" + (m.translationCount != null ? m.translationCount : "?") + "건)";
        stopLive();
        return;
      }
      if (m.type === "translation") {
        const body = $("detail-rows");
        // createdAt이 없는 payload(WU2 이전·insert 실패 폴백)는 수신 시각으로 표기.
        const createdAt = m.createdAt != null ? m.createdAt : new Date().toISOString();
        body.appendChild(pairRow({ ...m, createdAt }, { flash: true }));
        const wrap = body.parentElement;
        if (wrap) wrap.scrollTop = wrap.scrollHeight;
      }
    };
    ws.onerror = () => {
      $("detail-status").textContent = "라이브 오류 (연결 끊김)";
    };
    ws.onclose = () => {
      if (detail.liveOn) {
        $("detail-status").textContent = "라이브 연결 종료됨";
      }
      detail.ws = null;
      detail.liveOn = false;
      $("live-toggle").textContent = "라이브 시작";
      $("detail-mode-label").textContent = "이력";
      $("detail-mode-label").className = "mode-label";
      $("detail-more").hidden = detail.cursor == null;
    };
  }

  function stopLive(): void {
    closeLive();
    $("live-toggle").textContent = "라이브 시작";
    $("detail-mode-label").textContent = "이력";
    $("detail-mode-label").className = "mode-label";
    $("detail-more").hidden = detail.cursor == null;
  }

  $("live-toggle").addEventListener("click", () => {
    if (detail.liveOn || detail.ws) {
      stopLive();
    } else {
      startLive();
    }
  });

  // ====================================================================
  // 검색
  // ====================================================================
  const searchState = {
    cursor: null as string | null,
    loading: false,
    params: "",
  };

  function buildSearchParams(): string {
    const qs: string[] = [];
    const lang = $<HTMLInputElement>("f-lang").value.trim();
    const from = $<HTMLInputElement>("f-from").value.trim();
    const to = $<HTMLInputElement>("f-to").value.trim();
    const q = $<HTMLInputElement>("f-q").value.trim();
    if (lang) qs.push("lang=" + encodeURIComponent(lang));
    if (from) qs.push("from=" + encodeURIComponent(from));
    if (to) qs.push("to=" + encodeURIComponent(to));
    if (q) qs.push("q=" + encodeURIComponent(q));
    return qs.join("&");
  }

  function searchRow(p: SearchItem): HTMLTableRowElement {
    const tr = el("tr");
    const sess = el("td", { className: "col-sess", text: p.sessionId || "" });
    const src = el("td", { className: "col-src", text: p.sourceText || "" });
    const tgt = el("td", { className: "col-tgt", text: p.translatedText || "" });
    const lang = el("td", { className: "col-lang", text: langPair(p.sourceLang, p.targetLang) });
    const time = el("td", { className: "col-time", text: fmtTime(p.createdAt) });
    tr.appendChild(sess);
    tr.appendChild(src);
    tr.appendChild(tgt);
    tr.appendChild(lang);
    tr.appendChild(time);
    return tr;
  }

  function runSearch(reset: boolean): void {
    if (searchState.loading) return;
    searchState.loading = true;
    if (reset) {
      searchState.params = buildSearchParams();
      searchState.cursor = null;
      clear($("search-rows"));
    }
    $("search-status").textContent = "검색 중…";
    let url = `${API}/translations?limit=100`;
    if (searchState.params) url += "&" + searchState.params;
    if (searchState.cursor != null) {
      url += "&cursor=" + encodeURIComponent(searchState.cursor);
    }
    fetchJson<SearchResponse>(url)
      .then((data) => {
        const body = $("search-rows");
        (data.items ?? []).forEach((p) => {
          body.appendChild(searchRow(p));
        });
        searchState.cursor = data.nextCursor != null ? data.nextCursor : null;
        $("search-more").hidden = searchState.cursor == null;
        $("search-status").textContent =
          body.children.length + "행" +
          (searchState.cursor != null ? " (더 있음)" : "");
      })
      .catch((err: Error) => {
        $("search-status").textContent = "오류: " + err.message;
      })
      .finally(() => {
        searchState.loading = false;
      });
  }

  $<HTMLFormElement>("search-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    runSearch(true);
  });
  $("search-more").addEventListener("click", () => {
    if (searchState.cursor != null) runSearch(false);
  });

  // ====================================================================
  // 초기 로드
  // ====================================================================
  loadSessions(true);
})();
