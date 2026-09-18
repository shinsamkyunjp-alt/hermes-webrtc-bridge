#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M4 진단 — Chrome fake-audio 캡처가 WAV 파일 내용을 실제 마이크 입력으로
전달하는지 무서버(독립)로 확인한다.

배경: M4 E2E 하네스에서 업링크 `input_peak=2`(사실상 무음)가 관측됐다 →
Chrome 이 `--use-file-for-fake-audio-capture` 파일을 못 읽거나, 포맷/레이트가
맞지 않아 무음을 주는 것으로 의심. 여러 WAV 변형을 차례로 시험한다.

사용: .venv/bin/python tests/m4_mic_probe.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.m3_browser_e2e import CDP  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PAGE = ROOT / "tests" / "m4_mic_probe.html"
OUT = ROOT / "out"
CDP_PORT = 9351
PROFILE = Path("/tmp/m4-probe-profile")


def write_pcm_wav(path: Path, rate: int, ch: int, seconds: float = 3.0, freq: float = 440.0,
                  amp: float = 0.4) -> Path:
    """Python wave 모듈로 직접  WAV (ffmpeg 헤더 의존 제거 변형)."""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    tone = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)
    data = np.repeat(tone[:, None], ch, axis=1) if ch > 1 else tone
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return path


def http_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


async def wait_devtools(timeout: float = 25.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return await asyncio.to_thread(http_json, f"http://127.0.0.1:{CDP_PORT}/json/list")
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.3)
    raise TimeoutError("DevTools 미기동")


def launch(wav: Path) -> subprocess.Popen:
    if PROFILE.exists():
        shutil.rmtree(PROFILE, ignore_errors=True)
    return subprocess.Popen([
        CHROME, "--headless=new", f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={wav}",
        "--autoplay-policy=no-user-gesture-required",
        PAGE.as_uri(),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def probe(cdp, label: str) -> dict:
    res = await cdp.eval("window.__probe(3)", await_promise=True, timeout=60)
    res["case"] = label
    print(f"  {label:38s} peak={res['peak']:.4f} rms={res['rms']:.4f} "
          f"rate={res.get('sampleRate')} settings={res.get('settings')}")
    return res


async def run_case(wav: Path, label: str) -> dict:
    import websockets

    proc = launch(wav)
    try:
        targets = await wait_devtools()
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            return {"case": label, "error": f"페이지 없음: {targets}"}
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024) as ws:
            cdp = CDP(ws)
            await cdp.call("Runtime.enable")
            for _ in range(40):
                try:
                    if await cdp.eval("typeof window.__probe === 'function'"):
                        break
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.25)
            return await probe(cdp, label)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        await asyncio.sleep(0.5)


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cases: list[tuple[Path, str]] = []

    # 1) M4 하네스가 쓰는 ffmpeg 48k mono (문제 재현 기준)
    ff = OUT / "m4_probe_ffmpeg48k_mono.wav"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "3", "-ac", "1", "-c:a", "pcm_s16le", str(ff)],
                   check=True, capture_output=True)
    cases.append((ff, "ffmpeg 48k mono (현 하네스 포맷)"))

    # 2) Python wave 48k mono
    cases.append((write_pcm_wav(OUT / "m4_probe_py48k_mono.wav", 48000, 1), "python wave 48k mono"))
    # 3) 44.1k stereo
    cases.append((write_pcm_wav(OUT / "m4_probe_py44k_stereo.wav", 44100, 2), "python wave 44.1k stereo"))
    # 4) 48k stereo
    cases.append((write_pcm_wav(OUT / "m4_probe_py48k_stereo.wav", 48000, 2), "python wave 48k stereo"))

    results = []
    for wav, label in cases:
        with wave.open(str(wav), "rb") as w:
            meta = (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes())
        print(f"\n[케이스] {label} — rate={meta[0]} ch={meta[1]} bits={meta[2]*8} frames={meta[3]}")
        try:
            results.append(await run_case(wav, label))
        except Exception as exc:  # noqa: BLE001
            print(f"  실패: {type(exc).__name__}: {exc}")
            results.append({"case": label, "error": f"{type(exc).__name__}: {exc}"})

    (OUT / "m4_mic_probe_report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 요약 ===")
    for r in results:
        print(f"  {r.get('case'):38s} → peak={r.get('peak')} rms={r.get('rms')} err={r.get('error')}")
    print("리포트: out/m4_mic_probe_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))