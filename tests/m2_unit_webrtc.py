#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M2 단위 검증 — WebRTC 어댑터/송출 트랙 (마이크·네트워크·서버 불필요).

검증 항목 (PLAN §M2 검증 기준 + M0 실측 반영 5건)
  1. 무음 프레임 + PTS 규칙: 20ms/960샘플, pts 960 증가, time_base 1/48000, 실시간 페이스
  2. 다운링크 리샘플 무결성: 24k PCM → 48k, 440Hz 음정 보존, pts 연속
  3. Barge-in flush: 출 큐 즉시 비움 + 이후 무음 프레임
  4. 업링크 디인터리브: 스테레오 packed → 채널0 (피치 1/2 곡 없음)
  5. 업링크 다운스 보상: +3dB 게인 (M0 finding #4)
  6. 업링크 리샘플: 48k → 16k 비율/프라이밍 처리
  7. 어댑터 청크화: read_chunk()가 100ms(3200B) 단위로 올리고 트랙 종료 시 None

사용: .venv/bin/python -m unittest tests.m2_unit_webrtc -v
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from aiortc.mediastreams import MediaStreamError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.webrtc_adapter import (  # noqa: E402
    BYTES_PER_FRAME,
    DASHSCOPE_IN_RATE,
    DASHSCOPE_OUT_RATE,
    FRAME_MS,
    SAMPLES_PER_FRAME,
    UP_CHUNK_BYTES,
    WEBRTC_RATE,
    PCMResampler,
    QwenAudioTrack,
    WebRTCAudioAdapter,
    frame_to_mono_int16,
    gain_db,
)

TONE_HZ = 440.0


def tone(rate: int, samples: int, freq: float = TONE_HZ, amp: float = 0.3) -> np.ndarray:
    t = np.arange(samples) / rate
    return (amp * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)


def fft_peak_hz(pcm: np.ndarray, rate: int) -> float:
    seg = pcm[: min(len(pcm), rate)]
    spec = np.abs(np.fft.rfft(seg.astype(np.float64) * np.hanning(seg.size)))
    freqs = np.fft.rfftfreq(seg.size, 1 / rate)
    return float(freqs[int(np.argmax(spec))])


def mono_frame(pcm: np.ndarray, rate: int) -> av.AudioFrame:
    f = av.AudioFrame(format="s16", layout="mono", samples=len(pcm))
    f.sample_rate = rate
    f.planes[0].update(pcm.tobytes())
    return f


def stereo_interleaved_frame(left: np.ndarray, right: np.ndarray) -> av.AudioFrame:
    """packed interleaved stereo 프레임 (Opus 디코더 출력과 동일 형태)."""
    inter = np.empty(left.size * 2, dtype=np.int16)
    inter[0::2] = left
    inter[1::2] = right
    f = av.AudioFrame(format="s16", layout="stereo", samples=left.size)
    f.sample_rate = WEBRTC_RATE
    f.planes[0].update(inter.tobytes())
    return f


class FakeBrowserTrack:
    """브라우저 Opus 트랙 대역 — 스테레오 packed 프레임을 실시간 페이스로 공급."""

    kind = "audio"

    def __init__(self, pcm_left: np.ndarray, pcm_right: np.ndarray | None = None,
                 realtime: bool = False) -> None:
        self.left = pcm_left
        self.right = pcm_right if pcm_right is not None else np.zeros_like(pcm_left)
        self.realtime = realtime
        self.i = 0
        self._t0: float | None = None

    async def recv(self) -> av.AudioFrame:
        if self.i >= len(self.left) // SAMPLES_PER_FRAME:
            raise MediaStreamError()
        s = self.i * SAMPLES_PER_FRAME
        e = s + SAMPLES_PER_FRAME
        if self.realtime:
            if self._t0 is None:
                self._t0 = time.monotonic()
            delay = (self._t0 + self.i * FRAME_MS / 1000.0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        self.i += 1
        return stereo_interleaved_frame(self.left[s:e], self.right[s:e])


class TestQwenAudioTrack(unittest.TestCase):
    """1·2·3 — 송출 트랙: 무음 프레임/PTS/페이스, 리샘플 무결성, flush."""

    def test_silence_frames_pts_and_pacing(self) -> None:
        async def run() -> tuple[list[av.AudioFrame], float]:
            track = QwenAudioTrack()
            frames = []
            t0 = time.monotonic()
            for _ in range(25):  # 25 * 20ms = 0.5s
                frames.append(await track.recv())
            return frames, time.monotonic() - t0

        frames, elapsed = asyncio.run(run())
        self.assertEqual(len(frames), 25)
        for i, f in enumerate(frames):
            self.assertEqual(f.samples, SAMPLES_PER_FRAME, "20ms = 960 샘플")
            self.assertEqual(f.sample_rate, WEBRTC_RATE)
            self.assertEqual(f.pts, i * SAMPLES_PER_FRAME, "pts 는 960 증가")
            self.assertEqual(f.time_base, Fraction(1, WEBRTC_RATE), "time_base=1/48000")
            self.assertEqual(bytes(f.planes[0])[:BYTES_PER_FRAME], b"\x00" * BYTES_PER_FRAME,
                             "버퍼 비었을 때 0값 무음 프레임")
        # 실시간 페이스 (M0 finding #3): 페이스가 없으면 순식간에 방출된다
        self.assertGreater(elapsed, 0.40, f"페이스 미적용 의심 (elapsed={elapsed:.3f}s)")
        self.assertLess(elapsed, 0.75, f"과도한 지연 (elapsed={elapsed:.3f}s)")

    def test_downlink_resample_integrity(self) -> None:
        async def run() -> np.ndarray:
            # 1초 분량을 한 번에 넣으므로 지터 버퍼 상한을 넘긴다 → 상한을 넉넉히
            track = QwenAudioTrack(max_buffer_ms=1500)
            src = tone(DASHSCOPE_OUT_RATE, DASHSCOPE_OUT_RATE)  # 1초 440Hz @24k
            for s in range(0, len(src), 480):  # 20ms 청크
                track.feed(src[s : s + 480].tobytes())
            out = []
            while track.buffered_bytes >= BYTES_PER_FRAME:
                out.append(frame_to_mono_int16(await track.recv()))
            return np.concatenate(out)

        pcm = asyncio.run(run())
        self.assertGreater(pcm.size, WEBRTC_RATE * 0.9, "≈1초 분량이 나와야 한다")
        self.assertAlmostEqual(fft_peak_hz(pcm, WEBRTC_RATE), TONE_HZ, delta=3.0,
                               msg="24k→48k 리플 후에도 440Hz 보존")
        rms = float(np.sqrt(np.mean((pcm.astype(np.float64) / 32768) ** 2)))
        self.assertGreater(rms, 0.1, "무음이 아니어야 한다")

    def test_barge_in_flush(self) -> None:
        async def run() -> tuple[int, bytes]:
            track = QwenAudioTrack()
            track.feed(tone(DASHSCOPE_OUT_RATE, DASHSCOPE_OUT_RATE).tobytes())
            before = track.buffered_bytes
            track.flush()
            frame = await track.recv()
            return before, bytes(frame.planes[0])[:BYTES_PER_FRAME]

        before, pcm = asyncio.run(run())
        self.assertGreater(before, 0, "flush 전에는 재생 대기 데이터가 있어야 한다")
        self.assertEqual(pcm, b"\x00" * BYTES_PER_FRAME, "flush 직후 프레임은 무음")

    def test_buffer_overflow_drops_oldest(self) -> None:
        track = QwenAudioTrack(max_buffer_ms=100)  # 100ms = 9600 bytes
        track.feed(tone(DASHSCOPE_OUT_RATE, DASHSCOPE_OUT_RATE).tobytes())  # 1초
        self.assertLessEqual(track.buffered_bytes, 100 * WEBRTC_RATE // 1000 * 2)
        self.assertGreater(track.dropped_bytes, 0, "지연 누적 방지를 위해 오래된 것부터 폐기")


class TestPCMUtils(unittest.TestCase):
    """4·5·6 — 디인터리브/다운믹스 보상/리샘플 비율."""

    def test_deinterleave_stereo_keeps_left_pitch(self) -> None:
        left = tone(WEBRTC_RATE, SAMPLES_PER_FRAME)
        right = np.zeros_like(left)
        arr = frame_to_mono_int16(stereo_interleaved_frame(left, right))
        self.assertEqual(arr.size, SAMPLES_PER_FRAME, "packed 프레임을 절반 길이로 오독하면 안 된다")
        self.assertAlmostEqual(fft_peak_hz(arr, WEBRTC_RATE), TONE_HZ, delta=20.0,
                               msg="디인터리브 실패 시 피치가 1/2(220Hz)로 보인다")

    def test_downmix_compensation_gain(self) -> None:
        base = tone(WEBRTC_RATE, SAMPLES_PER_FRAME, amp=0.2)
        boosted = gain_db(base, 3.0)
        ratio = float(np.abs(boosted).max()) / float(np.abs(base).max())
        self.assertAlmostEqual(ratio, 10 ** (3.0 / 20.0), delta=0.05, msg="+3dB 보상")

    def test_uplink_resample_ratio_and_priming(self) -> None:
        rs = PCMResampler(WEBRTC_RATE, DASHSCOPE_IN_RATE)
        first = rs.process(tone(WEBRTC_RATE, SAMPLES_PER_FRAME).tobytes())
        second = rs.process(tone(WEBRTC_RATE, SAMPLES_PER_FRAME).tobytes())
        self.assertEqual(len(second) // 2, 320, "정상 상태에서는 48k 960 → 16k 320")
        self.assertLess(len(first) // 2, 320, "첫 프레임은 프라이밍 지연만큼 짧다")
        tail = rs.flush()
        self.assertEqual(len(first) // 2 + len(tail) // 2, 320, "flush 로 프라이밍 지연 보정")


class TestWebRTCAudioAdapter(unittest.TestCase):
    """7 — 업링크 청크화 + 트랙 종료 처리."""

    def test_read_chunk_packetisation_and_eof(self) -> None:
        async def run() -> tuple[list[bytes], int]:
            left = tone(WEBRTC_RATE, 20 * SAMPLES_PER_FRAME)  # 400ms (청크 3개 분량)
            adapter = WebRTCAudioAdapter(track_in=FakeBrowserTrack(left))
            await adapter.start()
            chunks = []
            while True:
                chunk = await adapter.read_chunk()
                if chunk is None:
                    break
                chunks.append(chunk)
            await adapter.stop()
            return chunks, adapter.stats()["frames_in"]

        chunks, frames_in = asyncio.run(run())
        self.assertEqual(frames_in, 20)
        self.assertGreaterEqual(len(chunks), 3, "400ms 입력 → 100ms 청크 3개 이상")
        for c in chunks:
            self.assertEqual(len(c), UP_CHUNK_BYTES, "업링크 청크는 100ms(3200B) 고정")

    def test_downlink_write_chunk_feeds_track(self) -> None:
        async def run() -> tuple[int, float]:
            track = QwenAudioTrack()
            adapter = WebRTCAudioAdapter(track_in=None, track_out=track)
            await adapter.write_chunk(tone(DASHSCOPE_OUT_RATE, 2400).tobytes())  # 100ms
            frame = await track.recv()
            await adapter.stop()
            return track.buffered_bytes, float(np.abs(frame_to_mono_int16(frame)).max())

        buffered, peak = asyncio.run(run())
        self.assertGreater(buffered, 0, "다운링크 PCM이 송출 트랙 버퍼로 들어가야 한다")
        self.assertGreater(peak, 0, "재생 프레임에 실제 오디오가 실려야 한다")

    def test_barge_in_event_flushes_track(self) -> None:
        async def run() -> int:
            track = QwenAudioTrack()
            adapter = WebRTCAudioAdapter(track_in=None, track_out=track)
            await adapter.write_chunk(tone(DASHSCOPE_OUT_RATE, 2400).tobytes())
            before = track.buffered_bytes
            adapter.on_session_event("speech_started", {})
            after = track.buffered_bytes
            await adapter.stop()
            self.assertGreater(before, 0)
            return after

        self.assertEqual(asyncio.run(run()), 0, "speech_started → 송출 큐 즉시 비움")


if __name__ == "__main__":
    unittest.main(verbosity=2)