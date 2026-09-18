# -*- coding: utf-8 -*-
"""Layer 3 — FastAPI 시그널링 서버 + 단일 사용자 선점형 세션 매니저 (M2).

엔드포인트
  * `POST /api/offer`  : SDP Offer → RTCPeerConnection → SDP Answer 반환 (핵심)
  * `POST /api/hangup` : 통화 종료 (DashScope 세션 + 에이전트 서브프로세스 정리)
  * `GET  /api/status` : 세션/트랙/토큰 사용량 상태 (M3 UI · 무인 모니터링용)
  * `GET  /healthz`    : 스체크
  * `GET  /`           : 정적 통화 페이지 (M3에서 `static/index.html` 구현)

설계 요점 (PLAN §M2 / 리뷰어 P1 반영)
  * DataChannel `events` : 자막·VAD 상태·위임 상태를 JSON으로 브라우저에 푸시
  * 무음 프레임 + PTS 규칙 : `server.webrtc_adapter.QwenAudioTrack` 참조
  * Barge-in : `speech_started` 수신 시 송출 큐 flush
  * 선점형 단일 세션 : 신규 접속 시 기존 세션(DashScope WS + hermes --resume)을 안전하게 교체
  * `BRIDGE_AUTH_TOKEN` : 시그널링/페이지 접근 토큰 (미설정 시 경고 후 개방)
  * STUN + 선택적 TURN(DERP/Tailscale) 폴백 : 환경변수로 주입

⚠️ 실측 함정 (2026-09-15): aiortc는 `setRemoteDescription()` **수행 중에** `track`
   이벤트를 발생시킨다. 들러를 그 뒤에 등록하면 브라우저 마이크 트랙을 영영
   놓쳐 업링크가 무음이 된다 → PC 생성 직후, setRemoteDescription 이전에 등록할 것.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from core.persona import VOICE
from core.pipeline import ConversationRunner, MicGate
from core.session import QwenRealtimeSession, turn_detection_for
from server.webrtc_adapter import QwenAudioTrack, WebRTCAudioAdapter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
ICE_GATHER_TIMEOUT = 5.0
DEFAULT_STUN = "stun:stun.l.google.com:19302"

PLACEHOLDER_HTML = """<!doctype html>
<html lang="ko"><meta charset="utf-8"><title>Hermes WebRTC Bridge</title>
<body style="font-family:-apple-system,sans-serif;padding:2rem">
<h2>Hermes WebRTC 브리지 (M2 시그널링 서버)</h2>
<p>프론트엔드(<code>static/index.html</code>)는 M3에서 구현됩니다.</p>
<p>현재 사용 가능: <code>POST /api/offer</code> · <code>GET /api/status</code> · <code>POST /api/hangup</code></p>
</body></html>"""


# --------------------------------------------------------------------------- #
# ICE 설정
# --------------------------------------------------------------------------- #
def build_ice_servers() -> list[dict[str, Any]]:
    """STUN 1개 + (환경변수가 있으면) TURN 폴백 — LTE/5G Symmetric NAT 대비.

    Tailscale DERP 릴레이는 표준 TURN이 아니므로, 실제 릴레이 폴백이 필요하면
    `BRIDGE_TURN_URL` / `BRIDGE_TURN_USERNAME` / `BRIDGE_TURN_CREDENTIAL` 로
    TURN 자격증명을 주입한다(미설정 시 STUN만 — Tailnet 내 직접 P2P).
    """
    servers: list[dict[str, Any]] = []
    stun = os.environ.get("BRIDGE_STUN_URL", DEFAULT_STUN).strip()
    urls: list[str] = [u for u in stun.split(",") if u.strip()]
    if urls:
        servers.append({"urls": urls})
    turn = os.environ.get("BRIDGE_TURN_URL", "").strip()
    if turn:
        entry: dict[str, Any] = {"urls": [u for u in turn.split(",") if u.strip()]}
        user = os.environ.get("BRIDGE_TURN_USERNAME")
        cred = os.environ.get("BRIDGE_TURN_CREDENTIAL")
        if user:
            entry["username"] = user
        if cred:
            entry["credential"] = cred
        servers.append(entry)
    return servers


def force_loopback_ice() -> None:
    """로 루프백 전용 검증용 — 비-루프백 host candidate 수집을 막는다.

    macOS 로컬 네트워크 권한이 없는 프로세스는 LAN/Tailnet 주소로 나가는 UDP가
    조용히 드롭돼 ICE가 `checking`에서 멈춘다 (M0 실측). 서버/테스트가 같은
    호스트에서 검증할 때는 `BRIDGE_FORCE_LOOPBACK_ICE=1` 로 이 경로를 쓴다.
    """
    import aioice.ice

    aioice.ice.get_host_addresses = (  # type: ignore[assignment]
        lambda use_ipv4, use_ipv6: ["127.0.0.1"] if use_ipv4 else ["::1"]
    )


# --------------------------------------------------------------------------- #
# 세션 매니저
# --------------------------------------------------------------------------- #
class OfferRequest(BaseModel):
    sdp: str
    type: str = "offer"


class VoiceBridge:
    """단일 사용자 선점형(Preemptive Takeover) 세션 매니저.

    동시 접속이 발생하면 기존 PeerConnection + DashScope 세션 + 에이전트
    서브프로세스를 **안전하게 닫고** 새 접속으로 교체한다(비용 중복 방지).
    """

    def __init__(
        self,
        *,
        voice: str = VOICE,
        mode: str = "speaker",
        agent_bridge: bool = True,
        mic_gain: float | None = None,
        ice_servers: list[dict] | None = None,
        runner_connect_wait: float = 90.0,
    ) -> None:
        self.voice = voice
        self.mode = mode
        self.agent_bridge = agent_bridge
        self.mic_gain = mic_gain
        self.ice_servers = ice_servers if ice_servers is not None else build_ice_servers()
        self.runner_connect_wait = runner_connect_wait

        self._lock = asyncio.Lock()
        self.pc: Any = None
        self.session: QwenRealtimeSession | None = None
        self.adapter: WebRTCAudioAdapter | None = None
        self.track_out: QwenAudioTrack | None = None
        self.gate: MicGate | None = None
        self.runner: asyncio.Task | None = None
        self.dc: Any = None
        self._event_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=500)
        self._event_task: asyncio.Task | None = None
        self._dc_task: asyncio.Task | None = None
        self._pending_tracks: list[Any] = []
        self.started_at: float | None = None
        self.sessions_total = 0
        self.takeovers = 0
        self.last_close_reason: str | None = None
        # 종료된 세션의 실측 스냅 (진단용 — 통화 후에도 수치 확인 가능)
        self.last_snapshot: dict[str, Any] | None = None
        self.datachannel_negotiated_by: str | None = None
        self._sent_events = 0

    # -- 상태 --------------------------------------------------------------- #
    @property
    def alive(self) -> bool:
        return self.runner is not None and not self.runner.done()

    def status(self) -> dict[str, Any]:
        s = self.session
        return {
            "alive": self.alive,
            "mode": self.mode,
            "voice": self.voice,
            "agent_bridge": self.agent_bridge,
            "pc_state": getattr(self.pc, "connectionState", None) if self.pc else None,
            "ice_state": getattr(self.pc, "iceConnectionState", None) if self.pc else None,
            "datachannel": getattr(self.dc, "readyState", None) if self.dc else None,
            "datachannel_by": self.datachannel_negotiated_by,
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
            "sessions_total": self.sessions_total,
            "takeovers": self.takeovers,
            "last_close_reason": self.last_close_reason,
            "last_snapshot": self.last_snapshot,
            "events_sent": self._sent_events,
            "session_state": s.state if s else None,
            "turns": s.turn if s else 0,
            "usage": dict(s.usage) if s else {},
            "transcripts": dict(s.transcripts) if s else {},
            "adapter": self.adapter.stats() if self.adapter else {},
            "gate": self.gate.stats() if self.gate else None,
            "ice_servers": self.ice_servers,
        }

    # -- 이벤트 시 (DataChannel) ----------------------------------------- #
    def _push_event(self, kind: str, payload: dict) -> None:
        item = {"type": kind, "payload": payload, "ts": round(time.time(), 3)}
        try:
            self._event_q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._event_q.get_nowait()
                self._event_q.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def _event_pump(self) -> None:
        """이벤트  → DataChannel JSON 전송.

        ⚠️ DataChannel이 열리기 전(ICE 협상 중)에 발생한 이벤트를 버리면 M3 UI가
        `connected`/`session_started`를 놓친다 → 연결 전에는 보관했다가 전송한다.
        """
        pending: list[dict] = []
        while True:
            if not pending:
                pending.append(await self._event_q.get())
            dc = self.dc
            if dc is None or getattr(dc, "readyState", "closed") != "open":
                while True:  # 대기 중에도 큐를 비워 프로듀서 블로킹 방지
                    try:
                        pending.append(self._event_q.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if len(pending) > 500:
                    del pending[:-500]
                await asyncio.sleep(0.1)
                continue
            item = pending.pop(0)
            try:
                dc.send(json.dumps(item, ensure_ascii=False))
                self._sent_events += 1
            except Exception:  # noqa: BLE001 — 전송 실패 항목은 버린다 (루프 유지)
                pass

    async def _consume_datachannel(self, dc: Any, origin: str) -> None:
        """클라이언트 명령 수신: hangup / text(타이핑 입력)."""
        self.dc = dc
        self.datachannel_negotiated_by = origin
        self._push_event("bridge_ready", {"mode": self.mode, "voice": self.voice})

        @dc.on("message")
        def _on_message(message: Any) -> None:  # noqa: ANN401
            try:
                data = json.loads(message) if isinstance(message, str) else message
            except (json.JSONDecodeError, TypeError):
                return
            if not isinstance(data, dict):
                return
            cmd = data.get("cmd")
            if cmd == "hangup":
                asyncio.ensure_future(self.close("client hangup"))
            elif cmd == "text" and data.get("text"):
                text = str(data["text"])
                if self.session is not None:
                    async def _send_text_task(s: Any, t: str) -> None:
                        try:
                            await s.send_text(t)
                        except Exception as exc:
                            logger.error("send_text failed: %s", exc)
                    asyncio.create_task(_send_text_task(self.session, text))

        @dc.on("close")
        def _on_close() -> None:  # noqa: ANN202
            self._push_event("datachannel_closed", {})

    # -- 세션 수명 ---------------------------------------------------------- #
    async def close(self, reason: str = "closed") -> None:
        """PeerConnection + DashScope 세션 + 러너 태스크 정리 (리소스 누수 방지)."""
        async with self._lock:
            await self._close_locked(reason)

    async def _close_locked(self, reason: str) -> None:
        self.last_close_reason = reason
        # ️ 세션/어댑터 객체는 곧 정리되므로, 진단 수치를 여기서 복사해 남긴다.
        #    (통화가 끝난 뒤 '마이크가 실제로 들어갔는지' 확인할 유일한 방법)
        snap: dict[str, Any] | None = None
        try:
            snap = {
                "reason": reason,
                "closed_at": round(time.time(), 1),
                "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
                "turns": self.session.turn if self.session else 0,
                "usage": dict(self.session.usage) if self.session else {},
                "adapter": self.adapter.stats() if self.adapter else {},
            }
        except Exception:  # noqa: BLE001
            snap = None
        # ️ close() 가 두 번 불리면(예: api hangup 직후 peer-state-change) 두 번째
        #    호출이 텅 빈 스냅으로 덮어써 진단 데이터가 사라진다. 실제 데이터가
        #    있는 스냅만 채택한다.
        if snap and (snap.get("turns") or snap.get("usage") or snap.get("adapter")):
            self.last_snapshot = snap
        elif self.last_snapshot is None:
            self.last_snapshot = snap
        # 업링크 오디오 덤프 (ASR 오전사 = 한국어→중국어/영어 원인을 사후 분석)
        if self.adapter is not None:
            try:
                wav = self.adapter.dump_wav(PROJECT_ROOT / "out" / "uplink_last.wav")
                if wav and self.last_snapshot is not None:
                    self.last_snapshot["uplink_wav"] = wav
            except Exception:  # noqa: BLE001
                pass
        runner, self.runner = self.runner, None
        if runner is not None:
            runner.cancel()
            try:
                await runner
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        elif self.session is not None:  # 러너 없이 세션만 있는 경우
            try:
                await self.session.close()
            except Exception:  # noqa: BLE001
                pass
        for task in (self._event_task, self._dc_task):
            if task is not None and not task.done():
                task.cancel()
        self._event_task = None
        self._dc_task = None
        pc, self.pc = self.pc, None
        if pc is not None:
            try:
                await pc.close()
            except Exception:  # noqa: BLE001
                pass
        self.dc = None
        self.adapter = None
        self.track_out = None
        self.gate = None
        self.session = None
        self._pending_tracks = []
        self.started_at = None
        self.datachannel_negotiated_by = None

    async def handle_offer(self, sdp: str, sdp_type: str = "offer") -> str:
        """SDP Offer → (선점 takeover) → Answer SDP 반환."""
        if not sdp or "m=" not in sdp:
            raise HTTPException(status_code=400, detail="invalid SDP offer")

        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

        ice_servers = []
        for s in self.ice_servers:
            if isinstance(s, RTCIceServer):
                ice_servers.append(s)
            elif isinstance(s, dict):
                urls = s.get("urls", [])
                if isinstance(urls, str):
                    urls = [urls]
                ice_servers.append(
                    RTCIceServer(
                        urls=urls,
                        username=s.get("username"),
                        credential=s.get("credential"),
                    )
                )

        async with self._lock:
            if self.pc is not None:  # 선점형 교체
                self.takeovers += 1
                await self._close_locked("preempted by new offer")

            pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
            self.pc = pc
            track_out = QwenAudioTrack()
            self.track_out = track_out
            pc.addTrack(track_out)
            self._pending_tracks = []
            gather_done = asyncio.Event()

            @pc.on("icegatheringstatechange")
            def _on_gather() -> None:  # noqa: ANN202
                if pc.iceGatheringState == "complete":
                    gather_done.set()

            @pc.on("datachannel")
            def _on_datachannel(dc: Any) -> None:  # noqa: ANN401
                self._dc_task = asyncio.create_task(self._consume_datachannel(dc, "client"))

            @pc.on("track")
            def _on_track(track: Any) -> None:  # noqa: ANN401
                if track.kind == "audio":
                    self._pending_tracks.append(track)
                    if self.adapter is not None:
                        self.adapter.attach_input_track(track)
                else:
                    self._push_event("unsupported_track", {"kind": track.kind})

            @pc.on("connectionstatechange")
            async def _on_state() -> None:  # noqa: ANN202
                if pc.connectionState in ("failed", "closed"):
                    self._push_event("peer_closed", {"state": pc.connectionState})
                    if self.pc is pc:
                        await self.close(f"peer {pc.connectionState}")

            # ️ 핸들러는 반드시 setRemoteDescription 이전에 등록 (모듈 docstring 참조)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))

            # DataChannel: 브라우저가 만들었으면 그대로 쓰고, 아니면 서버가 시도.
            if "m=application" not in sdp:
                try:
                    dc = pc.createDataChannel("events")
                    self._dc_task = asyncio.create_task(self._consume_datachannel(dc, "server"))
                except Exception as exc:  # noqa: BLE001 — 없어도 통화는 성립해야 함
                    self._push_event("datachannel_error", {"error": str(exc)})

            await pc.setLocalDescription(await pc.createAnswer())
            if pc.iceGatheringState != "complete":
                try:
                    await asyncio.wait_for(gather_done.wait(), timeout=ICE_GATHER_TIMEOUT)
                except asyncio.TimeoutError:
                    self._push_event("ice_gather_timeout", {"timeout_s": ICE_GATHER_TIMEOUT})

            # --- 코어 세션 + 파이프라인 기동 (M1 자산 재사용) ---
            adapter = WebRTCAudioAdapter(track_out=track_out)
            self.adapter = adapter
            for track in self._pending_tracks:  # setRemoteDescription 중 도착분 배정
                adapter.attach_input_track(track)

            session_kwargs: dict[str, Any] = {
                "voice": self.voice,
                "turn_detection": turn_detection_for(self.mode),
                "agent_bridge": self.agent_bridge,
            }
            if self.mic_gain is not None:
                session_kwargs["mic_gain"] = self.mic_gain
            session = QwenRealtimeSession(**session_kwargs)
            self.session = session
            session.subscribe(self._push_event)  # DataChannel 자막/VAD/위임 상태
            session.subscribe(adapter.on_session_event)  # Barge-in flush

            gate = MicGate(headphones=(self.mode == "headphones"))
            self.gate = gate
            # ⚠️ 에코 게이트는 '실제 재생 타임라인'으로 구동해야 한다.
            # output_pump 의 note_play_end 는 DashScope 버스트가 '도착한' 시점을 찍는데,
            # 지터 버퍼(6초) 때문에 실제 송출은 그보다 수 초 뒤다 → 게이트가 재생 중에
            # 열려 AI 목소리가 마이크로 돌아오고 ASR 이 한국어를 중국어/영어로 오전사한다
            # (2026-09-17 실기기 실측). 트랙이 진짜 소리를 내보 때 갱신한다.
            if track_out is not None:
                track_out.on_play = gate.note_play_end
            runner = ConversationRunner(
                session,
                adapter,
                gate=gate,
                connect_wait=self.runner_connect_wait,
            )
            self.runner = asyncio.create_task(runner.run(), name="bridge-pipeline")
            self.started_at = time.time()
            self.sessions_total += 1
            if self._event_task is None or self._event_task.done():
                self._event_task = asyncio.create_task(self._event_pump(), name="bridge-events")

            self._push_event(
                "session_started",
                {"session": self.sessions_total, "mode": self.mode, "voice": self.voice},
            )
            return pc.localDescription.sdp


# --------------------------------------------------------------------------- #
# FastAPI 앱
# --------------------------------------------------------------------------- #
def create_app(bridge: VoiceBridge | None = None, token: str | None = None) -> FastAPI:
    bridge = bridge or VoiceBridge(
        voice=os.environ.get("BRIDGE_VOICE", VOICE),
        mode=os.environ.get("BRIDGE_MODE", "speaker"),
        agent_bridge=os.environ.get("BRIDGE_AGENT", "1") != "0",
    )
    token = token if token is not None else os.environ.get("BRIDGE_AUTH_TOKEN", "")

    async def require_token(request: Request) -> None:
        """시그널링/페이지 접근 토큰 검증 (P1 — 에이전트 도구 실행 보호)."""
        if not token:
            return
        header = request.headers.get("authorization", "")
        bearer = header[7:].strip() if header.lower().startswith("bearer ") else ""
        query = request.query_params.get("token", "")
        if bearer == token or query == token:
            return
        raise HTTPException(status_code=401, detail="invalid or missing bridge token")

    app = FastAPI(title="Hermes WebRTC Bridge", version="M2", dependencies=[Depends(require_token)])
    app.state.bridge = bridge
    app.state.token = token

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await bridge.close("server shutdown")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "alive": bridge.alive, "ts": round(time.time(), 3)}

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse(bridge.status())

    @app.post("/api/offer")
    async def api_offer(req: OfferRequest) -> JSONResponse:
        answer = await bridge.handle_offer(req.sdp, req.type)
        return JSONResponse({"sdp": answer, "type": "answer"})

    @app.post("/api/hangup")
    async def api_hangup() -> dict[str, Any]:
        await bridge.close("api hangup")
        return {"ok": True, "reason": bridge.last_close_reason}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if page.exists():
            return HTMLResponse(page.read_text(encoding="utf-8"))
        return HTMLResponse(PLACEHOLDER_HTML)

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Hermes WebRTC 브리지 시그널링 서버 (M2)")
    parser.add_argument("--host", default=os.environ.get("BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BRIDGE_PORT", "8000")))
    parser.add_argument("--mode", default=os.environ.get("BRIDGE_MODE", "speaker"),
                        choices=["speaker", "headphones", "manual"])
    parser.add_argument("--voice", default=os.environ.get("BRIDGE_VOICE", VOICE))
    parser.add_argument("--no-agent", action="store_true", help="Hermes 에이전트 브리지 비활성")
    parser.add_argument("--force-loopback-ice", action="store_true",
                        help="동일 호스트 검증용 — 127.0.0.1 host candidate만 수집")
    args = parser.parse_args()

    if args.force_loopback_ice or os.environ.get("BRIDGE_FORCE_LOOPBACK_ICE") == "1":
        force_loopback_ice()
    if not os.environ.get("BRIDGE_AUTH_TOKEN"):
        print("⚠️  BRIDGE_AUTH_TOKEN 미설정 — 시그널링 API가 무인증으로 열립니다 (Tailnet 외 노출 금지)")

    app = create_app(
        VoiceBridge(voice=args.voice, mode=args.mode, agent_bridge=not args.no_agent)
    )
    print(f"▶ Hermes WebRTC 브리지: http://{args.host}:{args.port}  (mode={args.mode}, voice={args.voice})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()