#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 검증 — 마이크/스피커를 열지 않는 실서버 라운드트립 테스트.

새 코어 파이프라인(`core.session` + `core.adapter.FileAudioAdapter` +
`core.pipeline.ConversationRunner`)이 실제 DashScope 실시간 모델과 한국어 왕복을
수행하는지 확인한다. 마이크가 필요 없고, 실행 중인 음성 세션의 장치를 건드리지도
않는다(스킬 pitfall 16).

  기본(음성 파이프라인):   합성 한국어 클립 → 인식 전사 → 응답 오디오/전사
  --agent(브리지 회귀):     위임 발화 → delegate_to_agent 툴 호출 → 에이전트 결과 회신

실행:
  cd /Users/shinsamkyun/hermes-webrtc-bridge
  .venv/bin/python tests/live_roundtrip.py
  .venv/bin/python tests/live_roundtrip.py --agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.adapter import FileAudioAdapter  # noqa: E402
from core.pipeline import ConversationRunner  # noqa: E402
from core.session import QwenRealtimeSession  # noqa: E402

DEFAULT_TEXT = "안녕 헤르메스야. 오늘 반도체 시장 관련해서 짧게 인사해줘."
AGENT_TEXT = "헤르메스 에이전트에게 위임해서 현재 시각이 몇 시인지 확인해서 알려."
SAMPLE_RATE = 16000  # DashScope 업링크 규격 (PCM16 mono)


def synth_korean_wav(text: str, out_wav: str, voice: str = "Yuna") -> str:
    """macOS `say` + ffmpeg으로 16 kHz mono PCM16 WAV 생성 (마이크 불필요)."""
    aiff = "/tmp/m1_in.aiff"
    subprocess.run(["say", "-v", voice, "-o", aiff, text], check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", aiff, "-ar", str(SAMPLE_RATE), "-ac", "1", out_wav],
        check=True,
        capture_output=True,
    )
    return out_wav


def wav_seconds(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


async def run_case(
    *, text: str, out_wav: str, agent: bool, voice: str, max_seconds: float
) -> dict:
    in_wav = synth_korean_wav(text, "/tmp/m1_in16k.wav")
    session = QwenRealtimeSession(
        voice=voice,
        turn_detection=None,  # manual(PTT) — 서버 VAD에 의존하지 않는 검증 경로
        agent_bridge=agent,
        mic_gain=1.0,  # 합성 음성은 이미 정상 레벨 → 게인 불필요
    )
    runner = ConversationRunner(
        session,
        FileAudioAdapter(in_wav, out_wav),
        gate=None,  # 파일 입력엔 에코 문제 없음 (게이트는 로컬 마이크용)
        ptt=True,  # 입력 소진 시 commit + response.create
        stop_after_response=True,
        max_seconds=max_seconds,
    )
    stats = await runner.run()
    stats["expect_delegation"] = agent
    stats["request_text"] = text
    return stats


def evaluate(stats: dict) -> list[str]:
    """검증 기준 판정 — 실패 사유 리스트 (빈 리스트 = 전부 통과)."""
    fails: list[str] = []
    user = (stats.get("transcripts") or {}).get("user", "")
    reply = (stats.get("transcripts") or {}).get("assistant", "")
    usage = stats.get("usage") or {}
    adapter_stats = stats.get("adapter") or {}

    if not user:
        fails.append("입력 전사(transcript) 없음 — ASR 이벤트 미수신")
    if not reply:
        fails.append("응답 전사 없음 — response.audio_transcript.done 미수신")
    min_seconds = 1.0 if stats.get("expect_delegation") else 2.0
    if adapter_stats.get("output_seconds", 0) < min_seconds:
        fails.append(
            f"응답 오디오가 너무 짧음 ({adapter_stats.get('output_seconds')}s < {min_seconds}s)"
        )
    if int(usage.get("tokens_total", 0)) <= 0:
        fails.append("토큰 사용량 0 — response.done usage 집계 실패")
    if int(stats.get("input_chunks", 0)) == 0:
        fails.append("업링크 청크 0 — push_audio 경로 미동작")
    if stats.get("error"):
        fails.append(f"러너 오류: {stats['error']}")
    if stats.get("expect_delegation") and int(usage.get("tool_calls", 0)) == 0:
        fails.append("에이전트 위임(tool_call) 미발생 — 브리지 회귀 실패")
    return fails


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", action="store_true", help="Hermes 에이전트 브리지 회귀까지 수행")
    ap.add_argument("--text", default=None)
    ap.add_argument("--voice", default=None)
    ap.add_argument("--out", default="/tmp/m1_reply.wav")
    ap.add_argument("--max-seconds", type=float, default=180.0)
    args = ap.parse_args()

    from core.persona import VOICE

    text = args.text or (AGENT_TEXT if args.agent else DEFAULT_TEXT)
    print(f"▶ 요청 텍스트: {text}", flush=True)
    stats = await run_case(
        text=text, out_wav=args.out, agent=args.agent,
        voice=args.voice or VOICE, max_seconds=args.max_seconds,
    )
    fails = evaluate(stats)

    print("\n── 결과 ──")
    print(f"입력 전사 : {stats['transcripts'].get('user')}")
    print(f"응답 전사 : {stats['transcripts'].get('assistant')}")
    print(f"업링크    : {stats['input_chunks']}청크 / {stats['usage']['audio_in_bytes']} bytes")
    print(f"다운링크  : {stats['output_chunks']}청크 / {stats['usage']['audio_out_bytes']} bytes "
          f"→ {stats['adapter'].get('output_seconds')}s ({args.out})")
    print(f"토큰      : total={stats['usage']['tokens_total']} "
          f"in={stats['usage']['tokens_in']} out={stats['usage']['tokens_out']} "
          f"audio_in={stats['usage']['tokens_audio_in']}")
    print(f"툴 호출   : {stats['usage']['tool_calls']}")
    print(f"세션 수   : {stats['usage']['sessions']} / 이벤트: {json.dumps(stats['session_events'], ensure_ascii=False)}")
    Path("/tmp/m1_roundtrip_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"상세 저장 : /tmp/m1_roundtrip_stats.json")

    if fails:
        print("\n❌ 검증 실패:")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("\n✅ 검증 통과 — 코어 파이프라인 실서버 라운드트립 정상")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))