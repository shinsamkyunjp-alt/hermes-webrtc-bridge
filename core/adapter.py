# -*- coding: utf-8 -*-
"""Layer 2 (I/O) — AudioIOAdapter 추상화와 구현체.

코어 세션(`core.session.QwenRealtimeSession`)은 오디오 장치를 전혀 모른다. 모든
입출력은 이 모듈의 `AudioIOAdapter` 구현체가 담당한다.

  * `LocalSoundDeviceAdapter` — 기존 sounddevice 마이크/스피커 (레거시 로컬 모드)
  * `FileAudioAdapter`        — WAV 파일 입출력 (마이크 없는 검증/CI, PTT 턴 제어)

⚠️ 장치 인덱스는 macOS Core Audio에서 **불안정**하다(스킬 pitfall 14). 그래서
   `LocalSoundDeviceAdapter`는 인덱스를 `__init__`에서 확정하지 않고 `start()`
   시점에 **이름으로 재해석**한다. 스트림을 여는 것(=장치 점유)은 start()에서만
   일어나며, 진단용 조회 함수(resolve_*)는 장치를 열지 않는다(스킬 pitfall 16).
"""

from __future__ import annotations

import asyncio
import queue
import time
import wave
from abc import ABC, abstractmethod
from pathlib import Path

INPUT_RATE = 16000   # DashScope 업링크 규격 (PCM16 mono)
OUTPUT_RATE = 24000  # DashScope 다운링크 규격 (PCM16 mono)
DEFAULT_BLOCKSIZE = 1600  # 100ms @16k (원본 CLI와 동일)
CHUNK_BYTES = 3200   # 100ms @16k 프레임 크기

# 헤드셋 후보 이름 키워드 (headphones 모드 해석용)
HEADSET_KEYWORDS = ("arctis", "headphone", "headset", "airpods", "buds", "wh-", "bt-", "jabra")


class AudioIOAdapter(ABC):
    """오디오 입출력 추상 인터페이스.

    * ``read_chunk()``  → 16 kHz PCM16 mono bytes 한 덩어리, 입력 종료 시 None
    * ``write_chunk()`` → 24 kHz PCM16 mono bytes 한 덩어리 재생
    """

    input_sample_rate = INPUT_RATE
    output_sample_rate = OUTPUT_RATE

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def read_chunk(self) -> bytes | None: ...

    @abstractmethod
    async def write_chunk(self, chunk: bytes) -> None: ...

    async def __aenter__(self) -> "AudioIOAdapter":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def stats(self) -> dict:
        return {}


# --------------------------------------------------------------------------- #
# 장치 해석 (장치를 열지 않는다 — 읽기 전용 조회만)
# --------------------------------------------------------------------------- #
def _short_name(dev: dict) -> str:
    return dev["name"].split(",")[0]


def np_abs_mean(chunk: bytes) -> float:
    """int16 PCM의 평균 절대 진폭 (numpy 없이). chunk_energy와 동일 정의."""
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    import array

    arr = array.array("h")
    arr.frombytes(chunk[: n * 2])
    return sum(abs(s) for s in arr) / n


def find_index(sd, name: str, need: str) -> int | None:
    """이름이 일치하고 요구 채널(in/out)을 가진 장치 인덱스 — 없으면 None."""
    key = "max_input_channels" if need == "in" else "max_output_channels"
    for i, d in enumerate(sd.query_devices()):
        if _short_name(d) == name and d[key] > 0:
            return i
    return None


def resolve_single_device(sd, name: str | None = None) -> tuple[int | None, int | None]:
    """단일 장치(마이크+스피커 한 기기) 구성 — 이름 기준으로 in/out 인덱스를 각각 해석.

    블루투스 스피커(Bose Mini II 등)는 in/out 인덱스가 다를 수 있으므로 이름으로
    두 번 찾는다. 사용자 선호: 마이크와 출력을 같은 기기로(스킬 pitfall 15).
    """
    devs = sd.query_devices()
    if name is None:
        in_default = sd.default.device[0]
        if in_default is None or in_default < 0:
            return (None, None)
        name = _short_name(devs[in_default])
    return (find_index(sd, name, "in"), find_index(sd, name, "out"))


def resolve_headphones(sd) -> tuple[int | None, int | None]:
    """헤드 후보 탐색 — 마이크가 있는 헤드만 채택, 없으면 in+out 겸용 장치로 폴백."""
    devs = sd.query_devices()
    for i, d in enumerate(devs):
        nm = _short_name(d).lower()
        if any(k in nm for k in HEADSET_KEYWORDS) and d["max_input_channels"] > 0:
            # 같은 이름의 출력 인덱스를 우선 찾고, 없으면 자기 자신을 출력으로 사용
            out = find_index(sd, _short_name(d), "out")
            return (i, out if out is not None else i)
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0:
            out = find_index(sd, _short_name(d), "out")
            return (i, out) if out is not None else (None, None)
    return (None, None)


def pick_devices(sd, input_idx=None, output_idx=None, headphones=False):
    """원본 CLI `pick_devices()` 로직 그대로 — 스피커 모드는 BT 입력/출력 분리.

    (레거시 호환: M1에서는 동작을 바꾸지 않는다. 단일 장치 강제는 어댑터 인자로 명시.)
    """
    devs = sd.query_devices()
    d_in = sd.default.device[0] if input_idx is None else input_idx
    d_out = sd.default.device[1] if output_idx is None else output_idx
    in_name = _short_name(devs[d_in])
    out_name = _short_name(devs[d_out])
    note = ""
    if headphones and not (input_idx is not None and output_idx is not None):
        if output_idx is not None and input_idx is None:
            idx = find_index(sd, out_name, "in")
            if idx is not None:
                d_in, note = idx, f" 헤드 모드: 마이크도 '{out_name}'(으)로 통일했습니다."
            else:
                note = f"⚠️ '{out_name}'에 입력 장치가 없어 마이크는 기본({in_name})을 사용합니다."
        elif input_idx is not None and output_idx is None:
            idx = find_index(sd, in_name, "out")
            if idx is not None:
                d_out, note = idx, f"🎧 드폰 모드: 출력도 '{in_name}'(으)로 통일했습니다."
            else:
                note = f"⚠️ '{in_name}'에 출력 장치가 없어 스피커는 기본({out_name})을 사용합니다."
        elif in_name != out_name:
            idx = find_index(sd, out_name, "in")
            if idx is not None:
                d_in, note = idx, f"🎧 헤드폰 모드: 마이크도 '{out_name}'(으)로 통일했습니다."
            else:
                idx = find_index(sd, in_name, "out")
                if idx is not None:
                    d_out, note = idx, f"🎧 헤드 모드: 출력도 '{in_name}'(으)로 통일했습니다."
            if not note:
                note = (
                    f"⚠️ 헤드폰 모드인데 기본 입력({in_name})과 출력({out_name})이 서로 "
                    "다르고 같은 이름의 반대쪽 장치를 찾지 못했습니다. 기본 장치로 진행합니다."
                )
        else:
            note = f"🎧 헤드폰 모드: 마이크·스피커 모두 '{in_name}' 사용합니다."
    elif input_idx is None and output_idx is None and in_name == out_name:
        for i, d in enumerate(devs):
            if d["max_output_channels"] > 0 and _short_name(d) != in_name:
                d_out = i
                note = (
                    f"⚠️ 입력·출력이 같은 블루투스 기기({in_name})라 "
                    f"음질을 위해 출력을 '{_short_name(d)}'(으)로 분리했습니다."
                )
                break
    return d_in, d_out, note


# --------------------------------------------------------------------------- #
# 구현체 1: 로컬 sounddevice (레거시 CLI 경로)
# --------------------------------------------------------------------------- #
class LocalSoundDeviceAdapter(AudioIOAdapter):
    """sounddevice 기반 로컬 마이크/스피커 어댑터.

    sounddevice는 **지연 import**한다 — 프로젝트 venv에는 sounddevice가 없어도
    (예: WebRTC 전용 서버, CI) 코어/파일 어댑터는 정상 동작해야 하기 때문이다.
    """

    def __init__(
        self,
        input_idx: int | None = None,
        output_idx: int | None = None,
        headphones: bool = False,
        *,
        single_device: bool = False,
        blocksize: int = DEFAULT_BLOCKSIZE,
        device_name: str | None = None,
    ) -> None:
        self.input_idx = input_idx
        self.output_idx = output_idx
        self.headphones = headphones
        self.single_device = single_device
        self.blocksize = blocksize
        self.device_name = device_name
        self.d_in: int | None = None
        self.d_out: int | None = None
        self.note = ""
        self.in_name = ""
        self.out_name = ""
        self.mic_levels: list[float] = []  # watchdog 진단용 (스킬: 30초 레벨 리포트)
        self._mic_q: "queue.Queue[bytes]" = queue.Queue()
        self._in_stream = None
        self._out_stream = None
        self._written_bytes = 0

    # -- lazy import -------------------------------------------------------
    @staticmethod
    def _sd():
        import sounddevice as sd  # noqa: PLC0415 (지연 import가 설계 의도)

        return sd

    def _callback(self, indata, frames, t, status) -> None:  # noqa: ANN001
        chunk = bytes(indata)
        self._mic_q.put(chunk)
        # watchdog 진단용 평균 절대 진폭 (원본 chunk_energy와 동일 정의)
        levels = self.mic_levels
        levels.append(float(np_abs_mean(chunk)))
        if len(levels) > 400:
            del levels[:100]

    async def start(self) -> None:
        sd = self._sd()
        if self.headphones and self.input_idx is None and self.output_idx is None:
            self.d_in, self.d_out = resolve_headphones(sd)
            self.note = "🎧 헤드폰 모드: 헤드셋 한 기기로 입출력 통일"
        elif self.single_device and self.input_idx is None and self.output_idx is None:
            self.d_in, self.d_out = resolve_single_device(sd, self.device_name)
            self.note = "🎚 단일 장치 모드: 마이크+출력 동일 기기"
        else:
            self.d_in, self.d_out, self.note = pick_devices(
                sd, self.input_idx, self.output_idx, headphones=self.headphones
            )
        if self.d_in is None or self.d_out is None or self.d_in < 0 or self.d_out < 0:
            raise RuntimeError("사용 가능한 입력/출력 장치를 찾지 못했습니다 (--list-devices 확인)")
        devs = sd.query_devices()
        self.in_name = _short_name(devs[self.d_in])
        self.out_name = _short_name(devs[self.d_out])

        self._out_stream = sd.RawOutputStream(
            samplerate=self.output_sample_rate, channels=1, dtype="int16", device=self.d_out
        )
        self._out_stream.start()
        self._in_stream = sd.InputStream(
            samplerate=self.input_sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self.blocksize,
            device=self.d_in,
            callback=self._callback,
        )
        self._in_stream.start()

    async def read_chunk(self) -> bytes | None:
        return await asyncio.to_thread(self._mic_q.get)

    async def write_chunk(self, chunk: bytes) -> None:
        sd = self._sd()
        try:
            await asyncio.to_thread(self._out_stream.write, chunk)
        except sd.PortAudioError as exc:
            # -9983 "Stream is stopped": 스트림 재시작 후 해당 청크 재시도 (원본 계승)
            code = exc.args[0] if exc.args else None
            if code == -9983:
                try:
                    await asyncio.to_thread(self._out_stream.start)
                    await asyncio.to_thread(self._out_stream.write, chunk)
                except Exception as exc2:  # noqa: BLE001
                    raise RuntimeError(f"오디오 재생 실패: {exc2}") from exc2
            else:
                raise
        self._written_bytes += len(chunk)

    async def stop(self) -> None:
        for stream in (self._in_stream, self._out_stream):
            if stream is None:
                continue
            try:
                stream.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        self._in_stream = None
        self._out_stream = None

    def stats(self) -> dict:
        return {
            "input_device": self.in_name,
            "output_device": self.out_name,
            "note": self.note,
            "mic_peak": max(self.mic_levels) if self.mic_levels else 0,
            "written_bytes": self._written_bytes,
        }


# --------------------------------------------------------------------------- #
# 구현체 2: WAV 파일 (마이크 없는 검증 경로)
# --------------------------------------------------------------------------- #
class FileAudioAdapter(AudioIOAdapter):
    """WAV 파일 입출력 어댑터 — 마이크/스피커를 **전혀 열지 않는다**.

    ``--test-file`` 검증과 동일한 파이프라인을 어댑터 인터페이스로 재현하기 위한
    구현체다. 입력은 16 kHz mono PCM16 WAV, 출력은 24 kHz mono PCM16 WAV로 저장.
    """

    def __init__(
        self,
        input_wav: str | None = None,
        output_wav: str | None = None,
        *,
        chunk_bytes: int = CHUNK_BYTES,
        realtime: bool = False,
    ) -> None:
        self.input_wav = input_wav
        self.output_wav = output_wav
        self.chunk_bytes = chunk_bytes
        self.realtime = realtime  # True면 실시간 페이스(청크당 100ms)로 공급
        self._pcm = b""
        self._offset = 0
        self._out = bytearray()
        self._started = False
        self._t0 = 0.0

    async def start(self) -> None:
        if self.input_wav:
            with wave.open(self.input_wav, "rb") as w:
                if w.getframerate() != self.input_sample_rate:
                    raise RuntimeError(
                        f"입력 wav는 {self.input_sample_rate}Hz 여야 합니다 "
                        f"(현재 {w.getframerate()}Hz)"
                    )
                if w.getnchannels() != 1 or w.getsampwidth() != 2:
                    raise RuntimeError("입력 wav는 mono PCM16 이어야 합니다")
                self._pcm = w.readframes(w.getnframes())
        self._offset = 0
        self._t0 = time.time()
        self._started = True

    async def read_chunk(self) -> bytes | None:
        if not self._started:
            raise RuntimeError("read_chunk 이전에 start()가 필요합니다")
        if self._offset >= len(self._pcm):
            return None
        chunk = self._pcm[self._offset : self._offset + self.chunk_bytes]
        self._offset += len(chunk)
        if self.realtime:
            target = self._t0 + self._offset / (2 * self.input_sample_rate)
            delay = target - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
        return chunk

    async def write_chunk(self, chunk: bytes) -> None:
        self._out.extend(chunk)

    async def stop(self) -> None:
        if self.output_wav:
            Path(self.output_wav).parent.mkdir(parents=True, exist_ok=True)
            with wave.open(self.output_wav, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.output_sample_rate)
                w.writeframes(bytes(self._out))
        self._started = False

    def stats(self) -> dict:
        return {
            "input_wav": self.input_wav,
            "input_seconds": round(len(self._pcm) / (2 * self.input_sample_rate), 2),
            "output_wav": self.output_wav,
            "output_bytes": len(self._out),
            "output_seconds": round(len(self._out) / (2 * self.output_sample_rate), 2),
        }