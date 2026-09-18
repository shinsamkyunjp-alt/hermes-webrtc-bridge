#!/usr/bin/env node
/**
 * M3 프론트엔드 로직 검증 — jsdom + 모의 WebRTC (마이크·브라우저·서버 불필요)
 * ==========================================================================
 * `static/index.html` 의 인라인 스크립트를 실제 DOM 위에서 실행하고,
 * getUserMedia / RTCPeerConnection / DataChannel / fetch / WakeLock 을 모의해
 * 통화 시작 → 이벤트 수신 → 자막 렌더링 → 음소거 → 타이핑 → 종료 전 과정과
 * 오류 경로(마이크 거부)를 검증한다.
 *
 * 실행: node tests/m3_dom_test.mjs
 * 산출: out/m3_dom_report.json
 */
import { JSDOM, VirtualConsole } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const HTML = fs.readFileSync(path.join(ROOT, "static", "index.html"), "utf8");
const OUT = path.join(ROOT, "out");
fs.mkdirSync(OUT, { recursive: true });

const results = [];
const record = (name, pass, detail) => {
  results.push({ name, pass: !!pass, detail });
  console.log(`  ${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

// --------------------------------------------------------------------------- //
// 모의 객체
// --------------------------------------------------------------------------- //
class MockTrack {
  constructor(kind = "audio") { this.kind = kind; this.enabled = true; this.stopped = false; }
  stop() { this.stopped = true; }
  addEventListener() {}
}
class MockStream {
  constructor() { this.track = new MockTrack(); }
  getAudioTracks() { return [this.track]; }
  getTracks() { return [this.track]; }
}
class MockDataChannel {
  constructor(label) { this.label = label; this.readyState = "connecting"; this.sent = []; }
  send(data) { this.sent.push(data); }
  close() { this.readyState = "closed"; if (this.onclose) this.onclose(); }
  open() { this.readyState = "open"; if (this.onopen) this.onopen(); }
  deliver(type, payload) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify({ type, payload, ts: Date.now() / 1000 }) });
  }
}
class MockPC {
  constructor(cfg) {
    MockPC.instances.push(this);
    this.cfg = cfg;
    this.connectionState = "new";
    this.iceConnectionState = "new";
    this.iceGatheringState = "complete";
    this.dataChannels = [];
    this.addedTracks = [];
    this.closed = false;
    this.statsCalls = 0;
  }
  createDataChannel(label) { const dc = new MockDataChannel(label); this.dataChannels.push(dc); return dc; }
  addTrack(track, stream) { this.addedTracks.push({ track, stream }); }
  addEventListener() {}
  removeEventListener() {}
  async createOffer() { this.createOfferCalled = true; return { type: "offer", sdp: "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n" + (this.dataChannels.length ? "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n" : "") }; }
  async setLocalDescription(d) { this.localDescription = d; this.iceConnectionState = "checking"; this.connectionState = "connecting"; }
  async setRemoteDescription(d) { this.remoteDescription = d; this.connectionState = "connected"; this.iceConnectionState = "connected"; }
  async close() { this.closed = true; this.connectionState = "closed"; }
  async getStats() {
    this.statsCalls++;
    const rows = [
      { type: "inbound-rtp", kind: "audio", bytesReceived: 424242, packetsReceived: 1234, jitter: 0.004, audioLevel: 0.12 },
      { type: "outbound-rtp", kind: "audio", bytesSent: 99999, packetsSent: 555 },
    ];
    return { forEach: (cb) => rows.forEach(cb) };
  }
}
MockPC.instances = [];

class MockAnalyser {
  constructor() { this.fftSize = 512; this.smoothingTimeConstant = 0.6; }
  getByteTimeDomainData(buf) { for (let i = 0; i < buf.length; i++) buf[i] = 128; }
  connect() {}
}
class MockAudioContext {
  constructor() { this.state = "suspended"; this.resumed = false; }
  async resume() { this.state = "running"; this.resumed = true; }
  createAnalyser() { return new MockAnalyser(); }
  createMediaStreamSource() { return { connect() {} }; }
}

function makeEnv({ url = "http://127.0.0.1:8123/?token=test-token", micReject = false, status401 = false } = {}) {
  const calls = { fetch: [], getUserMedia: [], wakeLock: 0, wakeLockReleased: 0, constraints: null };
  const virtualConsole = new VirtualConsole(); // jsdom canvas/미구현 경고 소음 제거
  const dom = new JSDOM(HTML, {
    url,
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole,
    beforeParse(window) {
      window.RTCPeerConnection = MockPC;
      window.AudioContext = MockAudioContext;
      const mediaDevices = {
        async getUserMedia(constraints) {
          calls.constraints = constraints;
          calls.getUserMedia.push(constraints);
          if (micReject) { const e = new Error("Permission denied"); e.name = "NotAllowedError"; throw e; }
          return new MockStream();
        },
      };
      Object.defineProperty(window.navigator, "mediaDevices", { value: mediaDevices, configurable: true });
      Object.defineProperty(window.navigator, "wakeLock", {
        value: {
          async request(kind) {
            calls.wakeLock++;
            return { kind, addEventListener() {}, release() { calls.wakeLockReleased++; } };
          },
        },
        configurable: true,
      });
      window.fetch = async (u, opts) => {
        const opt = opts || {};
        calls.fetch.push({ url: String(u), method: opt.method || "GET", headers: opt.headers || {}, body: opt.body ? JSON.parse(opt.body) : null });
        const route = String(u);
        if (status401) {
          return { status: 401, ok: false, async text() { return JSON.stringify({ detail: "invalid or missing bridge token" }); } };
        }
        if (route.endsWith("/api/status")) {
          return { status: 200, ok: true, async text() { return JSON.stringify({
            alive: true, turns: 3, uptime_s: 12.0, pc_state: "connected", ice_state: "connected",
            usage: { tokens_total: 777 }, ice_servers: [{ urls: ["stun:stun.l.google.com:19302"] }],
          }); } };
        }
        if (route.endsWith("/api/offer")) {
          return { status: 200, ok: true, async text() { return JSON.stringify({ type: "answer", sdp: "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n" }); } };
        }
        if (route.endsWith("/api/hangup")) {
          return { status: 200, ok: true, async text() { return JSON.stringify({ ok: true }); } };
        }
        return { status: 404, ok: false, async text() { return JSON.stringify({ detail: "not found" }); } };
      };
      // window.performance 는 jsdom 기본 제공
    },
  });
  return { dom, calls };
}

const tick = (ms = 80) => new Promise((r) => setTimeout(r, ms));
const txt = (doc, id) => (doc.getElementById(id) ? doc.getElementById(id).textContent.trim() : null);
const bubbles = (doc) => Array.from(doc.querySelectorAll(".bubble")).map((el) => ({
  kind: el.className.replace("bubble ", "").trim(), text: el.textContent,
}));

// --------------------------------------------------------------------------- //
// 시나리오 1 — 초기 렌더 / 토큰 주입
// --------------------------------------------------------------------------- //
async function scenarioInit() {
  console.log("\n[1] 초기 렌더 및 토큰 주입");
  const { dom } = makeEnv();
  await tick();
  const doc = dom.window.document;
  record("초기 상태 라벨 = '대기'", txt(doc, "statePill") === "대기", txt(doc, "statePill"));
  record("통화 시작 버튼 활성", doc.getElementById("startBtn").disabled === false);
  record("통화 종료 버튼 비활성", doc.getElementById("hangupBtn").disabled === true);
  record("URL ?token= → localStorage 저장",
    dom.window.localStorage.getItem("bridge_token") === "test-token",
    String(dom.window.localStorage.getItem("bridge_token")));
  record("자막 비었을 때 안내문 노출", !!doc.getElementById("empty"));
  record("테스트 훅 window.__bridge 존재", typeof dom.window.__bridge === "object");
  dom.window.close();
}

// --------------------------------------------------------------------------- //
// 시나리오 2 — 통화 시작 (제약/협상 순서/인증/워크락)
// --------------------------------------------------------------------------- //
async function scenarioConnect() {
  console.log("\n[2] 통화 시작 — getUserMedia 제약, DataChannel 선생성, SDP 교환, WakeLock");
  MockPC.instances.length = 0;
  const { dom, calls } = makeEnv();
  await tick();
  const doc = dom.window.document;
  doc.getElementById("startBtn").click();
  await tick(200);

  const c = calls.constraints && calls.constraints.audio;
  record("getUserMedia audio.echoCancellation = true", c && c.echoCancellation === true, JSON.stringify(c));
  record("getUserMedia audio.noiseSuppression = true", c && c.noiseSuppression === true);
  record("getUserMedia audio.autoGainControl = true", c && c.autoGainControl === true);
  record("getUserMedia video 요청 없음", calls.constraints && calls.constraints.video === false);

  const pc = MockPC.instances[0];
  record("RTCPeerConnection 1회 생성", MockPC.instances.length === 1);
  record("DataChannel 'events' 생성", pc && pc.dataChannels[0] && pc.dataChannels[0].label === "events");
  const order = pc && pc.createOfferCalled ? "ok" : "no-offer";
  record("오퍼 SDP에 m=application 포함 (DC 선협상)", order === "ok" &&
    String(pc.localDescription.sdp).includes("m=application"));

  const offer = calls.fetch.find((f) => f.url.endsWith("/api/offer"));
  record("POST /api/offer 호출 + 마이크 트랙 등록", !!offer && pc.addedTracks.length === 1);
  record("Authorization: Bearer 토큰 헤더 전송",
    !!offer && offer.headers.Authorization === "Bearer test-token",
    offer ? String(offer.headers.Authorization) : "no-offer");
  record("Answer 적용 (remoteDescription=answer)",
    pc.remoteDescription && pc.remoteDescription.type === "answer");
  record("Screen WakeLock 요청", calls.wakeLock === 1, "wakeLock=" + calls.wakeLock);
  record("연결 후 상태 = 'live'", dom.window.__bridge.phase() === "live", dom.window.__bridge.phase());
  record("연결 후 종료 버튼 활성", doc.getElementById("hangupBtn").disabled === false);
  record("연결 RTT 표시", txt(doc, "rtt") !== "-", txt(doc, "rtt"));

  // --- 이벤트 계약 렌더링 ---
  const dc = pc.dataChannels[0];
  dc.open();
  await tick(20);
  dc.deliver("bridge_ready", { mode: "speaker", voice: "longanlingxin" });
  dc.deliver("session_started", { session: 1, mode: "speaker", voice: "longanlingxin" });
  dc.deliver("speech_started", {});
  dc.deliver("user_transcript", { text: "안녕 헤르메스야" });
  dc.deliver("response_started", { turn: 1 });
  dc.deliver("assistant_transcript", { text: "안녕하세요." });
  await tick(20);
  dc.deliver("assistant_transcript", { text: "안녕하세요. 무엇을 도와드릴까요?" });
  dc.deliver("response_done", { turn: 1, usage: { tokens_total: 636 } });
  dc.deliver("tool_call", { call_id: "c1", task: "오늘 반도체 뉴스 정리" });
  dc.deliver("tool_result", { call_id: "c1", chars: 812 });
  dc.deliver("ambient_transcript", { text: "주변 소리" });
  await tick(30);

  const b = bubbles(doc);
  record("mode/voice 필 표시", txt(doc, "modePill") === "speaker" && txt(doc, "voicePill") === "longanlingxin",
    txt(doc, "modePill") + "/" + txt(doc, "voicePill"));
  record("사용자 자막 말풍선 렌더", b.some((x) => x.kind === "user" && x.text.includes("안녕 헤르메스야")));
  const asst = b.filter((x) => x.kind === "assistant");
  record("AI 자막 스트리밍 교체(중복 말풍선 없음)", asst.length === 1 &&
    asst[0].text.includes("무엇을 도와드릴까요"), "assistant bubbles=" + asst.length);
  record("에이전트 위임/결과 말풍선", b.some((x) => x.kind === "tool" && x.text.includes("위임")) &&
    b.some((x) => x.kind === "tool" && x.text.includes("결과")), "tool=" + b.filter((x) => x.kind === "tool").length);
  record("주변음 자막 말풍선", b.some((x) => x.kind === "ambient"));
  record("토큰 사용량 표시(636)", txt(doc, "tokenUsage") === "636", txt(doc, "tokenUsage"));
  record("턴 카운터 = 1", txt(doc, "turnCount") === "1", txt(doc, "turnCount"));
  record("이벤트 카운터 집계", Number(txt(doc, "eventCount")) >= 10, txt(doc, "eventCount"));
  record("미지 이벤트 무시(크래시 없음)", (() => {
    dc.deliver("future_event_v9", { x: 1 }); return dom.window.__bridge.phase() === "live";
  })());

  // --- 상태 폴링 ---
  await tick(3200);
  record("/api/status 폴링 — 업타임/턴 반영", txt(doc, "uptime") !== "0s", "uptime=" + txt(doc, "uptime"));

  // --- stats 훅 ---
  const stats = await dom.window.__bridge.remoteStats();
  record("getStats 인바운드 오디오 집계",
    stats && stats.inbound.bytesReceived === 424242 && stats.outbound.packetsSent === 555,
    JSON.stringify(stats));

  // --- 음소거 ---
  doc.getElementById("muteBtn").click();
  await tick(20);
  record("음소거 — 마이크 트랙 enabled=false", pc.addedTracks[0].track.enabled === false);
  record("음소거 — 버튼 라 '음소거 해제'", txt(doc, "muteBtn") === "음소거 해제", txt(doc, "muteBtn"));
  record("음소거 — 상태 필 '통화 중 (음소거)'", txt(doc, "statePill") === "통화 중 (음소거)", txt(doc, "statePill"));
  doc.getElementById("muteBtn").click();
  await tick(20);
  record("음소거 해제 — 트랙 복귀", pc.addedTracks[0].track.enabled === true);

  // --- 타이핑 입력 ---
  doc.getElementById("textInput").value = "짧게 인사해줘";
  doc.getElementById("sendBtn").click();
  await tick(20);
  const sent = dc.sent.map((s) => JSON.parse(s));
  record("타이 입력 → DataChannel cmd=text",
    sent.some((m) => m.cmd === "text" && m.text === "짧게 인사해줘"), JSON.stringify(sent));
  record("타이 입력 말풍선 표시", bubbles(doc).some((x) => x.kind === "user" && x.text.includes("짧게 인사해줘")));

  // --- 종료 ---
  doc.getElementById("hangupBtn").click();
  await tick(200);
  const hang = calls.fetch.find((f) => f.url.endsWith("/api/hangup"));
  record("POST /api/hangup 호출", !!hang && hang.method === "POST");
  record("PeerConnection close 호출", pc.closed === true);
  record("마이크 트랙 stop 호출", pc.addedTracks[0].track.stopped === true);
  record("WakeLock 해제", calls.wakeLockReleased === 1, "released=" + calls.wakeLockReleased);
  record("종료 후 상태 = 'ended'", dom.window.__bridge.phase() === "ended", dom.window.__bridge.phase());
  record("종료 후 시작 버튼 재활성", doc.getElementById("startBtn").disabled === false);
  dom.window.close();
}

// --------------------------------------------------------------------------- //
// 시나리오 3 — 오류 경로 (마이크 거부 / 401)
// --------------------------------------------------------------------------- //
async function scenarioErrors() {
  console.log("\n[3] 오류 경로 — 마이크 거부, 인증 401");
  MockPC.instances.length = 0;
  const { dom } = makeEnv({ micReject: true });
  await tick();
  const doc = dom.window.document;
  doc.getElementById("startBtn").click();
  await tick(150);
  record("마이크 거부 → 상태 'error'", dom.window.__bridge.phase() === "error", dom.window.__bridge.phase());
  record("마이크 거부 → 배너에 안내문", (doc.getElementById("banner").textContent || "").includes("마이크 접근 실패"),
    (doc.getElementById("banner").textContent || "").slice(0, 48));
  record("마이크 거부 → PeerConnection 생성 안 함", MockPC.instances.length === 0);
  dom.window.close();

  // 401 응답 처리 (서버가 토큰을 요구하는데 잘못된 토큰을 든 경우)
  const { dom: dom2 } = makeEnv({ url: "http://127.0.0.1:8123/?token=wrong", status401: true });
  await tick();
  const win2 = dom2.window;
  win2.document.getElementById("startBtn").click();
  await tick(200);
  record("401 → 상태 'error' (인증 실패 배너)",
    win2.__bridge.phase() === "error" &&
    (win2.document.getElementById("banner").textContent || "").includes("인증"),
    "phase=" + win2.__bridge.phase() + " banner=" + (win2.document.getElementById("banner").textContent || "").slice(0, 80));
  dom2.window.close();
}

// --------------------------------------------------------------------------- //
// 시나리오 4 — 정적 계약(요소/기능 존재) 검사
// --------------------------------------------------------------------------- //
async function scenarioStatic() {
  console.log("\n[4] 정적 계약 — 필수 요소·모바일 기능");
  const checks = [
    ["viewport meta (모바일 스케일)", /name="viewport"[^>]*width=device-width/],
    ["safe-area 인셋 적용", /env\(safe-area-inset-top\)/],
    ["getUserMedia AEC/NS/AGC", /echoCancellation:\s*true[\s\S]*noiseSuppression:\s*true[\s\S]*autoGainControl:\s*true/],
    ["Screen WakeLock 요청", /wakeLock\.request\("screen"\)/],
    ["AudioContext unlock (resume)", /audioCtx\.resume\(\)/],
    ["DataChannel events 생성", /createDataChannel\("events"\)/],
    ["HTTPS(secure context) 경고", /isSecureContext/],
    ["외부 스크립트/스타일 의존 없음", null],
    ["자막 스크롤 영역", /-webkit-overflow-scrolling/],
    ["통화 종료 시 hangup API", /api\/hangup/],
  ];
  for (const [name, re] of checks) {
    if (re === null) {
      const external = /<script[^>]+src=|<link[^>]+href=["']https?:/i.test(HTML);
      record(name, !external, external ? "외부 리소스 발견" : "외부 리소스 0");
    } else {
      record(name, re.test(HTML));
    }
  }
  record("단일 파일(빌드 산출물 없음)", fs.existsSync(path.join(ROOT, "static", "index.html")));
}

// --------------------------------------------------------------------------- //
(async () => {
  console.log("=== M3 프론트엔드 DOM/로직 검증 (jsdom 30) ===");
  await scenarioInit();
  await scenarioConnect();
  await scenarioErrors();
  await scenarioStatic();
  const pass = results.filter((r) => r.pass).length;
  const fail = results.length - pass;
  console.log(`\n판정: ${fail === 0 ? "통과" : "실패"} — ${pass}/${results.length} PASS`);
  fs.writeFileSync(path.join(OUT, "m3_dom_report.json"),
    JSON.stringify({ total: results.length, pass, fail, results }, null, 2), "utf8");
  console.log("리포트: out/m3_dom_report.json");
  process.exit(fail === 0 ? 0 : 1);
})();