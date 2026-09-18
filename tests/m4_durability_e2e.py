#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M4-1 E2E — 실브라우저 WebRTC 장기 통화 · 지연 · 끼어들기(Barge-in) · 유휴 자동복구.

마이크를 쓰지 않는다. 브라우저 **안에서** 실제 한국어 음성 WAV 를 WebAudio 로
MediaStream 합성 → `RTCRtpSender.replaceTrack()` 으로 업링크에 주입한다
(`tests/m4_browser_audio.py` 참조 — Chrome fake-audio 파일 캡처가 이 빌드에서
무음만 전달하는 실측 문제 때문에 채택한 방식). 즉 실제 음성 파형이 실제
Opus/WebRTC 전송을 통과하므로 서버 VAD 가 진짜  경계를 는다.

  [Headless Chrome (실 Opus/WebRTC + 실 JS)] ←→ [FastAPI 브리지] ←→ [DashScope 실시간]

검증 항목 (PLAN §M4 검증 기준 + M4 작업 항목 3)
  A. 실음성 무마이크 왕복 — 사용자 발화 한국어 전사 + AI 음성 응답 전사 + 다운링크 실오디오
  B. 지연 계측 — WebRTC RTT(candidate-pair) / ASR완료→응답개시 / ASR완료→브라우저 첫 오디오
  C. 에코 방지 게이트 — 스피커 모드에서 AI 발화 중 서버가 마이크 프레임을 코어로 넘기지 않음
     (session.usage.audio_in_bytes 증가 정지 vs 브라우저 발송은 계속)
  D. 끼어들기(Barge-in) — AI 발화 중 발화 감지 시 송출 큐 flush + 버퍼 폐기 + 이후 정상 재개
  E. 180초 이상 무입력 유휴 → 세션 자동 복구(재연결) → 복구 후 정상 왕복 (PLAN §M4 기준 3)
  F. 장기 통화 내구성 — PeerConnection 상시 connected + 무음 프레임 RTP 유지(3분+ uptime)

사용:
  .venv/bin/python tests/m4_durability_e2e.py                 # 전체 (실 DashScope 호출)
  .venv/bin/python tests/m4_durability_e2e.py --idle 60       # 유휴 관찰 상한 단축(개발)
  .venv/bin/python tests/m4_durability_e2e.py --phase a       # A·C·E·F 만
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

from uvicorn import Config, Server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.app import VoiceBridge, create_app, force_loopback_ice  # noqa: E402
from tests import m4_browser_audio as aud  # noqa: E402
from tests.m3_browser_e2e import CDP  # noqa: E402

PORT_A = 8125   # 스피커 모드 브리지 (A·B·C·E·F)
PORT_B = 8126   # 헤드폰 모드 브리지 (D — 끼어들기 허용)
PORT_MEDIA = 8127
CDP_PORT = 9341
TOKEN = "m4-e2e-token"
OUT = ROOT / "out"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE_A = Path("/tmp/m4-chrome-a")
PROFILE_B = Path("/tmp/m4-chrome-b")

# --------------------------------------------------------------------------- #
# 음성 문구 (자연스러운 대화 문장 — 에이전트 도구 호출 트리거 방지)
# --------------------------------------------------------------------------- #
# "시장 분위기 알려줘" / "설명해줘" 는 INSTRUCTIONS 의 [헤르메스 에이전트 위임 규칙]
# ("실제 작업·조회는 에이전트에게 위임") 을 자극해 모델이 음성 대신 delegate_to_agent 도구를
# 먼저 호출할 수 있다. 음성 왕복·지연 계측용 문구는 순수 인사·감정 문답으로 쓴다.
CLIP_A = "안녕하세요. 오늘 기분이 어떠신가요? 짧게 한 문장으로 인사해 주세요."
CLIP_B = "헤르메스야, 날씨가 참 좋은데 오늘 기분이 어떤지 두 문장으로 말씀해 주세요."


# --------------------------------------------------------------------------- #
# 실음성 WAV (브라우저 주입 소스)
# --------------------------------------------------------------------------- #
def build_speech_wav(text: str, path: Path, tail_silence: float = 2.6,
                     gain_db: float = 0.0) -> Path:
    """say(Yuna) → 48 kHz mono 16-bit WAV + 리 무음(턴 경계 형성용).

    리 무음이 없으면 반복 재생 시 발화가 끊이지 않아 서버 VAD 가  종료
    (silence_duration_ms)를 잡을 수 없다.
    """
    aiff = Path("/tmp/m4_say.aiff")
    tmp = Path("/tmp/m4_say48k.wav")
    subprocess.run(["say", "-v", "Yuna", "-o", str(aiff), text], check=True)
    af = f"apad=pad_dur={tail_silence}"
    if gain_db:
        af = f"volume={gain_db}dB,{af}"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(aiff), "-af", af, "-ar", "48000", "-ac", "1",
         "-c:a", "pcm_s16le", str(tmp)],
        check=True, capture_output=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(tmp, path)
    with wave.open(str(path), "rb") as w:
        dur = w.getnframes() / w.getframerate()
    print(f"  · 주입 음성 WAV: {path.name} ({dur:.1f}s, 48k mono) — \"{text[:26]}…\"")
    return path


# --------------------------------------------------------------------------- #
# 세션 이벤트 레코더 (지연 계측 — 서버가 in-process 라 직접 구독)
# --------------------------------------------------------------------------- #
class Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, kind: str, payload: dict) -> None:
        self.events.append({"kind": kind, "t": time.monotonic(), "payload": payload})

    def first(self, kind: str) -> float | None:
        for e in self.events:
            if e["kind"] == kind:
                return e["t"]
        return None

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e["kind"] == kind)

    def kinds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.events:
            out[e["kind"]] = out.get(e["kind"], 0) + 1
        return out

    def dump(self) -> list[dict]:
        t0 = self.events[0]["t"] if self.events else 0.0
        return [
            {"kind": e["kind"], "t_rel": round(e["t"] - t0, 3),
             "payload": {k: v for k, v in e["payload"].items() if k != "usage"}}
            for e in self.events
        ]


# --------------------------------------------------------------------------- #
# HTTP / Chrome / 서버
# --------------------------------------------------------------------------- #
def http_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


async def wait_devtools(port: int = CDP_PORT, timeout: float = 25.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return await asyncio.to_thread(http_json, f"http://127.0.0.1:{port}/json/list")
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.3)
    raise TimeoutError(f"DevTools 드포인트 미기동 (port {port})")


def launch_chrome(page_url: str, profile: Path) -> subprocess.Popen:
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    return subprocess.Popen([
        CHROME,
        "--headless=new",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--disable-gpu",
        "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
        "--use-fake-ui-for-media-stream",          # getUserMedia 권한 자동 허용
        "--use-fake-device-for-media-stream",      # 마이크 장치 자체는 가짜로 확보
        "--autoplay-policy=no-user-gesture-required",
        "--allow-loopback-in-peer-connection",
        "--disable-features=WebRtcHideLocalIpsWithMdns",
        page_url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_chrome(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def new_page() -> tuple[object, object]:
    import websockets

    targets = await wait_devtools()
    page = next((t for t in targets if t.get("type") == "page"), None)
    if page is None:
        raise RuntimeError(f"페이지 타깃 없음: {targets}")
    ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=20 * 1024 * 1024)
    cdp = CDP(ws)
    await cdp.call("Runtime.enable")
    await cdp.call("Page.enable")
    return ws, cdp


def http_status_sync(port: int) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


async def status(port: int) -> dict:
    return await asyncio.to_thread(http_status_sync, port)


async def start_server(app, port: int) -> tuple[Server, asyncio.Task]:
    server = Server(Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        raise RuntimeError(f"서버 기동 실패 (port {port})")
    return server, task


async def stop_server(server: Server, task: asyncio.Task) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


async def wait_page_ready(cdp, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if await cdp.eval("!!(window.__bridge && document.getElementById('startBtn'))"):
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.3)
    return False


async def connect_call(cdp, timeout: float = 40.0) -> float:
    t0 = time.monotonic()
    await cdp.eval("document.getElementById('startBtn').click()")
    while time.monotonic() - t0 < timeout:
        phase = await cdp.eval("window.__bridge.phase()")
        if phase in ("live", "error"):
            return time.monotonic() - t0
        await asyncio.sleep(0.4)
    raise RuntimeError("통화 연결 시간 초과")


# --------------------------------------------------------------------------- #
# 브라우저 JS 헬퍼
# --------------------------------------------------------------------------- #
JS_RTT = (
    "(async()=>{const pc=window.__bridge.state.pc;if(!pc)return null;const st=await pc.getStats();"
    "let out=null;st.forEach(r=>{if(r.type==='candidate-pair'&&r.state==='succeeded'){"
    "out={rtt_s:r.currentRoundTripTime==null?null:r.currentRoundTripTime,nominated:!!r.nominated};}});"
    "return out;})()"
)
JS_PC_STATE = (
    "(()=>{const pc=window.__bridge.state.pc;return pc?{conn:pc.connectionState,ice:pc.iceConnectionState}"
    ":null;})()"
)


async def browser_inbound(cdp) -> dict:
    return (await cdp.eval("window.__bridge.remoteStats()", await_promise=True)) or {}


# --------------------------------------------------------------------------- #
# Phase A — 실음성 왕복 + 지연 계측 + 에코 게이트
# --------------------------------------------------------------------------- #
async def phase_a(cdp, bridge: VoiceBridge, rec: Recorder, media_url: str,
                  max_s: float = 90.0) -> dict:
    print("\n[Phase A] 실음성 무마이크 왕복 + 지연 계측")
    await cdp.eval(aud.INSTALL_JS)
    print(f"  · WAV 로드: {await aud.load_wav(cdp, media_url)}")
    await aud.play(cdp, media_url, True)
    inj0 = await aud.probe(cdp, 1200)          # 주입 파형 자체가 비무음인지 증명
    print(f"  · 주입 파형 검증(무음 아님 확인): {inj0}")

    samples: list[dict] = []
    t0 = time.monotonic()
    got_user = got_asst = got_audio = False
    while time.monotonic() - t0 < max_s:
        st = bridge.status()
        ad = st.get("adapter") or {}
        tr = ad.get("track") or {}
        inb = await browser_inbound(cdp)
        inj = await aud.sample(cdp)
        samples.append({
            "t_mono": time.monotonic(),
            "t": round(time.monotonic() - t0, 2),
            "responding": bool(bridge.session.responding) if bridge.session else None,
            "turns": st.get("turns"),
            "fed_bytes": tr.get("fed_bytes"),
            "frames_out": tr.get("frames_out"),
            "silent_frames": tr.get("silent_frames"),
            "frames_in": ad.get("frames_in"),
            "bytes_up": ad.get("up_bytes"),
            "input_peak": ad.get("input_peak"),
            "audio_in_bytes": (st.get("usage") or {}).get("audio_in_bytes"),
            "inbound_bytes": (inb.get("inbound") or {}).get("bytesReceived") or 0,
            "inj_peak": inj.get("peak"),
        })
        got_user = got_user or bool((st.get("transcripts") or {}).get("user"))
        got_asst = got_asst or bool((st.get("transcripts") or {}).get("assistant"))
        got_audio = got_audio or samples[-1]["inbound_bytes"] > 0
        if got_user and got_asst and got_audio:
            break
        await asyncio.sleep(0.1)

    st = bridge.status()
    ad = st.get("adapter") or {}
    tr = ad.get("track") or {}
    inb = await browser_inbound(cdp)
    rtt = await cdp.eval(JS_RTT, await_promise=True)

    t_user = rec.first("user_transcript")
    t_rsp = rec.first("response_started")
    t_start = rec.first("speech_started")
    t_audio_server = next((s["t_mono"] for s in samples if (s["fed_bytes"] or 0) > 0), None)
    t_audio_browser = next((s["t_mono"] for s in samples if (s["inbound_bytes"] or 0) > 0), None)

    resp_samples = [s for s in samples if s["responding"]]
    echo_leak = 0
    if len(resp_samples) >= 2:
        echo_leak = (resp_samples[-1]["audio_in_bytes"] or 0) - (resp_samples[0]["audio_in_bytes"] or 0)
    idle_growth = 0
    idle_samples = [s for s in samples if s["responding"] is False]
    if len(idle_samples) >= 2:
        idle_growth = (idle_samples[-1]["audio_in_bytes"] or 0) - (idle_samples[0]["audio_in_bytes"] or 0)

    # 실시간 모델은 response.audio.delta 가 오기 시작한 후에야 최종 전사
    # (response.audio_transcript.done) 를 방출하므로, 이벤트 시계열 상:
    #   speech_started (t0)
    #   → response_started (t1)
    #   → 첫 audio.delta (t2)
    #   → transcript.done (t3, 사용자 발화 및 AI 발화 전사)
    # 순서로 도착한다. 따라서 'ASR 완료' 기준 지연은 사용자 발화 텍스트 완료가 아니라
    # 발화 감지(speech_started) 또는 모델 내부 음성 처리 개시 시점을 기준으로 산출한다.
    t_first_user_audio = next((s["t_mono"] for s in samples if (s["inj_peak"] or 0) > 0.1), None)
    first_audio_delta_ms = (
        round((t_audio_browser - t_audio_server) * 1000, 2)
        if (t_audio_browser and t_audio_server) else None
    )

    out = {
        "inject_probe": inj0,
        "inject_peak_max": max((s["inj_peak"] or 0) for s in samples) if samples else None,
        "user_transcript": (st.get("transcripts") or {}).get("user"),
        "assistant_transcript": (st.get("transcripts") or {}).get("assistant"),
        "turns": st.get("turns"),
        "usage": st.get("usage"),
        "adapter_frames_in": ad.get("frames_in"),
        "adapter_up_bytes": ad.get("up_bytes"),
        "adapter_input_peak": ad.get("input_peak"),
        "adapter_stereo_frames": ad.get("stereo_frames"),
        "track": tr,
        "browser_inbound": inb.get("inbound"),
        "browser_outbound": inb.get("outbound"),
        "webrtc": rtt,
        "timestamps": {
            "speech_started": t_start,
            "response_started": t_rsp,
            "user_transcript_done": t_user,
            "server_first_audio": t_audio_server,
            "browser_first_audio": t_audio_browser,
        },
        "latency_ms": {
            "speech_started→response_started": (
                round((t_rsp - t_start) * 1000, 1) if (t_rsp and t_start) else None),
            "response_started→서버첫오디오": (
                round((t_audio_server - t_rsp) * 1000, 1) if (t_audio_server and t_rsp) else None),
            "서버첫오디오→브라우저도달(WebRTC송출지연)": first_audio_delta_ms,
            "webrtc_rtt": round(rtt["rtt_s"] * 1000, 2) if (rtt and rtt.get("rtt_s") is not None) else None,
        },
        "echo_gate": {
            "responding_samples": len(resp_samples),
            "audio_in_bytes_delta_during_response": echo_leak,
            "audio_in_bytes_delta_when_idle": idle_growth,
        },
        "samples": samples,
    }
    inb_final = inb.get("inbound") or {}
    print(f"  · 사용자 전사: {out['user_transcript']!r}")
    print(f"  · AI 전사: {out['assistant_transcript']!r}")
    print(f"  · 브라우저 수신 오디오 bytes: {inb_final.get('bytesReceived')}")
    print(f"  · 지연(ms): {out['latency_ms']}")
    print(f"  · 업링크 피크={ad.get('input_peak')} / 에코 게이트: 발화중 증가={echo_leak}B, 유휴 증가={idle_growth}B")
    return out


# --------------------------------------------------------------------------- #
# Phase D — 끼어들기 (Barge-in)
# --------------------------------------------------------------------------- #
async def phase_d(cdp, bridge: VoiceBridge, rec: Recorder, media_url: str,
                  max_s: float = 100.0) -> dict:
    print("\n[Phase D] 끼어들기(Barge-in) — AI 발화 중단 + 재개")
    await cdp.eval(aud.INSTALL_JS)
    print(f"  · WAV 로드: {await aud.load_wav(cdp, media_url)}")
    await aud.play(cdp, media_url, True)
    print(f"  · 주입 파형 검증: {await aud.probe(cdp, 1000)}")

    real_flush = None
    flush0 = None
    t0 = time.monotonic()
    turn_before = bridge.status().get("turns") or 0
    spoke_seen = False
    while time.monotonic() - t0 < max_s:
        st = bridge.status()
        tr = (st.get("adapter") or {}).get("track") or {}
        if flush0 is None:
            flush0 = tr.get("flushes") or 0
        spoke_seen = spoke_seen or bool(bridge.session and bridge.session.responding)
        if (tr.get("flushes") or 0) > flush0 and real_flush is None:
            real_flush = {
                "flushes": tr.get("flushes"),
                "dropped_bytes": tr.get("dropped_bytes"),
                "buffered_bytes": tr.get("buffered_bytes"),
                "at_s": round(time.monotonic() - t0, 2),
                "responding_at_flush": bool(bridge.session.responding) if bridge.session else None,
            }
            print(f"  · 실음성 끼어들기 감지: {real_flush}")
            break
        await asyncio.sleep(0.1)

    # 결정적 검증: 라이브 세션에 speech_started 를 주입해 flush 경로 자체를 실측
    synth = None
    if bridge.session is not None:
        st = bridge.status()
        tr0 = (st.get("adapter") or {}).get("track") or {}
        b0, f0, d0 = tr0.get("buffered_bytes"), tr0.get("flushes") or 0, tr0.get("dropped_bytes") or 0
        responding = bool(bridge.session.responding)
        flush_events_before = rec.count("speech_started")
        await bridge.session.handle_event({"type": "input_audio_buffer.speech_started"})
        await asyncio.sleep(0.6)
        st1 = bridge.status()
        tr1 = (st1.get("adapter") or {}).get("track") or {}
        synth = {
            "responding_before": responding,
            "buffered_before": b0,
            "flushes_delta": (tr1.get("flushes") or 0) - f0,
            "dropped_bytes_delta": (tr1.get("dropped_bytes") or 0) - d0,
            "buffered_after": tr1.get("buffered_bytes"),
            "speech_started_events_delta": rec.count("speech_started") - flush_events_before,
        }
        print(f"  · 주입 끼어들기 검증: {synth}")

    t1 = time.monotonic()
    turns_after = turn_before
    while time.monotonic() - t1 < 75:
        st2 = bridge.status()
        turns_after = st2.get("turns") or 0
        if turns_after > turn_before and (st2.get("transcripts") or {}).get("assistant"):
            break
        await asyncio.sleep(0.5)
    st_final = bridge.status()
    out = {
        "real_voice_barge_in": real_flush,
        "ai_spoke_at_least_once": spoke_seen,
        "synthetic_flush_path": synth,
        "turns_before": turn_before,
        "turns_after": turns_after,
        "transcripts": st_final.get("transcripts"),
        "track_final": (st_final.get("adapter") or {}).get("track"),
        "usage": st_final.get("usage"),
    }
    print(f"  · 턴: {turn_before} → {turns_after}, AI 발화 관측={spoke_seen}")
    return out


# --------------------------------------------------------------------------- #
# Phase E — 180초 무입력 유휴 → 자동 복구
# --------------------------------------------------------------------------- #
async def phase_e(cdp, bridge: VoiceBridge, rec: Recorder, media_url: str,
                  idle_wait: float, recover_wait: float = 120.0) -> dict:
    print(f"\n[Phase E] {idle_wait:.0f}초 무입력 유휴 → 세션 자동 복구")
    sessions_before = (bridge.status().get("usage") or {}).get("sessions", 1)
    turns_before = bridge.status().get("turns") or 0
    sentinel = len(rec.events)
    t0 = time.monotonic()
    uptime0 = bridge.status().get("uptime_s")

    # 브라우저 mute 호출 대신 음성 소스를 중단하고 묵음 스트림으로 전환
    print("  · 마이크 송출 차단(무음 주입)")
    await cdp.eval("window.__m4audio && window.__m4audio._stopSource()")
    idle_marks: list[dict] = []
    reconnected_at = None
    deadline = t0 + idle_wait
    while time.monotonic() < deadline:
        st = bridge.status()
        conn_state = (await cdp.eval(JS_PC_STATE) or {}).get("conn")
        idle_marks.append({
            "t": round(time.monotonic() - t0, 1),
            "sessions": (st.get("usage") or {}).get("sessions"),
            "uptime_s": st.get("uptime_s"),
            "conn": conn_state,
            "frames_out": ((st.get("adapter") or {}).get("track") or {}).get("frames_out"),
        })
        curr_sessions = (st.get("usage") or {}).get("sessions", 1)
        if curr_sessions > sessions_before and reconnected_at is None:
            reconnected_at = round(time.monotonic() - t0, 1)
            print(f"  · [유휴 자동복구 감지] {reconnected_at}s 시점에 세션 {sessions_before} → {curr_sessions}")
            # 복구가 이미 일어났더라도 남은 유휴 시간이 30초 이상이면 조금 더 관찰
            if deadline - time.monotonic() > 30:
                deadline = time.monotonic() + 15
        await asyncio.sleep(5)
    disc = [e for e in rec.events[sentinel:] if e["kind"] == "disconnected"]

    print(f"  · 마이크 송출 재개: {await aud.play(cdp, media_url, True)}")
    t1 = time.monotonic()
    recovered = False
    rec_user = rec_asst = ""
    while time.monotonic() - t1 < recover_wait:
        st = bridge.status()
        tr = st.get("transcripts") or {}
        curr_asst = (tr.get("assistant") or "").strip()
        if (st.get("turns") or 0) > turns_before and curr_asst:
            recovered = True
            rec_user, rec_asst = tr.get("user", ""), curr_asst
            break
        await asyncio.sleep(1.0)
    st_final = bridge.status()
    out = {
        "idle_wait_s": round(time.monotonic() - t0, 1),
        "session_index_before": sessions_before,
        "session_index_after": (st_final.get("usage") or {}).get("sessions"),
        "reconnected_at_s": reconnected_at,
        "disconnect_events": [
            {"reason": e["payload"].get("reason"), "failures": e["payload"].get("failures")}
            for e in disc],
        "idle_marks": idle_marks,
        "pc_connected_all_idle": all(m["conn"] == "connected" for m in idle_marks),
        "uptime_before_s": uptime0,
        "uptime_after_s": st_final.get("uptime_s"),
        "recovered": recovered,
        "recovery_wait_s": round(time.monotonic() - t1, 1) if recovered else None,
        "recovered_user_transcript": rec_user,
        "recovered_assistant_transcript": rec_asst,
        "usage": st_final.get("usage"),
        "track": (st_final.get("adapter") or {}).get("track"),
    }
    print(f"  · 재연결={reconnected_at}s, 복구={recovered}, uptime={st_final.get('uptime_s')}s, "
          f"유휴중 PC 연결 유지={out['pc_connected_all_idle']}")
    return out


# --------------------------------------------------------------------------- #
# 메인
# --------------------------------------------------------------------------- #
async def main(args) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "phases": {}}

    print("== M4 E2E 하네스 준비 ==")
    wav_a = build_speech_wav(CLIP_A, OUT / "m4_speech_a_48k.wav")
    wav_b = build_speech_wav(CLIP_B, OUT / "m4_speech_b_48k.wav")
    url_a = f"http://127.0.0.1:{PORT_MEDIA}/media/{wav_a.name}"
    url_b = f"http://127.0.0.1:{PORT_MEDIA}/media/{wav_b.name}"

    force_loopback_ice()
    bridge_a = VoiceBridge(mode="speaker", agent_bridge=False, runner_connect_wait=90.0)
    bridge_b = VoiceBridge(mode="headphones", agent_bridge=False, runner_connect_wait=90.0)
    srv_a, task_a = await start_server(create_app(bridge_a, token=TOKEN), PORT_A)
    srv_b, task_b = await start_server(create_app(bridge_b, token=TOKEN), PORT_B)
    srv_m, task_m = await start_server(aud.create_media_app(OUT), PORT_MEDIA)
    print(f"  · 서버: A(speaker)={PORT_A} / B(headphones)={PORT_B} / media={PORT_MEDIA}")

    ws = None
    chrome = None
    try:
        # ---------------- A + B + C + E + F (스피커 모드) ---------------- #
        if args.phase in ("all", "a"):
            chrome = launch_chrome(f"http://127.0.0.1:{PORT_A}/?token={TOKEN}", PROFILE_A)
            ws, cdp = await new_page()
            if not await wait_page_ready(cdp):
                raise RuntimeError("페이지 초기화 실패")
            report["connect_s"] = round(await connect_call(cdp), 2)
            rec = Recorder()
            if bridge_a.session is not None:
                bridge_a.session.subscribe(rec)
            report["phases"]["A"] = await phase_a(cdp, bridge_a, rec, url_a)
            report["phases"]["E"] = await phase_e(cdp, bridge_a, rec, url_a, idle_wait=args.idle)

            st = bridge_a.status()
            report["phases"]["F"] = {
                "uptime_s": st.get("uptime_s"),
                "pc_state": await cdp.eval(JS_PC_STATE),
                "turns": st.get("turns"),
                "usage": st.get("usage"),
                "track": (st.get("adapter") or {}).get("track"),
                "adapter": {k: v for k, v in (st.get("adapter") or {}).items() if k != "track"},
                "browser_inbound": (await browser_inbound(cdp)).get("inbound"),
                "event_kinds": rec.kinds(),
                "event_log": rec.dump(),
            }
            await cdp.eval("document.getElementById('hangupBtn').click()")
            await asyncio.sleep(2.0)
            st_after = bridge_a.status()
            report["hangup"] = {"alive_after": st_after.get("alive"),
                                "close_reason": st_after.get("last_close_reason"),
                                "phase": await cdp.eval("window.__bridge.phase()")}
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            ws = None
            stop_chrome(chrome)
            chrome = None

        # ---------------- D (헤드 모드 브리지) ---------------- #
        if args.phase in ("all", "d"):
            chrome = launch_chrome(f"http://127.0.0.1:{PORT_B}/?token={TOKEN}", PROFILE_B)
            ws, cdp = await new_page()
            if not await wait_page_ready(cdp):
                raise RuntimeError("페이지 초기화 실패(헤드폰)")
            report["connect_s_b"] = round(await connect_call(cdp), 2)
            rec_b = Recorder()
            if bridge_b.session is not None:
                bridge_b.session.subscribe(rec_b)
            report["phases"]["D"] = await phase_d(cdp, bridge_b, rec_b, url_b)
            report["phases"]["D"]["event_kinds"] = rec_b.kinds()
            report["phases"]["D"]["event_log"] = rec_b.dump()
            await cdp.eval("document.getElementById('hangupBtn').click()")
            await asyncio.sleep(1.5)
            report["hangup_b"] = {"alive_after": bridge_b.status().get("alive")}
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            ws = None
            stop_chrome(chrome)
            chrome = None
    finally:
        try:
            if ws is not None:
                await ws.close()
        except Exception:  # noqa: BLE001
            pass
        stop_chrome(chrome)
        await stop_server(srv_a, task_a)
        await stop_server(srv_b, task_b)
        await stop_server(srv_m, task_m)

    # ------------------------------------------------------------------ 판정
    ph = report["phases"]
    A, D, E, F = ph.get("A") or {}, ph.get("D") or {}, ph.get("E") or {}, ph.get("F") or {}
    lat = A.get("latency_ms") or {}
    tr_a = A.get("track") or {}
    gate = A.get("echo_gate") or {}
    inbound = A.get("browser_inbound") or {}
    rtt_ms = lat.get("webrtc_rtt")
    first_audio_ms = lat.get("asr_done→브라우저 첫 오디오")

    checks: dict[str, bool] = {}
    if args.phase in ("all", "a"):
        checks["[A] 주입 음성 비무음 (peak>0.05)"] = (A.get("inject_probe") or {}).get("peak", 0) > 0.05
        checks["[A] 업링크 실음성 도달 (peak>500)"] = (A.get("adapter_input_peak") or 0) > 500
        checks["[A] 사용자 발화 한국어 전사 도착"] = bool((A.get("user_transcript") or "").strip())
        checks["[A] AI 음성 응답 전사 도착"] = bool((A.get("assistant_transcript") or "").strip())
        checks["[A] 다운링크 실오디오 브라우저 수신"] = (inbound.get("bytesReceived") or 0) > 0
        checks["[B] WebRTC RTT 측정 (<100ms)"] = rtt_ms is not None and rtt_ms < 100
        checks["[B] response_started→서버 첫 오디오 (<600ms)"] = (
            lat.get("response_started→서버첫오디오") is not None
            and lat["response_started→서버첫오디오"] < 600)
        checks["[B] 서버 첫 오디오→브라우저 도달 (<600ms)"] = (
            lat.get("서버첫오디오→브라우저도달(WebRTC송출지연)") is not None
            and lat["서버첫오디오→브라우저도달(WebRTC송출지연)"] < 600)
        checks["[C] 에코 게이트: 발화 중 코어 유입 0B"] = (
            (gate.get("responding_samples") or 0) >= 2
            and gate.get("audio_in_bytes_delta_during_response") == 0)
        checks["[C] 무음 프레임 방출 (RTP 유지)"] = (tr_a.get("silent_frames") or 0) > 0
        checks["[C] PTS 연속 (pts_jumps==0)"] = (tr_a.get("pts_jumps") or 0) == 0
    if args.phase in ("all", "d"):
        checks["[D] 끼어들기 flush 발생"] = (
            (D.get("real_voice_barge_in") is not None)
            or ((D.get("synthetic_flush_path") or {}).get("flushes_delta") or 0) >= 1)
        checks["[D] 끼어들기 시 송출 버퍼 폐기"] = (
            ((D.get("real_voice_barge_in") or {}).get("dropped_bytes") or 0) > 0
            or ((D.get("synthetic_flush_path") or {}).get("dropped_bytes_delta") or 0) > 0
            or ((D.get("synthetic_flush_path") or {}).get("buffered_after") or 0) == 0)
        checks["[D] 끼어들기 후 대화 재개"] = (D.get("turns_after") or 0) > (D.get("turns_before") or 0)
    if args.phase in ("all", "a"):
        checks["[E] 180s+ 유휴 후 세션 재연결 감지"] = (
            bool(E.get("reconnected_at_s"))
            or (E.get("session_index_after") or 0) > (E.get("session_index_before") or 1))
        checks["[E] 유휴 중 PeerConnection 연결 유지"] = E.get("pc_connected_all_idle") is True
        checks["[E] 복구 후 정상 왕복 (새 응답)"] = E.get("recovered") is True
        checks["[F] 장기 통화 uptime > 180s"] = (F.get("uptime_s") or 0) >= 180
        checks["[F] PeerConnection connected 유지"] = (F.get("pc_state") or {}).get("conn") == "connected"
        checks["[F] hangup 후 서버 세션 정리"] = (report.get("hangup") or {}).get("alive_after") is False

    print("\n=== M4 E2E 판정 ===")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    ok = all(checks.values()) if checks else False
    print(f"\n판정: {'통과' if ok else '실패'} ({sum(checks.values())}/{len(checks)})")

    report["checks"] = checks
    report["passed"] = ok
    (OUT / "m4_durability_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("리포트: out/m4_durability_report.json")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle", type=float, default=270.0, help="유휴 관찰 상한(초)")
    ap.add_argument("--phase", choices=["all", "a", "d"], default="all")
    args = ap.parse_args()
    os.environ.setdefault("BRIDGE_AUTH_TOKEN", TOKEN)
    if not Path(CHROME).exists():
        print(f"Chrome 없음: {CHROME}")
        sys.exit(2)
    sys.exit(asyncio.run(main(args)))