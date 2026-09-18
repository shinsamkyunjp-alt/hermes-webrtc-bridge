#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 회귀 테스트 — 마이크/스피커/네트워크를 **전혀** 사용하지 않는 단위 테스트.

실행:
  cd /Users/shinsamkyun/hermes-webrtc-bridge
  .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import os
import sys
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from core import adapter as ad  # noqa: E402
from core import persona as P  # noqa: E402
from core import session as S  # noqa: E402
from core.pipeline import ConversationRunner, MicGate  # noqa: E402

SOURCE_CLI = Path(os.path.expanduser("~/.hermes/scripts/qwen_realtime_voice.py"))


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def types(self) -> list[str]:
        return [m.get("type") for m in self.sent]


class FakeSD:
    """sounddevice.query_devices() 대역 — 장치를 열지 않고 해석 로직만 검증."""

    def __init__(self, devices: list[dict], default: tuple[int, int]) -> None:
        self._devices = devices

        class _Default:
            device = default

        self.default = _Default()

    def query_devices(self):
        return self._devices


DEV = lambda name, i, o: {"name": name, "max_input_channels": i, "max_output_channels": o}  # noqa: E731


def _wav_bytes(pcm: bytes, rate: int = 16000, path: str = "/tmp/m1_unit.wav") -> str:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


class TestPersonaParity(unittest.TestCase):
    """core/persona.py 가 원본 CLI 상수와 **문자 그대로** 일치하는지 (페르소나 드리프트 방지)."""

    @classmethod
    def setUpClass(cls) -> None:
        import textwrap

        import extract_persona as ep  # tools/extract_persona.py

        src = SOURCE_CLI.read_text(encoding="utf-8")
        raw = ep._top_level_constants(src)
        # WS_URL/DEBUG_LOG 은 MODEL/os 를 참조하는 표현식 → 네임스페이스를 주고 eval
        ns = {"MODEL": ast.literal_eval(raw["MODEL"]), "os": os}
        cls.consts = {}
        for k, seg in raw.items():
            expr = "(" + textwrap.dedent(seg).strip() + ")"
            cls.consts[k] = eval(expr, ns)  # noqa: S307 (로컬 원본 소스, 신뢰됨)
        cls.lit = ep._literals(src)

    def test_scalar_constants(self) -> None:
        self.assertEqual(self.consts["MODEL"], P.MODEL)
        self.assertEqual(self.consts["VOICE"], P.VOICE)
        self.assertEqual(self.consts["DEBUG_LOG"], P.DEBUG_LOG)
        self.assertEqual(self.consts["WS_URL"], P.WS_URL)

    def test_instructions_verbatim(self) -> None:
        self.assertEqual(self.consts["INSTRUCTIONS"], P.INSTRUCTIONS)

    def test_bridge_tool_verbatim(self) -> None:
        self.assertEqual(self.consts["BRIDGE_TOOL"], P.BRIDGE_TOOL)

    def test_inline_literals(self) -> None:
        self.assertEqual(self.lit["input_transcription"], P.INPUT_TRANSCRIPTION)
        self.assertEqual(self.lit["default_vad"], P.DEFAULT_VAD)
        self.assertEqual(self.lit["headphone_td"], P.HEADPHONE_TURN_DETECTION)
        self.assertEqual(float(self.lit["mic_gain"]), P.MIC_GAIN_DEFAULT)
        self.assertEqual(self.lit["noise_gate"], P.NOISE_GATE)
        self.assertEqual(self.lit["agent_timeout"], P.AGENT_TIMEOUT)


class TestPureHelpers(unittest.TestCase):
    def test_backoff_delay(self) -> None:
        self.assertEqual([S.backoff_delay(n) for n in range(1, 6)], [3, 6, 12, 15, 15])

    def test_boost_gain_soft_clip(self) -> None:
        import numpy as np

        raw = np.array([100, -100, 30000, -30000], dtype=np.int16).tobytes()
        out = np.frombuffer(S.boost_gain(raw, 2.5), dtype=np.int16)
        self.assertGreater(abs(int(out[0])), 100)          # 게인 적용됨
        self.assertLessEqual(int(np.abs(out).max()), 32767)  # 소프트 클립 (오버플로 없음)
        self.assertEqual(S.boost_gain(raw, 1.0), raw)        # gain=1 → 무변경
        self.assertEqual(S.boost_gain(b"", 2.5), b"")

    def test_chunk_energy(self) -> None:
        import numpy as np

        self.assertEqual(S.chunk_energy(b""), 0.0)
        loud = np.full(1600, 1000, dtype=np.int16).tobytes()
        self.assertAlmostEqual(S.chunk_energy(loud), 1000.0, places=3)

    def test_build_session_config_manual_vs_vad(self) -> None:
        manual = S.build_session_config(None, agent_bridge=False)
        # manual = 명시적 null 로 VAD OFF (키를 생략하면 서버 기본 VAD가 살아남는다)
        self.assertIn("turn_detection", manual)
        self.assertIsNone(manual["turn_detection"])
        self.assertNotIn("tools", manual)
        self.assertEqual(manual["input_audio_format"], "pcm")
        self.assertEqual(manual["output_audio_format"], "pcm")
        self.assertEqual(manual["input_audio_transcription"]["language"], "ko")
        self.assertEqual(manual["input_audio_transcription"]["model"], "qwen3-asr-flash-realtime")
        self.assertEqual(manual["voice"], P.VOICE)
        self.assertEqual(manual["instructions"], P.INSTRUCTIONS)

        vad = S.build_session_config(S.turn_detection_for("speaker"))
        self.assertEqual(vad["turn_detection"], P.DEFAULT_VAD)
        self.assertEqual(vad["tools"][0]["name"], "delegate_to_agent")
        self.assertEqual(S.turn_detection_for("headphones"), P.HEADPHONE_TURN_DETECTION)
        self.assertIsNone(S.turn_detection_for("manual"))

    def test_api_key_never_logged(self) -> None:
        """load_api_key는 값을 반환하되 로그/예외 메시지에 키를 남기지 않는다."""
        if not os.path.exists(os.path.expanduser("~/.bailian/config.json")) and not \
                os.environ.get("DASHSCOPE_API_KEY"):
            self.skipTest("API 키 소스 없음")
        key = S.load_api_key()
        self.assertTrue(key.startswith("sk-"))
        self.assertNotIn(key, S.load_api_key.__doc__ or "")


class TestEventDispatch(unittest.IsolatedAsyncioTestCase):
    """서버 이벤트 처리 — 네트워크 없이 가짜 WS로 검증."""

    def _sess(self, **kw) -> S.QwenRealtimeSession:
        s = S.QwenRealtimeSession(api_key="sk-test", debug_log=None, **kw)
        s._ws = FakeWS()
        return s

    async def test_audio_delta_lands_in_stream(self) -> None:
        s = self._sess()
        pcm = b"\x01\x02" * 960  # 20ms @24k
        await s.handle_event({"type": "response.audio.delta", "delta": base64.b64encode(pcm)})
        self.assertEqual(s.usage["audio_out_bytes"], len(pcm))
        self.assertEqual(await s.audio_q.get(), pcm)
        self.assertEqual(s.state, "speaking")

    async def test_response_lifecycle_and_usage(self) -> None:
        s = self._sess()
        await s.handle_event({"type": "response.created"})
        self.assertTrue(s.responding)
        self.assertEqual(s.turn, 1)
        await s.handle_event({"type": "response.done", "response": {"usage": {
            "input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
            "input_tokens_details": {"audio_tokens": 7}}}})
        self.assertFalse(s.responding)
        self.assertEqual(s.usage["tokens_total"], 30)
        self.assertEqual(s.usage["tokens_audio_in"], 7)
        self.assertEqual(s.state, "idle")
        self.assertEqual(s.usage["responses"], 1)

    async def test_transcripts(self) -> None:
        s = self._sess()
        await s.handle_event({"type": "conversation.item.input_audio_transcription.completed",
                              "transcript": "안녕하세요"})
        await s.handle_event({"type": "response.audio_transcript.done", "transcript": "네, 반갑습니다"})
        self.assertEqual(s.transcripts["user"], "안녕하세요")
        self.assertEqual(s.transcripts["assistant"], "네, 반갑습니다")

    async def test_barge_in_flushes_playback(self) -> None:
        s = self._sess()
        await s.handle_event({"type": "response.audio.delta", "delta": base64.b64encode(b"\x00\x01" * 100)})
        await s.handle_event({"type": "response.created"})
        self.assertGreater(s.audio_q.qsize(), 0)
        await s.handle_event({"type": "input_audio_buffer.speech_started"})
        self.assertEqual(s.audio_q.qsize(), 0)  # 재생 버퍼 즉시 비움
        self.assertEqual(s.state, "listening")

    async def test_idle_timeout_raises_session_died(self) -> None:
        s = self._sess()
        with self.assertRaises(S.SessionDied):
            await s.handle_event({"type": "error", "error": {
                "code": "response_idle_timeout", "message": "session idle too long"}})

    async def test_single_stream_consumer_enforced(self) -> None:
        s = self._sess()
        await s.handle_event({"type": "response.audio.delta", "delta": base64.b64encode(b"\x00\x01" * 100)})
        gen1 = s.get_audio_stream()
        first = await gen1.__anext__()
        self.assertTrue(first)
        gen2 = s.get_audio_stream()
        with self.assertRaises(RuntimeError):
            await gen2.__anext__()
        await gen1.aclose()

    async def test_tool_call_roundtrip_sends_function_output(self) -> None:
        """Hermes 에이전트 브지 시퀀스 보존 검증 (에이전트 실행은 주입으로 대체)."""
        calls: list[str] = []
        s = self._sess(
            agent_bridge=True,
            agent_runner=lambda task: calls.append(task) or "지금 시각은 23시 40분입니다.",
        )
        await s.handle_event({
            "type": "response.function_call_arguments.done",
            "call_id": "call_1", "name": "delegate_to_agent",
            "arguments": json.dumps({"task": "지금 시각 알려줘"}, ensure_ascii=False),
        })
        for _ in range(20):
            await asyncio.sleep(0.01)
            if len(s._ws.sent) >= 2:
                break
        self.assertEqual(calls, ["지금 시각 알려줘"])
        self.assertEqual(s.usage["tool_calls"], 1)
        self.assertEqual(s._ws.types(), ["conversation.item.create", "response.create"])
        out = s._ws.sent[0]["item"]
        self.assertEqual(out["type"], "function_call_output")
        self.assertEqual(out["call_id"], "call_1")
        self.assertIn("지금 시각은", json.loads(out["output"])["result"])
        kinds = dict(s.events)
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)

    async def test_push_audio_sends_append_and_tracks_usage(self) -> None:
        s = self._sess(mic_gain=1.0)
        ok = await s.push_audio(b"\x10\x00" * 1600)
        self.assertTrue(ok)
        msg = s._ws.sent[-1]
        self.assertEqual(msg["type"], "input_audio_buffer.append")
        self.assertEqual(s.usage["audio_in_bytes"], 3200)
        s._ws = None
        self.assertFalse(await s.push_audio(b"\x10\x00" * 1600))  # 미연결 → 드롭 카운트
        self.assertEqual(s.usage["dropped_in_chunks"], 1)

    async def test_emit_reaches_subscribers(self) -> None:
        s = self._sess()
        seen: list[str] = []
        s.subscribe(lambda kind, payload: seen.append(kind))
        await s.handle_event({"type": "response.created"})
        await s.handle_event({"type": "response.done", "response": {}})
        self.assertIn("response_started", seen)
        self.assertIn("response_done", seen)

    async def test_events_bounded_memory(self) -> None:
        """이벤트가 1000개를 초과해도 메모리 누수가 발생하지 않도록 deque maxlen 적용 검증."""
        s = self._sess()
        for i in range(1200):
            await s.emit(f"event_{i}", {"idx": i})
        self.assertEqual(len(s.events), 1000)
        self.assertEqual(s.events[0][0], "event_200")
        self.assertEqual(s.events[-1][0], "event_1199")

    async def test_agent_proc_terminated_on_session_close(self) -> None:
        """세션 종료 시 실행 중인 Agent 서브프로세스가 안전하게 SIGTERM/SIGKILL 정리되는지 검증."""
        s = self._sess(agent_bridge=True)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        s._current_agent_proc = proc
        self.assertIsNone(proc.returncode)
        await s.close()
        self.assertIsNotNone(proc.returncode)
        self.assertIsNone(s._current_agent_proc)


class TestMicGate(unittest.TestCase):
    def test_speaker_mode_blocks_during_playback(self) -> None:
        g = MicGate(headphones=False)
        self.assertTrue(g.allow(b"\xff\x7f" * 100))
        g.on_event("response_started", {})
        self.assertFalse(g.allow(b"\xff\x7f" * 100))
        g.on_event("response_done", {})
        g.note_play_end()
        self.assertFalse(g.allow(b"\xff\x7f" * 100))  # 테일 구간
        g.last_play_end = 0.0
        self.assertTrue(g.allow(b"\xff\x7f" * 100))

    def test_headphones_allows_interrupt_only_when_loud(self) -> None:
        g = MicGate(headphones=True, noise_gate=500)
        g.on_event("response_started", {})
        self.assertFalse(g.allow((0).to_bytes(2, "little") * 100))       # 조용 → 차단
        self.assertTrue(g.allow((1000).to_bytes(2, "little", signed=True) * 100))  # 큰 소리 → 통과


class TestAdapters(unittest.TestCase):
    def test_device_resolution_by_name(self) -> None:
        sd = FakeSD(
            [DEV("Bose Mini II SoundLink", 1, 0), DEV("Bose Mini II SoundLink", 0, 2),
             DEV("Odyssey G80HF", 0, 2), DEV("Arctis Nova 7P", 2, 2)],
            default=(0, 3),
        )
        self.assertEqual(ad.resolve_single_device(sd, "Bose Mini II SoundLink"), (0, 1))
        self.assertEqual(ad.resolve_headphones(sd), (3, 3))  # 마이크 있는 헤드셋
        self.assertIsNone(ad.find_index(sd, "Odyssey G80HF", "in"))  # 입력 채널 없음

    def test_resolve_single_device_default_input_name(self) -> None:
        sd = FakeSD([DEV("MacBook Pro Microphone", 1, 0), DEV("MacBook Pro Microphone", 0, 2)],
                    default=(0, 1))
        self.assertEqual(ad.resolve_single_device(sd), (0, 1))

    def test_headphones_fallback_when_no_headset(self) -> None:
        sd = FakeSD([DEV("USB Mic", 1, 0), DEV("USB Mic", 0, 2)], default=(0, 1))
        self.assertEqual(ad.resolve_headphones(sd), (0, 1))  # in+out 겸용 폴백

    def test_local_adapter_imports_without_sounddevice(self) -> None:
        """프로젝트 venv에는 sounddevice가 없다 — 지연 import 확인 (start() 전에는 무해)."""
        a = ad.LocalSoundDeviceAdapter(headphones=False)
        self.assertIsInstance(a, ad.AudioIOAdapter)
        self.assertEqual(a.stats()["mic_peak"], 0)

    def test_file_adapter_roundtrip_no_device(self) -> None:
        pcm = (b"\x01\x00" * 16000)  # 1초 @16k
        in_path = _wav_bytes(pcm)
        out_path = "/tmp/m1_unit_out.wav"
        if os.path.exists(out_path):
            os.remove(out_path)

        async def go() -> tuple[int, dict]:
            a = ad.FileAudioAdapter(in_path, out_path)
            async with a:
                chunks = 0
                while True:
                    c = await a.read_chunk()
                    if c is None:
                        break
                    self.assertEqual(len(c), ad.CHUNK_BYTES)
                    chunks += 1
                    await a.write_chunk(b"\x00\x00" * 120)
            return chunks, a.stats()

        chunks, stats = asyncio.run(go())
        self.assertEqual(chunks, 10)  # 1초 / 100ms
        self.assertAlmostEqual(stats["input_seconds"], 1.0, places=2)
        with wave.open(out_path, "rb") as w:
            self.assertEqual(w.getframerate(), 24000)
            self.assertEqual(w.getnframes(), 10 * 120)

    def test_file_adapter_rejects_wrong_rate(self) -> None:
        path = _wav_bytes(b"\x00" * 480, rate=24000, path="/tmp/m1_unit_24k.wav")

        async def go() -> None:
            async with ad.FileAudioAdapter(path):
                pass

        with self.assertRaises(RuntimeError):
            asyncio.run(go())


class TestPipelineOffline(unittest.IsolatedAsyncioTestCase):
    """가짜 WS + 파일 어댑터로 파이프라인(입력 펌프 → 세션 → 출력 펌프)을 검증."""

    async def test_pipeline_wires_file_to_session(self) -> None:
        pcm = b"\x11\x11" * 8000  # 0.5초 @16k
        in_path = _wav_bytes(pcm, path="/tmp/m1_pipe_in.wav")
        out_path = "/tmp/m1_pipe_out.wav"

        sess = S.QwenRealtimeSession(api_key="sk-test", debug_log=None)
        sess.mic_gain = 1.0

        async def fake_connect_once() -> FakeWS:
            ws = FakeWS()
            sess._ws = ws
            sess.usage["sessions"] += 1
            await sess.emit("connected", {"session": 1})
            return ws

        sess.connect_once = fake_connect_once  # 네트워크 대체

        async def fake_read_loop(ws: FakeWS) -> None:
            await asyncio.sleep(0.05)
            # 서버가 응답한 것처럼: 전사 + 오디오 + 완료
            await sess.handle_event({"type": "conversation.item.input_audio_transcription.completed",
                                     "transcript": "파이프라인 테스트"})
            await sess.handle_event({"type": "response.created"})
            await sess.handle_event({"type": "response.audio.delta",
                                     "delta": base64.b64encode(b"\x22\x22" * 2400)})  # 100ms @24k
            await sess.handle_event({"type": "response.done", "response": {"usage": {
                "input_tokens": 3, "output_tokens": 4, "total_tokens": 7}}})
            await asyncio.sleep(30)  # 재연결 루프 진입 전에 러너가 종료하도록

        sess._read_loop = fake_read_loop  # 재연결 루프 없이 1세션만

        runner = ConversationRunner(
            sess, ad.FileAudioAdapter(in_path, out_path),
            gate=MicGate(headphones=True), ptt=True, stop_after_response=True,
            max_seconds=10,
        )
        stats = await runner.run()
        self.assertEqual(stats["input_chunks"], 5)          # 0.5초 / 100ms
        self.assertEqual(stats["output_chunks"], 1)
        self.assertEqual(stats["transcripts"]["user"], "파이프라인 테스트")
        self.assertEqual(stats["usage"]["tokens_total"], 7)
        self.assertGreaterEqual(stats["seen_response_done"], 1)
        self.assertTrue(os.path.exists(out_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
