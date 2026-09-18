#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M3 E2E — 실제 브라우저(Headless Chrome)로 static/index.html 통화 전 과정 검증.

구성
  [Headless Chrome (실 WebRTC/Opus + 실 JS)]  ←→  [FastAPI 브리지 (실 서버)]  ←→  [DashScope 실시간]

검증 (PLAN §M3 검증 기준: "데스크톱 브라우저에서 버튼 원클릭으로 통화 연결,
끊김 없는 음성 대화 및 자막 렌더링")
  1. 페이지 로드 + 단일 파일 SPA 렌더 (통화 시작 버튼 존재)
  2. 버튼 클릭 → getUserMedia(가짜 마이크) → SDP 교환 → PeerConnection connected
  3. 브라우저 → 서버 업링크 프레임 도달 (서버 어댑터 frames_in > 0)
  4. 서버 → 브라우저 다운링크 (브라우저 getStats inbound.bytesReceived > 0)
  5. 타이핑 입력 → DashScope 응답 → 브라우저 자막 DOM 렌더 (실시간 자막)
  6. VAD 상태/토큰 사용량/이벤트 카운터 DOM 갱신
  7. 통화 종료 버튼 → hangup + 서버 세션 정리
  8. 스크린샷 · DOM 스냅샷 산출

사용:
  .venv/bin/python tests/m3_browser_e2e.py                 # 전체(실 DashScope 호출 1회)
  .venv/bin/python tests/m3_browser_e2e.py --no-llm        # 텍스트 주입 없이 연결/오디오 경로만
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
from uvicorn import Config, Server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.app import VoiceBridge, create_app, force_loopback_ice  # noqa: E402

PORT = 8124
CDP_PORT = 9334
TOKEN = "m3-e2e-token"
OUT = ROOT / "out"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE = Path("/tmp/m3-chrome-profile")


# --------------------------------------------------------------------------- #
# 가짜 마이크 입력 (무음) — Chrome 의 --use-file-for-fake-audio-capture 용
# --------------------------------------------------------------------------- #
def build_silence_wav(path: Path, seconds: float = 8.0, rate: int = 48000) -> Path:
    """무음 WAV. 무음이면 VAD 오탐(불필요한 과금 턴)이 발생하지 않는다."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.zeros(int(rate * seconds), dtype=np.int16).tobytes())
    return path


# --------------------------------------------------------------------------- #
# CDP 최소 클라이언트 (websockets — venv 의존성)
# --------------------------------------------------------------------------- #
class CDP:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._id = 0
        self.events: list[dict] = []

    async def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise TimeoutError(f"CDP {method} 시간 초과")
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remain))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 오류: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)

    async def eval(self, expr: str, await_promise: bool = False, timeout: float = 30.0):
        res = await self.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
            timeout=timeout,
        )
        if res.get("exceptionDetails"):
            raise RuntimeError(f"JS 예외: {res['exceptionDetails'].get('text')} / {expr[:80]}")
        return res.get("result", {}).get("value")


def http_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_status_sync(path: str, token: str | None = TOKEN) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode()[:200]}


async def server_status(token: str | None = TOKEN) -> tuple[int, dict]:
    return await asyncio.to_thread(http_status_sync, "/api/status", token)


# --------------------------------------------------------------------------- #
# Chrome 기동
# --------------------------------------------------------------------------- #
def launch_chrome(page_url: str, fake_audio: Path) -> subprocess.Popen:
    if PROFILE.exists():
        shutil.rmtree(PROFILE, ignore_errors=True)
    args = [
        CHROME,
        "--headless=new",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run", "--no-default-browser-check", "--disable-gpu",
        "--disable-background-timer-throttling",
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={fake_audio}",
        "--autoplay-policy=no-user-gesture-required",
        "--allow-loopback-in-peer-connection",
        "--disable-features=WebRtcHideLocalIpsWithMdns",
        page_url,
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_devtools(timeout: float = 25.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return await asyncio.to_thread(http_json, f"http://127.0.0.1:{CDP_PORT}/json/list")
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.3)
    raise TimeoutError("DevTools 엔드포인트 미기동")


# --------------------------------------------------------------------------- #
# 메인
# --------------------------------------------------------------------------- #
async def main(no_llm: bool, wait_response: float, keep_open: float) -> int:
    import websockets

    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"stages": {}, "dom": {}, "stats": {}, "events": []}
    silence = build_silence_wav(OUT / "m3_silence48k.wav")

    force_loopback_ice()
    bridge = VoiceBridge(mode="speaker", agent_bridge=False, runner_connect_wait=60.0)
    app = create_app(bridge, token=TOKEN)
    server = Server(Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False))
    server_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        print("서버 기동 실패")
        return 1

    chrome: subprocess.Popen | None = None
    ws = None
    try:
        page_url = f"http://127.0.0.1:{PORT}/?token={TOKEN}"
        chrome = launch_chrome(page_url, silence)
        targets = await wait_devtools()
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            print("페이지 타깃 없음:", targets)
            return 1
        report["stages"]["devtools_target"] = {"type": page.get("type"), "url": page.get("url")}

        ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=20 * 1024 * 1024)
        cdp = CDP(ws)
        await cdp.call("Runtime.enable")
        await cdp.call("Page.enable")

        # -- 1) 페이지 로드/렌더 확인 (실서버가 static/index.html 서빙) -------- #
        deadline = time.monotonic() + 20
        ready = False
        while time.monotonic() < deadline:
            try:
                ready = bool(await cdp.eval("!!(window.__bridge && document.getElementById('startBtn'))"))
                if ready:
                    break
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.3)
        report["stages"]["page_ready"] = ready
        if not ready:
            raise RuntimeError("페이지/스크립트 초기화 실패")
        report["dom"]["title"] = await cdp.eval("document.title")
        report["dom"]["start_label"] = await cdp.eval("document.getElementById('startBtn').textContent")
        report["dom"]["initial_state"] = await cdp.eval("document.getElementById('statePill').textContent")
        report["dom"]["external_resources"] = await cdp.eval(
            "Array.from(document.querySelectorAll('script[src],link[href]')).map(e=>e.src||e.href)"
        )
        report["stages"]["secure_context"] = await cdp.eval("window.isSecureContext")
        report["stages"]["has_media_devices"] = await cdp.eval("!!(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia)")

        # -- 2) 통화 시작 버튼 클릭 → WebRTC 연결 ---------------------------- #
        t0 = time.monotonic()
        await cdp.eval("document.getElementById('startBtn').click()")
        phase = None
        while time.monotonic() - t0 < 30:
            phase = await cdp.eval("window.__bridge.phase()")
            if phase in ("live", "error"):
                break
            await asyncio.sleep(0.4)
        report["stages"]["connect_s"] = round(time.monotonic() - t0, 2)
        report["stages"]["phase_after_click"] = phase
        report["dom"]["state_pill"] = await cdp.eval("document.getElementById('statePill').textContent")
        report["dom"]["banner"] = await cdp.eval("document.getElementById('banner').textContent")
        report["dom"]["rtt"] = await cdp.eval("document.getElementById('rtt').textContent")
        report["dom"]["conn_info"] = await cdp.eval("document.getElementById('connInfo').textContent")
        if phase != "live":
            raise RuntimeError(f"브라우저 연결 실패: phase={phase} banner={report['dom']['banner']}")

        # -- 3) 업링크: 브라우저 마이크 → 서버 어댑터 ------------------------ #
        t1 = time.monotonic()
        frames_in = 0
        while time.monotonic() - t1 < 20:
            _, st = await server_status()
            frames_in = (st.get("adapter") or {}).get("frames_in") or 0
            if frames_in > 0:
                break
            await asyncio.sleep(0.5)
        report["stages"]["uplink_wait_s"] = round(time.monotonic() - t1, 2)
        report["stages"]["server_frames_in"] = frames_in

        # -- 4) 이벤트(DC) → DOM 자막 --------------------------------------- #
        if not no_llm:
            # DashScope 세션 웹소켓 연결 완료 대기 (조기 주입 레이스 방지)
            t_conn = time.monotonic()
            while time.monotonic() - t_conn < 20:
                _, st_curr = await server_status()
                if (st_curr.get("usage") or {}).get("sessions", 0) > 0:
                    break
                await asyncio.sleep(0.5)
            await cdp.eval(
                "(()=>{const i=document.getElementById('textInput');"
                "i.value='오늘 반도체 시장 상황을 한 문장으로 알려줘';"
                "document.getElementById('sendBtn').click();return true;})()"
            )
            t2 = time.monotonic()
            dom_asst = ""
            while time.monotonic() - t2 < wait_response:
                dom_asst = await cdp.eval(
                    "Array.from(document.querySelectorAll('.bubble.assistant'))"
                    ".map(e=>e.textContent).join(' | ')"
                ) or ""
                if dom_asst.strip():
                    break
                await asyncio.sleep(0.5)
            report["stages"]["assistant_render_s"] = round(time.monotonic() - t2, 2)
            report["dom"]["assistant_bubbles"] = dom_asst

        # -- 5) 다운링크: 서버 → 브라우저 (실 오디오 수신) ------------------- #
        t3 = time.monotonic()
        stats = None
        while time.monotonic() - t3 < 30:
            stats = await cdp.eval("window.__bridge.remoteStats()", await_promise=True)
            if stats and (stats.get("inbound", {}).get("bytesReceived") or 0) > 0:
                break
            await asyncio.sleep(0.5)
        report["stats"] = stats or {}
        report["stages"]["downlink_wait_s"] = round(time.monotonic() - t3, 2)

        await asyncio.sleep(keep_open)

        # -- 6) DOM 종합 스냅샷 --------------------------------------------- #
        report["dom"]["bubbles"] = await cdp.eval(
            "Array.from(document.querySelectorAll('.bubble')).map(e=>({k:e.className.replace('bubble ',''),"
            "t:e.textContent}))"
        )
        report["dom"]["vad_state"] = await cdp.eval("document.getElementById('vadState').textContent")
        report["dom"]["turns"] = await cdp.eval("document.getElementById('turnCount').textContent")
        report["dom"]["tokens"] = await cdp.eval("document.getElementById('tokenUsage').textContent")
        report["dom"]["event_count"] = await cdp.eval("document.getElementById('eventCount').textContent")
        report["dom"]["mic_level"] = await cdp.eval("document.getElementById('micLevel').textContent")
        report["dom"]["ai_level"] = await cdp.eval("document.getElementById('aiLevel').textContent")
        report["dom"]["viz_mode"] = await cdp.eval("window.__bridge.state.vizMode")

        # -- 7) 스크린샷 · DOM 저장 ----------------------------------------- #
        try:
            shot = await cdp.call("Page.captureScreenshot", {"format": "png"}, timeout=20)
            import base64
            (OUT / "m3_browser_screenshot.png").write_bytes(base64.b64decode(shot["data"]))
            report["stages"]["screenshot"] = str(OUT / "m3_browser_screenshot.png")
        except Exception as exc:  # noqa: BLE001
            report["stages"]["screenshot_error"] = str(exc)
        html = await cdp.eval("document.documentElement.outerHTML")
        (OUT / "m3_browser_dom.html").write_text(html or "", encoding="utf-8")

        _, st_before = await server_status()
        report["server_status_before_hangup"] = {
            "alive": st_before.get("alive"), "turns": st_before.get("turns"),
            "usage": st_before.get("usage"), "transcripts": st_before.get("transcripts"),
            "frames_in": (st_before.get("adapter") or {}).get("frames_in"),
            "silent_frames": ((st_before.get("adapter") or {}).get("track") or {}).get("silent_frames"),
        }

        # -- 8) 통화 종료 ---------------------------------------------------- #
        await cdp.eval("document.getElementById('hangupBtn').click()")
        await asyncio.sleep(2.0)
        report["stages"]["phase_after_hangup"] = await cdp.eval("window.__bridge.phase()")
        report["dom"]["state_pill_after_hangup"] = await cdp.eval("document.getElementById('statePill').textContent")
        _, st_after = await server_status()
        report["after_hangup"] = {"alive": st_after.get("alive"),
                                  "last_close_reason": st_after.get("last_close_reason")}
    finally:
        try:
            if ws is not None:
                await ws.close()
        except Exception:  # noqa: BLE001
            pass
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server_task.cancel()

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    dom = report.get("dom", {})
    stg = report.get("stages", {})
    inbound = (report.get("stats", {}).get("inbound") or {}).get("bytesReceived") or 0
    checks = {
        "페이지 로드 + __bridge 훅": stg.get("page_ready") is True,
        "단일 파일 SPA (외부 리소스 0)": (dom.get("external_resources") or []) == [],
        "시작 상태 라벨 = 대기": dom.get("initial_state") == "대기",
        "버튼 원클릭 → 통화 연결(live)": stg.get("phase_after_click") == "live",
        "상태 필 '통화 중'": dom.get("state_pill") == "통화 중",
        "업링크: 브라우저 마이크 → 서버 프레임": (stg.get("server_frames_in") or 0) > 0,
        "다운링크: 브라우저 오디오 수신(bytes>0)": inbound > 0,
        "통화 종료 → 시작 버튼 복귀": stg.get("phase_after_hangup") in ("ended", "idle"),
        "hangup 후 서버 세션 정리": report.get("after_hangup", {}).get("alive") is False,
    }
    if not no_llm:
        checks["자막: AI 답변 DOM 렌더"] = bool((dom.get("assistant_bubbles") or "").strip())
        checks["턴/토큰 카운터 갱신"] = (int(dom.get("turns") or 0) > 0) or (int(dom.get("tokens") or 0) > 0)

    print("\n=== M3 브라우저 E2E 판정 ===")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    ok = all(checks.values())
    print(f"\n판정: {'통과' if ok else '실패'} ({sum(checks.values())}/{len(checks)})")
    report["checks"] = checks
    report["passed"] = ok
    (OUT / "m3_browser_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("리포트: out/m3_browser_report.json")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="텍스트 주입/응답 대기 생략 (연결·오디오 경로만)")
    ap.add_argument("--wait-response", type=float, default=45.0, help="AI 자막 대기 상한(초)")
    ap.add_argument("--keep-open", type=float, default=3.0, help="다운링크 수신 후 추가 관찰 시간(초)")
    args = ap.parse_args()
    os.environ.setdefault("BRIDGE_AUTH_TOKEN", TOKEN)
    if not Path(CHROME).exists():
        print(f"Chrome 없음: {CHROME}")
        sys.exit(2)
    sys.exit(asyncio.run(main(args.no_llm, args.wait_response, args.keep_open)))