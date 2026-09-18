#!/usr/bin/env python
"""M0-2b: aiortc 오디오 루프백 검증 (마이크/스피커 장치 불필요).

두 개의 RTCPeerConnection 을 같은 프로세스 안에서 SDP 교환으로 연결하고
  pc1 --(Opus 48k 사인파 440Hz)--> pc2 --(수신 PCM 그대로 재송출)--> pc1
경로를 실시간으로 려 다음을 실측한다.

  1. 로컬 SDP Offer/Answer + ICE 연결 성립 시간 (M2 시그널링 설계 근거)
  2. 단방향 프레임 지연 (pc1 송신 -> pc2 수신), pts 정렬
  3. 왕복 지연 (pc1 -> pc2 -> pc1)
  4. 오디오 무결성: 수신 PCM 을 FFT 해서 440Hz 성분 확인 + WAV 저장
  5. 프레임 유실 여부 (pts 연속성)

사용: .venv/bin/python m0/loopback_test.py
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
import wave
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCConfiguration
from aiortc.mediastreams import MediaStreamError

RATE = 48000
FRAME_MS = 20
SAMPLES = RATE * FRAME_MS // 1000  # 960
TONE_HZ = 440.0
DURATION_S = 4.0
TOTAL_FRAMES = int(DURATION_S * 1000 / FRAME_MS)
OUT_DIR = Path(__file__).parent / "out"


def force_loopback_ice() -> None:
    """이 프로세스에서 LAN/tailnet 주소로 나가는 UDP 가 OS 단에서 드롭되는 문제 우회.

    실측(2026-09-14, macOS 26.6.2): 같은 호스트의 비-루프백 주소(192.168.45.167,
    100.126.185.45)로 보 UDP 가 조용히 사라진다 — macOS 로컬 네트워크 개인정보
    보호(macOS 15+) 로 에이전트가 운 python 프로세스가 차단된 상태.
      loopback 127.0.0.1: OK / LAN IP: DROP / tailnet IP: DROP
    aioice 는 기본적으로 모든 인터페이스 주소를 host candidate 로 수집하므로
    로컬 루프백 테스트가 ICE 'checking' 에서 춘다. 여기서는 127.0.0.1 만
    수집하도록 monkeypatch 해서 aiortc 파이프라인 자체를 검증한다.
    (브라우저 실연결 검증은 별도 — M2/M4 에서 로컬 네트워크 권한 승인 필요.)
    """
    import aioice.ice

    aioice.ice.get_host_addresses = (  # type: ignore[assignment]
        lambda use_ipv4, use_ipv6: ["127.0.0.1"] if use_ipv4 else ["::1"]
    )


def frame_to_mono_int16(frame: av.AudioFrame) -> np.ndarray:
    """aiortc 디코더 출력 프레임 -> mono int16 ndarray.

    ⚠️ 함정(실측): OpusDecoder 는 layout=stereo 로 디코드하므로
    `frame.to_ndarray()` 가 (channels, samples) 가 아니라 **packed interleaved**
    (1, samples*2) 를 돌려준다. 이걸 그대로 mono 로 읽으면 L/R 이 교차한 채
    절반 길이로 해석돼 주파수가 1/2 로 보인다(440Hz -> 220Hz 로 관측됨).
    반드시 reshape(-1, nch)[:, 0] 로 디인터리브 할 것.
    """
    arr = frame.to_ndarray()
    nch = frame.layout.nb_channels
    if nch > 1:
        if arr.ndim == 2 and arr.shape[0] == nch:  # planar
            arr = arr[0]
        else:  # packed interleaved
            arr = arr.reshape(-1, nch)[:, 0]
    else:
        arr = arr.reshape(-1)
    return np.frombuffer(np.ascontiguousarray(arr).tobytes(), dtype=np.int16)


def make_frame(i: int, freq: float = TONE_HZ) -> av.AudioFrame:
    t = (np.arange(SAMPLES) + i * SAMPLES) / RATE
    pcm = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    frame = av.AudioFrame(format="s16", layout="mono", samples=SAMPLES)
    frame.sample_rate = RATE
    frame.planes[0].update(pcm.tobytes())
    frame.pts = i * SAMPLES
    frame.time_base = Fraction(1, RATE)
    return frame


class ToneTrack(AudioStreamTrack):
    """pc1 송신 트랙 — 실시간 페이스로 사인파 프레임을 밀어낸다."""

    def __init__(self, total_frames: int = TOTAL_FRAMES) -> None:
        super().__init__()
        self.total_frames = total_frames
        self.i = 0
        self.sent_at: dict[int, float] = {}
        self._t0: float | None = None

    async def recv(self) -> av.AudioFrame:
        if self._t0 is None:
            self._t0 = time.monotonic()
        target = self._t0 + self.i * FRAME_MS / 1000
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        frame = make_frame(self.i)
        self.sent_at[frame.pts] = time.monotonic()
        self.i += 1
        return frame


class EchoTrack(AudioStreamTrack):
    """pc2 송신 트랙 — 수신한 (원본 pts, PCM) 을 그대로 되돌려보낸다.

    큐가 비면 무음 프레임을 내보낸다(PLAN §M2 무음 프레임 규칙의 축소판).
    ️ 실시간 페이스(20ms/프레임)를 반드시 유지할 것 — 페이스를 빼면
    aiortc 가 원하는 만큼 프레임을 뽑아가서 수천 개의 무음 프레임이 순식간에
    나간다(실측: 5초에 17390 프레임). pts 는 원본 값을 그대로 실어서
    왕복 지연을 pts 로 정렬할 수 있게 한다.
    """

    MAX_QUEUE = 10  # 지연 누적 방지: 밀린 프레임은 버린다

    def __init__(self) -> None:
        super().__init__()
        self.queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self.i = 0
        self.dropped = 0

    def put(self, pts: int, data: bytes) -> None:
        # 무음 프레임을 끼워지 않는다 — 큐에 프레임이 없으면 recv() 가 대기한다.
        # 그래야 에코 스트림이 다운링크와 1:1, 순서대로 미러링되어
        # 왕복 지연을 'k번째 프레임' 순서로 정렬할 수 있다.
        self.queue.put_nowait((pts, data))

    async def recv(self) -> av.AudioFrame:
        pts, data = await self.queue.get()
        frame = av.AudioFrame(format="s16", layout="mono", samples=SAMPLES)
        frame.sample_rate = RATE
        frame.planes[0].update(data)
        frame.pts = pts
        frame.time_base = Fraction(1, RATE)
        self.i += 1
        return frame


class Sink:
    """수신 트랙 소비자 — pts 별 도착시각과 PCM 을 모은다.

    ⚠️ 하나의 aiortc 트랙에는 소비자를 하나만 붙일 것. recv() 는 큐 pop 이므로
    두 태스크가 같은 트랙을 읽으면 프레임을 절반씩 나 갖는다(실측: 253 송신 중
    127 수신, pts 가 1920 씩 건너뜀 — 가짜 패킷 손실로 보임).
    따라서 여기서 한 번만 읽고 필요한 소비자에게 fan-out 한다.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.recv_at: dict[int, float] = {}
        self.order_pts: list[int | None] = []
        self.order_times: list[float] = []
        self.pcm: list[np.ndarray] = []
        self.gaps: list[int] = []
        self.last_pts: int | None = None
        self.error: str | None = None
        self.count = 0

    def push(self, frame: av.AudioFrame) -> None:
        self.count += 1
        now = time.monotonic()
        pts = frame.pts
        self.order_pts.append(pts)
        self.order_times.append(now)
        if pts is not None:
            self.recv_at[pts] = now
            if self.last_pts is not None and pts != self.last_pts + frame.samples:
                self.gaps.append(pts - (self.last_pts + frame.samples))
            self.last_pts = pts
        self.pcm.append(frame_to_mono_int16(frame))

    async def run(self, track, fanout=None) -> None:
        try:
            while True:
                frame = await track.recv()
                self.push(frame)
                if fanout is not None and frame.samples == SAMPLES:
                    fanout(frame.pts, frame_to_mono_int16(frame))
        except (MediaStreamError, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001 — 진단 목적
            self.error = f"{type(exc).__name__}: {exc}"


def stat(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    s = sorted(values)
    return {
        "n": len(s),
        "min_ms": round(min(s) * 1000, 2),
        "mean_ms": round(statistics.mean(s) * 1000, 2),
        "p50_ms": round(s[len(s) // 2] * 1000, 2),
        "p95_ms": round(s[int(0.95 * (len(s) - 1))] * 1000, 2),
        "max_ms": round(max(s) * 1000, 2),
    }


def wait_ice_complete(pc: RTCPeerConnection, timeout: float = 5.0) -> asyncio.Future:
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    @pc.on("icegatheringstatechange")
    def _on_state() -> None:
        if pc.iceGatheringState == "complete" and not done.done():
            done.set_result(None)

    if pc.iceGatheringState == "complete" and not done.done():
        done.set_result(None)
    return asyncio.ensure_future(asyncio.wait_for(done, timeout))


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {"stages": {}}

    force_loopback_ice()
    # 로컬 루프백이므로 STUN 도 불필요 (실서비스는 Google STUN 1개 + DERP 폴백)
    cfg = RTCConfiguration(iceServers=[])
    pc1 = RTCPeerConnection(cfg)
    pc2 = RTCPeerConnection(cfg)

    t_start = time.monotonic()

    tone = ToneTrack()
    pc1.addTrack(tone)
    echo = EchoTrack()
    pc2.addTrack(echo)

    sink_down = Sink("pc1->pc2")
    sink_up = Sink("pc2->pc1(echo)")

    def _enqueue(pts: int, arr: np.ndarray) -> None:
        echo.put(pts, arr.tobytes())

    @pc2.on("track")
    def _on_track(track) -> None:
        # 단일 소비자 + fan-out (에코 경로로 복사)
        asyncio.ensure_future(sink_down.run(track, fanout=_enqueue))

    @pc1.on("track")
    def _on_track_up(track) -> None:
        asyncio.ensure_future(sink_up.run(track))

    # --- 1) SDP 교환 ---
    gather1 = wait_ice_complete(pc1)
    gather2 = wait_ice_complete(pc2)
    t_offer = time.monotonic()
    offer = await pc1.createOffer()
    await pc1.setLocalDescription(offer)
    await gather1
    t_offer_done = time.monotonic()
    report["stages"]["offer+ice_gather_ms"] = round((t_offer_done - t_offer) * 1000, 2)
    report["sdp_offer_bytes"] = len(pc1.localDescription.sdp)

    t_answer = time.monotonic()
    await pc2.setRemoteDescription(pc1.localDescription)
    answer = await pc2.createAnswer()
    await pc2.setLocalDescription(answer)
    await gather2
    t_answer_done = time.monotonic()
    report["stages"]["answer+ice_gather_ms"] = round((t_answer_done - t_answer) * 1000, 2)
    report["sdp_answer_bytes"] = len(pc2.localDescription.sdp)

    await pc1.setRemoteDescription(pc2.localDescription)

    # --- 2) 연결 성립 대기 ---
    t_conn = time.monotonic()
    for _ in range(200):  # 최대 10s
        if pc1.connectionState == "connected" and pc2.connectionState == "connected":
            break
        await asyncio.sleep(0.05)
    report["stages"]["connect_ms_after_answer"] = round((time.monotonic() - t_conn) * 1000, 2)
    report["connection_state"] = {"pc1": pc1.connectionState, "pc2": pc2.connectionState}
    report["stages"]["total_setup_ms"] = round((time.monotonic() - t_start) * 1000, 2)

    if pc1.connectionState != "connected":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("\n판정: 실패 — PeerConnection 이 connected 상태가 되지 않음")
        await pc1.close()
        await pc2.close()
        return 1

    # --- 3) 스트리밍 ---
    deadline = time.monotonic() + DURATION_S + 1.0
    while tone.i < TOTAL_FRAMES and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    await asyncio.sleep(1.0)  # 마지막 프레임 전파 여유
    t_end = time.monotonic()

    await pc1.close()
    await pc2.close()

    # --- 4) 지연 분석 ---
    down_lat = [sink_down.recv_at[p] - tone.sent_at[p]
                for p in tone.sent_at if p in sink_down.recv_at]
    # 왕복: 에코 스트림은 다운링크와 순서가 1:1 이므로 k번째 프레임리 정렬한다.
    # (pts 로 정렬하면 안 된다 — 에코 송신에서 RTP 타임스탬프가 재작성되어 어긋난다.)
    n = min(len(sink_up.order_times), len(sink_down.order_pts))
    rtt = []
    for k in range(n):
        src_pts = sink_down.order_pts[k]
        if src_pts in tone.sent_at:
            rtt.append(sink_up.order_times[k] - tone.sent_at[src_pts])

    report["frames_sent"] = tone.i
    report["frames_recv_downlink"] = sink_down.count
    report["frames_recv_roundtrip"] = sink_up.count
    report["one_way_pc1_to_pc2"] = stat(down_lat)
    report["roundtrip_pc1_pc2_pc1"] = stat(rtt)
    report["pts_gaps_downlink"] = sink_down.gaps[:10]
    report["pts_gaps_roundtrip"] = sink_up.gaps[:10]
    report["bytes_per_20ms_frame"] = SAMPLES * 2

    # --- 5) 무결성: FFT 로 톤 확인 + WAV 저장 ---
    if sink_down.pcm:
        audio = np.concatenate(sink_down.pcm)
        wav_path = OUT_DIR / "loopback_received.wav"
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(audio.tobytes())
        report["downlink_samples"] = int(audio.size)
        report["downlink_seconds"] = round(audio.size / RATE, 2)
        if sink_up.pcm:
            audio_up = np.concatenate(sink_up.pcm)
            with wave.open(str(OUT_DIR / "loopback_roundtrip.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(audio_up.tobytes())
        # 440Hz 성분 검출 ( 1초 구간)
        seg = audio[: RATE]
        if seg.size > 1000:
            spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
            freqs = np.fft.rfftfreq(seg.size, 1 / RATE)
            report["fft_peak_hz"] = round(float(freqs[int(np.argmax(spec))]), 1)
            report["fft_target_hz"] = TONE_HZ
            report["rms"] = round(float(np.sqrt(np.mean((seg.astype(np.float64) / 32768) ** 2))), 4)
        report["wav"] = str(wav_path)

    report["errors"] = {"downlink": sink_down.error, "roundtrip": sink_up.error}
    report["wall_seconds"] = round(t_end - t_start, 2)

    print(json.dumps(report, ensure_ascii=False, indent=2))

    # --- 판정 ---
    checks = {
        "SDP 협상 성공": report["connection_state"]["pc1"] == "connected",
        "다운링크 프레임 수신(>=90%)": sink_down.count >= TOTAL_FRAMES * 0.9,
        "왕복 프레임 수신(>=80%)": sink_up.count >= TOTAL_FRAMES * 0.8,
        "PTS  없음": not sink_down.gaps,
        "440Hz 보존": abs(report.get("fft_peak_hz", 0) - TONE_HZ) < 5,
        "지연 수집": bool(down_lat) and bool(rtt),
    }
    print("\n=== 검증 판정 ===")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    ok = all(checks.values())
    print(f"\n판정: {'통과' if ok else '실패'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))