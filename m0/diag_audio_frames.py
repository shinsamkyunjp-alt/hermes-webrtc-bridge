#!/usr/bin/env python
"""M0 진단: pc1->pc2 수신 프레임의 pts/samples 전개와 실제 스펙트럼 확인.

loopback_test.py 가 이상 신호(프레임 수 불일치, FFT 피크 215Hz)를 냈을 때
원인을 좁히기 위한 프레임 레벨 계측기. 마이크 불필요.

사용: .venv/bin/python m0/diag_audio_frames.py
"""
from __future__ import annotations

import asyncio
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

sys.path.insert(0, str(Path(__file__).parent))
from loopback_test import SAMPLES, RATE, TONE_HZ, force_loopback_ice  # noqa: E402

OUT = Path(__file__).parent / "out"


class Tone(AudioStreamTrack):
    def __init__(self, n: int = 100) -> None:
        super().__init__()
        self.n = n
        self.i = 0
        self._t0: float | None = None
        self.sent: dict[int, float] = {}

    async def recv(self) -> av.AudioFrame:
        if self._t0 is None:
            self._t0 = time.monotonic()
        d = (self._t0 + self.i * 0.02) - time.monotonic()
        if d > 0:
            await asyncio.sleep(d)
        t = (np.arange(SAMPLES) + self.i * SAMPLES) / RATE
        pcm = (0.3 * np.sin(2 * np.pi * TONE_HZ * t) * 32767).astype(np.int16)
        f = av.AudioFrame(format="s16", layout="mono", samples=SAMPLES)
        f.sample_rate = RATE
        f.planes[0].update(pcm.tobytes())
        f.pts = self.i * SAMPLES
        f.time_base = Fraction(1, RATE)
        self.sent[f.pts] = time.monotonic()
        self.i += 1
        return f


async def main() -> int:
    force_loopback_ice()
    cfg = RTCConfiguration(iceServers=[])
    pc1, pc2 = RTCPeerConnection(cfg), RTCPeerConnection(cfg)
    tone = Tone(100)
    pc1.addTrack(tone)

    frames: list[tuple[int, int, float]] = []

    @pc2.on("track")
    def _on_track(track) -> None:
        async def run() -> None:
            try:
                while True:
                    fr = await track.recv()
                    arr = fr.to_ndarray()
                    if arr.ndim > 1:
                        arr = arr[0]
                    a = np.frombuffer(arr.tobytes(), dtype=np.int16).astype(np.float64) / 32768
                    frames.append((fr.pts, fr.samples, float(np.sqrt(np.mean(a**2)))))
            except (MediaStreamError, asyncio.CancelledError):
                pass

        asyncio.ensure_future(run())

    o = await pc1.createOffer()
    await pc1.setLocalDescription(o)
    while pc1.iceGatheringState != "complete":
        await asyncio.sleep(0.05)
    await pc2.setRemoteDescription(pc1.localDescription)
    a = await pc2.createAnswer()
    await pc2.setLocalDescription(a)
    while pc2.iceGatheringState != "complete":
        await asyncio.sleep(0.05)
    await pc1.setRemoteDescription(pc2.localDescription)

    t_end = time.monotonic() + 4.0
    while tone.i < 100 and time.monotonic() < t_end:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.6)
    await pc1.close()
    await pc2.close()

    print(f"송신 프레임 {tone.i}개 (각 {SAMPLES} samples @ {RATE}Hz)")
    print(f"수신 프레임 {len(frames)}개")
    print("\n앞 12개 수신 프레임 (pts, samples, rms):")
    for pts, smp, rms in frames[:12]:
        print(f"  pts={pts:8d}  samples={smp:5d}  rms={rms:.4f}")

    if frames:
        sizes = [s for _, s, _ in frames]
        print(f"\nsamples 분포: min={min(sizes)} max={max(sizes)} "
              f"unique={sorted(set(sizes))[:6]}")
        ptss = [p for p, _, _ in frames]
        steps = [b - a for a, b in zip(ptss, ptss[1:])]
        print(f"pts 증가폭: min={min(steps)} max={max(steps)} "
              f"unique={sorted(set(steps))[:6]}")
        total = sum(sizes)
        print(f"총 수신 샘플 {total} ({total/RATE:.2f}s) vs 송신 {tone.i*SAMPLES} "
              f"({tone.i*SAMPLES/RATE:.2f}s)")
        zero_ratio = sum(1 for _, _, r in frames if r < 1e-6) / len(frames)
        print(f"무음 프레임 비율: {zero_ratio:.2%}")

    # WAV 저장 + 스펙트럼
    allpcm: list[np.ndarray] = []
    print("\n(수신 PCM 재수집은 생략 — loopback_test.py 의 WAV 사용)")
    _ = allpcm
    wav = OUT / "loopback_received.wav"
    if wav.exists():
        with wave.open(str(wav), "rb") as w:
            raw = w.readframes(w.getnframes())
            sr = w.getframerate()
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768
        print(f"\nWAV: {wav}  {x.size} samples @ {sr}Hz ({x.size/sr:.2f}s)")
        seg = x[: sr]
        spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
        freqs = np.fft.rfftfreq(seg.size, 1 / sr)
        top = np.argsort(spec)[::-1][:5]
        print("상위 5개 주파수 성분 (앞 1초):")
        for idx in top:
            print(f"  {freqs[idx]:8.1f} Hz  mag={spec[idx]:.3g}")
        # 자기상관으로 주기 추정
        d = x[: sr] - np.mean(x[: sr])
        ac = np.correlate(d, d, mode="full")[d.size - 1:]
        ac[: int(sr / 2000)] = 0
        lag = int(np.argmax(ac[: sr // 100]))
        print(f"자기상관 주기 lag={lag} samples -> {sr/max(lag,1):.1f} Hz")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))