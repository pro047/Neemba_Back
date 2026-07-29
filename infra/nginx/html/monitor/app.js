"use strict";
/* 번역 모니터 — TypeScript 소스.
 *
 * app.js는 이 파일의 tsc 빌드 산출물이다 — app.js를 직접 수정하지 말 것.
 * 수정 후 이 디렉터리에서 `npm run build` (검사만: `npm run check`).
 *
 * 시각은 ISO8601 문자열. 모든 외부 텍스트는 textContent로만 렌더(XSS 방지).
 */
(() => {
    "use strict";
    const API = "/api/monitor";
    // ---- DOM 헬퍼 (textContent 전용, innerHTML 사용 안 함) ----------------
    function el(tag, opts) {
        const node = document.createElement(tag);
        if (opts) {
            if (opts.className)
                node.className = opts.className;
            if (opts.text != null)
                node.textContent = String(opts.text);
        }
        return node;
    }
    function clear(node) {
        while (node.firstChild)
            node.removeChild(node.firstChild);
    }
    function $(id) {
        const node = document.getElementById(id);
        if (!node)
            throw new Error("missing element: #" + id);
        return node;
    }
    function fmtTime(iso) {
        if (!iso)
            return "";
        const d = new Date(iso);
        if (isNaN(d.getTime()))
            return String(iso); // 파싱 실패 시 원본 그대로
        const p = (n) => (n < 10 ? "0" : "") + n;
        return (`${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
            ` ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`);
    }
    function langPair(src, tgt) {
        return `${src || "?"} → ${tgt || "?"}`;
    }
    function fmtAgoSec(sec) {
        const s = Math.max(0, Math.round(sec));
        if (s < 60)
            return s + "초 전";
        if (s < 3600)
            return Math.floor(s / 60) + "분 전";
        return Math.floor(s / 3600) + "시간 전";
    }
    async function fetchJson(url) {
        const res = await fetch(url, { headers: { Accept: "application/json" } });
        if (!res.ok) {
            throw new Error(`HTTP ${res.status} — ${url}`);
        }
        return res.json();
    }
    // ---- 자동 재연결 WebSocket 헬퍼 --------------------------------------
    const RECONNECT_BASE_MS = 1_000;
    const RECONNECT_MAX_MS = 30_000;
    /** D4-b: live인데 마지막 번역 후 180s 무활동이면 STALE (사이드카 GAP 기준과 정렬). */
    const STALE_THRESHOLD_MS = 180_000;
    const STALE_TICK_MS = 10_000;
    function wsUrl(path) {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        return `${proto}//${location.host}${path}`;
    }
    /** 지수 백오프(1s→2s→4s…최대 30s, open 성공 시 리셋) 재연결 WebSocket. */
    function connectReconnecting(opts) {
        let ws = null;
        let timer = null;
        let attempt = 0;
        let resumed = false;
        let closed = false;
        function scheduleRetry() {
            const delayMs = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
            attempt += 1;
            resumed = true;
            opts.onDown(delayMs);
            timer = window.setTimeout(connect, delayMs);
        }
        function connect() {
            if (closed)
                return;
            timer = null;
            let sock;
            try {
                sock = new WebSocket(opts.url());
            }
            catch {
                scheduleRetry();
                return;
            }
            ws = sock;
            sock.onopen = () => {
                if (closed)
                    return;
                attempt = 0; // 백오프 리셋
                const wasResumed = resumed;
                resumed = true; // 다음 open부터는 항상 회복으로 취급
                opts.onOpen(wasResumed);
            };
            sock.onmessage = (ev) => {
                if (closed)
                    return;
                let msg;
                try {
                    msg = JSON.parse(String(ev.data));
                }
                catch {
                    return;
                }
                opts.onMessage(msg);
            };
            sock.onerror = () => { };
            sock.onclose = () => {
                ws = null;
                if (closed)
                    return;
                scheduleRetry();
            };
        }
        connect();
        return {
            close() {
                closed = true;
                if (timer != null) {
                    window.clearTimeout(timer);
                    timer = null;
                }
                if (ws) {
                    try {
                        ws.close();
                    }
                    catch { /* noop */ }
                    ws = null;
                }
            },
        };
    }
    // ====================================================================
    // 탭 전환
    // ====================================================================
    const VIEW_NAMES = ["sessions", "search", "blips"];
    function showView(name) {
        VIEW_NAMES.forEach((v) => {
            $("view-" + v).hidden = v !== name;
            $("tab-" + v).classList.toggle("active", v === name);
        });
    }
    $("tab-sessions").addEventListener("click", () => showView("sessions"));
    $("tab-search").addEventListener("click", () => showView("search"));
    $("tab-blips").addEventListener("click", () => {
        // 결정 3: 별도 탭, 진입 시 로드 (상시 폴링 없음).
        showView("blips");
        loadBlips(true);
    });
    const sessionsState = {
        offset: 0,
        nextOffset: null,
        loading: false,
        reloadQueued: false, // 로딩 중 들어온 복구 재조회를 잃지 않고 이어서 실행
        selectedId: null,
        rows: new Map(), // sessionId → 행 (이벤트로 개별 갱신)
    };
    function isStale(s) {
        if (!s.live || !s.lastTranslationAt)
            return false;
        const t = new Date(s.lastTranslationAt).getTime();
        if (isNaN(t))
            return false;
        return Date.now() - t > STALE_THRESHOLD_MS;
    }
    function sessionBadge(s) {
        if (s.live && isStale(s))
            return el("span", { className: "badge stale", text: "STALE" });
        if (s.live)
            return el("span", { className: "badge live", text: "LIVE" });
        return el("span", { className: "badge ended", text: "종료" });
    }
    function renderSessionRow(entry) {
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
    function makeSessionRow(s) {
        const li = el("li");
        const btn = el("button", { className: "session-item" });
        btn.type = "button";
        btn.dataset.sessionId = s.sessionId;
        li.appendChild(btn);
        const entry = { li, btn, session: s };
        // entry.session은 제자리 병합되므로 클릭 시점의 최신 데이터로 선택된다.
        btn.addEventListener("click", () => selectSession(entry.session, btn));
        renderSessionRow(entry);
        return entry;
    }
    /** 선택 중 세션과 같은 id면 기존 객체에 병합 — 목록 행·상세 뷰가 같은 객체를 보게 유지. */
    function adoptSession(s) {
        if (detail.session && detail.session.sessionId === s.sessionId) {
            Object.assign(detail.session, s);
            return detail.session;
        }
        return s;
    }
    function renderSessionsCount() {
        $("sessions-status").textContent =
            $("session-list").children.length + "개 세션" +
                (sessionsState.nextOffset != null ? " (더 있음)" : "");
    }
    function loadSessions(reset) {
        if (sessionsState.loading) {
            // 재조회(reset)는 놓친 이벤트 복구용이라 버리면 안 됨 — 끝나고 이어서 실행.
            if (reset)
                sessionsState.reloadQueued = true;
            return;
        }
        sessionsState.loading = true;
        if (reset) {
            sessionsState.offset = 0;
            sessionsState.rows.clear();
            clear($("session-list"));
        }
        $("sessions-status").textContent = "불러오는 중…";
        const url = `${API}/sessions?limit=50&offset=${sessionsState.offset}`;
        fetchJson(url)
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
            .catch((err) => {
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
        session: null,
        cursor: null, // 이력 페이지네이션 (정수 id)
        loading: false,
        live: null, // 라이브 WebSocket (자동 재연결)
        liveOn: false,
        seenIds: new Set(), // 이력·라이브 행 중복 방지 (id 기반)
        lastId: null, // 마지막 수신 id — gap fill 기준
        filling: false, // gap fill 진행 중이면 라이브 수신은 버퍼링
        refillQueued: false, // fill 도중 재연결 → 종료 후 한 번 더
        fillBuffer: [],
        // 세션 전환·라이브 중지마다 증가 — 같은 세션을 빠르게 재선택해도
        // 이전 fill 루프의 늦은 fetch가 새 뷰에 끼어들지 못하게 무효화한다.
        epoch: 0,
        // WU5 결정 4: 분당 건수는 프런트 파생 — 수신 행 createdAt(epoch ms)만 모아
        // 렌더 시 최근 60초로 걸러 센다. 라이브·gap fill 수신 공용, 세션 전환 시 리셋.
        recentTimes: [],
    };
    function closeLive() {
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
    function pairRow(p, opts) {
        const tr = el("tr");
        if (opts && opts.flash)
            tr.className = "new-row";
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
    function renderDetailHeader() {
        const s = detail.session;
        if (!s)
            return;
        const title = $("detail-title");
        title.textContent = s.sessionId;
        if (s.live && isStale(s)) {
            title.appendChild(el("span", { className: "badge stale", text: "STALE" }));
        }
        // WU5 미니 통계: 분당 건수(최근 60s 수신 행)·마지막 수신 경과 — 진행중 세션만,
        // 종료 세션은 "—" (결정 4). 10s STALE 타이머가 재렌더해 주기 갱신된다.
        let stats;
        if (s.live) {
            const now = Date.now();
            detail.recentTimes = detail.recentTimes.filter((t) => now - t <= 60_000);
            stats = "  ·  분당 " + detail.recentTimes.length + "건";
            const last = s.lastTranslationAt ? new Date(s.lastTranslationAt).getTime() : NaN;
            stats += "  ·  마지막 수신 " + (isNaN(last) ? "없음" : fmtAgoSec((now - last) / 1000));
        }
        else {
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
    function selectSession(s, btnEl) {
        closeLive(); // 이전 세션의 라이브·재연결 중단
        detail.session = s;
        detail.cursor = null;
        detail.seenIds = new Set();
        detail.lastId = null;
        detail.recentTimes = [];
        sessionsState.selectedId = s.sessionId;
        document.querySelectorAll(".session-item").forEach((b) => {
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
        }
        else {
            loadHistory(true);
        }
    }
    function loadHistory(reset) {
        if (!detail.session || detail.loading)
            return;
        detail.loading = true;
        if (reset) {
            detail.cursor = null;
            clear($("detail-rows"));
            detail.seenIds = new Set();
        }
        $("detail-status").textContent = "이력 불러오는 중…";
        let url = `${API}/sessions/${encodeURIComponent(detail.session.sessionId)}` +
            "/translations?limit=100";
        if (detail.cursor != null)
            url += "&cursor=" + encodeURIComponent(detail.cursor);
        fetchJson(url)
            .then((data) => {
            const body = $("detail-rows");
            (data.items ?? []).forEach((p) => {
                if (detail.seenIds.has(p.id))
                    return;
                detail.seenIds.add(p.id);
                if (detail.lastId == null || p.id > detail.lastId)
                    detail.lastId = p.id;
                body.appendChild(pairRow(p));
            });
            detail.cursor = data.nextCursor != null ? data.nextCursor : null;
            $("detail-more").hidden = detail.cursor == null || detail.liveOn;
            $("detail-status").textContent =
                body.children.length + "행" +
                    (detail.cursor != null ? " (더 있음)" : "");
        })
            .catch((err) => {
            $("detail-status").textContent = "오류: " + err.message;
        })
            .finally(() => {
            detail.loading = false;
        });
    }
    $("detail-more").addEventListener("click", () => {
        if (detail.cursor != null)
            loadHistory(false);
    });
    // ---- 라이브 (WebSocket, 자동 재연결 + gap fill) -----------------------
    function startLive() {
        if (!detail.session || detail.live)
            return;
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
                if (detail.session !== sess || !detail.liveOn)
                    return;
                $("detail-status").textContent = "라이브 수신 중";
                void syncLive(); // 최초 연결·재연결 공통 — lastId 이후 이력으로 gap fill
            },
            onMessage: (msg) => handleLiveMessage(sess, msg),
            onDown: (retryDelayMs) => {
                if (detail.session !== sess || !detail.liveOn)
                    return;
                $("detail-status").textContent =
                    "라이브 연결 끊김 — " + Math.round(retryDelayMs / 1000) + "초 후 재연결…";
            },
        });
    }
    function handleLiveMessage(sess, msg) {
        if (detail.session !== sess || !detail.liveOn)
            return;
        if (!msg || typeof msg !== "object")
            return;
        const m = msg;
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
    function appendLiveRow(m) {
        if (m.id != null) {
            if (detail.seenIds.has(m.id))
                return; // 이력·gap fill과 id 기반 dedup
            detail.seenIds.add(m.id);
            if (detail.lastId == null || m.id > detail.lastId)
                detail.lastId = m.id;
        }
        // id:null(insert 실패 폴백)은 dedup 불가 — 그대로 append (D3 수용 범위).
        // createdAt이 없는 payload는 수신 시각으로 표기.
        const createdAt = m.createdAt != null ? m.createdAt : new Date().toISOString();
        const createdMs = new Date(createdAt).getTime();
        if (!isNaN(createdMs))
            detail.recentTimes.push(createdMs); // 분당 건수(WU5)
        const body = $("detail-rows");
        body.appendChild(pairRow({ ...m, createdAt }, { flash: true }));
        const wrap = body.parentElement;
        if (wrap)
            wrap.scrollTop = wrap.scrollHeight;
        if (detail.session) {
            // 결정 3: 선택 세션은 라이브 수신으로 lastTranslationAt 즉시 갱신 → STALE 즉시 해제
            detail.session.lastTranslationAt = createdAt;
            const entry = sessionsState.rows.get(detail.session.sessionId);
            if (entry)
                renderSessionRow(entry);
            renderDetailHeader();
        }
    }
    function flushLiveBuffer() {
        const buffered = detail.fillBuffer;
        detail.fillBuffer = [];
        buffered.forEach((m) => appendLiveRow(m));
    }
    /** lastId 이후 이력을 nextCursor 소진까지 전부 당겨와 append (결정 4: 상한 없음). */
    async function fillFrom(sess, myEpoch) {
        let cursor = detail.lastId;
        for (;;) {
            let url = `${API}/sessions/${encodeURIComponent(sess.sessionId)}` +
                "/translations?limit=100";
            if (cursor != null)
                url += "&cursor=" + encodeURIComponent(String(cursor));
            const data = await fetchJson(url);
            if (detail.epoch !== myEpoch)
                return; // 세션 전환·중지·재선택 시 중단
            const body = $("detail-rows");
            const items = data.items ?? [];
            items.forEach((p) => {
                if (detail.seenIds.has(p.id))
                    return;
                detail.seenIds.add(p.id);
                if (detail.lastId == null || p.id > detail.lastId)
                    detail.lastId = p.id;
                const tMs = new Date(p.createdAt).getTime();
                if (!isNaN(tMs))
                    detail.recentTimes.push(tMs); // 분당 건수(WU5)
                body.appendChild(pairRow(p));
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
            const wrap = body.parentElement;
            if (wrap)
                wrap.scrollTop = wrap.scrollHeight;
            if (data.nextCursor == null)
                return;
            cursor = data.nextCursor;
        }
    }
    /** 라이브 시작·재연결 시 이력 동기화. 진행 중 수신분은 버퍼링했다가 끝나고 flush. */
    async function syncLive() {
        if (!detail.session || !detail.liveOn)
            return;
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
                if (detail.epoch !== myEpoch)
                    return;
            } while (detail.refillQueued);
            detail.filling = false;
            flushLiveBuffer();
            $("detail-status").textContent = "라이브 수신 중";
            const entry = sessionsState.rows.get(sess.sessionId);
            if (entry)
                renderSessionRow(entry); // 동기화로 갱신된 lastTranslationAt 반영
            renderDetailHeader();
        }
        catch (err) {
            if (detail.epoch !== myEpoch)
                return;
            // 동기화 실패해도 라이브는 유지 — 갭은 남을 수 있지만 수신분은 표시.
            detail.filling = false;
            flushLiveBuffer();
            $("detail-status").textContent =
                "이력 동기화 실패 (라이브는 유지): " + err.message;
        }
        finally {
            if (detail.epoch === myEpoch)
                detail.filling = false;
        }
    }
    function stopLive() {
        flushLiveBuffer(); // fill 중이던 버퍼도 화면에 남긴다
        closeLive();
        $("live-toggle").textContent = "라이브 시작";
        $("detail-mode-label").textContent = "이력";
        $("detail-mode-label").className = "mode-label";
        $("detail-more").hidden = detail.cursor == null;
        renderDetailHeader();
    }
    $("live-toggle").addEventListener("click", () => {
        if (detail.liveOn || detail.live) {
            stopLive();
        }
        else {
            startLive();
        }
    });
    // ====================================================================
    // 검색
    // ====================================================================
    const searchState = {
        cursor: null,
        loading: false,
        params: "",
    };
    function buildSearchParams() {
        const qs = [];
        const lang = $("f-lang").value.trim();
        const from = $("f-from").value.trim();
        const to = $("f-to").value.trim();
        const q = $("f-q").value.trim();
        if (lang)
            qs.push("lang=" + encodeURIComponent(lang));
        if (from)
            qs.push("from=" + encodeURIComponent(from));
        if (to)
            qs.push("to=" + encodeURIComponent(to));
        if (q)
            qs.push("q=" + encodeURIComponent(q));
        return qs.join("&");
    }
    function searchRow(p) {
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
    function runSearch(reset) {
        if (searchState.loading)
            return;
        searchState.loading = true;
        if (reset) {
            searchState.params = buildSearchParams();
            searchState.cursor = null;
            clear($("search-rows"));
        }
        $("search-status").textContent = "검색 중…";
        let url = `${API}/translations?limit=100`;
        if (searchState.params)
            url += "&" + searchState.params;
        if (searchState.cursor != null) {
            url += "&cursor=" + encodeURIComponent(searchState.cursor);
        }
        fetchJson(url)
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
            .catch((err) => {
            $("search-status").textContent = "오류: " + err.message;
        })
            .finally(() => {
            searchState.loading = false;
        });
    }
    $("search-form").addEventListener("submit", (ev) => {
        ev.preventDefault();
        runSearch(true);
    });
    $("search-more").addEventListener("click", () => {
        if (searchState.cursor != null)
            runSearch(false);
    });
    // ====================================================================
    // 시스템 상태 칩 바 (WU5) — 10s 폴링, 현재값 표시 전용 (판정은 사이드카)
    // ====================================================================
    const STATUS_POLL_MS = 10_000;
    function chip(text, tone) {
        const node = el("span", { className: "chip" + (tone ? " " + tone : "") });
        if (tone)
            node.appendChild(el("span", { className: "dot" }));
        node.appendChild(el("span", { text }));
        return node;
    }
    function renderStatus(st) {
        const bar = $("status-bar");
        clear(bar);
        // 결정 2: 활성 칩은 둘 다 — 자막기기(/ws) 연결 여부 + DB 기준 live 세션 수.
        bar.appendChild(chip("자막기기 " + (st.wsClientConnected ? "연결" : "끊김"), st.wsClientConnected ? "ok" : "bad"));
        bar.appendChild(chip("live 세션 " + st.activeSessions + "개"));
        bar.appendChild(chip("NATS " + (st.natsConnected ? "연결" : "끊김"), st.natsConnected ? "ok" : "bad"));
        bar.appendChild(chip("마지막 브로드캐스트 " +
            (st.lastBroadcastAgoSec != null ? fmtAgoSec(st.lastBroadcastAgoSec) : "없음")));
        if (!st.nodeUp || !st.node) {
            bar.appendChild(chip("node 응답 없음", "bad"));
            return;
        }
        const n = st.node;
        bar.appendChild(chip("STT " + (n.sttPaused == null ? "—" : n.sttPaused ? "일시정지" : "동작"), n.sttPaused == null ? undefined : n.sttPaused ? "warn" : "ok"));
        bar.appendChild(chip("RTMP 인증 " + (n.rtmpAuthEnabled == null ? "—" : n.rtmpAuthEnabled ? "켜짐" : "꺼짐"), n.rtmpAuthEnabled == null ? undefined : n.rtmpAuthEnabled ? "ok" : "warn"));
        bar.appendChild(chip("버퍼 " + (n.publishBufferSize != null ? n.publishBufferSize : "—")));
    }
    function pollStatus() {
        fetchJson(`${API}/status`)
            .then(renderStatus)
            .catch(() => {
            // python 자체가 안 죽어도 nginx·네트워크 단절일 수 있다 — 칩 하나로 표시.
            const bar = $("status-bar");
            clear(bar);
            bar.appendChild(chip("상태 조회 실패", "bad"));
        });
    }
    window.setInterval(pollStatus, STATUS_POLL_MS);
    // ====================================================================
    // 순단 이력 탭 (WU5 — §4-7 ws_blips, REST 조회만·라이브 이벤트 없음(결정 1))
    // ====================================================================
    const blipsState = {
        offset: 0,
        nextOffset: null,
        loading: false,
    };
    function fmtDurationMs(ms) {
        if (ms == null)
            return "—";
        if (ms < 1000)
            return ms + "ms";
        return (ms / 1000).toFixed(1) + "s";
    }
    function blipRow(b) {
        const tr = el("tr");
        tr.appendChild(el("td", { className: "col-sess", text: b.sessionId }));
        tr.appendChild(el("td", { className: "col-time", text: fmtTime(b.disconnectedAt) }));
        const rec = el("td", { className: "col-time" });
        if (b.reconnectedAt) {
            rec.textContent = fmtTime(b.reconnectedAt);
        }
        else {
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
    function loadBlips(reset) {
        if (blipsState.loading)
            return;
        blipsState.loading = true;
        if (reset) {
            blipsState.offset = 0;
            clear($("blips-rows"));
        }
        $("blips-status").textContent = "불러오는 중…";
        const url = `${API}/ws-blips?limit=50&offset=${blipsState.offset}`;
        fetchJson(url)
            .then((data) => {
            const body = $("blips-rows");
            (data.items ?? []).forEach((b) => body.appendChild(blipRow(b)));
            blipsState.nextOffset = data.nextOffset != null ? data.nextOffset : null;
            $("blips-more").hidden = blipsState.nextOffset == null;
            $("blips-status").textContent =
                body.children.length + "건" +
                    (blipsState.nextOffset != null ? " (더 있음)" : "");
        })
            .catch((err) => {
            $("blips-status").textContent = "오류: " + err.message;
        })
            .finally(() => {
            blipsState.loading = false;
        });
    }
    $("blips-refresh").addEventListener("click", () => loadBlips(true));
    $("blips-more").addEventListener("click", () => {
        if (blipsState.nextOffset != null) {
            blipsState.offset = blipsState.nextOffset;
            loadBlips(false);
        }
    });
    // ====================================================================
    // 전역 이벤트 채널 (/ws/monitor/events) — 세션 목록 실시간화
    // ====================================================================
    function handleGlobalEvent(msg) {
        if (!msg || typeof msg !== "object")
            return;
        const m = msg;
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
                if (detail.session === s)
                    renderDetailHeader();
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
            if (!entry)
                return; // 목록 밖(다음 페이지) 세션 — 재조회 때 반영
            const s = entry.session;
            s.live = false;
            s.endedAt = m.endedAt;
            if (m.translationCount != null)
                s.translationCount = m.translationCount;
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
            if (resumed)
                loadSessions(true);
        },
        onMessage: handleGlobalEvent,
        onDown: () => {
            $("sessions-status").textContent = "실시간 연결 끊김 — 재연결 중…";
        },
    });
    // D4-b: STALE 배지는 10초 주기 재평가 (결정 3 — 목록 폴링은 추가하지 않음)
    window.setInterval(() => {
        sessionsState.rows.forEach((entry) => {
            if (entry.session.live)
                renderSessionRow(entry);
        });
        if (detail.session && detail.session.live)
            renderDetailHeader();
    }, STALE_TICK_MS);
    // ====================================================================
    // 초기 로드
    // ====================================================================
    loadSessions(true);
    pollStatus();
})();
