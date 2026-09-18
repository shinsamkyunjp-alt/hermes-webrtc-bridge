#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M3 진단 — 타이핑 입력(DataChannel cmd=text) 경로가 실제 응답을 만드는지 검증.

브리지 E2E에서 '텍스트 주입 후 AI 응답 없음'이 관측되어, WebRTC/브라우저를 배제한
순수 세션 레벨에서 어떤 조합이 응답을 만드는지 A/B로 확인한다.

  A) turn_detection=server_vad(현재 브리지 speaker 모드) + send_text
  B) turn_detection=None(manual) + send_text + commit + response.create

사용: .venv/bin/python tests/m3_text_probe.py [--mode a|b|both] [--seconds 30]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.session import QwenRealtimeSession, turn_detection_for  # noqa: E402

PHRASE = "오늘 반도체 시장 상황을 한 문장으로 알려줘"


async def probe(label: str, turn_detection, seconds: float, manual_commit: bool) -> dict:
    events: list[tuple[str, dict, float]] = []
    t0 = time.monotonic()
    session = QwenRealtimeSession(turn_detection=turn_detection, agent_bridge=False)
    session.subscribe(lambda kind, payload: events.append((kind, payload, round(time.monotonic() - t0, 2))))

    async def driver() -> None:
        while session.state != "connected" and time.monotonic() - t0 < 30:
            await asyncio.sleep(0.1)
        if session.state != "connected":
            print(f"[{label}] 연결 실패 (state={session.state})")
            return
        print(f"[{label}] connected @{round(time.monotonic()-t0,2)}s → 텍스트 주입")
        if manual_commit:
            await session.push_audio(b"\x00" * 3200)  # manual 모드: 빈 오디오 1청크
            await session.send_text(PHRASE)
            await session.commit()
            await session.create_response()
        else:
            await session.send_text(PHRASE)
        print(f"[{label}] 주입 완료 @{round(time.monotonic()-t0,2)}s")

    run_task = asyncio.create_task(session.run())
    drive_task = asyncio.create_task(driver())
    deadline = time.monotonic() + seconds
    got_assistant = False
    while time.monotonic() < deadline:
        if any(k == "assistant_transcript" and (p.get("text") or "").strip() for k, p, _ in events):
            got_assistant = True
            break
        await asyncio.sleep(0.2)
    drive_task.cancel()
    run_task.cancel()
    try:
        await run_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await session.close()

    diag = {
        "label": label,
        "turn_detection": str(turn_detection)[:120],
        "assistant_response": got_assistant,
        "assistant_text": session.transcripts.get("assistant", ""),
        "usage_tokens": session.usage.get("tokens_total"),
        "errors": [{"code": p.get("code"), "message": (p.get("message") or "")[:160]}
                   for k, p, _ in events if k == "error"],
        "event_kinds": [k for k, _, _ in events],
        "timeline": [{"kind": k, "t": t, "payload": json.dumps(p, ensure_ascii=False)[:120]}
                     for k, p, t in events][:40],
    }
    print(f"[{label}] assistant_response={got_assistant} tokens={diag['usage_tokens']}")
    print(f"[{label}] 이벤트: {diag['event_kinds']}")
    for e in diag["errors"]:
        print(f"[{label}] 오류: {e}")
    return diag


async def main(which: str, seconds: float) -> int:
    out: list[dict] = []
    if which in ("a", "both"):
        out.append(await probe("A: server_vad + send_text", turn_detection_for("speaker"), seconds, False))
    if which in ("b", "both"):
        out.append(await probe("B: manual(None) + send_text + commit + response.create", None, seconds, True))
    Path(ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / "m3_text_probe.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n리포트: out/m3_text_probe.json")
    return 0 if any(o["assistant_response"] for o in out) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="both", choices=["a", "b", "both"])
    ap.add_argument("--seconds", type=float, default=30.0)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.mode, a.seconds)))