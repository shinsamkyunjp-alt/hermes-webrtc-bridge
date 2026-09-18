# -*- coding: utf-8 -*-
"""M4 테스트 자산 — 브라우저 안에서 **실제 음성 스트림을 만들어 WebRTC 업링크로 주입**.

왜 필요한가 (2026-09-15 실측)
  Chrome `--use-file-for-fake-audio-capture` 는 이 빌드에서 파일 내용을 전혀
  전달하지 않는다(무음). `tests/m4_mic_probe.py` 로 4가지 WAV 포맷
  (ffmpeg/파이썬, 44.1k/48k, mono/stereo)을 모두 시험했으나 **peak=0.0000** →
  가짜-마이크 파일 경로는 M4 E2E 자극원으로 쓸 수 없다(무음이면 서버 VAD 가 턴을
  못 잡아 실음성 왕복 검증이 불가능하다).

대안 (이 모듈)
  브라우저 페이지 안에서 `fetch(WAV) → decodeAudioData → AudioBufferSourceNode →
  MediaStreamAudioDestinationNode` 로 **MediaStreamTrack 을 합성**하고,
  `RTCRtpSender.replaceTrack()` 으로 그 트랙을 업링크에 꽂는다.
  → 실제 음성 파형이 **실 Opus/WebRTC 전송**을 그대로 통과한다(무음 아님을
     AnalyserNode 로 자체 증명한다).

서버 코드/`static/index.html` 은 건드리지 않는다. WAV 는 테스트가 띄우는
별도 정적 미디어 서버(`http://127.0.0.1:<port>/media/...`, CORS 허용)에서 받는다.

제어 API (페이지 전역):
  window.__m4audio.load(url)              → 디코드(지속시간 반환)
  window.__m4audio.play(url, loop)        → 송신 트랙 교체 + 재생
  window.__m4audio.mute()                 → 재생 중단 + replaceTrack(null)
  window.__m4audio.sample()               → 현재 주입 파형 peak/rms
  window.__m4audio.probe(ms)              → ms 구간 최대 peak/rms (비무음 증명)
  window.__m4audio.status()               → 재생/컨텍스트 상태
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# --------------------------------------------------------------------------- #
# 브라우저에 주입하는 JS (설치는 1회, 멱등)
# --------------------------------------------------------------------------- #
INSTALL_JS = r"""
(function(){
  if (window.__m4audio) return 'exists';
  const A = {
    ctx: null, src: null, dest: null, wavs: {}, meter: null, t0: null, dur: 0,
    async _ctx(){
      if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === 'suspended') { try { await this.ctx.resume(); } catch (e) {} }
      return this.ctx;
    },
    async load(url){
      if (this.wavs[url]) return {url: url, cached: true, dur: this.wavs[url].duration};
      const ctx = await this._ctx();
      const r = await fetch(url, {cache: 'no-store'});
      if (!r.ok) throw new Error('fetch ' + r.status + ' ' + url);
      const buf = await ctx.decodeAudioData(await r.arrayBuffer());
      this.wavs[url] = buf;
      return {url: url, dur: buf.duration, rate: buf.sampleRate, ch: buf.numberOfChannels};
    },
    _sender(){
      const pc = window.__bridge && window.__bridge.state ? window.__bridge.state.pc : null;
      if (!pc) throw new Error('no-peerconnection');
      if (!window.__m4sender){
        window.__m4sender = pc.getSenders().find(x => x.track && x.track.kind === 'audio') || pc.getSenders()[0];
      }
      if (!window.__m4sender) throw new Error('no-sender');
      return window.__m4sender;
    },
    _stopSource(){ if (this.src) { try { this.src.stop(); } catch (e) {} this.src = null; } return true; },
    async play(url, loop){
      const ctx = await this._ctx();
      await this.load(url);
      if (!this.dest) this.dest = ctx.createMediaStreamDestination();
      const track = this.dest.stream.getAudioTracks()[0];
      const s = this._sender();
      if (s.track !== track) await s.replaceTrack(track);
      this._stopSource();
      if (!this.meter) { this.meter = ctx.createAnalyser(); this.meter.fftSize = 2048; }
      const src = ctx.createBufferSource();
      src.buffer = this.wavs[url];
      src.loop = !!loop;
      src.connect(this.meter);
      src.connect(this.dest);
      src.start();
      this.src = src;
      this.t0 = ctx.currentTime;
      this.dur = this.wavs[url].duration;
      return {playing: true, loop: !!loop, dur: this.wavs[url].duration, ctxState: ctx.state,
              senderTrack: s.track ? s.track.kind : null};
    },
    async mute(){
      this._stopSource();
      this.t0 = null;
      const s = this._sender();
      await s.replaceTrack(null);
      return 'muted';
    },
    async unmute(url, loop){
      return await this.play(url, loop === undefined ? true : loop);
    },
    pos(){
      // 루프 재생 위치(초) — 발화 구간 종료 시점을 테스트가 알 수 있게 한다
      if (!this.ctx || this.t0 === null) return null;
      const el = this.ctx.currentTime - this.t0;
      return this.dur > 0 ? +(el % this.dur).toFixed(3) : null;
    },
    sample(){
      if (!this.meter) return {peak: null, rms: null, pos: this.pos()};
      const buf = new Float32Array(this.meter.fftSize);
      this.meter.getFloatTimeDomainData(buf);
      let peak = 0, sum = 0;
      for (let i = 0; i < buf.length; i++) { const a = Math.abs(buf[i]); if (a > peak) peak = a; sum += buf[i] * buf[i]; }
      return {peak: +peak.toFixed(5), rms: +Math.sqrt(sum / buf.length).toFixed(5), pos: this.pos()};
    },
    probe(ms){
      const self = this;
      const t0 = performance.now();
      let peak = 0, acc = 0, n = 0, frames = 0;
      return new Promise(res => {
        const tick = () => {
          const s = self.sample();
          if (s.peak !== null) { if (s.peak > peak) peak = s.peak; acc += s.rms * s.rms; n++; }
          frames++;
          if (performance.now() - t0 < (ms || 1500)) requestAnimationFrame(tick);
          else res({peak: +peak.toFixed(5), rms: +Math.sqrt(acc / Math.max(n, 1)).toFixed(5), frames: frames});
        };
        tick();
      });
    },
    status(){
      return {playing: !!this.src, hasCtx: !!this.ctx,
              ctxTime: this.ctx ? +this.ctx.currentTime.toFixed(2) : null,
              ctxState: this.ctx ? this.ctx.state : null};
    }
  };
  window.__m4audio = A;
  return 'ok';
})()
"""


# --------------------------------------------------------------------------- #
# 미디어 서버 (테스트 전용 — 프로젝트 서버 코드는 건드리지 않는다)
# --------------------------------------------------------------------------- #
def create_media_app(media_dir: Path) -> FastAPI:
    """`/media/<file>.wav` 정적 제공 + CORS (브라우저 fetch 용)."""
    app = FastAPI(title="M4 test media server")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
    return app


# --------------------------------------------------------------------------- #
# 편의 래퍼 (CDP eval)
# --------------------------------------------------------------------------- #
async def install(cdp) -> str:
    return await cdp.eval(INSTALL_JS)


async def load_wav(cdp, url: str) -> dict:
    return await cdp.eval(f"window.__m4audio.load({url!r})", await_promise=True, timeout=30)


async def play(cdp, url: str, loop: bool = True) -> dict:
    return await cdp.eval(f"window.__m4audio.play({url!r}, {str(bool(loop)).lower()})",
                          await_promise=True, timeout=30)


async def mute(cdp) -> str:
    return await cdp.eval("window.__m4audio && window.__m4audio.mute()", await_promise=True)


async def unmute(cdp, url: str, loop: bool = True) -> dict:
    return await cdp.eval(
        f"window.__m4audio && window.__m4audio.unmute({json.dumps(url)}, {json.dumps(loop)})",
        await_promise=True,
    )


async def probe(cdp, ms: int = 1500) -> dict:
    return await cdp.eval(f"window.__m4audio.probe({int(ms)})", await_promise=True, timeout=30)


async def sample(cdp) -> dict:
    return await cdp.eval("window.__m4audio.sample()") or {}


async def status(cdp) -> dict:
    return await cdp.eval("window.__m4audio.status()") or {}