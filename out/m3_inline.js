
"use strict";
/* =========================================================================
   Hermes WebRTC 브리지 클라이언트 (M3)
   ========================================================================= */
(function () {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);

  const CFG = {
    token: params.get("token") || localStorage.getItem("bridge_token") || "",
    statusPollMs: 3000,
    connectTimeoutMs: 15000,
    maxBubbles: 200,
    stunFallback: [{ urls: ["stun:stun.l.google.com:19302"] }],
  };
  if (params.get("token")) localStorage.setItem("bridge_token", params.get("token"));

  // ---- 상태 ----------------------------------------------------------------
  const S = {
    phase: "idle",           // idle | connecting | live | ended | error
    muted: false,
    wakeLock: null,
    pc: null,
    dc: null,
    micStream: null,
    micTrack: null,
    remoteStream: null,
    audioCtx: null,
    micAnalyser: null,
    aiAnalyser: null,
    levels: { mic: 0, ai: 0 },
    vad: "대기",
    events: 0,
    turns: 0,
    usage: {},
    rttMs: null,
    lastEvent: "-",
    session: 0,
    pollTimer: null,
    vizMode: "canvas",
    startedAt: null,
  };

  // ---- UI 헬퍼 -------------------------------------------------------------
  const PHASE_LABEL = { idle: "대기", connecting: "연결 중", live: "통화 중",
                        ended: "종료", error: "오류" };
  function renderStatePill() {
    const pill = $("statePill");
    let label = PHASE_LABEL[S.phase] || S.phase;
    if (S.phase === "live" && S.muted) label += " (음소거)";
    pill.textContent = label;
    pill.className = "pill" + (S.phase === "live" ? " live" : S.phase === "error" ? " err" : "");
  }
  function setPhase(phase, note) {
    S.phase = phase;
    renderStatePill();
    $("startBtn").disabled = phase === "connecting" || phase === "live";
    $("hangupBtn").disabled = !(phase === "connecting" || phase === "live");
    $("muteBtn").disabled = phase !== "live";
    $("sendBtn").disabled = phase !== "live";
    if (note) banner(note, phase === "error");
  }
  function banner(text, isError) {
    const el = $("banner");
    el.textContent = text;
    el.style.display = text ? "block" : "none";
    el.style.borderColor = isError ? "var(--err)" : "var(--line)";
  }
  function sys(text) { addBubble("system", text, "시스템"); }
  function addBubble(kind, text, who) {
    const box = $("transcript");
    const empty = $("empty");
    if (empty) empty.remove();
    let el;
    if (kind === "assistant" && S._lastAssistant) { el = S._lastAssistant; }
    else {
      el = document.createElement("div");
      el.className = "bubble " + kind;
      if (who) { const w = document.createElement("span"); w.className = "who"; w.textContent = who; el.appendChild(w); }
      el.appendChild(document.createTextNode(text || ""));
      box.appendChild(el);
      S._lastAssistant = kind === "assistant" ? el : null;
    }
    if (el.lastChild && el.lastChild.nodeType === 3) el.lastChild.textContent = text || "";
    while (box.children.length > CFG.maxBubbles) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
    return el;
  }
  function setVad(text, cls) {
    S.vad = text;
    $("vadState").textContent = text;
  }
  function draw() {
    const cv = $("viz");
    let ctx = null;
    if (S.vizMode === "canvas") {
      try { ctx = cv && cv.getContext ? cv.getContext("2d") : null; } catch (e) { ctx = null; }
      if (!ctx) S.vizMode = "text";           // jsdom 등 canvas 없는 환경 → 수치 표시만
    }
    const w = cv ? cv.width : 0, h = cv ? cv.height : 0;
    if (ctx) {
      ctx.clearRect(0, 0, w, h);
      const bars = 48, barW = w / bars;
      ctx.fillStyle = "#16202b";
      ctx.fillRect(0, 0, w, h);
      ctx.fillStyle = "#4cc2ff";
      for (let i = 0; i < bars; i++) {
        const t = i / bars;
        const amp = S.levels.mic * (0.55 + 0.45 * Math.sin(t * Math.PI));
        const bh = Math.min(h, 6 + amp * (h - 12) * 1.6);
        ctx.fillRect(i * barW + 1, h - bh, barW - 2, bh);
      }
      ctx.fillStyle = "#7cf0c0";
      const aiH = Math.min(h, 4 + S.levels.ai * (h - 8) * 1.6);
      ctx.fillRect(0, 0, w, aiH);
    }
    $("micLevel").textContent = S.levels.mic.toFixed(2);
    $("aiLevel").textContent = S.levels.ai.toFixed(2);
  }
  function tick() {
    const read = (an) => {
      if (!an || !S.audioCtx) return 0;
      const buf = new Uint8Array(an.fftSize);
      try { an.getByteTimeDomainData(buf); } catch (e) { return 0; }
      let sum = 0;
      for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
      return Math.min(1, Math.sqrt(sum / buf.length) * 3.2);
    };
    S.levels.mic = read(S.micAnalyser);
    S.levels.ai = read(S.aiAnalyser);
    draw();
    requestAnimationFrame(tick);
  }

  // ---- API -----------------------------------------------------------------
  function apiUrl(path) { return path; }
  async function api(method, path, body) {
    const headers = {};
    if (CFG.token) headers["Authorization"] = "Bearer " + CFG.token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(apiUrl(path), {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401) throw new Error("인증 실패(401) — 토큰을 확인하세요 (?token=...)");
    const text = await res.text();
    let json = null;
    try { json = text ? JSON.parse(text) : null; } catch (e) { json = { raw: text }; }
    if (!res.ok) throw new Error((json && (json.detail || json.raw)) || ("HTTP " + res.status));
    return json;
  }

  // ---- 오디오 컨스트 / 분석기 --------------------------------------------
  async function unlockAudio() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      if (!S.audioCtx) S.audioCtx = new AC();
      if (S.audioCtx.state === "suspended") await S.audioCtx.resume();
      return S.audioCtx;
    } catch (e) { return null; }
  }
  function attachAnalyser(stream, which) {
    if (!S.audioCtx || !stream) return null;
    try {
      const src = S.audioCtx.createMediaStreamSource(stream);
      const an = S.audioCtx.createAnalyser();
      an.fftSize = 512;
      an.smoothingTimeConstant = 0.65;
      src.connect(an);          // 스피커로는 연결하지 않음(에코 방지)
      if (which === "mic") S.micAnalyser = an; else S.aiAnalyser = an;
      return an;
    } catch (e) { return null; }
  }

  // ---- WakeLock ------------------------------------------------------------
  async function requestWakeLock() {
    try {
      if (navigator.wakeLock && navigator.wakeLock.request) {
        S.wakeLock = await navigator.wakeLock.request("screen");
        if (S.wakeLock.addEventListener) {
          S.wakeLock.addEventListener("release", () => { S.wakeLock = null; });
        }
      }
    } catch (e) { /* 미지원/거부 — 통화 자체는 계속 */ }
  }
  function releaseWakeLock() {
    try { if (S.wakeLock) S.wakeLock.release(); } catch (e) {}
    S.wakeLock = null;
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && S.phase === "live" && !S.wakeLock) requestWakeLock();
  });

  // ---- DataChannel 이벤트 계약 --------------------------------------------
  function handleEvent(type, payload) {
    payload = payload || {};
    S.events += 1;
    S.lastEvent = type;
    $("eventCount").textContent = String(S.events);
    $("lastEvent").textContent = type;
    switch (type) {
      case "bridge_ready":
        $("modePill").textContent = payload.mode || "?";
        $("voicePill").textContent = payload.voice || "?";
        break;
      case "session_started":
        S.session = payload.session || S.session;
        S.startedAt = Date.now();
        $("modePill").textContent = payload.mode || "?";
        $("voicePill").textContent = payload.voice || "?";
        setPhase("live");
        setVad("듣는 중");
        sys("통화가 연결되었습니다. (세션 #" + S.session + ", voice=" + (payload.voice || "?") + ")");
        break;
      case "connected":
        setVad("듣는 중");
        break;
      case "speech_started":
        setVad("듣는 중");
        break;
      case "response_started":
        S.turns = payload.turn != null ? payload.turn : S.turns + 1;
        $("turnCount").textContent = String(S.turns);
        setVad("AI 응답 중");
        break;
      case "response_done":
        S.usage = payload.usage || S.usage;
        $("tokenUsage").textContent = String(S.usage.tokens_total || 0);
        setVad("듣는 중");
        S._lastAssistant = null;          // 다음 응답은 새 말풍선
        break;
      case "user_transcript":
        setVad("인식됨");
        addBubble("user", payload.text, "나");
        break;
      case "assistant_transcript":
        addBubble("assistant", payload.text, "Hermes");
        break;
      case "ambient_transcript":
        addBubble("ambient", payload.text, "주변음");
        break;
      case "tool_call":
        S._lastAssistant = null;
        addBubble("tool", "🛠 헤르메스 에이전트에 위임 중… " + (payload.task || ""), "에이전트");
        setVad("위임 중");
        break;
      case "tool_result":
        addBubble("tool", "✅ 에이전트 결과 수신 (" + (payload.chars || 0) + "자)", "에이전트");
        break;
      case "error":
        setVad("오류");
        banner("세션 오류: " + (payload.code || "") + " " + (payload.message || ""), true);
        break;
      case "disconnected":
        setVad("재연결 중");
        sys("세션 연결이 끊겨 자동 재연결을 시도합니다…");
        break;
      case "closed":
        sys("세션이 종료되었습니다.");
        if (S.phase === "live") setPhase("ended");
        break;
      case "peer_closed":
        banner("PeerConnection 종료: " + (payload.state || ""), true);
        teardown("peer closed");
        break;
      case "datachannel_closed":
        sys("이벤트 채널이 닫습니다.");
        break;
      case "ice_gather_timeout":
        banner("ICE 후보 수집 시간 초과 — NAT/Tailscale 경로를 확인하세요.", true);
        break;
      case "unsupported_track":
        sys("지원하지 않는 트랙: " + (payload.kind || ""));
        break;
      default:
        break;                              // 미지의 이벤트는 무시(전방 호환)
    }
  }

  async function pollStatus() {
    if (S.phase !== "live" && S.phase !== "connecting") return;
    try {
      const st = await api("GET", "/api/status");
      if (st.usage) {
        S.usage = st.usage;
        $("tokenUsage").textContent = String(st.usage.tokens_total || 0);
      }
      $("turnCount").textContent = String(st.turns || S.turns);
      $("uptime").textContent = Math.round(st.uptime_s || 0) + "s";
      if (st.adapter && st.adapter.track && st.adapter.track.pace_late_ms_max != null) {
        $("connInfo").textContent = "pc=" + (st.pc_state || "-") + " ice=" + (st.ice_state || "-");
      }
    } catch (e) { /* 상태 폴링 실패는 무시 */ }
  }

  // ---- 연결 ---------------------------------------------------------------
  async function iceServers() {
    try {
      const st = await api("GET", "/api/status");
      const list = (st.ice_servers || []).map((s) => {
        if (typeof s === "string") return { urls: [s] };
        if (s && s.urls) return s;
        return null;
      }).filter(Boolean);
      if (list.length) return list;
    } catch (e) { /* 폴백 사용 */ }
    return CFG.stunFallback;
  }

  async function connect() {
    if (S.phase === "connecting" || S.phase === "live") return;
    banner("");
    setPhase("connecting");
    S._lastAssistant = null;
    await unlockAudio();
    requestWakeLock();

    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error("이 브라우저는 getUserMedia를 지원하지 않습니다 (HTTPS 필요)");
      }
      S.micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
    } catch (err) {
      setPhase("error", "마이크 접근 실패: " + (err && err.message ? err.message : err) +
        " — HTTPS(또는 localhost)에서 실행하고 브라우저 마이크 권한을 허용하세요.");
      return;
    }

    try {
      attachAnalyser(S.micStream, "mic");
      const servers = await iceServers();
      S.iceServersUsed = servers;
      const pc = new RTCPeerConnection({ iceServers: servers });
      S.pc = pc;

      const dc = pc.createDataChannel("events");       // 브라우저가 이벤트 채널을 만든다
      S.dc = dc;
      dc.onopen = () => { sys("이벤트 채널 연결됨"); };
      dc.onclose = () => { sys("이벤트 채널 닫힘"); };
      dc.onmessage = (ev) => {
        let item = null;
        try { item = JSON.parse(ev.data); } catch (e) { return; }
        if (item && item.type) handleEvent(item.type, item.payload);
      };

      pc.oniceconnectionstatechange = () => {
        $("connInfo").textContent = "ice=" + pc.iceConnectionState;
        if (pc.iceConnectionState === "failed") banner("ICE 연결 실패 — 네트워크 경로 확인 필요", true);
      };
      pc.onconnectionstatechange = () => {
        $("connInfo").textContent = "pc=" + pc.connectionState;
        if (pc.connectionState === "failed" || pc.connectionState === "closed") {
          if (S.phase === "live") setPhase("ended");
        }
      };
      pc.ontrack = (ev) => {
        S.remoteStream = ev.streams && ev.streams[0] ? ev.streams[0] : null;
        const audio = $("remoteAudio");
        try {
          if (S.remoteStream) { audio.srcObject = S.remoteStream; }
          else { audio.srcObject = new MediaStream([ev.track]); S.remoteStream = audio.srcObject; }
          const p = audio.play();
          if (p && p.catch) p.catch(() => { banner("자동재생이 차단되었습니다 — 화면을 한 번 터치하세요.", true); });
        } catch (e) {}
        const west = S.remoteStream;
        if (west) attachAnalyser(west, "ai");
        if (ev.track && ev.track.addEventListener) {
          ev.track.addEventListener("ended", () => sys("AI 오디오 트랙 종료"));
        }
      };

      S.micTrack = S.micStream.getAudioTracks()[0];
      if (S.micTrack) {
        S.micTrack.enabled = !S.muted;
        pc.addTrack(S.micTrack, S.micStream);
      }

      const t0 = performance.now();
      await pc.setLocalDescription(await pc.createOffer());
      await waitIceGathering(pc);
      const answer = await api("POST", "/api/offer",
        { sdp: pc.localDescription.sdp, type: pc.localDescription.type || "offer" });
      if (!answer || !answer.sdp) throw new Error("서버가 유효한 SDP Answer를 반환하지 않았습니다");
      await pc.setRemoteDescription({ type: answer.type || "answer", sdp: answer.sdp });
      S.rttMs = Math.round(performance.now() - t0);
      $("rtt").textContent = S.rttMs + "ms";
      $("endpointInfo").textContent = "offer→answer " + S.rttMs + "ms · ice=" +
        (S.iceServersUsed || []).length + " server(s)";

      const deadline = Date.now() + CFG.connectTimeoutMs;
      while (pc.connectionState !== "connected" && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 120));
      }
      if (pc.connectionState !== "connected") {
        banner("PeerConnection 미연결 (state=" + pc.connectionState + ") — ICE/네트워크 확인", true);
      } else {
        setPhase("live");
        setVad("듣는 중");
      }
      if (!S.pollTimer) S.pollTimer = setInterval(pollStatus, CFG.statusPollMs);
    } catch (err) {
      setPhase("error", "통화 연결 실패: " + (err && err.message ? err.message : err));
      await teardown("connect failed");
    }
  }

  function waitIceGathering(pc, timeoutMs) {
    timeoutMs = timeoutMs || 5000;
    if (pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => { pc.removeEventListener("icegatheringstatechange", onChange); resolve(); };
      const onChange = () => { if (pc.iceGatheringState === "complete") done(); };
      pc.addEventListener("icegatheringstatechange", onChange);
      setTimeout(done, timeoutMs);
    });
  }

  async function teardown(reason) {
    releaseWakeLock();
    if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
    try { if (S.dc && S.dc.readyState === "open") S.dc.close(); } catch (e) {}
    try { if (S.pc) await S.pc.close(); } catch (e) {}
    try {
      if (S.micStream) S.micStream.getTracks().forEach((t) => t.stop());
    } catch (e) {}
    S.pc = null; S.dc = null; S.micStream = null; S.micTrack = null;
    S.micAnalyser = null; S.aiAnalyser = null;
    S.levels.mic = 0; S.levels.ai = 0;
    setVad("대기");
    if (S.phase !== "error") setPhase("ended");
    banner("");
  }

  async function hangup() {
    if (S.phase === "idle" || S.phase === "ended") { await teardown("idle"); return; }
    try { if (S.dc && S.dc.readyState === "open") S.dc.send(JSON.stringify({ cmd: "hangup" })); } catch (e) {}
    try { await api("POST", "/api/hangup", {}); } catch (e) {}
    await teardown("hangup");
    sys("통화를 종료했습니다.");
  }

  function sendText() {
    const input = $("textInput");
    const text = (input.value || "").trim();
    if (!text || !S.dc || S.dc.readyState !== "open") return;
    S.dc.send(JSON.stringify({ cmd: "text", text }));
    addBubble("user", text, "나 (타이핑)");
    input.value = "";
  }

  function toggleMute() {
    S.muted = !S.muted;
    if (S.micTrack) S.micTrack.enabled = !S.muted;
    const btn = $("muteBtn");
    btn.textContent = S.muted ? "음소거 해제" : "음소거";
    btn.className = S.muted ? "on" : "";
    renderStatePill();
  }

  // ---- 초기화 -------------------------------------------------------------
  function init() {
    $("startBtn").addEventListener("click", () => { unlockAudio(); connect(); });
    $("hangupBtn").addEventListener("click", hangup);
    $("muteBtn").addEventListener("click", toggleMute);
    $("sendBtn").addEventListener("click", sendText);
    $("textInput").addEventListener("keydown", (e) => { if (e.key === "Enter") sendText(); });
    $("tokenInput").value = CFG.token || "";
    $("tokenSave").addEventListener("click", () => {
      CFG.token = ($("tokenInput").value || "").trim();
      if (CFG.token) localStorage.setItem("bridge_token", CFG.token);
      else localStorage.removeItem("bridge_token");
      banner(CFG.token ? "토큰을 저장했습니다." : "토큰을 삭제했습니다.", false);
    });
    $("clearBtn").addEventListener("click", () => {
      const box = $("transcript");
      box.innerHTML = '<div id="empty">통화를 시작하면 실시간 자막이 여기에 표시됩니다.</div>';
      S._lastAssistant = null;
    });
    window.addEventListener("beforeunload", () => { try { hangup(); } catch (e) {} });

    if (!CFG.token) {
      banner("BRIDGE_AUTH_TOKEN이 설정된 서버라면 URL에 ?token=...  붙여 접속하세요.", false);
    }
    $("endpointInfo").textContent = "endpoint: " + location.origin;
    setPhase("idle");
    requestAnimationFrame(tick);
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      const secure = window.isSecureContext;
      if (!secure) banner("HTTPS가 아닙니다 — 모바일 브라우저는 마이크를 차단합니다 (Tailscale Serve 사용).", true);
    }
  }

  // 서버 이벤트 단위 테스트/자동화용 훅
  window.__bridge = {
    state: S, CFG, handleEvent, connect, hangup, teardown, sendText, toggleMute,
    addBubble, setPhase, els: { transcript: () => $("transcript") },
    bubbles: () => Array.from($("transcript").querySelectorAll(".bubble"))
      .map((el) => ({ kind: el.className.replace("bubble ", ""), text: el.textContent })),
    phase: () => S.phase,
    remoteStats: async () => {
      if (!S.pc || !S.pc.getStats) return null;
      const out = { inbound: {}, outbound: {} };
      const stats = await S.pc.getStats();
      stats.forEach((r) => {
        if (r.type === "inbound-rtp" && r.kind === "audio") {
          out.inbound = { bytesReceived: r.bytesReceived, packetsReceived: r.packetsReceived,
                          jitter: r.jitter, audioLevel: r.audioLevel };
        }
        if (r.type === "outbound-rtp" && r.kind === "audio") {
          out.outbound = { bytesSent: r.bytesSent, packetsSent: r.packetsSent };
        }
      });
      return out;
    },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
