# -*- coding: utf-8 -*-
"""Layer 1 — QwenRealtimeSession: DashScope 실시간 음성 세션 코어.

`~/.hermes/scripts/qwen_realtime_voice.py` (모놀리식 CLI)에서 **I/O 의존성만**
제거하고 추출한 순수 스트리밍 세션 클래스다. 마이크/스피커/WebRTC/파일 입출력은
전부 `core.adapter.AudioIOAdapter` 구현체가 담당하고, 이 클래스는 다음만 책임진다.

  * DashScope WebSocket 세션 수명주기 + 지수 백오프 자동 재연결 (SessionDied 대응)
  * 표준 비동기 오디오 인터페이스
      - ``await session.push_audio(chunk)``      : 16 kHz PCM16 mono 입력
      - ``async for pcm in session.get_audio_stream()`` : 24 kHz PCM16 mono 출력
  * 서버 이벤트 파싱/디스패치 (전사, 응답 상태, Barge-in, usage)
  * Hermes 에이전트 브리지 (`delegate_to_agent` → hermes chat --resume)
  * 토큰/바이트 사용량 집계

**절대 유지 (PLAN §7)**: DashScope WS 프로토콜 규격, 에이전트 브리지 호출 시퀀스,
SessionDied 타임아웃·재연결 제어. 이 세 가지는 리팩토링 과정에서 의미가 바뀌면 안 된다.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Iterable

import numpy as np
import websockets

from .persona import (
    BRIDGE_TOOL,
    DEBUG_LOG,
    DEFAULT_VAD,
    HEADPHONE_TURN_DETECTION,
    INPUT_TRANSCRIPTION,
    INSTRUCTIONS,
    MIC_GAIN_DEFAULT,
    MODEL,
    VOICE,
)

AGENT_TIMEOUT = 300  # 헤르메스 에이전트 위임 최대 대기(초)

# 서버가 세션을 닫는 에러 코드 → 재연결 필요 신호
FATAL_ERROR_CODES = ("response_idle_timeout", "session_timeout", "timeout")

_IDLE_STATES = {"idle", "listening", "thinking", "speaking"}


class SessionDied(Exception):
    """서버가 세션을 닫음(idle timeout 등) — 재연결이 필요하다 (스킬 pitfall 9)."""


def backoff_delay(failures: int) -> float:
    """지수 백오프 3s → 15s 상한 (원본 CLI와 동일 계수)."""
    return float(min(3 * 2 ** max(failures - 1, 0), 15))


def boost_gain(chunk: bytes, gain: float = MIC_GAIN_DEFAULT) -> bytes:
    """int16 PCM 소프트 클리핑 게인 (저레벨 마이크의 ASR 언어 혼동 완화, pitfall 21)."""
    if gain == 1.0 or not chunk:
        return chunk
    count = len(chunk) // 2
    if count <= 0:
        return chunk
    samples = np.frombuffer(chunk[: count * 2], dtype=np.int16).astype(np.float32)
    samples *= gain
    samples = np.tanh(samples / 32767.0) * 32767.0
    return samples.astype(np.int16).tobytes()


def chunk_energy(chunk: bytes) -> float:
    """프레임 평균 절대 진폭 (마이크 레벨/노이즈 게이트 판정용)."""
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    samples = np.frombuffer(chunk[: n * 2], dtype=np.int16)
    return float(np.abs(samples.astype(np.int32)).mean())


def load_api_key() -> str:
    """DASHSCOPE_API_KEY 환경변수 → ~/.bailian/config.json 순서 (값은 절대 로그 금지)."""
    env = os.environ.get("DASHSCOPE_API_KEY")
    if env:
        return env
    cfg = os.path.expanduser("~/.bailian/config.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            return json.load(f)["api_key"]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"API 키를 찾을 수 없습니다 (~/.bailian/config.json): {exc}") from exc


def build_session_config(
    turn_detection: dict | None = None,
    agent_bridge: bool = True,
    voice: str = VOICE,
    instructions: str = INSTRUCTIONS,
) -> dict:
    """ `session.update`에 실릴 세션 설정.

    ``turn_detection=None`` 이면 manual 모드(푸시투토크 / 테스트 경로) — 서버 VAD가
    턴을 지 않으므로 호출자가 commit + response.create 를 직접 제어한다.
    """
    cfg: dict[str, Any] = {
        "modalities": ["text", "audio"],
        "voice": voice,
        "instructions": instructions,
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "input_audio_transcription": dict(INPUT_TRANSCRIPTION),
    }
    # manual(None)인 경우 반드시 명시적 None(null)을 실어 보내야 서버 기본 VAD가 꺼진다.
    # 키 자체를 생략하면 DashScope 서버가 기본 VAD(0.5 / 800ms)를 강제 유지한다.
    cfg["turn_detection"] = turn_detection
    if agent_bridge:
        cfg["tools"] = [BRIDGE_TOOL]
    return cfg


def turn_detection_for(mode: str) -> dict | None:
    """'headphones'(스마트턴) / 'speaker'(server_vad) / 'manual'(PTT·테스트)."""
    if mode == "headphones":
        return dict(HEADPHONE_TURN_DETECTION)
    if mode == "speaker":
        return dict(DEFAULT_VAD)
    return None


class QwenRealtimeSession:
    """DashScope 실시간 세션 코어. I/O 없음 — 오디오는 큐/메서드로만 오간다."""

    def __init__(
        self,
        *,
        voice: str = VOICE,
        instructions: str = INSTRUCTIONS,
        turn_detection: dict | None = None,
        agent_bridge: bool = True,
        api_key: str | None = None,
        mic_gain: float = MIC_GAIN_DEFAULT,
        debug_log: str | None = DEBUG_LOG,
        ws_url: str | None = None,
        agent_runner: Callable[[str], str] | None = None,
        audio_queue_maxsize: int = 400,
        connect_timeout: float = 20.0,
    ) -> None:
        self.model = MODEL
        self.voice = voice
        self.instructions = instructions
        self.turn_detection = turn_detection
        self.agent_bridge = agent_bridge
        self.debug_log = os.path.expanduser(debug_log) if debug_log else None
        self.ws_url = ws_url or os.environ.get("QWEN_REALTIME_WS_URL") or (
            "wss://token-plan.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime?model="
            + MODEL
        )
        self.mic_gain = mic_gain
        self.connect_timeout = connect_timeout
        self._api_key = api_key
        self._agent_runner = agent_runner  # 테스트 주입용 (None = 실제 hermes chat 실행)

        self.audio_q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=audio_queue_maxsize)
        self.usage: dict[str, Any] = {
            "audio_in_bytes": 0,
            "audio_out_bytes": 0,
            "responses": 0,
            "started_at": time.time(),
            "voice": voice,
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_audio_in": 0,
            "tokens_total": 0,
            "tool_calls": 0,
            "sessions": 0,
            "dropped_in_chunks": 0,
        }
        self.transcripts: dict[str, str] = {"user": "", "assistant": ""}
        self.state = "idle"
        self.turn = 0
        self.responding = False
        self.last_play_end = 0.0

        # 에이전트 브리지: 음성 대화당 에이전트 세션 1개를 이어붙여 락 유지
        self.agent_session_id: str | None = None
        self.events: list[tuple[str, dict]] = []  # (kind, payload) 전체 이력 (회 검증용)

        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._subscribers: list[Callable[[str, dict], Any]] = []
        self._closing = False
        self._stream_active = False
        self._reader_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ events
    def subscribe(self, cb: Callable[[str, dict], Any]) -> None:
        """(kind, payload) 백 등록. 동기/비동기 모두 허용 (M3 DataChannel 푸시용)."""
        self._subscribers.append(cb)

    async def emit(self, kind: str, payload: dict | None = None) -> None:
        payload = payload or {}
        self.events.append((kind, payload))
        for cb in list(self._subscribers):
            try:
                res = cb(kind, payload)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:  # noqa: BLE001
                self.log("SUBSCRIBER_ERROR", {"kind": kind, "error": str(exc)})

    def log(self, tag: str, ev: dict) -> None:
        """디버그 로그 (오디오 payload는 크기만 기록). debug_log=None 이면 no-op."""
        if not self.debug_log:
            return
        try:
            red = dict(ev)
            for key in ("delta", "audio"):
                if key in red and len(str(red[key])) > 200:
                    red[key] = f"<audio {len(red[key])} chars>"
            os.makedirs(os.path.dirname(self.debug_log), exist_ok=True)
            with open(self.debug_log, "a", encoding="utf-8") as f:
                f.write(
                    f"[{datetime.now().isoformat(timespec='milliseconds')}] {tag} "
                    f"{json.dumps(red, ensure_ascii=False)}\n"
                )
        except Exception:  # noqa: BLE001
            pass

    def event_kinds(self) -> dict[str, int]:
        """수신 이벤트 종류별 카운트 (검증 리포트용)."""
        counts: dict[str, int] = {}
        for kind, _ in self.events:
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # ------------------------------------------------------------------- send
    async def _send(self, obj: dict) -> None:
        """동시 전송 직렬화. 미연결이면 조용히 버린다(재연결 루프가 복구)."""
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            await ws.send(json.dumps(obj))

    # ----------------------------------------------- 표준 오디오 인터페이스
    async def push_audio(self, chunk: bytes, *, boost: bool = True) -> bool:
        """업링크: 16 kHz PCM16 mono 프레임을 DashScope로 전송. 전송 성공 여부 반환."""
        if not chunk:
            return False
        if self._ws is None:
            self.usage["dropped_in_chunks"] += 1
            return False
        data = boost_gain(chunk, self.mic_gain) if boost else chunk
        await self._send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(data).decode()}
        )
        self.usage["audio_in_bytes"] += len(chunk)
        return True

    async def get_audio_stream(self) -> AsyncIterator[bytes]:
        """다운링크: 24 kHz PCM16 mono 청크를 순차 yield (M0 실측: 소비자는 1개만!)."""
        if self._stream_active:
            raise RuntimeError(
                "get_audio_stream 소비자는 1개만 허용된다 (M0 실측: recv()는 큐 pop)"
            )
        self._stream_active = True
        try:
            while True:
                chunk = await self.audio_q.get()
                if chunk is None:  # 종료 센티넬
                    return
                yield chunk
        finally:
            self._stream_active = False

    async def commit(self) -> None:
        """manual 모드: 입력 버퍼 확정(서버 VAD 없이 턴 종료)."""
        await self._send({"type": "input_audio_buffer.commit"})

    async def create_response(self, modalities: Iterable[str] = ("audio", "text")) -> None:
        await self._send({"type": "response.create", "response": {"modalities": list(modalities)}})

    async def send_text(self, text: str) -> None:
        """타이핑 입력 주입 (시각화 화면의 .voice_say 경로 계승)."""
        # 세션 웹소켓이 아직 연결 중이면 대기 (초기 핸드셰이크 레이스 방지)
        for _ in range(150):
            if self._ws is not None:
                break
            await asyncio.sleep(0.1)
        if self._ws is None:
            self.log("WARN_SEND_TEXT_NOT_CONNECTED", {"text": text[:60]})
            return
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self.create_response()

    def flush_audio(self) -> None:
        """Barge-in: 재생 대기 중인 출력 PCM을 전부 버린다 (원본 play_q.clear() 계승)."""
        dropped = 0
        while True:
            try:
                self.audio_q.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        self.log("AUDIO_FLUSH", {"dropped_chunks": dropped})

    # -------------------------------------------------------- agent bridge
    def _run_agent_sync(self, task: str) -> str:
        """hermes chat 원샷 모드로 실제 에이전트 실행 (음성 대화당 세션 1개로 --resume 유지)."""
        if self._agent_runner is not None:  # 테스트 주입
            return self._agent_runner(task)
        hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        venv_py = os.path.join(hermes_home, "hermes-agent", "venv", "bin", "python")
        if not os.path.exists(venv_py):
            venv_py = sys.executable
        cmd = [venv_py, "-m", "hermes_cli.main", "chat", "-q", task, "-Q", "--accept-hooks"]
        if self.agent_session_id:
            cmd += ["--resume", self.agent_session_id]
        env = {**os.environ, "HERMES_HOME": hermes_home, "NO_COLOR": "1", "TERM": "dumb"}
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=AGENT_TIMEOUT + 30,
                cwd=str(os.path.expanduser("~")),
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise asyncio.TimeoutError() from exc
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        new_sid = None
        for stream in (out, err):
            for ln in stream.splitlines():
                if ln.startswith("session_id:"):
                    new_sid = ln.split(":", 1)[1].strip()
                    break
            if new_sid:
                break
        if new_sid:
            self.agent_session_id = new_sid

        def _clean(text: str) -> str:
            return "\n".join(
                ln
                for ln in text.splitlines()
                if ln.strip()
                and not ln.startswith("session_id:")
                and not ln.startswith("↻ Resumed session")
            ).strip()

        answer = _clean(out)
        if not answer:
            answer = _clean(err)[-1000:] or "에이전트가 결과를 반환하지 않았습니다."
        if self.agent_session_id and ("not found" in answer.lower() or "존재하지 않" in answer):
            self.agent_session_id = None
            return self._run_agent_sync(task)
        return answer

    async def handle_function_call(self, call_id: str, name: str, args_str: str) -> None:
        """delegate_to_agent → 헤르메스 실행 → 결과 회신 → 음성 응답 트리거 (시퀀스 보존)."""
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args = {}
        task = str(args.get("task") or args_str or "").strip() or "상태 확인"
        self.usage["tool_calls"] += 1
        await self.emit("tool_call", {"call_id": call_id, "task": task})
        self.log("AGENT_DELEGATE", {"call_id": call_id, "task": task})
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._run_agent_sync, task), timeout=AGENT_TIMEOUT
            )
        except asyncio.TimeoutError:
            result = f"에이전트 작업이 {AGENT_TIMEOUT}초 안에 끝나지 않았습니다."
        except Exception as exc:  # noqa: BLE001
            result = f"에이전트 실행 오류: {exc}"
        if len(result) > 4000:
            result = result[:4000] + "\n...(내용이 길어 일부 생략)"
        await self.emit("tool_result", {"call_id": call_id, "chars": len(result)})
        self.log("AGENT_RESULT", {"call_id": call_id, "chars": len(result)})
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"result": result}, ensure_ascii=False),
                },
            }
        )
        await self.create_response()

    # ------------------------------------------------------- event dispatch
    async def handle_event(self, ev: dict) -> None:
        """서버 이벤트 1건 처리. 네트워크 없이 단위 테스트 가능하도록 분리했다."""
        t = ev.get("type")
        self.log("RECV", ev)
        if t == "response.audio.delta":
            pcm = base64.b64decode(ev["delta"])
            self.usage["audio_out_bytes"] += len(pcm)
            self._set_state("speaking")
            try:
                self.audio_q.put_nowait(pcm)
            except asyncio.QueueFull:  # 소비자 지연 → 가장 오래된 것 버림 (RTP 유지)
                try:
                    self.audio_q.get_nowait()
                    self.audio_q.put_nowait(pcm)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        elif t == "response.created":
            self.responding = True
            self.turn += 1
            self.usage["responses"] = self.turn
            self._set_state("thinking")
            await self.emit("response_started", {"turn": self.turn})
        elif t == "response.done":
            self.responding = False
            resp_usage = (ev.get("response") or {}).get("usage") or {}
            if resp_usage:
                details = resp_usage.get("input_tokens_details") or {}
                self.usage["tokens_in"] += int(resp_usage.get("input_tokens", 0))
                self.usage["tokens_out"] += int(resp_usage.get("output_tokens", 0))
                self.usage["tokens_audio_in"] += int(details.get("audio_tokens", 0))
                self.usage["tokens_total"] += int(resp_usage.get("total_tokens", 0))
            self._set_state("idle")
            await self.emit("response_done", {"turn": self.turn, "usage": dict(self.usage)})
        elif t == "response.function_call_arguments.done":
            # 브릿지 실행은 백그라운드 — 수신 루프를 블로킹하지 않는다
            asyncio.create_task(
                self.handle_function_call(
                    ev.get("call_id", ""), ev.get("name", ""), ev.get("arguments", "")
                )
            )
        elif t == "input_audio_buffer.speech_started":
            self._set_state("listening")
            if self.responding:  # 끼어들기: 재생 버퍼 즉시 비움
                self.flush_audio()
            await self.emit("speech_started", {})
        elif t == "conversation.item.input_audio_transcription.completed":
            self.transcripts["user"] = ev.get("transcript", "")
            await self.emit("user_transcript", {"text": self.transcripts["user"]})
        elif t == "conversation.item.ambient_audio_transcription.completed":
            await self.emit("ambient_transcript", {"text": ev.get("text", "")})
        elif t == "response.audio_transcript.done":
            self.transcripts["assistant"] = ev.get("transcript", "")
            await self.emit("assistant_transcript", {"text": self.transcripts["assistant"]})
        elif t == "error":
            err = ev.get("error", {})
            code = err.get("code", "")
            await self.emit("error", {"code": code, "message": err.get("message", "")})
            if code in FATAL_ERROR_CODES or "closed" in str(err.get("message", "")).lower():
                raise SessionDied(str(err.get("message", code)))

    def _set_state(self, state: str) -> None:
        if state not in _IDLE_STATES or state == self.state:
            return
        self.state = state
        # 상태 전이는 논블로킹으로 알린다 (emit은 큐 적재만)
        for cb in list(self._subscribers):
            try:
                res = cb("state", {"state": state})
                if asyncio.iscoroutine(res):
                    asyncio.ensure_future(res)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- run loop
    async def connect_once(self) -> Any:
        """WS 1회 연결 + session.update 전송. 테스트/서버에서 세션을 직접 열 때 사용."""
        key = self._api_key or load_api_key()
        ws = await websockets.connect(
            self.ws_url,
            additional_headers={"Authorization": f"Bearer {key}"},
            open_timeout=self.connect_timeout,
        )
        # 1. 서버가 보내는 첫 session.created 이벤트를 먼저 수신한다
        # (이걸 건너뛰고 바로 session.update를 보내면 레이스로 인해 설정이 씹히거나 VAD 충돌 발생)
        try:
            created_msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
            created_ev = json.loads(created_msg)
            self.log("RECV_CREATED", created_ev)
        except Exception as exc:  # noqa: BLE001
            self.log("WARN_RECV_CREATED", {"error": str(exc)})

        self._ws = ws
        await self._send(
            {
                "type": "session.update",
                "session": build_session_config(
                    self.turn_detection, self.agent_bridge, self.voice, self.instructions
                ),
            }
        )
        self.usage["sessions"] += 1
        await self.emit("connected", {"session": self.usage["sessions"], "voice": self.voice})
        return ws

    async def _read_loop(self, ws: Any) -> None:
        async for msg in ws:
            await self.handle_event(json.loads(msg))

    async def run(self) -> None:
        """재연결 루프. `close()` 또는 태스크 취소로 종료된다 (스킬 pitfall 9·10)."""
        failures = 0
        self._closing = False
        while not self._closing:
            try:
                ws = await self.connect_once()
                failures = 0
                await self._read_loop(ws)
                raise SessionDied("server closed the websocket")
            except asyncio.CancelledError:
                raise
            except (
                SessionDied,
                websockets.exceptions.ConnectionClosed,
                asyncio.TimeoutError,
                OSError,
            ) as exc:
                failures += 1
                wait = backoff_delay(failures)
                self.log(
                    "SESSION_RECONNECT",
                    {"reason": str(exc)[:200], "failures": failures, "retry_in": wait},
                )
                await self.emit(
                    "disconnected",
                    {"reason": f"{exc.__class__.__name__}: {str(exc)[:120]}",
                     "failures": failures, "retry_in": wait},
                )
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    raise
            finally:
                self._ws = None
        await self.emit("closed", {})

    async def close(self) -> None:
        """세션 안전 종료: WS close + 오디오 스트림 센티 (리소스 누수 방지)."""
        self._closing = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.audio_q.put_nowait(None)
        except asyncio.QueueFull:
            pass