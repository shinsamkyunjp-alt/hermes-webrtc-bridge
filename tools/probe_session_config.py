#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""세션 설정 에코 프로브 — `session.update` 가 서버에 어떻게 반영되는지 확인한다.

M1 검증 중 발견: `turn_detection` 키를 **생략**하면 서버 기본값(server_vad,
threshold 0.5 / silence 800ms)이 그대로 살아 있어 manual(PTT) 모드가 성립하지
않았다. 이 프로브는 "manual 로 끄는 방법"이 실제로 통하는지 마이크 없이 확인한다.

사용:
  .venv/bin/python tools/probe_session_config.py            # 모든 후보 변형 시도
  .venv/bin/python tools/probe_session_config.py --variant null
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import websockets  # noqa: E402

from core.persona import DEFAULT_VAD, VOICE, WS_URL  # noqa: E402
from core.session import load_api_key  # noqa: E402

VARIANTS: dict[str, dict] = {
    # (a) 명시적 null — OpenAI Realtime 규격에서 VAD 비활성화를 뜻한다
    "null": {"turn_detection": None},
    # (b) 서버 VAD 유지 + 파라미터만 우리 값으로 (비교 기준)
    "server_vad": {"turn_detection": DEFAULT_VAD},
    # (c) 키 생략 (원본 CLI manual 경로가 쓰던 방식 → 실제로는 꺼지지 않음)
    "omitted": {},
}


async def probe(variant: str, extra: dict) -> dict:
    session_cfg = {"modalities": ["text", "audio"], "voice": VOICE}
    session_cfg.update(extra)
    key = load_api_key()
    async with websockets.connect(
        WS_URL, additional_headers={"Authorization": f"Bearer {key}"}, open_timeout=20
    ) as ws:
        created = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "session.update", "session": session_cfg}))
        result: dict = {"variant": variant, "sent": session_cfg, "created_turn_detection":
                        (created.get("session") or {}).get("turn_detection")}
        for _ in range(6):
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if ev.get("type") == "session.updated":
                s = ev.get("session") or {}
                result["updated_turn_detection"] = s.get("turn_detection")
                result["updated_voice"] = s.get("voice")
                result["updated_transcription"] = s.get("input_audio_transcription")
                break
            if ev.get("type") == "error":
                result["error"] = ev.get("error")
                break
        return result


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=sorted(VARIANTS), default=None)
    args = ap.parse_args()
    names = [args.variant] if args.variant else list(VARIANTS)
    for name in names:
        try:
            res = await probe(name, VARIANTS[name])
        except Exception as exc:  # noqa: BLE001
            res = {"variant": name, "exception": f"{exc.__class__.__name__}: {exc}"}
        print(json.dumps(res, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))