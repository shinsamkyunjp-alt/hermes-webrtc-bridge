#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/persona.py  원본 CLI 스크립트에서 **문자 그대로** 재생성한다.

리팩토링(M1) 동안 정본은 `~/.hermes/scripts/qwen_realtime_voice.py` 이다. 손으로
옮겨면 한국어 르소나 문장이 미세하게 틀어지므로(오타·글자 누락), 원본을
AST로 파싱해 소스 세그먼트를 그대로 복사한다. 원본은 절대 수정하지 않는다(읽기 전용).

사용:
  /Users/shinsamkyun/hermes-webrtc-bridge/.venv/bin/python tools/extract_persona.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SOURCE = Path(os.path.expanduser("~/.hermes/scripts/qwen_realtime_voice.py"))
TARGET = PROJECT / "core" / "persona.py"

# 원본에서 그대로 가져올 최상위 상수
WANTED = ("MODEL", "WS_URL", "VOICE", "DEBUG_LOG", "INSTRUCTIONS", "BRIDGE_TOOL")

HEADER = '''# -*- coding: utf-8 -*-
"""Hermes 음성 비서 페르소나 / 모델 상수 (Layer 1 데이터).

⚠️ 이 파일은 `tools/extract_persona.py` 가 생성한다 — 직접 편집하지 말 것.
원본: 열린 파일 `~/.hermes/scripts/qwen_realtime_voice.py` (읽기 전용 정본, M4 컷오버까지)
생성 시각: {ts}

상수 값은 원본의 소스 세그먼트를 AST로 그대로 복사한 것이다. 페르소나/보이스는
서버가 첫 `session.update` 시점에 고정하므로(스킬 pitfall 6) 변경 후에는
Stop → Start 로 세션을 재시작해야 반영된다.
"""

from __future__ import annotations

import os  # DEBUG_LOG / MIC_GAIN 원본 표현식이 참조
'''

FOOTER = '''

# ─ 아래는 원본에 리터럴로 박혀 있던 값들 (session_config / _run_once 에서 추출) ──

# 한국어 ASR 고정: 기본 fun-asr는 한국어를 한자/영문으로 혼입 표기한다.
# 검증됨: qwen3-asr-flash-realtime + language:ko = 한국어 전사 정상 (corpus = 편향 앵커)
INPUT_TRANSCRIPTION = {input_transcription}

ASR_TRANSCRIPTION_MODEL = "{asr_model}"

# 서버 VAD 기본값: 임계값↑ = 큰 소리만 턴 시작, 묵↑ = 은 소음에 턴이 끊기지 않음
DEFAULT_VAD = {default_vad}

# 헤드폰 모드(=끼어들기 허용)  감지
HEADPHONE_TURN_DETECTION = {headphone_td}

MIC_GAIN_DEFAULT = float(os.environ.get("QWEN_MIC_GAIN", "{mic_gain}"))
NOISE_GATE = {noise_gate}  # AI 재생 중 저에너지 프레임 차단 임계값 (헤드폰 모드)
AGENT_TIMEOUT = {agent_timeout}  # 헤르메스 에이전트 위임 최대 대기(초)

# 위임 브리지 도구 이름 (원본 BRIDGE_TOOL["name"] 과 동일해야 함)
BRIDGE_TOOL_NAME = BRIDGE_TOOL["name"]
'''


def _top_level_constants(src: str) -> dict[str, str]:
    """최상위 대입문 상수 → 원본 소스 세그먼트."""
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id in WANTED:
                out[tgt.id] = ast.get_source_segment(src, node.value)
    missing = [n for n in WANTED if n not in out]
    if missing:
        raise SystemExit(f"원본에서 상수를 찾지 못했습니다: {missing}")
    return out


def _literals(src: str) -> dict[str, object]:
    """session_config / _run_once 에 인라인으로 박힌 값들을 AST에서 뽑아낸다."""
    tree = ast.parse(src)
    found: dict[str, object] = {
        "input_transcription": None,
        "asr_model": None,
        "default_vad": None,
        "headphone_td": None,
        "mic_gain": None,
        "noise_gate": None,
        "agent_timeout": None,
    }
    for node in ast.walk(tree):
        # input_audio_transcription = {"model": ..., "language": ..., "corpus": {...}}
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "input_audio_transcription"
        ):
            found["input_transcription"] = ast.literal_eval(node.value)
        # "input_audio_transcription": {...}  (session_config 내부 dict 리터럴)
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "input_audio_transcription" \
                        and isinstance(v, ast.Dict):
                    found["input_transcription"] = ast.literal_eval(v)
            # 턴 감지 리터럴: {"type": "server_vad", ...} / {"type": "smart_turn"}
            try:
                d = ast.literal_eval(node)
            except Exception:  # noqa: BLE001  (변수 섞인 dict)
                d = None
            if isinstance(d, dict) and d.get("type") == "server_vad":
                found["default_vad"] = d
            if isinstance(d, dict) and d.get("type") == "smart_turn":
                found["headphone_td"] = d
        # MIC_GAIN = float(os.environ.get("QWEN_MIC_GAIN", "2.5"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
            pass
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("MIC_GAIN", "NOISE_GATE", "AGENT_TIMEOUT"):
                key = {"MIC_GAIN": "mic_gain", "NOISE_GATE": "noise_gate",
                       "AGENT_TIMEOUT": "agent_timeout"}[name]
                try:
                    found[key] = ast.literal_eval(node.value)
                except Exception:  # noqa: BLE001  MIC_GAIN = float(os.environ.get("QWEN_MIC_GAIN", "2.5"))
                    for sub in ast.walk(node.value):
                        if isinstance(sub, ast.Constant):
                            try:
                                found[key] = float(sub.value)
                                break
                            except (TypeError, ValueError):
                                continue
        # {"type": "smart_turn"}  → 헤드폰 턴 감지
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant) \
                        and k.value == "type" and v.value == "smart_turn":
                    found["headphone_td"] = {"type": "smart_turn"}
        # ASR 모델명: qwen3-asr-flash-realtime 문자열 리터럴
        if isinstance(node, ast.Constant) and node.value == "qwen3-asr-flash-realtime":
            found["asr_model"] = node.value
    missing = [k for k, v in found.items() if v is None]
    if missing:
        raise SystemExit(f"원본에서 값을 찾지 못했습니다: {missing}")
    return found


def main() -> int:
    src = SOURCE.read_text(encoding="utf-8")
    consts = _top_level_constants(src)
    lit = _literals(src)
    from datetime import datetime

    body = [HEADER.format(ts=datetime.now().isoformat(timespec="seconds"))]
    import textwrap

    for name in WANTED:
        # 원본 소스 세그먼트를 괄호로 감싸 그대로 붙인다 (여러 줄 표현식도 안전).
        seg = textwrap.dedent(consts[name]).strip()
        indented = "\n".join("    " + ln if ln.strip() else ln for ln in seg.splitlines())
        body.append(f"\n{name} = (\n{indented}\n)\n")
    body.append(
        FOOTER.format(
            input_transcription=repr(lit["input_transcription"]),
            asr_model=lit["asr_model"],
            default_vad=repr(lit["default_vad"]),
            headphone_td=repr(lit["headphone_td"]),
            mic_gain=lit["mic_gain"],
            noise_gate=lit["noise_gate"],
            agent_timeout=lit["agent_timeout"],
        )
    )
    text = "".join(body)
    TARGET.write_text(text, encoding="utf-8")
    print(f"✅ {TARGET} 재생성 완료 ({len(text)} chars, 원본 {SOURCE})")
    return 0


if __name__ == "__main__":
    sys.exit(main())