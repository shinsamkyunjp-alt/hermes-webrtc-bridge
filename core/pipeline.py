# -*- coding: utf-8 -*-
"""Layer 2 — 세션 ↔ 어댑터 연결 파이프라인 (입력 펌프 / 출력 펌프 / 너).

M1의 목적은 "코어는 I/O를 모른다"를 실제로 성립시키는 것이다. 이 모듈이 그
경계를 잇는다.

  입력 펌프 : adapter.read_chunk() → (게이트) → session.push_audio()
  출력 프 : session.get_audio_stream() → adapter.write_chunk()
  러너      : 위 둘 + session.run()(재연결 루프) 수명주기 관리 + 이벤트 구독

`MicGate`는 원본 CLI의 `_mic_suppressed()` / 헤드폰 노이즈 게이트를 그대로 옮
것이다(스피커 모드 에코 방지, 헤드 모드 끼어들기 허용).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .adapter import AudioIOAdapter
from .persona import NOISE_GATE
from .session import QwenRealtimeSession, chunk_energy


class MicGate:
    """마이크 프레임 송신 허용 판정 (원본 RealtimeChat._mic_suppressed 계승)."""

    def __init__(
        self,
        headphones: bool = False,
        *,
        tail_seconds: float = 0.8,
        noise_gate: float = float(NOISE_GATE),
    ) -> None:
        self.headphones = headphones
        self.tail_seconds = tail_seconds
        self.noise_gate = noise_gate
        self.responding = False
        self.last_play_end = 0.0
        # 진단 카운터 — 게이트가 사용자 발화를 얼마나 먹었는지 수치로 확인
        self.passed = 0
        self.blocked = 0

    def stats(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocked": self.blocked,
            "responding": self.responding,
            "tail": self.in_tail,
            "since_play_end_s": round(time.time() - self.last_play_end, 2)
            if self.last_play_end
            else None,
        }

    def on_event(self, kind: str, payload: dict) -> None:
        """세션 이벤트로 상태 동기화 (구독 콜백으로 그대로 전달 가능)."""
        if kind == "response_started":
            self.responding = True
        elif kind == "response_done":
            self.responding = False
        elif kind == "error":
            self.responding = False

    def note_play_end(self) -> None:
        """출력 펌프가 실제로 스피커에 쓴 시각 — 테일 구간 판정용."""
        self.last_play_end = time.time()

    @property
    def in_tail(self) -> bool:
        return (time.time() - self.last_play_end) < self.tail_seconds

    def allow(self, chunk: bytes) -> bool:
        ok = self._allow_impl(chunk)
        if ok:
            self.passed += 1
        else:
            self.blocked += 1
        return ok

    def _allow_impl(self, chunk: bytes) -> bool:
        if self.headphones:
            # 헤드폰 모드: 재생 중/직후엔 큰 소리(끼어들기)만 통과
            if self.responding or self.in_tail:
                return chunk_energy(chunk) >= self.noise_gate
            return True
        # 스피커 모드: AI 발화 중·직후 프레임 전부 차단 (에코 방지)
        return not (self.responding or self.in_tail)


class ConversationRunner:
    """세션 + 어댑터를 어 한 번의 대화 파이프라인을 실행한다.

    * ``ptt=True``          : 입력 파일/버퍼가 소진되면 commit + response.create (테스트 경로)
    * ``stop_after_response``: 첫 응답 완료 시 자동 종료 (마이크 없는 검증용)
    * ``max_seconds``        : 안전 상한 (행 방지)
    """

    def __init__(
        self,
        session: QwenRealtimeSession,
        adapter: AudioIOAdapter,
        *,
        gate: MicGate | None = None,
        ptt: bool = False,
        stop_after_response: bool = False,
        max_seconds: float | None = None,
        connect_wait: float = 25.0,
        auto_response: bool = True,
    ) -> None:
        self.session = session
        self.adapter = adapter
        self.gate = gate
        self.ptt = ptt
        self.stop_after_response = stop_after_response
        self.max_seconds = max_seconds
        self.connect_wait = connect_wait
        self.auto_response = auto_response

        self._connected = asyncio.Event()
        self._response_done = asyncio.Event()
        self.input_chunks = 0
        self.skipped_chunks = 0
        self.output_chunks = 0
        self.error: str | None = None

    # -- event hooks --------------------------------------------------------
    def _on_event(self, kind: str, payload: dict) -> None:
        if kind == "connected":
            self._connected.set()
        elif kind == "disconnected":
            self._connected.clear()
        elif kind == "response_done":
            self._response_done.set()
        if self.gate is not None:
            self.gate.on_event(kind, payload)

    # -- pumps --------------------------------------------------------------
    async def input_pump(self) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=self.connect_wait)
        except asyncio.TimeoutError:
            self.error = f"세션 연결 대기 {self.connect_wait}초 초과"
            return
        while True:
            chunk = await self.adapter.read_chunk()
            if chunk is None:
                if self.ptt and self.auto_response:
                    await self.session.commit()
                    await self.session.create_response()
                return
            if self.gate is not None and not self.gate.allow(chunk):
                self.skipped_chunks += 1
                continue
            if await self.session.push_audio(chunk):
                self.input_chunks += 1

    async def output_pump(self) -> None:
        async for pcm in self.session.get_audio_stream():
            await self.adapter.write_chunk(pcm)
            self.output_chunks += 1
            if self.gate is not None:
                self.gate.note_play_end()

    async def _drain_audio(self) -> None:
        """출력 큐가 빌 때까지 대기 — 응답 마지막 구간 오디오 유실 방지."""
        while not self.session.audio_q.empty():
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)  # 마지막 write_chunk 완료 여유

    async def run(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        tasks: list[asyncio.Task] = []
        await self.adapter.start()
        self.session.subscribe(self._on_event)
        self.session.subscribe(
            lambda kind, payload: stats.update({f"seen_{kind}": stats.get(f"seen_{kind}", 0) + 1})
        )
        try:
            tasks.append(asyncio.create_task(self.session.run(), name="session"))
            tasks.append(asyncio.create_task(self.output_pump(), name="output"))
            tasks.append(asyncio.create_task(self.input_pump(), name="input"))
            waiters: list[asyncio.Task] = []
            if self.stop_after_response:
                waiters.append(asyncio.create_task(self._response_done.wait()))
            if self.max_seconds:
                waiters.append(asyncio.create_task(asyncio.sleep(self.max_seconds)))
            if waiters:
                done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
            else:  # 상한/종료 조건이 없으면 입력 소진 또는 취소까지 대기
                await tasks[2]
        finally:
            # 재생 큐에 남은 마지막 오디오를 스피커로 내보낸 뒤 정리한다 (끊김 방지)
            if self.stop_after_response:
                try:
                    await asyncio.wait_for(self._drain_audio(), timeout=1.5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.session.close()
            await self.adapter.stop()
        stats.update(
            {
                "input_chunks": self.input_chunks,
                "skipped_chunks": self.skipped_chunks,
                "output_chunks": self.output_chunks,
                "error": self.error,
                "usage": dict(self.session.usage),
                "transcripts": dict(self.session.transcripts),
                "adapter": self.adapter.stats(),
                "session_events": self.session.event_kinds(),
            }
        )
        return stats


async def run_pipeline(
    session: QwenRealtimeSession,
    adapter: AudioIOAdapter,
    **kwargs: Any,
) -> dict[str, Any]:
    """편의 함수 — ConversationRunner(session, adapter).run()."""
    return await ConversationRunner(session, adapter, **kwargs).run()