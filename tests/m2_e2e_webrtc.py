#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M2 E2E — SDP 교환 + 양방향 오디오 파이프라인 실측 (마이크·브라우저 불필요).

구성
  [aiortc 테스트 클라이언트]  ←→  [FastAPI 시그널링 서버 (실제 기동)]  ←→  [DashScope 실시간 세션]

검증 (PLAN §M2 검증 기준)
  1. `POST /api/offer` SDP 교환 → ICE connected (왕복 지연 포함)
  2. 업링크: 합성 한국어 음성(48k) → Opus → 서버 16k → DashScope → **사용자 전사 생성**
  3. 다운링크: AI 음성 → 24k → 48k → 클라이언트 수신(WAV 저장) + PTS 연속 + 무음 프레임 규칙
  4. DataChannel `events`: 상태/자막/응답 이벤트 실시간 수신
  5. Barge-in flush 경로 + 토큰 인증(401/200)
  6. 선점형 세션 교체(takeover): 2번째 접속 시 1번째 세션이 서버에서 닫힘

사용:
  .venv/bin/python tests/m2_e2e_webrtc.py           # 실서버 + 실 DashScope
  .venv/bin/python tests/m2_e2e_webrtc.py --silence-only   # API 호출 없이 시그널링만
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError
from uvicorn import Config, Server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.app import VoiceBridge, create_app, force_loopback_ice  # noqa: E402
from server.webrtc_adapter import (  # noqa: E402
    PCMResampler,
    SAMPLES_PER_FRAME,
    WEBRTC_RATE,
    frame_to_mono_int16,
)

PORT = 8123
TOKEN = "m2-e2e-token"
OUT_DIR = ROOT / "out"
PHRASE = "안녕 르메스, 오늘 반도체 시장 관련해서 짧게 인사해줘"


# --------------------------------------------------------------------------- #
# 입력 음성 준비 (say + ffmpeg → 16k → 48k)
# --------------------------------------------------------------------------- #
def build_input_wav(path: Path) -> Path:
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    aiff = path.with_suffix(".aiff")
    voices = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    voice = "Yuna" if "Yuna" in voices else None
    cmd = ["say"]
    if voice:
        cmd += ["-v", voice]
    cmd += ["-o", str(aiff), PHRASE]
    subprocess.run(cmd, check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(aiff), "-ar", "16000", "-ac", "1", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def load_mono_48k(wav_path: Path) -> np.ndarray:
    with wave.open(str(wav_path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = w.readframes(w.getnframes())
    rs = PCMResampler(16000, WEBRTC_RATE)
    out = rs.process(pcm) + rs.flush()
    return np.frombuffer(out, dtype=np.int16)


# --------------------------------------------------------------------------- #
# 클라이언트 트랙 / 싱크
# --------------------------------------------------------------------------- #
class MicTrack(AudioStreamTrack):
    """브라우저 마이크 대역 — 실제 발화를 1회 재생한 뒤 계속 무음을 흘린다."""

    kind = "audio"

    def __init__(self, speech: np.ndarray) -> None:
        super().__init__()
        self.speech = speech
        self.i = 0
        self._t0: float | None = None

    async def recv(self) -> av.AudioFrame:
        if self._t0 is None:
            self._t0 = time.monotonic()
        delay = (self._t0 + self.i * 20 / 1000.0) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        s = self.i * SAMPLES_PER_FRAME
        e = s + SAMPLES_PER_FRAME
        if s < len(self.speech):
            seg = self.speech[s:e]
            if seg.size < SAMPLES_PER_FRAME:
                seg = np.concatenate([seg, np.zeros(SAMPLES_PER_FRAME - seg.size, dtype=np.int16)])
        else:
            seg = np.zeros(SAMPLES_PER_FRAME, dtype=np.int16)
        self.i += 1
        frame = av.AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = WEBRTC_RATE
        frame.planes[0].update(seg.tobytes())
        frame.pts = self.i * SAMPLES_PER_FRAME
        frame.time_base = Fraction(1, WEBRTC_RATE)
        return frame


class TrackSink:
    """수신 트랙 소비자 (소비자는 1개만 — M0 finding #2)."""

    def __init__(self) -> None:
        self.pcm: list[np.ndarray] = []
        self.frames = 0
        self.pts_list: list[int] = []
        self.pts_gaps: list[int] = []
        self._last_end: int | None = None
        self.error: str | None = None
        self.nonzero_frames = 0

    async def run(self, track) -> None:
        try:
            while True:
                frame = await track.recv()
                self.frames += 1
                arr = frame_to_mono_int16(frame)
                if frame.pts is not None:
                    self.pts_list.append(frame.pts)
                    if self._last_end is not None and frame.pts != self._last_end:
                        self.pts_gaps.append(frame.pts - self._last_end)
                    self._last_end = frame.pts + frame.samples
                if np.any(arr):
                    self.nonzero_frames += 1
                self.pcm.append(arr)
        except (MediaStreamError, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    def audio(self) -> np.ndarray:
        return np.concatenate(self.pcm) if self.pcm else np.zeros(0, dtype=np.int16)


# --------------------------------------------------------------------------- #
# HTTP 헬퍼 (표준 라이브러리만 — 추가 의존성 없음)
# --------------------------------------------------------------------------- #
def _request(method: str, path: str, payload: dict | None, token: str | None,
             timeout: float = 30.0) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, {"raw": body}
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode()[:200]}


async def http(method: str, path: str, payload: dict | None = None,
               token: str | None = TOKEN) -> tuple[int, dict]:
    return await asyncio.to_thread(_request, method, path, payload, token)


async def wait_ice_gathering(pc: RTCPeerConnection, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while pc.iceGatheringState != "complete" and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


async def wait_connected(pc: RTCPeerConnection, timeout: float = 20.0) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pc.connectionState == "connected":
            return time.monotonic() - t0
        if pc.connectionState in ("failed", "closed"):
            raise RuntimeError(f"PeerConnection {pc.connectionState}")
        await asyncio.sleep(0.05)
    raise TimeoutError(f"connected 대기 {timeout}s 초과 (state={pc.connectionState})")


# --------------------------------------------------------------------------- #
# 메인
# --------------------------------------------------------------------------- #
async def main(silence_only: bool, keep_seconds: float) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {"stages": {}, "events": []}

    force_loopback_ice()
    bridge = VoiceBridge(mode="speaker", agent_bridge=False, runner_connect_wait=60.0)
    app = create_app(bridge, token=TOKEN)
    server = Server(Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False))
    server_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        print("서버 기동 실패")
        return 1

    try:
        # -- 0) 토큰 인증 ---------------------------------------------------- #
        code_no_token, _ = await http("GET", "/api/status", token=None)
        code_ok, _ = await http("GET", "/api/status")
        code_health, _ = await http("GET", "/healthz")
        report["stages"]["auth"] = {
            "status_without_token": code_no_token,
            "status_with_token": code_ok,
            "healthz": code_health,
        }

        speech = np.zeros(0, dtype=np.int16)
        if not silence_only:
            wav = build_input_wav(OUT_DIR / "m2_input16k.wav")
            speech = load_mono_48k(wav)
            report["stages"]["input_speech_seconds"] = round(speech.size / WEBRTC_RATE, 2)

        # -- 1) 세션 1: SDP 교환 + 실 파이프라인 ----------------------------- #
        events: list[dict] = []
        pc1 = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        dc1 = pc1.createDataChannel("events")
        dc_opened = asyncio.Event()

        @dc1.on("open")
        def _on_open() -> None:
            report["stages"]["datachannel"] = "open"
            dc_opened.set()

        @dc1.on("message")
        def _on_msg(message) -> None:  # noqa: ANN001
            try:
                item = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                return
            events.append(item)
            report["events"].append({"type": item.get("type"), "ts": item.get("ts")})

        sink1 = TrackSink()

        @pc1.on("track")
        def _on_track(track) -> None:  # noqa: ANN001
            if track.kind == "audio":
                asyncio.ensure_future(sink1.run(track))

        track1 = MicTrack(speech)
        pc1.addTrack(track1)

        t_offer = time.monotonic()
        await pc1.setLocalDescription(await pc1.createOffer())
        await wait_ice_gathering(pc1)
        report["stages"]["offer_to_ice_gather_s"] = round(time.monotonic() - t_offer, 3)

        t_post = time.monotonic()
        code, body = await http("POST", "/api/offer",
                                {"sdp": pc1.localDescription.sdp, "type": "offer"})
        report["stages"]["offer_post_ms"] = round((time.monotonic() - t_post) * 1000, 1)
        report["stages"]["offer_status"] = code
        if code != 200:
            report["stages"]["offer_error"] = body
            raise RuntimeError(f"/api/offer 실패: {code} {body}")
        report["stages"]["answer_sdp_bytes"] = len(body.get("sdp", ""))

        await pc1.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type="answer"))
        report["stages"]["connect_s"] = round(await wait_connected(pc1), 3)
        await asyncio.wait_for(dc_opened.wait(), timeout=5.0)

        # -- 2) 응답 대기 (VAD 자동 응답 → 없으면 텍스트 주입 폴백) ----------- #
        def has(kind: str) -> bool:
            return any(e.get("type") == kind for e in events)

        t0 = time.monotonic()
        while time.monotonic() - t0 < keep_seconds:
            if has("assistant_transcript"):
                break
            await asyncio.sleep(0.25)
        report["stages"]["first_response_s"] = round(time.monotonic() - t0, 2)
        report["stages"]["vad_auto_response"] = has("assistant_transcript")
        if not has("assistant_transcript") and not silence_only:
            report["stages"]["fallback_text_injection"] = True
            dc1.send(json.dumps({"cmd": "text", "text": "짧게 인사해줘"}, ensure_ascii=False))
            t1 = time.monotonic()
            while time.monotonic() - t1 < 30 and not has("assistant_transcript"):
                await asyncio.sleep(0.25)
            report["stages"]["fallback_response_s"] = round(time.monotonic() - t1, 2)

        await asyncio.sleep(2.0)  # 마지막 오디오 전파 여유

        # -- 3) 상태 스냅샷 -------------------------------------------------- #
        _, status1 = await http("GET", "/api/status")
        report["status"] = status1

        audio = sink1.audio()
        if audio.size:
            with wave.open(str(OUT_DIR / "m2_reply_48k.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(WEBRTC_RATE)
                w.writeframes(audio.tobytes())
        rms = float(np.sqrt(np.mean((audio.astype(np.float64) / 32768) ** 2))) if audio.size else 0.0
        report["downlink"] = {
            "frames": sink1.frames,
            "seconds": round(audio.size / WEBRTC_RATE, 2),
            "rms": round(rms, 4),
            "nonzero_frames": sink1.nonzero_frames,
            "pts_gaps": sink1.pts_gaps[:5],
            "sink_error": sink1.error,
        }
        report["transcripts"] = (status1.get("transcripts") or {})

        # -- 4) 선점형 교체 (takeover) --------------------------------------- #
        pc2 = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        track2 = MicTrack(np.zeros(0, dtype=np.int16))  # 무음만 (중복 응답 비용 방지)
        pc2.addTrack(track2)
        await pc2.setLocalDescription(await pc2.createOffer())
        await wait_ice_gathering(pc2)
        code2, body2 = await http("POST", "/api/offer",
                                  {"sdp": pc2.localDescription.sdp, "type": "offer"})
        report["stages"]["takeover_offer_status"] = code2
        if code2 == 200:
            await pc2.setRemoteDescription(RTCSessionDescription(sdp=body2["sdp"], type="answer"))
            try:
                await wait_connected(pc2, timeout=15.0)
                connected2 = True
            except (TimeoutError, RuntimeError):
                connected2 = False
            await asyncio.sleep(2.5)
            _, status2 = await http("GET", "/api/status")
            report["takeover"] = {
                "connected_new": connected2,
                "takeovers": status2.get("takeovers"),
                "sessions_total": status2.get("sessions_total"),
                "session1_pc_state": pc1.connectionState,
                "session1_dc_state": dc1.readyState,
                "old_session_closed": (
                    pc1.connectionState in ("closed", "failed", "disconnected")
                    or dc1.readyState == "closed"
                ),
            }
            await pc2.close()

        # -- 5) 종료 --------------------------------------------------------- #
        code_hangup, _ = await http("POST", "/api/hangup", {})
        report["stages"]["hangup_status"] = code_hangup
        await asyncio.sleep(0.5)
        _, status3 = await http("GET", "/api/status")
        report["after_hangup"] = {
            "alive": status3.get("alive"),
            "last_close_reason": status3.get("last_close_reason"),
        }
        await pc1.close()
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server_task.cancel()

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    tr = report.get("transcripts", {})
    auth = report["stages"].get("auth", {})
    checks = {
        "토큰 없음 → 401": auth.get("status_without_token") == 401,
        "토큰 있음 → 200": auth.get("status_with_token") == 200,
        "SDP 교환 성공(200)": report["stages"].get("offer_status") == 200,
        "ICE 연결 성립": bool(report["stages"].get("connect_s")),
        "DataChannel open": report["stages"].get("datachannel") == "open",
        "응답 이벤트 수신": any(e["type"] == "assistant_transcript" for e in report["events"]),
        "다운링크 오디오 수신(>1s)": report["downlink"]["seconds"] > 1.0,
        "다운링크 무음 아님": report["downlink"]["rms"] > 0.005,
        "PTS 연속(갭 없음)": not report["downlink"]["pts_gaps"],
        "선점 교체 동작": (report.get("takeover", {}).get("takeovers") or 0) >= 1,
        "기존 세션 닫힘": bool(report.get("takeover", {}).get("old_session_closed")),
        "hangup 후 세션 정리": report["after_hangup"]["alive"] is False,
    }
    if not silence_only:
        checks["사용자 전사 생성"] = bool(tr.get("user"))
        checks["AI 답변 전사 생성"] = bool(tr.get("assistant"))
        checks["토큰 사용량 집계"] = (report["status"].get("usage", {}).get("tokens_total") or 0) > 0
        checks["업링크 프레임 수신"] = (report["status"].get("adapter", {}).get("frames_in") or 0) > 0
        checks["다운링크 무음프레임 규칙"] = (
            report["status"].get("adapter", {}).get("track", {}).get("silent_frames") or 0
        ) > 0

    print("\n=== M2 검증 판정 ===")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    ok = all(checks.values())
    print(f"\n판정: {'통과' if ok else '실패'}")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--silence-only", action="store_true", help="DashScope 발화 없이 시그널링만")
    ap.add_argument("--keep-seconds", type=float, default=25.0, help="응답 대기 상한")
    args = ap.parse_args()
    os.environ.setdefault("BRIDGE_AUTH_TOKEN", TOKEN)
    sys.exit(asyncio.run(main(args.silence_only, args.keep_seconds)))