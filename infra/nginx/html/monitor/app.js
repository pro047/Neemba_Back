/* 번역 모니터 — 바닐라 JS.
 *
 * 백엔드 계약(Phase 5 인계 메모):
 *   GET /api/monitor/sessions?limit=&offset=
 *       -> { items:[{sessionId, startedAt, endedAt, sourceLang, targetLang,
 *                    translationCount, live}], limit, offset, nextOffset }
 *   GET /api/monitor/sessions/{id}/translations?cursor=&limit=
 *       -> { items:[{id, segmentId, sequence, sourceText, translatedText,
 *                    sourceLang, targetLang, confidence, createdAt}],
 *            limit, nextCursor }            // nextCursor = 정수 id 키셋
 *   GET /api/monitor/translations?lang=&from=&to=&q=&cursor=&limit=
 *       -> { items:[{...pair, sessionId}], limit, nextCursor }  // opaque 문자열
 *   WS  /ws/monitor?sessionId=<id>
 *       -> {type:"translation", sessionId, segmentId, sequence, sourceText,
 *           translatedText, sourceLang, targetLang, confidence}
 *       -> {type:"session_closed", sessionId, translationCount}
 *
 * 시각은 ISO8601 문자열. 모든 외부 텍스트는 textContent로만 렌더(XSS 방지).
 */
(function () {
  "use strict";

  var API = "/api/monitor";

  // ---- DOM 헬퍼 (textContent 전용, innerHTML 사용 안 함) ----------------
  function el(tag, opts) {
    var node = document.createElement(tag);
    if (opts) {
      if (opts.className) node.className = opts.className;
      if (opts.text != null) node.textContent = String(opts.text);
    }
    return node;
  }
  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }
  function $(id) {
    return document.getElementById(id);
  }

  function fmtTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso); // 파싱 실패 시 원본 그대로
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return (
      d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds())
    );
  }
  function langPair(src, tgt) {
    return (src || "?") + " → " + (tgt || "?");
  }

  function fetchJson(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(
      function (res) {
        if (!res.ok) {
          throw new Error("HTTP " + res.status + " — " + url);
        }
        return res.json();
      }
    );
  }

  // ====================================================================
  // 탭 전환
  // ====================================================================
  function showView(name) {
    var isSessions = name === "sessions";
    $("view-sessions").hidden = !isSessions;
    $("view-search").hidden = isSessions;
    $("tab-sessions").classList.toggle("active", isSessions);
    $("tab-search").classList.toggle("active", !isSessions);
  }
  $("tab-sessions").addEventListener("click", function () { showView("sessions"); });
  $("tab-search").addEventListener("click", function () { showView("search"); });

  // ====================================================================
  // 세션 목록
  // ====================================================================
  var sessionsState = { offset: 0, nextOffset: null, loading: false, selectedId: null };

  function sessionRow(s) {
    var li = el("li");
    var btn = el("button", { className: "session-item" });
    btn.type = "button";
    btn.dataset.sessionId = s.sessionId;

    var sid = el("div", { className: "sid", text: s.sessionId });
    var badge = el("span", {
      className: "badge " + (s.live ? "live" : "ended"),
      text: s.live ? "LIVE" : "종료",
    });
    sid.appendChild(badge);

    var sub = el("div", { className: "sub" });
    sub.textContent =
      "시작 " + fmtTime(s.startedAt) +
      "  ·  " + langPair(s.sourceLang, s.targetLang) +
      "  ·  " + (s.translationCount != null ? s.translationCount : 0) + "건";

    btn.appendChild(sid);
    btn.appendChild(sub);
    btn.addEventListener("click", function () {
      selectSession(s, btn);
    });
    li.appendChild(btn);
    return li;
  }

  function loadSessions(reset) {
    if (sessionsState.loading) return;
    sessionsState.loading = true;
    if (reset) {
      sessionsState.offset = 0;
      clear($("session-list"));
    }
    $("sessions-status").textContent = "불러오는 중…";
    var url = API + "/sessions?limit=50&offset=" + sessionsState.offset;
    fetchJson(url)
      .then(function (data) {
        var list = $("session-list");
        (data.items || []).forEach(function (s) {
          list.appendChild(sessionRow(s));
        });
        sessionsState.nextOffset = data.nextOffset != null ? data.nextOffset : null;
        $("sessions-more").hidden = sessionsState.nextOffset == null;
        $("sessions-status").textContent =
          list.children.length + "개 세션" +
          (sessionsState.nextOffset != null ? " (더 있음)" : "");
      })
      .catch(function (err) {
        $("sessions-status").textContent = "오류: " + err.message;
      })
      .finally(function () {
        sessionsState.loading = false;
      });
  }

  $("sessions-refresh").addEventListener("click", function () { loadSessions(true); });
  $("sessions-more").addEventListener("click", function () {
    if (sessionsState.nextOffset != null) {
      sessionsState.offset = sessionsState.nextOffset;
      loadSessions(false);
    }
  });

  // ====================================================================
  // 세션 상세 (이력 / 라이브)
  // ====================================================================
  var detail = {
    session: null,
    cursor: null,      // 이력 페이지네이션 (정수 id)
    loading: false,
    ws: null,          // 라이브 WebSocket
    liveOn: false,
    seenIds: null,     // 이력 행 중복 방지
  };

  function closeLive() {
    if (detail.ws) {
      try { detail.ws.close(); } catch (e) { /* noop */ }
      detail.ws = null;
    }
    detail.liveOn = false;
  }

  function pairRow(p, opts) {
    var tr = el("tr");
    if (opts && opts.flash) tr.className = "new-row";
    var seq = el("td", { className: "col-seq" });
    seq.textContent = p.sequence != null ? p.sequence : "·"; // NULL sequence 허용
    var src = el("td", { className: "col-src", text: p.sourceText || "" });
    var tgt = el("td", { className: "col-tgt", text: p.translatedText || "" });
    var time = el("td", { className: "col-time", text: fmtTime(p.createdAt) });
    tr.appendChild(seq);
    tr.appendChild(src);
    tr.appendChild(tgt);
    tr.appendChild(time);
    return tr;
  }

  function selectSession(s, btnEl) {
    detail.session = s;
    detail.cursor = null;
    detail.seenIds = {};
    closeLive();

    sessionsState.selectedId = s.sessionId;
    Array.prototype.forEach.call(
      document.querySelectorAll(".session-item"),
      function (b) { b.classList.toggle("selected", b === btnEl); }
    );

    $("detail-title").textContent = s.sessionId;
    $("detail-controls").hidden = false;
    $("detail-meta").textContent =
      langPair(s.sourceLang, s.targetLang) +
      "  ·  시작 " + fmtTime(s.startedAt) +
      (s.endedAt ? "  ·  종료 " + fmtTime(s.endedAt) : "  ·  진행중") +
      "  ·  " + (s.translationCount != null ? s.translationCount : 0) + "건";

    // live(ended_at IS NULL) 세션이면 라이브 토글 노출, 종료 세션이면 이력만.
    var liveBtn = $("live-toggle");
    liveBtn.hidden = !s.live;
    liveBtn.textContent = "라이브 시작";
    $("detail-mode-label").textContent = "이력";
    $("detail-mode-label").className = "mode-label";

    clear($("detail-rows"));
    loadHistory(true);
  }

  function loadHistory(reset) {
    if (!detail.session || detail.loading) return;
    detail.loading = true;
    if (reset) {
      detail.cursor = null;
      clear($("detail-rows"));
      detail.seenIds = {};
    }
    $("detail-status").textContent = "이력 불러오는 중…";
    var url =
      API + "/sessions/" + encodeURIComponent(detail.session.sessionId) +
      "/translations?limit=100";
    if (detail.cursor != null) url += "&cursor=" + encodeURIComponent(detail.cursor);
    fetchJson(url)
      .then(function (data) {
        var body = $("detail-rows");
        (data.items || []).forEach(function (p) {
          if (detail.seenIds[p.id]) return;
          detail.seenIds[p.id] = true;
          body.appendChild(pairRow(p));
        });
        detail.cursor = data.nextCursor != null ? data.nextCursor : null;
        $("detail-more").hidden = detail.cursor == null || detail.liveOn;
        $("detail-status").textContent =
          body.children.length + "행" +
          (detail.cursor != null ? " (더 있음)" : "");
      })
      .catch(function (err) {
        $("detail-status").textContent = "오류: " + err.message;
      })
      .finally(function () {
        detail.loading = false;
      });
  }

  $("detail-more").addEventListener("click", function () {
    if (detail.cursor != null) loadHistory(false);
  });

  // ---- 라이브 (WebSocket) --------------------------------------------
  function startLive() {
    if (!detail.session || detail.ws) return;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var url =
      proto + "//" + location.host +
      "/ws/monitor?sessionId=" + encodeURIComponent(detail.session.sessionId);

    var ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      $("detail-status").textContent = "라이브 연결 실패: " + e.message;
      return;
    }
    detail.ws = ws;
    detail.liveOn = true;
    $("detail-more").hidden = true;
    $("live-toggle").textContent = "라이브 중지";
    $("detail-mode-label").textContent = "라이브";
    $("detail-mode-label").className = "mode-label live";
    $("detail-status").textContent = "라이브 연결 중…";

    ws.onopen = function () {
      $("detail-status").textContent = "라이브 수신 중";
    };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg || typeof msg !== "object") return;
      if (msg.type === "session_closed") {
        $("detail-status").textContent =
          "세션 종료됨 (" + (msg.translationCount != null ? msg.translationCount : "?") + "건)";
        stopLive();
        return;
      }
      if (msg.type === "translation") {
        var body = $("detail-rows");
        // 라이브 payload는 createdAt이 없으므로 수신 시각으로 표기.
        if (msg.createdAt == null) msg.createdAt = new Date().toISOString();
        body.appendChild(pairRow(msg, { flash: true }));
        body.parentNode.scrollTop = body.parentNode.scrollHeight;
      }
    };
    ws.onerror = function () {
      $("detail-status").textContent = "라이브 오류 (연결 끊김)";
    };
    ws.onclose = function () {
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

  function stopLive() {
    closeLive();
    $("live-toggle").textContent = "라이브 시작";
    $("detail-mode-label").textContent = "이력";
    $("detail-mode-label").className = "mode-label";
    $("detail-more").hidden = detail.cursor == null;
  }

  $("live-toggle").addEventListener("click", function () {
    if (detail.liveOn || detail.ws) {
      stopLive();
    } else {
      startLive();
    }
  });

  // ====================================================================
  // 검색
  // ====================================================================
  var searchState = { cursor: null, loading: false, params: "" };

  function buildSearchParams() {
    var qs = [];
    var lang = $("f-lang").value.trim();
    var from = $("f-from").value.trim();
    var to = $("f-to").value.trim();
    var q = $("f-q").value.trim();
    if (lang) qs.push("lang=" + encodeURIComponent(lang));
    if (from) qs.push("from=" + encodeURIComponent(from));
    if (to) qs.push("to=" + encodeURIComponent(to));
    if (q) qs.push("q=" + encodeURIComponent(q));
    return qs.join("&");
  }

  function searchRow(p) {
    var tr = el("tr");
    var sess = el("td", { className: "col-sess", text: p.sessionId || "" });
    var src = el("td", { className: "col-src", text: p.sourceText || "" });
    var tgt = el("td", { className: "col-tgt", text: p.translatedText || "" });
    var lang = el("td", { className: "col-lang", text: langPair(p.sourceLang, p.targetLang) });
    var time = el("td", { className: "col-time", text: fmtTime(p.createdAt) });
    tr.appendChild(sess);
    tr.appendChild(src);
    tr.appendChild(tgt);
    tr.appendChild(lang);
    tr.appendChild(time);
    return tr;
  }

  function runSearch(reset) {
    if (searchState.loading) return;
    searchState.loading = true;
    if (reset) {
      searchState.params = buildSearchParams();
      searchState.cursor = null;
      clear($("search-rows"));
    }
    $("search-status").textContent = "검색 중…";
    var url = API + "/translations?limit=100";
    if (searchState.params) url += "&" + searchState.params;
    if (searchState.cursor != null) {
      url += "&cursor=" + encodeURIComponent(searchState.cursor);
    }
    fetchJson(url)
      .then(function (data) {
        var body = $("search-rows");
        (data.items || []).forEach(function (p) {
          body.appendChild(searchRow(p));
        });
        searchState.cursor = data.nextCursor != null ? data.nextCursor : null;
        $("search-more").hidden = searchState.cursor == null;
        $("search-status").textContent =
          body.children.length + "행" +
          (searchState.cursor != null ? " (더 있음)" : "");
      })
      .catch(function (err) {
        $("search-status").textContent = "오류: " + err.message;
      })
      .finally(function () {
        searchState.loading = false;
      });
  }

  $("search-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    runSearch(true);
  });
  $("search-more").addEventListener("click", function () {
    if (searchState.cursor != null) runSearch(false);
  });

  // ====================================================================
  // 초기 로드
  // ====================================================================
  loadSessions(true);
})();
