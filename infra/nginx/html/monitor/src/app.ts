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
//   GET /api/monitor/status                             -> StatusResponse
//   GET /api/monitor/ws-blips?limit=&offset=            -> WsBlipsResponse
//   WS  /ws/monitor?sessionId=<id>                      -> WsMessage
//   WS  /ws/monitor/events                              -> EventsMessage
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

/** WU1 전역 이벤트 채널. read-only, 백로그 없음(끊긴 동안 이벤트 유실). */
interface EventSessionStarted {
  type: "session_started";
  sessionId: string;
  sourceLang: string | null;
  targetLang: string | null;
  startedAt: string;
}

interface EventSessionEnded {
  type: "session_ended";
  sessionId: string;
  translationCount: number | null;
  endedAt: string;
}

type EventsMessage = EventSessionStarted | EventSessionEnded;

/** WU5: 시스템 상태 개요. 현재값 표시 전용 — 경보 판정은 사이드카 책임. */
interface NodeGauges {
  /** node /metrics 에 해당 게이지가 없으면 null. */
  sttPaused: boolean | null;
  rtmpAuthEnabled: boolean | null;
  publishBufferSize: number | null;
}

interface StatusResponse {
  /** null = DB 조회 실패로 이 필드만 열화 (나머지 필드는 유효). */
  activeSessions: number | null;
  /** 자막 기기(/ws) 소켓이 지금 붙어 있는지. */
  wsClientConnected: boolean;
  natsConnected: boolean;
  /** null = python 프로세스에서 브로드캐스트 이력 없음 (기동 직후 등). */
  lastBroadcastAgoSec: number | null;
  /** node /metrics 수집 실패 시 false + node:null. */
  nodeUp: boolean;
  node: NodeGauges | null;
}

/** §4-7 순단 계측 — reconnectedAt null = 미복귀. */
interface WsBlip {
  id: number;
  sessionId: string;
  disconnectedAt: string;
  reconnectedAt: string | null;
  durationMs: number | null;
  flushedCount: number | null;
  lostCount: number | null;
  closeCode: number | null;
  closeReason: string | null;
  detectedBy: string;
}

interface WsBlipsResponse {
  items: WsBlip[];
  limit: number;
  offset: number;
  nextOffset: number | null;
}

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

  /* 캐시 스큐 방어: 브라우저가 옛 index.html 을 캐시한 채 새 app.js 를 받으면
   * 새 릴리스가 추가한 요소가 없다. 전체가 IIFE 한 덩어리라 $() 가 최상위에서
   * 던지면 그 아래 배선이 통째로 안 일어나 페이지가 죽는다 — 목록도 상태 칩도 빈다.
   * 배선만 건너뛰어 "죽은 페이지" 를 "죽은 버튼 하나" 로 줄인다. */
  function $opt<T extends HTMLElement = HTMLElement>(id: string): T | null {
    return document.getElementById(id) as T | null;
  }

  /** 요소가 없으면 경고만 남기고 건너뛴다 (최상위 배선 전용 — 렌더 경로는 $() 유지). */
  function wire<K extends keyof HTMLElementEventMap>(
    id: string,
    type: K,
    fn: (ev: HTMLElementEventMap[K]) => void
  ): void {
    const node = $opt(id);
    if (!node) {
      console.warn(`monitor: missing #${id} — skip ${type}`);
      return;
    }
    node.addEventListener(type, fn);
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

  function fmtAgoSec(sec: number): string {
    const s = Math.max(0, Math.round(sec));
    if (s < 60) return s + "초 전";
    if (s < 3600) return Math.floor(s / 60) + "분 전";
    return Math.floor(s / 3600) + "시간 전";
  }

  async function fetchJson<T>(url: string): Promise<T> {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status} — ${url}`);
    }
    return res.json() as Promise<T>;
  }

  // ---- 자동 재연결 WebSocket 헬퍼 --------------------------------------
  const RECONNECT_BASE_MS = 1_000;
  const RECONNECT_MAX_MS = 30_000;
  /** D4-b: live인데 마지막 번역 후 180s 무활동이면 STALE (사이드카 GAP 기준과 정렬). */
  const STALE_THRESHOLD_MS = 180_000;
  const STALE_TICK_MS = 10_000;

  function wsUrl(path: string): string {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${path}`;
  }

  interface ReconnectingWs {
    /** 재연결 타이머까지 취소하고 완전히 닫는다 (이후 재시도 없음). */
    close(): void;
  }

  /** 지수 백오프(1s→2s→4s…최대 30s, open 성공 시 리셋) 재연결 WebSocket. */
  function connectReconnecting(opts: {
    url: () => string;
    /** resumed=true면 끊김(또는 실패한 시도) 후의 회복 open. */
    onOpen: (resumed: boolean) => void;
    /** JSON 파싱까지 끝난 메시지. 파싱 불가 프레임은 무시된다. */
    onMessage: (msg: unknown) => void;
    onDown: (retryDelayMs: number) => void;
  }): ReconnectingWs {
    let ws: WebSocket | null = null;
    let timer: number | null = null;
    let attempt = 0;
    let resumed = false;
    let closed = false;

    function scheduleRetry(): void {
      const delayMs = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
      attempt += 1;
      resumed = true;
      // 재예약을 먼저 건다. onDown 은 상태 문구를 쓰려고 $() 를 부르는데, 캐시
      // 스큐로 그 요소가 없으면 던진다 — 예전 순서에서는 그 예외가 setTimeout
      // 앞에서 터져 재연결이 영영 예약되지 않았다(라이브가 통째로 죽는다).
      // 이제 던져도 잃는 것은 문구 한 줄뿐이고, 예외는 콘솔에 남아 진단된다.
      timer = window.setTimeout(connect, delayMs);
      opts.onDown(delayMs);
    }

    function connect(): void {
      if (closed) return;
      timer = null;
      let sock: WebSocket;
      try {
        sock = new WebSocket(opts.url());
      } catch {
        scheduleRetry();
        return;
      }
      ws = sock;
      sock.onopen = () => {
        if (closed) return;
        attempt = 0; // 백오프 리셋
        const wasResumed = resumed;
        resumed = true; // 다음 open부터는 항상 회복으로 취급
        opts.onOpen(wasResumed);
      };
      sock.onmessage = (ev: MessageEvent) => {
        if (closed) return;
        let msg: unknown;
        try { msg = JSON.parse(String(ev.data)); } catch { return; }
        opts.onMessage(msg);
      };
      sock.onerror = () => { /* onclose가 뒤따르므로 여기선 무시 */ };
      sock.onclose = () => {
        ws = null;
        if (closed) return;
        scheduleRetry();
      };
    }

    connect();
    return {
      close(): void {
        closed = true;
        if (timer != null) {
          window.clearTimeout(timer);
          timer = null;
        }
        if (ws) {
          try { ws.close(); } catch { /* noop */ }
          ws = null;
        }
      },
    };
  }

  // ====================================================================
  // 탭 전환
  // ====================================================================
  const VIEW_NAMES = ["sessions", "search", "blips"] as const;
  type ViewName = (typeof VIEW_NAMES)[number];

  function showView(name: ViewName): void {
    VIEW_NAMES.forEach((v) => {
      $("view-" + v).hidden = v !== name;
      $("tab-" + v).classList.toggle("active", v === name);
    });
  }
  wire("tab-sessions", "click", () => showView("sessions"));
  wire("tab-search", "click", () => showView("search"));
  wire("tab-blips", "click", () => {
    // 결정 3: 별도 탭, 진입 시 로드 (상시 폴링 없음).
    showView("blips");
    loadBlips(true);
  });

  // ====================================================================
  // 세션 목록
  // ====================================================================
  interface SessionRowEntry {
    li: HTMLLIElement;
    btn: HTMLButtonElement;
    /** 이벤트·재조회 시 이 객체를 제자리 병합(mutate)해 참조 공유를 유지한다. */
    session: SessionItem;
  }

  const sessionsState = {
    offset: 0,
    nextOffset: null as number | null,
    loading: false,
    reloadQueued: false, // 로딩 중 들어온 복구 재조회를 잃지 않고 이어서 실행
    selectedId: null as string | null,
    rows: new Map<string, SessionRowEntry>(), // sessionId → 행 (이벤트로 개별 갱신)
  };

  function isStale(s: SessionItem): boolean {
    if (!s.live) return false;
    // 번역이 한 건도 없는 세션(lastTranslationAt=null)이야말로 STALE 로 잡아야 할
    // 대상이다 — "송출은 켰는데 번역이 안 나온다". 마지막 번역이 없으면 시작
    // 시각을 기준으로 재서 영영 초록 LIVE 로 남지 않게 한다.
    const ref = s.lastTranslationAt ?? s.startedAt;
    const t = new Date(ref).getTime();
    if (isNaN(t)) return false;
    return Date.now() - t > STALE_THRESHOLD_MS;
  }

  function sessionBadge(s: SessionItem): HTMLSpanElement {
    if (s.live && isStale(s)) return el("span", { className: "badge stale", text: "STALE" });
    if (s.live) return el("span", { className: "badge live", text: "LIVE" });
    return el("span", { className: "badge ended", text: "종료" });
  }

  function renderSessionRow(entry: SessionRowEntry): void {
    const s = entry.session;
    clear(entry.btn);

    const sid = el("div", { className: "sid", text: s.sessionId });
    sid.appendChild(sessionBadge(s));

    const sub = el("div", { className: "sub" });
    sub.textContent =
      "시작 " + fmtTime(s.startedAt) +
      "  ·  " + langPair(s.sourceLang, s.targetLang) +
      "  ·  " + (s.translationCount != null ? s.translationCount : 0) + "건";

    entry.btn.appendChild(sid);
    entry.btn.appendChild(sub);
    entry.btn.classList.toggle("selected", s.sessionId === sessionsState.selectedId);
  }

  function makeSessionRow(s: SessionItem): SessionRowEntry {
    const li = el("li");
    const btn = el("button", { className: "session-item" });
    btn.type = "button";
    btn.dataset.sessionId = s.sessionId;
    li.appendChild(btn);
    const entry: SessionRowEntry = { li, btn, session: s };
    // entry.session은 제자리 병합되므로 클릭 시점의 최신 데이터로 선택된다.
    btn.addEventListener("click", () => selectSession(entry.session, btn));
    renderSessionRow(entry);
    return entry;
  }

  /** 선택 중 세션과 같은 id면 기존 객체에 병합 — 목록 행·상세 뷰가 같은 객체를 보게 유지. */
  function adoptSession(s: SessionItem): SessionItem {
    if (detail.session && detail.session.sessionId === s.sessionId) {
      Object.assign(detail.session, s);
      return detail.session;
    }
    return s;
  }

  function renderSessionsCount(): void {
    $("sessions-status").textContent =
      $("session-list").children.length + "개 세션" +
      (sessionsState.nextOffset != null ? " (더 있음)" : "");
  }

  function loadSessions(reset: boolean): void {
    if (sessionsState.loading) {
      // 재조회(reset)는 놓친 이벤트 복구용이라 버리면 안 됨 — 끝나고 이어서 실행.
      if (reset) sessionsState.reloadQueued = true;
      return;
    }
    if (reset) {
      sessionsState.offset = 0;
      sessionsState.rows.clear();
      clear($("session-list"));
    }
    $("sessions-status").textContent = "불러오는 중…";
    // 플래그는 동기 DOM 작업을 넘긴 뒤에 세운다. 위쪽 $() 가 던지면(캐시 스큐)
    // 해제하는 .finally 에 영영 닿지 못해 loading 이 true 로 굳고, refreshSessions·
    // 새로고침·WS 복구가 전부 조용히 no-op 이 된다. 여기까지는 콜백이 끼어들
    // 여지가 없는 동기 구간이라 가드와 세팅 사이가 벌어져도 재진입 위험은 없다.
    sessionsState.loading = true;
    const url = `${API}/sessions?limit=50&offset=${sessionsState.offset}`;
    fetchJson<SessionsResponse>(url)
      .then((data) => {
        const list = $("session-list");
        (data.items ?? []).forEach((raw) => {
          const existing = sessionsState.rows.get(raw.sessionId);
          if (existing) {
            // 이벤트로 먼저 삽입된 행 — 자리는 유지하고 데이터만 갱신 (offset 중복 방지).
            Object.assign(existing.session, raw);
            renderSessionRow(existing);
            return;
          }
          const entry = makeSessionRow(adoptSession(raw));
          sessionsState.rows.set(raw.sessionId, entry);
          list.appendChild(entry.li);
        });
        sessionsState.nextOffset = data.nextOffset != null ? data.nextOffset : null;
        $("sessions-more").hidden = sessionsState.nextOffset == null;
        renderSessionsCount();
        // 재조회로 선택 세션이 종료로 판명되면(놓친 session_ended) 라이브 중단.
        if (detail.session && !detail.session.live && (detail.liveOn || detail.live)) {
          $("detail-status").textContent =
            "세션 종료됨 (" +
            (detail.session.translationCount != null
              ? detail.session.translationCount
              : "?") +
            "건)";
          stopLive();
        }
        renderDetailHeader(); // 병합된 최신 데이터로 상세 헤더 동기화
      })
      .catch((err: Error) => {
        $("sessions-status").textContent = "오류: " + err.message;
      })
      .finally(() => {
        sessionsState.loading = false;
        if (sessionsState.reloadQueued) {
          sessionsState.reloadQueued = false;
          loadSessions(true);
        }
      });
  }

  /* 통합 리뷰(2026-08-02): WU5 "결정 3 — 목록 폴링 없음"을 뒤집는다.
   *
   * 미선택 라이브 행은 lastTranslationAt 을 갱신할 경로가 전혀 없다 —
   * 라이브 WS 는 선택한 세션에만 붙고 전역 이벤트 채널은 start/end 만 나른다.
   * 그래서 STALE 판정이 마지막 REST 조회 시점 데이터로 굳어, 번역이 정상으로
   * 흐르는 세션도 배지가 STALE 로 고착됐다. 주기 병합 갱신이 유일한 해법이다.
   *
   * loadSessions(true) 를 쓰지 않는 이유: 그쪽은 행 Map 과 DOM 을 통째로 비우고
   * 다시 쌓아 스크롤·포커스가 튄다. 여기서는 제자리 병합만 한다. */
  const SESSIONS_POLL_MS = 30_000;

  /* sessionsState.loading 을 공유하지 않는 이유: 그 플래그는 loadSessions 의 것이고
   * finally 에서 reloadQueued 이어달리기를 발동시킨다. 여기서 세우면 폴링이 뜬 사이
   * 누른 "더 보기"(loadSessions(false))가 조기 리턴하는데 reset=false 라 큐에도 안
   * 걸려 클릭이 조용히 사라진다. 읽기는 양쪽 다, 쓰기는 각자 자기 것만. */
  let refreshInFlight = false;

  function refreshSessions(): void {
    // 진행 중인 조회가 곧 최신값을 준다 — 겹쳐 쏘지 않는다.
    if (sessionsState.loading || refreshInFlight) return;
    refreshInFlight = true;
    fetchJson<SessionsResponse>(`${API}/sessions?limit=50&offset=0`)
      .then((data) => {
        const list = $("session-list");
        (data.items ?? []).forEach((raw) => {
          const existing = sessionsState.rows.get(raw.sessionId);
          if (existing) {
            Object.assign(existing.session, raw);
            renderSessionRow(existing);
            return;
          }
          // 이벤트를 놓친 새 세션 — 목록 맨 위가 최신순 정렬과 맞는다.
          const entry = makeSessionRow(adoptSession(raw));
          sessionsState.rows.set(raw.sessionId, entry);
          list.insertBefore(entry.li, list.firstChild);
        });
        renderSessionsCount();
        // 놓친 session_ended 를 여기서 만나면 라이브 재연결 루프도 끊어야 한다
        // (loadSessions 의 복구 경로와 같은 이유 — 끊긴 사이 종료된 세션).
        if (detail.session && !detail.session.live && (detail.liveOn || detail.live)) {
          $("detail-status").textContent =
            "세션 종료됨 (" +
            (detail.session.translationCount != null
              ? detail.session.translationCount
              : "?") +
            "건)";
          stopLive();
        }
        renderDetailHeader();
      })
      .catch(() => {
        // 폴링 실패는 조용히 넘긴다 — 다음 틱에 재시도하고, 실패를 화면에
        // 쓰면 사용자가 누른 조회 결과 문구를 덮어쓴다.
      })
      .finally(() => {
        refreshInFlight = false;
      });
  }
  window.setInterval(refreshSessions, SESSIONS_POLL_MS);

  wire("sessions-refresh", "click", () => loadSessions(true));
  wire("sessions-more", "click", () => {
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
    live: null as ReconnectingWs | null, // 라이브 WebSocket (자동 재연결)
    liveOn: false,
    seenIds: new Set<number>(), // 이력·라이브 행 중복 방지 (id 기반)
    lastId: null as number | null, // 마지막 수신 id — gap fill 기준
    filling: false, // gap fill 진행 중이면 라이브 수신은 버퍼링
    refillQueued: false, // fill 도중 재연결 → 종료 후 한 번 더
    fillBuffer: [] as WsTranslation[],
    // 세션 전환·라이브 중지마다 증가 — 같은 세션을 빠르게 재선택해도
    // 이전 fill 루프의 늦은 fetch가 새 뷰에 끼어들지 못하게 무효화한다.
    epoch: 0,
    // WU5 결정 4: 분당 건수는 프런트 파생 — 수신 행 createdAt(epoch ms)만 모아
    // 렌더 시 최근 60초로 걸러 센다. 라이브·gap fill 수신 공용, 세션 전환 시 리셋.
    recentTimes: [] as number[],
    // V1: 바닥에서 벗어난 동안 도착한 행 수. "새 번역 N건" 버튼에 표시한다.
    pendingNew: 0,
  };

  // ---- 스크롤 추종 (V3-A) ----------------------------------------------
  // 바닥 판정 여유값. 0 비교는 소수점 스크롤·브라우저 확대 배율에서 깨지고,
  // 행 높이(약 34px)보다 크면 "한 행 밀렸는데 바닥"으로 오판한다.
  const BOTTOM_THRESHOLD_PX = 32;

  /**
   * 상세 목록의 실제 스크롤 컨테이너.
   * #detail-rows 는 <tbody> 이고 그 부모는 <table> 이다. overflow 는 조부모인
   * .table-wrap 에만 걸려 있어, 한 단계만 올라가면 스크롤 불가 요소를 잡게 된다.
   * 그런 요소에 scrollTop 을 대입하면 예외 없이 0 으로 클램프되어 조용히 버려진다.
   */
  function rowsWrap(): HTMLElement | null {
    return $opt("detail-rows")?.closest<HTMLElement>(".table-wrap") ?? null;
  }

  function isAtBottom(w: HTMLElement): boolean {
    return w.scrollHeight - w.scrollTop - w.clientHeight <= BOTTOM_THRESHOLD_PX;
  }

  // 바닥 이동을 예약해 두고 아직 반영하지 않은 상태. 예약과 실행 사이에 낀
  // 코드가 옛 scrollTop 을 읽고 "바닥 아님" 으로 오판하는 것을 막는다.
  let bottomScrollQueued = false;

  /** 행 추가 직후 scrollHeight 는 레이아웃 확정 전 값일 수 있어 다음 프레임에 민다. */
  function scrollToBottom(): void {
    const w = rowsWrap();
    if (!w || bottomScrollQueued) return; // 한 프레임에 한 번이면 충분
    bottomScrollQueued = true;
    requestAnimationFrame(() => {
      bottomScrollQueued = false;
      w.scrollTop = w.scrollHeight;
    });
  }

  /** 행을 붙이기 "전에" 호출할 것 — 붙인 뒤엔 항상 바닥이 아니게 된다. */
  function stickToBottom(): boolean {
    if (bottomScrollQueued) return true; // 곧 바닥으로 갈 예정 = 바닥으로 친다
    const w = rowsWrap();
    return !w || isAtBottom(w); // 컨테이너를 못 찾으면 카운터를 늘리지 않는다
  }

  function setPendingNew(n: number): void {
    detail.pendingNew = n;
    // selectSession 이 맨 먼저 부르는 경로다 — #detail-new 가 없는 캐시 스큐에서
    // 여기서 던지면 배선을 건너뛴 보람 없이 세션 선택 자체가 죽는다.
    const btn = $opt("detail-new");
    if (!btn) return;
    btn.hidden = n <= 0;
    btn.textContent = "↓ 새 번역 " + n + "건";
  }

  function closeLive(): void {
    if (detail.live) {
      detail.live.close();
      detail.live = null;
    }
    detail.liveOn = false;
    detail.filling = false;
    detail.refillQueued = false;
    detail.fillBuffer = [];
    detail.epoch += 1;
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

  /** 상세 헤더(제목+STALE 배지·메타·라이브 토글 노출)를 detail.session 기준으로 재렌더. */
  function renderDetailHeader(): void {
    const s = detail.session;
    if (!s) return;
    const title = $("detail-title");
    title.textContent = s.sessionId;
    if (s.live && isStale(s)) {
      title.appendChild(el("span", { className: "badge stale", text: "STALE" }));
    }
    // WU5 미니 통계: 분당 건수(최근 60s 수신 행)·마지막 수신 경과 — 진행중 세션만,
    // 종료 세션은 "—" (결정 4). 10s STALE 타이머가 재렌더해 주기 갱신된다.
    let stats: string;
    if (s.live) {
      const now = Date.now();
      detail.recentTimes = detail.recentTimes.filter((t) => now - t <= 60_000);
      stats = "  ·  분당 " + detail.recentTimes.length + "건";
      const last = s.lastTranslationAt ? new Date(s.lastTranslationAt).getTime() : NaN;
      stats += "  ·  마지막 수신 " + (isNaN(last) ? "없음" : fmtAgoSec((now - last) / 1000));
    } else {
      stats = "  ·  분당 —";
    }
    $("detail-meta").textContent =
      langPair(s.sourceLang, s.targetLang) +
      "  ·  시작 " + fmtTime(s.startedAt) +
      (s.endedAt ? "  ·  종료 " + fmtTime(s.endedAt) : "  ·  진행중") +
      "  ·  " + (s.translationCount != null ? s.translationCount : 0) + "건" +
      stats;
    // live(ended_at IS NULL) 세션이면 라이브 토글 노출. 수신 중(liveOn)엔 중지용으로 유지.
    $("live-toggle").hidden = !s.live && !detail.liveOn;
  }

  function selectSession(s: SessionItem, btnEl: HTMLButtonElement): void {
    closeLive(); // 이전 세션의 라이브·재연결 중단
    detail.session = s;
    detail.cursor = null;
    detail.seenIds = new Set();
    detail.lastId = null;
    detail.recentTimes = [];
    setPendingNew(0); // 세션 전환 — 이전 뷰의 미확인 건수를 물려주지 않는다

    sessionsState.selectedId = s.sessionId;
    document.querySelectorAll<HTMLButtonElement>(".session-item").forEach((b) => {
      b.classList.toggle("selected", b === btnEl);
    });

    $("detail-controls").hidden = false;
    $("live-toggle").textContent = "라이브 시작";
    $("detail-mode-label").textContent = "이력";
    $("detail-mode-label").className = "mode-label";
    renderDetailHeader();

    clear($("detail-rows"));
    if (s.live) {
      startLive(); // D5: live 세션은 자동 구독 — 이력도 syncLive가 처음부터 채운다
    } else {
      loadHistory(true);
    }
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
          if (detail.lastId == null || p.id > detail.lastId) detail.lastId = p.id;
          body.appendChild(pairRow(p));
        });
        detail.cursor = data.nextCursor != null ? data.nextCursor : null;
        $("detail-more").hidden = detail.cursor == null || detail.liveOn;
        $("detail-status").textContent =
          body.children.length + "행" +
          (detail.cursor != null ? " (더 있음)" : "");
        // V2: 상세 진입 시 맨 아래로. "더 보기"(reset=false)는 사용자가 읽던
        // 위치를 유지해야 하므로 최초 로드에서만 민다.
        if (reset) scrollToBottom();
      })
      .catch((err: Error) => {
        $("detail-status").textContent = "오류: " + err.message;
      })
      .finally(() => {
        detail.loading = false;
      });
  }

  wire("detail-more", "click", () => {
    if (detail.cursor != null) loadHistory(false);
  });

  function handleNewRowsClick(): void {
    setPendingNew(0);
    scrollToBottom();
  }
  wire("detail-new", "click", handleNewRowsClick);

  // 사용자가 직접 바닥까지 내려오면 버튼을 거둔다. 프로그램적 스크롤이 이 핸들러를
  // 깨워도 결과가 같으므로(어차피 바닥) "내가 스크롤했음" 플래그가 필요 없다.
  function handleRowsScroll(): void {
    if (detail.pendingNew === 0) return; // scroll 은 초당 수십 번 — 싼 검사부터
    const w = rowsWrap();
    if (w && isAtBottom(w)) setPendingNew(0);
  }
  const rowsScrollTarget = rowsWrap();
  if (rowsScrollTarget) rowsScrollTarget.addEventListener("scroll", handleRowsScroll);

  // ---- 라이브 (WebSocket, 자동 재연결 + gap fill) -----------------------
  function startLive(): void {
    if (!detail.session || detail.live) return;
    const sess = detail.session;
    detail.liveOn = true;
    $("detail-more").hidden = true;
    $("live-toggle").textContent = "라이브 중지";
    $("detail-mode-label").textContent = "라이브";
    $("detail-mode-label").className = "mode-label live";
    $("detail-status").textContent = "라이브 연결 중…";

    detail.live = connectReconnecting({
      url: () => wsUrl(`/ws/monitor?sessionId=${encodeURIComponent(sess.sessionId)}`),
      onOpen: () => {
        if (detail.session !== sess || !detail.liveOn) return;
        $("detail-status").textContent = "라이브 수신 중";
        void syncLive(); // 최초 연결·재연결 공통 — lastId 이후 이력으로 gap fill
      },
      onMessage: (msg) => handleLiveMessage(sess, msg),
      onDown: (retryDelayMs) => {
        if (detail.session !== sess || !detail.liveOn) return;
        $("detail-status").textContent =
          "라이브 연결 끊김 — " + Math.round(retryDelayMs / 1000) + "초 후 재연결…";
      },
    });
  }

  function handleLiveMessage(sess: SessionItem, msg: unknown): void {
    if (detail.session !== sess || !detail.liveOn) return;
    if (!msg || typeof msg !== "object") return;
    const m = msg as WsMessage;
    if (m.type === "session_closed") {
      $("detail-status").textContent =
        "세션 종료됨 (" + (m.translationCount != null ? m.translationCount : "?") + "건)";
      stopLive(); // 목록 배지·건수는 전역 session_ended 이벤트가 갱신
      return;
    }
    if (m.type === "translation") {
      if (detail.filling) {
        detail.fillBuffer.push(m); // 순서 꼬임 방지 — fill 완료 후 flush
        return;
      }
      appendLiveRow(m);
    }
  }

  function appendLiveRow(m: WsTranslation): void {
    if (m.id != null) {
      if (detail.seenIds.has(m.id)) return; // 이력·gap fill과 id 기반 dedup
      detail.seenIds.add(m.id);
      if (detail.lastId == null || m.id > detail.lastId) detail.lastId = m.id;
    }
    // id:null(insert 실패 폴백)은 dedup 불가 — 그대로 append (D3 수용 범위).
    // createdAt이 없는 payload는 수신 시각으로 표기.
    const createdAt = m.createdAt != null ? m.createdAt : new Date().toISOString();
    const createdMs = new Date(createdAt).getTime();
    if (!isNaN(createdMs)) detail.recentTimes.push(createdMs); // 분당 건수(WU5)
    const body = $("detail-rows");
    const stick = stickToBottom(); // V1: 붙이기 전에 측정
    body.appendChild(pairRow({ ...m, createdAt }, { flash: true }));
    if (stick) scrollToBottom();
    else setPendingNew(detail.pendingNew + 1);
    if (detail.session) {
      // 결정 3: 선택 세션은 라이브 수신으로 lastTranslationAt 즉시 갱신 → STALE 즉시 해제
      detail.session.lastTranslationAt = createdAt;
      const entry = sessionsState.rows.get(detail.session.sessionId);
      if (entry) renderSessionRow(entry);
      renderDetailHeader();
    }
  }

  function flushLiveBuffer(): void {
    const buffered = detail.fillBuffer;
    detail.fillBuffer = [];
    buffered.forEach((m) => appendLiveRow(m));
  }

  /** lastId 이후 이력을 nextCursor 소진까지 전부 당겨와 append (결정 4: 상한 없음). */
  async function fillFrom(sess: SessionItem, myEpoch: number): Promise<void> {
    let cursor = detail.lastId;
    for (;;) {
      let url =
        `${API}/sessions/${encodeURIComponent(sess.sessionId)}` +
        "/translations?limit=100";
      if (cursor != null) url += "&cursor=" + encodeURIComponent(String(cursor));
      const data = await fetchJson<HistoryResponse>(url);
      if (detail.epoch !== myEpoch) return; // 세션 전환·중지·재선택 시 중단
      const body = $("detail-rows");
      const items = data.items ?? [];
      const stick = stickToBottom(); // V1: gap fill 도 사용자 위치를 뺏지 않는다
      let added = 0;
      items.forEach((p) => {
        if (detail.seenIds.has(p.id)) return;
        detail.seenIds.add(p.id);
        if (detail.lastId == null || p.id > detail.lastId) detail.lastId = p.id;
        const tMs = new Date(p.createdAt).getTime();
        if (!isNaN(tMs)) detail.recentTimes.push(tMs); // 분당 건수(WU5)
        body.appendChild(pairRow(p));
        added += 1;
      });
      // gap fill로 받은 행도 활동으로 반영 — 복구 직후 STALE 오탐 방지 (items는 id ASC).
      const lastItem = items[items.length - 1];
      if (lastItem) {
        const prevT = sess.lastTranslationAt
          ? new Date(sess.lastTranslationAt).getTime()
          : NaN;
        const curT = new Date(lastItem.createdAt).getTime();
        if (!isNaN(curT) && (isNaN(prevT) || curT > prevT)) {
          sess.lastTranslationAt = lastItem.createdAt;
        }
      }
      $("detail-status").textContent = "이력 동기화 중… " + body.children.length + "행";
      if (stick) scrollToBottom();
      else if (added > 0) setPendingNew(detail.pendingNew + added);
      if (data.nextCursor == null) return;
      cursor = data.nextCursor;
    }
  }

  /** 라이브 시작·재연결 시 이력 동기화. 진행 중 수신분은 버퍼링했다가 끝나고 flush. */
  async function syncLive(): Promise<void> {
    if (!detail.session || !detail.liveOn) return;
    if (detail.filling) {
      detail.refillQueued = true; // fill 도중 재연결 — 끝나고 한 번 더 돈다
      return;
    }
    const sess = detail.session;
    const myEpoch = detail.epoch;
    detail.filling = true;
    try {
      do {
        detail.refillQueued = false;
        await fillFrom(sess, myEpoch);
        if (detail.epoch !== myEpoch) return;
      } while (detail.refillQueued);
      detail.filling = false;
      flushLiveBuffer();
      $("detail-status").textContent = "라이브 수신 중";
      const entry = sessionsState.rows.get(sess.sessionId);
      if (entry) renderSessionRow(entry); // 동기화로 갱신된 lastTranslationAt 반영
      renderDetailHeader();
    } catch (err) {
      if (detail.epoch !== myEpoch) return;
      // 동기화 실패해도 라이브는 유지 — 갭은 남을 수 있지만 수신분은 표시.
      detail.filling = false;
      flushLiveBuffer();
      $("detail-status").textContent =
        "이력 동기화 실패 (라이브는 유지): " + (err as Error).message;
    } finally {
      if (detail.epoch === myEpoch) detail.filling = false;
    }
  }

  function stopLive(): void {
    flushLiveBuffer(); // fill 중이던 버퍼도 화면에 남긴다
    closeLive();
    // 라이브가 끝났으면 "새 번역" 알림도 끝난다. flush 로 들어온 행까지 세고
    // 나서 지워야 하므로 flushLiveBuffer 뒤여야 한다.
    setPendingNew(0);
    $("live-toggle").textContent = "라이브 시작";
    $("detail-mode-label").textContent = "이력";
    $("detail-mode-label").className = "mode-label";
    $("detail-more").hidden = detail.cursor == null;
    renderDetailHeader();
  }

  wire("live-toggle", "click", () => {
    if (detail.liveOn || detail.live) {
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

  wire("search-form", "submit", (ev) => {
    ev.preventDefault();
    runSearch(true);
  });
  wire("search-more", "click", () => {
    if (searchState.cursor != null) runSearch(false);
  });

  // ====================================================================
  // 시스템 상태 칩 바 (WU5) — 10s 폴링, 현재값 표시 전용 (판정은 사이드카)
  // ====================================================================
  const STATUS_POLL_MS = 10_000;

  /* 10s 주기 폴링이라 /status 가 그보다 오래 걸리면 — 즉 DB 가 느려 정확히 상태를
   * 봐야 할 때 — 요청이 쌓이고 응답마다 칩 바가 다시 그려져 깜빡인다. */
  let statusInFlight = false;
  /** 실패 칩이 이미 붙어 있는가 — 연속 실패마다 중복으로 덧붙이지 않기 위한 상태. */
  let statusStale = false;

  type ChipTone = "ok" | "bad" | "warn";

  function chip(text: string, tone?: ChipTone): HTMLSpanElement {
    const node = el("span", { className: "chip" + (tone ? " " + tone : "") });
    if (tone) node.appendChild(el("span", { className: "dot" }));
    node.appendChild(el("span", { text }));
    return node;
  }

  function renderStatus(st: StatusResponse): void {
    const bar = $("status-bar");
    clear(bar);
    statusStale = false;
    // 결정 2: 활성 칩은 둘 다 — 자막기기(/ws) 연결 여부 + DB 기준 live 세션 수.
    bar.appendChild(chip(
      "자막기기 " + (st.wsClientConnected ? "연결" : "끊김"),
      st.wsClientConnected ? "ok" : "bad"
    ));
    // activeSessions=null 은 DB 조회만 실패한 열화 상태 — 나머지 칩은 유효하다.
    bar.appendChild(st.activeSessions != null
      ? chip("live 세션 " + st.activeSessions + "개")
      : chip("live 세션 조회 실패", "warn"));
    bar.appendChild(chip(
      "NATS " + (st.natsConnected ? "연결" : "끊김"),
      st.natsConnected ? "ok" : "bad"
    ));
    // "브로드캐스트"가 아니라 "자막 전송"인 이유: 이 값의 소스인
    // record_broadcast 는 자막기기 소켓 send 성공 시에만 찍힌다. 기기가 끊겨
    // pending 큐잉되는 동안은 번역이 정상 생산돼도 멈춘다 — 파이프라인 생존
    // 지표로 읽히면 오독이다(옆 "자막기기" 칩과 같이 봐야 하는 값).
    bar.appendChild(chip(
      "마지막 자막 전송 " +
      (st.lastBroadcastAgoSec != null ? fmtAgoSec(st.lastBroadcastAgoSec) : "없음")
    ));
    if (!st.nodeUp || !st.node) {
      bar.appendChild(chip("node 응답 없음", "bad"));
      return;
    }
    const n = st.node;
    bar.appendChild(chip(
      "STT " + (n.sttPaused == null ? "—" : n.sttPaused ? "일시정지" : "동작"),
      n.sttPaused == null ? undefined : n.sttPaused ? "warn" : "ok"
    ));
    bar.appendChild(chip(
      "RTMP 인증 " + (n.rtmpAuthEnabled == null ? "—" : n.rtmpAuthEnabled ? "켜짐" : "꺼짐"),
      n.rtmpAuthEnabled == null ? undefined : n.rtmpAuthEnabled ? "ok" : "warn"
    ));
    bar.appendChild(chip(
      "버퍼 " + (n.publishBufferSize != null ? n.publishBufferSize : "—")
    ));
  }

  /* 결정(2026-08-06): 실패해도 기존 칩을 지우지 않고 "조회 실패" 만 덧붙인다.
   * 오래된 값을 보여줄 위험은 있지만 실패 사실을 같이 띄우므로 최신으로 오인하지
   * 않는다 — 아무것도 안 보이는 것보다 "이 값은 N초 전 것" 이 낫다.
   * 바로 위 refreshSessions 와 실패 철학을 맞추는 변경이기도 하다. */
  function markStatusStale(): void {
    if (statusStale) return; // 연속 실패 — 칩은 하나면 된다
    statusStale = true;
    const bar = $opt("status-bar");
    if (!bar) return;
    // 칩이 하나도 없으면(최초 로드부터 실패) 덧붙일 옛 값 자체가 없다.
    bar.appendChild(chip(
      bar.children.length > 0 ? "조회 실패 — 앞의 값은 마지막 성공분" : "상태 조회 실패",
      "bad"
    ));
  }

  function pollStatus(): void {
    if (statusInFlight) return;
    statusInFlight = true;
    fetchJson<StatusResponse>(`${API}/status`)
      .then(renderStatus)
      // python 자체가 안 죽어도 nginx·네트워크 단절일 수 있다. renderStatus 가
      // 던진 경우도 여기로 오는데 칩 문구만으로는 그 둘이 구분되지 않는다 —
      // 백엔드는 멀쩡한데 백엔드를 뒤지게 되므로 에러는 반드시 콘솔에 남긴다.
      .catch((err: unknown) => {
        console.error("monitor: 상태 조회·렌더 실패", err);
        markStatusStale();
      })
      .finally(() => {
        statusInFlight = false;
      });
  }
  window.setInterval(pollStatus, STATUS_POLL_MS);

  // ====================================================================
  // 순단 이력 탭 (WU5 — §4-7 ws_blips, REST 조회만·라이브 이벤트 없음(결정 1))
  // ====================================================================
  const blipsState = {
    offset: 0,
    nextOffset: null as number | null,
    loading: false,
  };

  function fmtDurationMs(ms: number | null): string {
    if (ms == null) return "—";
    if (ms < 1000) return ms + "ms";
    return (ms / 1000).toFixed(1) + "s";
  }

  function blipRow(b: WsBlip): HTMLTableRowElement {
    const tr = el("tr");
    tr.appendChild(el("td", { className: "col-sess", text: b.sessionId }));
    tr.appendChild(el("td", { className: "col-time", text: fmtTime(b.disconnectedAt) }));
    const rec = el("td", { className: "col-time" });
    if (b.reconnectedAt) {
      rec.textContent = fmtTime(b.reconnectedAt);
    } else {
      // reconnected_at NULL 잔존 = 미복귀 (§4-7 결정 — sweeper 없음).
      rec.appendChild(el("span", { className: "badge stale", text: "미복귀" }));
    }
    tr.appendChild(rec);
    tr.appendChild(el("td", { className: "col-num", text: fmtDurationMs(b.durationMs) }));
    tr.appendChild(el("td", { className: "col-num", text: b.flushedCount != null ? b.flushedCount : "—" }));
    tr.appendChild(el("td", { className: "col-num", text: b.lostCount != null ? b.lostCount : "—" }));
    tr.appendChild(el("td", {
      className: "col-code",
      text: b.closeCode != null
        ? String(b.closeCode) + (b.closeReason ? " " + b.closeReason : "")
        : "—",
    }));
    tr.appendChild(el("td", { className: "col-det", text: b.detectedBy }));
    return tr;
  }

  function loadBlips(reset: boolean): void {
    if (blipsState.loading) return;
    blipsState.loading = true;
    if (reset) {
      // offset 만 되돌리면 fetch 실패 시 nextOffset 이 옛 위치로 남는다 — 표는
      // 비었는데 [더 보기]가 그 위치를 요청해 앞 페이지가 통째로 빠진 목록이
      // 그려진다. 순단 이력은 "몇 번 끊겼나" 를 세는 용도라 누락이 곧 오판이다.
      blipsState.offset = 0;
      blipsState.nextOffset = null;
      $("blips-more").hidden = true;
      clear($("blips-rows"));
    }
    $("blips-status").textContent = "불러오는 중…";
    const url = `${API}/ws-blips?limit=50&offset=${blipsState.offset}`;
    fetchJson<WsBlipsResponse>(url)
      .then((data) => {
        const body = $("blips-rows");
        (data.items ?? []).forEach((b) => body.appendChild(blipRow(b)));
        blipsState.nextOffset = data.nextOffset != null ? data.nextOffset : null;
        $("blips-more").hidden = blipsState.nextOffset == null;
        $("blips-status").textContent =
          body.children.length + "건" +
          (blipsState.nextOffset != null ? " (더 있음)" : "");
      })
      .catch((err: Error) => {
        $("blips-status").textContent = "오류: " + err.message;
      })
      .finally(() => {
        blipsState.loading = false;
      });
  }

  wire("blips-refresh", "click", () => loadBlips(true));
  wire("blips-more", "click", () => {
    if (blipsState.nextOffset != null) {
      blipsState.offset = blipsState.nextOffset;
      loadBlips(false);
    }
  });

  // ====================================================================
  // 전역 이벤트 채널 (/ws/monitor/events) — 세션 목록 실시간화
  // ====================================================================
  function handleGlobalEvent(msg: unknown): void {
    if (!msg || typeof msg !== "object") return;
    const m = msg as EventsMessage;
    const list = $("session-list");

    if (m.type === "session_started") {
      const existing = sessionsState.rows.get(m.sessionId);
      if (existing) {
        // 결정 2: 이벤트 신뢰 — 기존 행 LIVE 전환 + 상단 이동 (sessionId 재사용.
        // DB는 종료 상태일 수 있어 새로고침 시 되돌아감 — §7 메모 대상).
        const s = existing.session;
        s.startedAt = m.startedAt;
        s.endedAt = null;
        s.sourceLang = m.sourceLang;
        s.targetLang = m.targetLang;
        s.live = true;
        s.lastTranslationAt = null; // 이전 런 기준의 즉시 STALE 오탐 방지
        renderSessionRow(existing);
        list.insertBefore(existing.li, list.firstChild);
        if (detail.session === s) renderDetailHeader();
        return;
      }
      const entry = makeSessionRow(adoptSession({
        sessionId: m.sessionId,
        startedAt: m.startedAt,
        endedAt: null,
        sourceLang: m.sourceLang,
        targetLang: m.targetLang,
        translationCount: 0,
        lastTranslationAt: null,
        live: true,
      }));
      sessionsState.rows.set(m.sessionId, entry);
      list.insertBefore(entry.li, list.firstChild);
      renderSessionsCount();
      return;
    }

    if (m.type === "session_ended") {
      const entry = sessionsState.rows.get(m.sessionId);
      if (!entry) return; // 목록 밖(다음 페이지) 세션 — 재조회 때 반영
      const s = entry.session;
      s.live = false;
      s.endedAt = m.endedAt;
      if (m.translationCount != null) s.translationCount = m.translationCount;
      renderSessionRow(entry);
      if (detail.session === s) {
        // 세션 WS가 끊긴 사이 종료됐으면 session_closed를 영영 못 받는다 —
        // 전역 이벤트가 라이브(재연결 루프 포함)를 대신 끝낸다.
        if (detail.liveOn || detail.live) {
          $("detail-status").textContent =
            "세션 종료됨 (" +
            (m.translationCount != null ? m.translationCount : "?") +
            "건)";
          stopLive();
        }
        renderDetailHeader();
      }
    }
  }

  connectReconnecting({
    url: () => wsUrl("/ws/monitor/events"),
    onOpen: (resumed) => {
      // 결정 1: 끊긴 동안 놓친 이벤트는 REST 재조회 1회로 복구 (선택·상세 뷰는 유지).
      if (resumed) loadSessions(true);
    },
    onMessage: handleGlobalEvent,
    onDown: () => {
      $("sessions-status").textContent = "실시간 연결 끊김 — 재연결 중…";
    },
  });

  // D4-b: STALE 배지는 10초 주기 재평가 (결정 3 — 목록 폴링은 추가하지 않음)
  window.setInterval(() => {
    sessionsState.rows.forEach((entry) => {
      if (entry.session.live) renderSessionRow(entry);
    });
    if (detail.session && detail.session.live) renderDetailHeader();
  }, STALE_TICK_MS);

  // ====================================================================
  // 초기 로드
  // ====================================================================
  // 한쪽 렌더 경로가 던져도 나머지 초기화는 살린다 (배선과 같은 캐시 스큐 방어).
  try {
    loadSessions(true);
  } catch (err) {
    console.error("monitor: 초기 세션 로드 실패", err);
  }
  try {
    pollStatus();
  } catch (err) {
    console.error("monitor: 초기 상태 조회 실패", err);
  }
})();
