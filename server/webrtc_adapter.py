# -*- coding: utf-8 -*-
"""Layer 2 (I/O) — WebRTC 오디오 어댑터 (PLAN §2.1 `WebRTCAudioAdapter`).

코어 세션은 여전히 I/O를 모른다. 이 어댑터가 `core.adapter.AudioIOAdapter`
계약을 구현해서 `core.pipeline.ConversationRunner`에 그대로 꽂힌다.

  업링크 : 브라우저 Opus 48k 트랙 → deinterleave(mono) → PyAV 16k → session.push_audio
  다운링크: session 24k PCM → PyAV 48k → `QwenAudioTrack`(20ms/960샘플, 실시간 페이스)

**M0 실측 반영 (PLAN §11)**
 1. `frame.to_ndarray()`는 스테레오에서 packed interleaved `(1, 2N)`을 돌려준다 →
    반드시 `reshape(-1, nch)[:, 0]` 디인터리브 (안 하면 피치가 1/2로 보인다).
 2. 하나의 `MediaStreamTrack` 소비자는 1개만 → 이 어댑터는 리더 태스크 1개만 둔다.
 3. 커스텀 송출 트랙은 실시간 페이스(20ms) 직접 유지 필수 (없으면 수천 프레임 주).
 4. 업링크 다운믹스 −3.01dB 손실 → 스테레오에서 채널0만 취할 때 +3dB 보정.
 5. PyAV 리샘플러 프라이밍 16플 지연 → 종료 시 `resample(None)` 플러시.
"""

from __future__ import annotations

import asyncio
import time
from fractions import Fraction
from typing import Any

import av
import numpy as np
from aiortc import AudioStreamTrack
from aiortc.mediastreams import MediaStreamError

from core.adapter import AudioIOAdapter

# --- 규격 상수 -------------------------------------------------------------- #
WEBRTC_RATE = 48000
FRAME_MS = 20
SAMPLES_PER_FRAME = WEBRTC_RATE * FRAME_MS // 1000  # 960
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2  # 1920
DASHSCOPE_IN_RATE = 16000
DASHSCOPE_OUT_RATE = 24000
UP_CHUNK_BYTES = 3200  # 100ms @16k — 기존 CLI와 동일 업링크 청크 규격
DOWNMIX_COMP_DB = 3.0  # M0 finding #4 (−3.01dB) 보정
DEFAULT_TARGET_BUFFER_MS = 500  # 일반 스트리밍 목표 버퍼 (지연 최소화)
DEFAULT_MAX_BUFFER_MS = 4000     # 적응형 상한 (DashScope 빠른 버스트 유입 시 확장 한도)


# --------------------------------------------------------------------------- #
# PCM 유틸 (마이크/네트워크 불필요 — 단위 테스트 대상)
# --------------------------------------------------------------------------- #
def frame_to_mono_int16(frame: av.AudioFrame) -> np.ndarray:
    """aiortc 디코더 출력 프레임 → mono int16 ndarray (M0 finding #1 디인터리브)."""
    arr = frame.to_ndarray()
    nch = frame.layout.nb_channels
    if nch > 1:
        if arr.ndim == 2 and arr.shape[0] == nch:  # planar (nch, samples)
            arr = arr[0]
        else:  # packed interleaved (1, samples*nch)
            arr = arr.reshape(-1, nch)[:, 0]
    else:
        arr = arr.reshape(-1)
    return np.frombuffer(np.ascontiguousarray(arr).tobytes(), dtype=np.int16)


def gain_db(arr: np.ndarray, db: float) -> np.ndarray:
    """int16 배열에 dB 게인 적용 (클리핑 포함)."""
    if db == 0.0:
        return arr
    out = arr.astype(np.float32) * (10.0 ** (db / 20.0))
    return np.clip(out, -32768, 32767).astype(np.int16)


def silence(bytes_len: int) -> bytes:
    """0으로 채워진 PCM (무음 프레임 — RTP 유지용)."""
    return b"\x00" * bytes_len


class PCMResampler:
    """PyAV `AudioResampler` (s16 mono) 스트리밍 퍼.

    프라이밍 16플 지연이 있으므로 세션 종료 시 `flush()`를 반드시 호출한다.
    """

    def __init__(self, in_rate: int, out_rate: int) -> None:
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._rs = av.AudioResampler(format="s16", layout="mono", rate=out_rate)
        self._pts = 0
        self.samples_in = 0
        self.samples_out = 0
        self.calls = 0

    @staticmethod
    def _pack(frames: list[av.AudioFrame]) -> bytes:
        out = bytearray()
        for f in frames:
            out.extend(bytes(f.planes[0])[: f.samples * 2])
        return bytes(out)

    def process(self, pcm: bytes) -> bytes:
        n = len(pcm) // 2
        if n <= 0:
            return b""
        frame = av.AudioFrame(format="s16", layout="mono", samples=n)
        frame.sample_rate = self.in_rate
        frame.planes[0].update(pcm[: n * 2])
        frame.pts = self._pts
        frame.time_base = Fraction(1, self.in_rate)
        frames = self._rs.resample(frame)
        self._pts += n
        self.samples_in += n
        self.calls += 1
        out = self._pack(frames)
        self.samples_out += len(out) // 2
        return out

    def flush(self) -> bytes:
        """프라이밍 지연으로 남은 샘플 방출 (스트림 종료 시 1회)."""
        try:
            frames = self._rs.resample(None)
        except Exception:  # noqa: BLE001 — 이미 닫힌 경우 등
            return b""
        out = self._pack(frames)
        self.samples_out += len(out) // 2
        return out


# --------------------------------------------------------------------------- #
# 송출 트랙 (DashScope → 브라우저)
# --------------------------------------------------------------------------- #
class QwenAudioTrack(AudioStreamTrack):
    """DashScope 24k PCM을 48k/20ms WebRTC 프레임으로 내보내는 커스텀 소스 트랙.

    PLAN §M2 필수 규칙:
      * `recv()`는 매 20ms(960 플) 프레임을 반환하고 `pts`는 960씩 증가,
        `time_base=1/48000`.
      * 버퍼 언더런(버스트 틈/유휴)에는 **0값 무음 프레임**을 방출해 RTP 스트림과
        재생 크를 유지한다.
      * 실시간 페이스를 직접 유지한다 (M0 finding #3 — 없으면 프레임 폭주).
      * `flush()`는 Barge-in(말 끊기) 시 출 큐를 즉시 비운다.
    """

    def __init__(
        self,
        max_buffer_ms: int = DEFAULT_MAX_BUFFER_MS,
        target_buffer_ms: int = DEFAULT_TARGET_BUFFER_MS,
        adaptive: bool = True,
    ) -> None:
        super().__init__()
        self._buf = bytearray()
        self._max_buffer_ms = max_buffer_ms
        self._target_buffer_ms = min(target_buffer_ms, max_buffer_ms)
        self._adaptive = adaptive
        self._current_max_bytes = max(1, (self._target_buffer_ms if adaptive else max_buffer_ms) * WEBRTC_RATE // 1000) * 2
        self._absolute_max_bytes = max(1, max_buffer_ms * WEBRTC_RATE // 1000) * 2
        self._resampler = PCMResampler(DASHSCOPE_OUT_RATE, WEBRTC_RATE)
        self._index = 0
        self._t0: float | None = None
        # 실제 재생 타임라인 훅 — recv() 가 '진짜 소리'를 내보낼 때 호출된다.
        # 에코 게이트(MicGate.note_play_end)를 이 시점에 갱신해야 재생 중 에코를 막는다.
        self.on_play: Any = None

        # 진단 카운터 (마이크 없이 검증 가능)
        self.frames_out = 0
        self.silent_frames = 0
        self.underruns = 0
        self.flushes = 0
        self.dropped_bytes = 0
        self.fed_bytes = 0
        self.resample_samples_out = 0
        self.pts_jumps = 0
        self.pace_late_ms_max = 0.0
        self._first_feed_time: float | None = None
        self._first_audio_frame_time: float | None = None
        self._peak_bytes = 0
        self._last_feed_time: float | None = None

    # -- 주입/제어 ---------------------------------------------------------- #
    def feed(self, pcm24k: bytes) -> None:
        """DashScope 24k PCM 청크를 48k로 변환해 지터 버퍼에 넣는다."""
        if not pcm24k:
            return
        now = time.monotonic()
        if self._first_feed_time is None:
            self._first_feed_time = now

        # 적응형 버퍼: 빠른 버스트 유입 시 상한 확장
        if self._adaptive:
            if self._last_feed_time is not None and (now - self._last_feed_time) < 0.10:
                growth = int(400 * WEBRTC_RATE // 1000 * 2)
                self._current_max_bytes = min(self._absolute_max_bytes, self._current_max_bytes + growth)
        self._last_feed_time = now

        self.fed_bytes += len(pcm24k)
        out = self._resampler.process(pcm24k)
        if not out:
            return
        self.resample_samples_out += len(out) // 2
        self._buf.extend(out)
        self._peak_bytes = max(self._peak_bytes, len(self._buf))

        limit = self._current_max_bytes if self._adaptive else self._absolute_max_bytes
        if len(self._buf) > limit:  # 지연 누적 방지: 오래된 것부터 폐기
            over = len(self._buf) - limit
            over -= over % 2
            del self._buf[:over]
            self.dropped_bytes += over

    def flush(self) -> None:
        """Barge-in — 재생 대기 중인 모든 오디오를 즉시 폐기 (로컬 play_q.clear() 계승)."""
        dropped = len(self._buf)
        self._buf.clear()
        self.flushes += 1
        if dropped:
            self.dropped_bytes += dropped
        if self._adaptive:
            self._current_max_bytes = max(1, self._target_buffer_ms * WEBRTC_RATE // 1000) * 2

    @property
    def buffered_bytes(self) -> int:
        return len(self._buf)

    # -- WebRTC 소스 -------------------------------------------------------- #
    async def recv(self) -> av.AudioFrame:
        if self._t0 is None:
            self._t0 = time.monotonic()
        target = self._t0 + self._index * FRAME_MS / 1000.0
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            self.pace_late_ms_max = max(self.pace_late_ms_max, -delay * 1000.0)

        if len(self._buf) >= BYTES_PER_FRAME:
            pcm = bytes(self._buf[:BYTES_PER_FRAME])
            del self._buf[:BYTES_PER_FRAME]
            if self._first_audio_frame_time is None:
                self._first_audio_frame_time = time.monotonic()
            if self.on_play is not None:  # 실제 송출 시점 → 에코 게이트 갱신
                try:
                    self.on_play()
                except Exception:  # noqa: BLE001
                    pass
            # 버퍼가 안정적으로 소진되면 점진적으로 타깃 버퍼 크기로 회귀
            if self._adaptive and self._current_max_bytes > (self._target_buffer_ms * WEBRTC_RATE // 1000 * 2):
                target_bytes = self._target_buffer_ms * WEBRTC_RATE // 1000 * 2
                if len(self._buf) <= target_bytes:
                    drain_step = int(20 * WEBRTC_RATE // 1000 * 2)
                    self._current_max_bytes = max(target_bytes, self._current_max_bytes - drain_step)
        else:
            pcm = silence(BYTES_PER_FRAME)
            self.silent_frames += 1
            self.underruns += 1
            if self._adaptive:
                self._current_max_bytes = max(1, self._target_buffer_ms * WEBRTC_RATE // 1000) * 2

        frame = av.AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = WEBRTC_RATE
        frame.planes[0].update(pcm)
        pts = self._index * SAMPLES_PER_FRAME
        frame.pts = pts
        frame.time_base = Fraction(1, WEBRTC_RATE)
        self._index += 1
        self.frames_out += 1
        return frame

    def stats(self) -> dict[str, Any]:
        first_audio_delay_ms = None
        if self._first_feed_time is not None and self._first_audio_frame_time is not None:
            first_audio_delay_ms = round((self._first_audio_frame_time - self._first_feed_time) * 1000, 1)
        completion_rate = round(
            (self.fed_bytes - self.dropped_bytes) / max(1, self.fed_bytes) * 100, 1
        )
        return {
            "frames_out": self.frames_out,
            "silent_frames": self.silent_frames,
            "underruns": self.underruns,
            "flushes": self.flushes,
            "fed_bytes": self.fed_bytes,
            "resample_samples_out": self.resample_samples_out,
            "dropped_bytes": self.dropped_bytes,
            "buffered_bytes": len(self._buf),
            "current_buffer_ms": round(len(self._buf) / (WEBRTC_RATE * 2) * 1000, 1),
            "peak_buffer_ms": round(self._peak_bytes / (WEBRTC_RATE * 2) * 1000, 1),
            "first_audio_delay_ms": first_audio_delay_ms,
            "completion_rate": completion_rate,
            "pts_jumps": self.pts_jumps,
            "sample_rate": WEBRTC_RATE,
            "frame_samples": SAMPLES_PER_FRAME,
            "pace_late_ms_max": round(self.pace_late_ms_max, 2),
        }


# --------------------------------------------------------------------------- #
# 어댑터 (브라우저 ⇄ 코어)
# --------------------------------------------------------------------------- #
class WebRTCAudioAdapter(AudioIOAdapter):
    """브라우저 트랙 ⇄ 코어 세션 오디오 어댑터.

    * `attach_input_track(track)`  : 브라우저가 보낸 마이크 트랙 (pc.on("track"))
    * `attach_output_track(track)` : AI 음성을 보낼 송출 트랙 (pc.addTrack)
    * `read_chunk()`  → 16k PCM16 100ms(3200B) 청크, 트랙 종료 시 None
    * `write_chunk()` → 24k PCM16 청크를 송출 트랙 지터 버퍼로

    트랙이 아직 안 붙었어도 `start()`는 즉시 반환한다 (리더 태스크가 대기).
    """

    def __init__(
        self,
        *,
        track_in: Any | None = None,
        track_out: QwenAudioTrack | None = None,
        chunk_bytes: int = UP_CHUNK_BYTES,
        queue_maxsize: int = 200,
        downmix_comp_db: float = DOWNMIX_COMP_DB,
        input_buffer_max: int = 50,
    ) -> None:
        self._track_in = track_in
        self.track_out = track_out if track_out is not None else QwenAudioTrack()
        self.chunk_bytes = chunk_bytes
        self.queue_maxsize = queue_maxsize
        self.downmix_comp_db = downmix_comp_db
        self.input_buffer_max = input_buffer_max

        self._q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_maxsize)
        self._in_ready = asyncio.Event()
        self._reader_task: asyncio.Task | None = None
        self._resampler = PCMResampler(WEBRTC_RATE, DASHSCOPE_IN_RATE)
        self._pending = bytearray()
        self._stopping = False

        # 진단 카운터
        self.frames_in = 0
        self.chunks_up = 0
        self.bytes_up = 0
        self.track_ended = False
        self.reader_error: str | None = None
        self.input_peak = 0.0
        # 레벨 진단용 — ASR 언어 혼동은 과레벨(포화)·저레벨 양쪽에서 발생한다.
        # RMS 평균과 클리핑 비율을 함께 남겨야 게인을 객관적으로 정할 수 있다.
        self._rms_sum = 0.0
        self._rms_n = 0
        self.input_clip_pct = 0.0
        # 업링크 최근 구간 보존 (16k mono) — 통화 후 WAV로 덤프해 ASR 오전사 원인을
        # 마이크 없이 재현/분석하기 위한 것. 20초 = 16000*2*20 바이트.
        self._up_tail = bytearray()
        self._up_tail_max = DASHSCOPE_IN_RATE * 2 * 20
        self.stereo_frames = 0
        self.mono_frames = 0

    # -- 트랙 부착 ---------------------------------------------------------- #
    def attach_input_track(self, track: Any) -> None:
        """브라우저 마이크 트랙 등록 (소비자는 이 어댑터 1개만 — M0 finding #2)."""
        self._track_in = track
        self._in_ready.set()

    def attach_output_track(self, track: QwenAudioTrack) -> None:
        self.track_out = track

    async def wait_input_track(self, timeout: float = 15.0) -> Any:
        await asyncio.wait_for(self._in_ready.wait(), timeout=timeout)
        return self._track_in

    # -- 세션 이벤트 연동 (Barge-in) --------------------------------------- #
    def on_session_event(self, kind: str, payload: dict) -> None:
        """세션 구독 콜백 — 끼어들기/오류 시 송출 버퍼를 즉시 비운다 (PLAN §M2)."""
        if kind in ("speech_started", "disconnected", "closed"):
            self.track_out.flush()

    # -- AudioIOAdapter ----------------------------------------------------- #
    async def start(self) -> None:
        self._stopping = False
        if self._track_in is not None:
            self._in_ready.set()
        self._reader_task = asyncio.create_task(self._reader(), name="webrtc-reader")

    async def stop(self) -> None:
        self._stopping = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        tail = self._resampler.flush()  # 프라이밍 지연 방출
        if tail:
            self._pending.extend(tail)

    async def read_chunk(self) -> bytes | None:
        return await self._q.get()

    async def write_chunk(self, chunk: bytes) -> None:
        self.track_out.feed(chunk)

    # -- 내부 --------------------------------------------------------------- #
    async def _reader(self) -> None:
        """브라우저 트랙 → 48k→16k → 3200B(100ms) 청크  (단일 소비자)."""
        try:
            track = await self.wait_input_track()
        except asyncio.TimeoutError:
            self.reader_error = "브라우저 오디오 트랙이 15초 안에 도착하지 않았습니다"
            self._put(None)
            return
        try:
            while True:
                frame = await track.recv()
                self.frames_in += 1
                nch = frame.layout.nb_channels
                if nch > 1:
                    self.stereo_frames += 1
                else:
                    self.mono_frames += 1
                arr = frame_to_mono_int16(frame)
                if nch > 1:
                    arr = gain_db(arr, self.downmix_comp_db)  # −3.01dB 업믹스 손실 보정
                if arr.size:
                    a = np.abs(arr)
                    peak = float(a.max())
                    self.input_peak = max(self.input_peak, peak)
                    self._rms_sum += float(np.sqrt(np.mean(np.square(arr.astype(np.float32)))))
                    self._rms_n += 1
                    self.input_clip_pct = max(
                        self.input_clip_pct, float((a >= 32700).mean() * 100.0)
                    )
                pcm16k = self._resampler.process(arr.tobytes())
                if not pcm16k:
                    continue
                self._pending.extend(pcm16k)
                if len(self._pending) > self.input_buffer_max * self.chunk_bytes:
                    del self._pending[: len(self._pending) - self.input_buffer_max * self.chunk_bytes]
                while len(self._pending) >= self.chunk_bytes:
                    chunk = bytes(self._pending[: self.chunk_bytes])
                    del self._pending[: self.chunk_bytes]
                    self.chunks_up += 1
                    self.bytes_up += len(chunk)
                    self._up_tail.extend(chunk)
                    if len(self._up_tail) > self._up_tail_max:
                        del self._up_tail[: len(self._up_tail) - self._up_tail_max]
                    self._put(chunk)
        except (MediaStreamError, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001 — 진단 목적
            self.reader_error = f"{type(exc).__name__}: {exc}"
        finally:
            self.track_ended = True
            if not self._stopping:
                self._put(None)  # 입력 종료 → 파이프라인 정상 종료

    def dump_wav(self, path: Any) -> str | None:
        """업링크 최근 구간(16k mono)을 WAV로 저장 — ASR 오전사 진단용.

        이 파일을 `qwen_realtime_voice.py --test-file <wav>` 로 으면 폰 없이
        같은 오전사를 재현할 수 있다(스킬 korean-transcript-fix 의 A/B 방법).
        """
        data = bytes(self._up_tail)
        if not data:
            return None
        import wave
        from pathlib import Path

        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(DASHSCOPE_IN_RATE)
                w.writeframes(data)
        except Exception:  # noqa: BLE001
            return None
        return str(p)

    def _put(self, item: bytes | None) -> None:
        "큐가 가득 차면 가장 오래된 청크를 버리고 넣는다 (지연 누적 방지)."
        try:
            self._q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._q.put_nowait(item)
            except asyncio.QueueFull:
                pass

    def stats(self) -> dict[str, Any]:
        return {
            "frames_in": self.frames_in,
            "stereo_frames": self.stereo_frames,
            "mono_frames": self.mono_frames,
            "up_chunks": self.chunks_up,
            "up_bytes": self.bytes_up,
            "up_chunk_bytes": self.chunk_bytes,
            "input_peak": int(self.input_peak),
            "input_rms_avg": round(self._rms_sum / self._rms_n, 1) if self._rms_n else 0.0,
            "input_clip_pct_max": round(self.input_clip_pct, 2),
            "queued_chunks": self._q.qsize(),
            "track_ended": self.track_ended,
            "reader_error": self.reader_error,
            "downmix_comp_db": self.downmix_comp_db if self.stereo_frames else 0.0,
            "resample_in_samples": self._resampler.samples_in,
            "resample_out_samples": self._resampler.samples_out,
            "track": self.track_out.stats(),
        }
