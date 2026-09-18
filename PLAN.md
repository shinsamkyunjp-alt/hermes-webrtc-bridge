# [개발 계획서] Qwen 실시간 음성 비서 WebRTC 브리지 구축

> 작성: 2026-09-14 | 멀티 에이전트 파이프라인(탐색이→분석이→작문이) 산출물
> 상태: **계획 수립 완료 + 리뷰어 검토 반영 완료 (구현 전)** — 삼균 님 승인 후 M0 착수
> 리뷰 이력: 2026-09-14 분석이(리뷰어) 독립 검토 → **PASS-WITH-FIXES**, P1 5건 + P2 4건 + P3 2건 반영 완료

---

## 1. 핵심 요약 (Executive Summary)

* **목표**: Mac mini(M4)에서 동작 중인 Qwen 음성 비서(`qwen_realtime_voice.py`)를 **WebRTC 기반 전이중(Full-Duplex) 실시간 음성 통화 시스템**으로 확장하여, 스마트폰/PC 브라우저에서 '전화 통화하듯' 자연스러운 대화를 구현한다.
* **핵심 아키텍처**: 기존 모놀리식 스크립트를 **3계층 구조**(코어 엔진 ↔ I/O 어댑터 ↔ FastAPI/aiortc 시그널링 서버)로 분리 리팩토링하여 로컬 마이크 모드와 원격 WebRTC 모드를 동시 지원한다.
* **통신 & 인프라**: **aiortc + PyAV**로 단일 Python 비동기 이벤트 루프 내에서 저지연(0.05ms 미만) 오디오 트랜스코딩(Opus 48kHz ↔ PCM 16k/24k) 처리, **Tailscale Serve**로 모바일 필수 HTTPS 환경을 zero-config로 확보.
* **예상 일정**: M0(사전검증)부터 M4(모바일 실증)까지 **총 5~7영업일(약 14~18시간 공수)** 예상. M5 네이티브 백그라운드 앱은 선택적 2단계 과제.

## 2. 목표 아키텍처

### 2.1 3계층 모듈화 아키텍처

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Layer 3: Presentation                           │
│  [Web Client (Safari/Chrome)]  /  [Hermes-Relay App (Foreground Svc)]  │
│  - getUserMedia (AEC/NS/AGC)        - AudioRecord / AudioTrack (PCM)   │
│  - RTCPeerConnection (Opus 48k)     - WebSocket JSON/Binary            │
└─────────────────────────────────┬──────────────────────────────────────┘
                                  │ WebRTC (SRTP/Opus + DataChannel) / WSS
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        Layer 2: Transport & I/O                        │
│  [FastAPI Signaling Server + aiortc Endpoint] (Port 8000 / Tailscale)  │
│  ├─ WebRTCAudioAdapter: Opus 48k ⇄ PyAV Resampler ⇄ PCM 16k/24k        │
│  └─ LocalSoundDeviceAdapter: (기존 sounddevice 입출력 - 레거시 호환)  │
└─────────────────────────────────┬──────────────────────────────────────┘
                                  │ async push_audio() / get_audio()
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        Layer 1: Core Engine                            │
│  [QwenRealtimeSession]                                                 │
│  ├─ DashScope WebSocket Manager (지수 백오프 자동 재연결, 180s Keepalive)│
│  ├─ Event Parser & Dispatcher (ASR 텍스트, TTS PCM 스트림, 토큰 추적)  │
│  └─ Agent Bridge (`hermes chat --resume` 서브프로세스 툴 실행기)       │
└─────────────────────────────────┬──────────────────────────────────────┘
                                  │ DashScope Realtime Protocol (WSS)
                                  ▼
                     [Alibaba DashScope Cloud]
```

### 2.2 오디오 데이터 스트림 파이프라인

| 방향 | 오디오 경로 및 트랜스코딩 흐름 | 버퍼/패킷 규격 |
| :--- | :--- | :--- |
| **업링크 (User → AI)** | `Mic (48kHz)` → `WebRTC (Opus 48kHz)` → `aiortc AudioTrack` → `PyAV Resampler (16kHz PCM16 Mono)` → `DashScope WS (base64 append)` | 100ms 단위 (3,200 Bytes) 누적 전송 |
| **다운링크 (AI → User)** | `DashScope WS (24kHz PCM16 Mono delta)` → `PyAV Resampler (48kHz PCM16 Mono)` → `aiortc CustomTrack` → `WebRTC (Opus 48kHz)` → `Speaker` | 20ms 단위 (960 Samples) 패킷 스트리밍 |
| **보조 (Signal/자막)** | DashScope 텍스트·상태 이벤트(자막, VAD/listening-thinking-speaking, 에이전트 위임) → JSON → **WebRTC DataChannel** → 브라우저 UI 렌더링 | 이벤트 발생 즉시 |

## 3. 마일스톤별 상세 실행 계획 (M0 ~ M5)

### 📌 M0: 사전 준비 및 기반 환경 검증
* **목표**: M4 Mac mini 개발 환경에 필수 라이브러리 설치 및 aiortc 루프백 동작 검증.
* **작업 항목**:
  1. `aiortc`, `av` (PyAV), `fastapi`, `uvicorn[standard]`, `soxr-python` 가상환경 설치.
  2. aiortc 오디오 루프백(Opus 수신 → PCM 변환 → Opus 송출) 독립 테스트 스크립트 작성 및 레이턴시 측정.
  3. `tailscale serve`를 통한 8443 포트 HTTPS 바인딩 및 인증서 발급 동작 확인.
     ⚠️ **실측 함정**(2026-09-14): Tailscale 계정에서 TLS 인증서 기능이 미활성화면 `tailscale cert`가
     `500 Internal Server Error: your Tailscale account does not support getting TLS certs`를 반환.
     → **Admin Console에서 HTTPS Certificates(MagicDNS) 활성화 선행 필수**, 또는 mkcert 기반 자체서명 인증서 폴백 옵션 병행.
     * 추가: Mac의 `tailscaled`는 `--socket=/Users/shinsamkyun/.tailscale/tailscaled.sock`로 구동 중
       → 명령 실행 시 `TS_SOCKET` 환경변수/`--socket` 플래그 명시.
* **검증 기준**: 로컬 브라우저에서 `https://<tailnet-name>:8443` 접속 시 마이크 권한 정상 획득 및 50ms 이내 에코 루프백 확인.
* **공수/담당**: 1.5시간 / 에이전트 (사용자는 Tailscale 권한 확인 지원)

### 📌 M1: 코어 엔진 분리 및 인터페이스 추상화
* **목표**: `qwen_realtime_voice.py`(836줄)에서 I/O 의존성을 분리하고 순수 스트리밍 세션 클래스 추출.
* **작업 항목**:
  1. `QwenRealtimeSession` 코어 클래스 정의:
     * DashScope WebSocket 세션 수명주기, 지수 백오프 재연결(`SessionDied` 대응).
     * 비동기 인터페이스 표준화: `async def push_audio(chunk: bytes)` / `async def get_audio_stream() -> AsyncIterator[bytes]`.
     * Hermes 브리지 함수 호출(`_handle_function_call`, `_run_agent_sync`) 및 토큰 사용량 집계 보존.
  2. `AudioIOAdapter` 추상 베이스 클래스 작성.
  3. `LocalSoundDeviceAdapter`를 작성하여 기존 `sounddevice.InputStream / OutputStream`을 새 인터페이스에 결합.
* **검증 기준**: 기존 CLI 로컬 마이크 모드로 음성 대화 및 Hermes 에이전트 툴 호출 회귀 테스트 100% 통과.
* **공수/담당**: 3.5시간 / 에이전트

### 📌 M2: WebRTCAudioAdapter 및 FastAPI 시그널링 서버 구축
* **목표**: aiortc 기반의 WebRTC 미디어 파이프라인 및 SDP Offer/Answer 시그널링 엔드포인트 구축.
* **작업 항목**:
  1. `WebRTCAudioAdapter` 구현:
     * `MediaStreamTrack`을 상속받은 `QwenAudioTrack`(DashScope PCM 24k → PyAV 48k 변환 후 브라우저 송출).
     * 브라우저 Opus 트랙 수신 핸들러(PyAV 48k → 16k 변환 후 `push_audio` 전달).
  2. FastAPI 기반 세션 관리 서버(`server.py`):
     * `POST /api/offer`: SDP Offer 수신 → RTCPeerConnection 생성 → ICE Candidate 교환 → SDP Answer 반환.
     * STUN(`stun:stun.l.google.com:19302`) + Tailscale DERP 릴레이 폴백 설정 (LTE/5G Symmetric NAT 대비).
     * 세션 종료/연결 끊김 시 DashScope 세션 안전 종료(리소스 누수 방지).
     * **DataChannel `events` 생성**: 자막(ASR/어시스턴트 텍스트), VAD 상태(listening/thinking/speaking),
       에이전트 위임 상태를 JSON으로 브라우저에 실시간 푸시. (M3 UI와 연동)
     * **무음 프레임 + PTS 규칙 (필수)**: `QwenAudioTrack.recv()`는 매 20ms(48kHz·960샘플·s16/모노)마다
       `av.AudioFrame`(`pts` 960씩 증가, `time_base=1/48000`) 반환. 지터 버퍼 언더런(버스트 틈/유휴) 시
       **0값 무음 프레임 방출** → RTP 스트림 유지·재생 싱크 보호.
     * **Barge-in(말 끊기) 플러시**: `input_audio_buffer.speech_started` 수신 시 WebRTC 송출 큐 즉시
       `clear()` → AI 발화가 스마트폰에서 즉각 중단 (기존 로컬 `play_q.clear()` 로직 계승).
     * **단일 사용자 선점형 세션(Preemptive Takeover)**: 신규 브라우저 접속 시 기존 세션(DashScope WS +
       Hermes `--resume` 서브프로세스)을 안전하게 닫고 교체 — 동시 접속·비용 중복 방지.
* **검증 기준**: `curl` 또는 Python 테스트 클라이언트로 SDP 교환 후 실시간 양방향 오디오 파이프라인 수립 확인.
* **공수/담당**: 4.0시간 / 에이전트

### 📌 M3: 반응형 웹 프론트엔드 구축 (단일 HTML5 SPA)
* **목표**: 모바일 최적화 통화 UI 및 WebRTC 클라이언트 완성 (외부 빌드 도구 없는 무의존성 단일 HTML).
* **작업 항목**:
  1. WebRTC 연결 로직 구현: `RTCPeerConnection`, `getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })`.
  2. 모바일 브라우저 필수 기능 적용:
     * **Screen WakeLock API**: 통화 중 화면 꺼짐 방지.
     * **오디오 컨텍스트 자동 재생 정책 해제**: 통화 시작 버튼 클릭 시 AudioContext Unlock.
  3. 통화 상태 UI (대기/연결중/통화중/음소거/종료, VAD 실시간 볼륨 시각화 게이지, 실시간 대화 자막 표시).
* **검증 기준**: 데스크톱 브라우저에서 버튼 원클릭으로 통화 연결, 끊김 없는 음성 대화 및 자막 렌더링 확인.
* **공수/담당**: 2.5시간 / 에이전트

### 📌 M4: E2E 통합 및 모바일 실증 테스트
* **목표**: 실제 iOS Safari 및 Android Chrome 환경에서 무선 실시간 음성 통화 실증.
* **작업 항목**:
  1. Mac mini에서 `tailscale serve --https=8443 http://127.0.0.1:8000` 활성화.
  2. iPhone(Safari) 및 Android(Chrome)에서 Tailscale 사설 도메인으로 접속 테스트.
  3. 실시간 인터럽트(말 끊기/Barge-in), 3분 이상 장기 통화, 네트워크 전환(Wi-Fi ↔ LTE) 내구성 테스트.
* **검증 기준**:
  * 왕복 지연시간(RTT + AI 생성) 600ms 이내 체감.
  * 스피커폰 모드에서 하울링 및 에코 0건 (AEC 정상 동작).
  * 180초 이상 유휴 상태 후 발화 시 세션 자동 복구 확인.
* **공수/담당**: 2.5시간 / 에이전트 + 사용자 (실기기 통화 테스트)

### 📌 M5 (확장 과제): Hermes-Relay 네이티브 백그라운드 통화 연동
* **목표**: iOS/Android 브라우저의 화면 잠금 시 마이크 차단 제약을 극복하는 전용 백그라운드 통화 모드.
* **작업 항목**:
  1. Hermes-Relay Android 앱에 Foreground Service(`AudioRecord` + `AudioTrack`) 모듈 추가.
  2. WebSocket(포트 8767) 직접 스트리밍 엔드포인트 연동.
* **검증 기준**: 화면 잠금 상태 및 타 앱 사용 중에도 백그라운드 음성 통화 유지.
* **공수/담당**: 별도 프로젝트 정의 (2단계 진행)

## 4. 마일스톤 요약 표

| 마일스톤 | 작업 명칭 | 핵심 산출물 | 예상 시간 | 담당 |
| :---: | :--- | :--- | :---: | :---: |
| **M0** | 기반 환경 검증 | aiortc 루프백 테스트 스크립트, Tailscale HTTPS 검증 | 2.0h | 에이전트 |
| **M1** | 코어 엔진 분리 | `core/session.py`, `core/adapter.py`, 로컬 회귀 통과 | 3.5h | 에이전트 |
| **M2** | WebRTC 서버 | `server/webrtc_adapter.py`, `server/app.py` (FastAPI) | 5.0h | 에이전트 |
| **M3** | 웹 프론트엔드 | `static/index.html` (WakeLock + WebRTC + AEC) | 2.5h | 에이전트 |
| **M4** | E2E 모바일 실증 | 실기기 통화 검증 보고서, 지연/음질 튜닝 완료 | 2.5h | 협업 |
| **M5** | 네이티브 백그라운드 | Hermes-Relay Foreground Audio Service (옵션) | TBD | 2단계 |

**합계 (M0~M4)**: 약 **16~18시간** / 6~7영업일 (리뷰어 재산정 반영)

## 5. 보안 및 운영 관리 방안

1. **TLS / Secure Context 보장**: 모바일 WebRTC(`getUserMedia`)는 HTTPS 필수. Tailscale MagicDNS의 Let's Encrypt 자동 인증서(`tailscale serve`) 사용.
2. **네트워크 및 NAT 통과 (ICE/STUN)**: 개인 VPN(Tailscale) 망 내 통신 → 복잡한 TURN 불필요, Google 공용 STUN 1개로 P2P 직접 연결.
3. **세션 수명 및 리소스 관리**: WebRTC PeerConnection `closed`/`failed` 이벤트 시 DashScope WebSocket 및 PyAV 컨텍스트 즉시 close. 브라우저 비정상 종료 방지 유휴 타임아웃(30초) GC 타이머.
4. **인증 및 자격증명**: DashScope API Key는 서버 환경변수로만 보관, 클라이언트로 일체 노출 금지.
5. **웹 시그널링 접근 제어 (P1)**: `POST /api/offer` 및 웹 진입 시 `BRIDGE_AUTH_TOKEN`(환경변수) 기반
   URL 쿼리 토큰/`Authorization: Bearer` 검증 미들웨어 적용 — Tailnet 외부 또는 임의 접근으로부터
   Hermes 에이전트 도구 실행(`hermes chat -q --accept-hooks`) 보호.

## 6. 핵심 리스크 및 완화책

| 리스크 요인 | 기술적 영향 | 완화 방안 |
| :--- | :--- | :--- |
| **iOS 잠금 화면 마이크 차단** | 화면 꺼짐 즉시 통화 단절 | ① Screen Wake Lock API 필수 ② 화면 켜짐 유지 안내 UX ③ 필요시 M5 네이티브 서비스 |
| **스피커폰 하울링/에코** | AI 발화가 마이크로 재유입 | ① 브라우저단 `echoCancellation: true` ② 서버측 소프트웨어 억제(Ducking) 유지 |
| **DashScope 180초 타임아웃** | 유휴 시 연결 끊김 | 기존 `SessionDied` 감지 + 지수 백오프 재수립 메커니즘 100% 계승 |
| **오디오 리샘플링 지연** | 48kHz ↔ 16k/24k 변환 병목 | PyAV / soxr C-바인딩 리샘플러 사용 (0.05ms 미만 보장) |
| **Tailscale TLS 인증서 미활성** | serve --https 인증서 발급 시 500 에러 | Admin Console HTTPS 인증 활성화 선행 또는 mkcert 폴백 (M0에서 검증) |

## 7. 코드 분석 근거 (분석이 산출물 요약)

**재사용 모듈** (`qwen_realtime_voice.py`):
- 42~75: 모델/보이스/INSTRUCTIONS 페르소나
- 80~91: `_boost_gain()` PCM 증폭
- 95~115: `BRIDGE_TOOL` (`delegate_to_agent`)
- 133~161: `load_api_key()`, `session_config()`
- 290~354: `_send()`/`_run_agent_sync()` (hermes chat --resume 브리지)
- 355~390: `_handle_function_call()`
- 462~524: `recv_events()` 이벤트 파서
- 626~651: 지수 백오프 재연결
- 272, 412~421, 484~491: 토큰 usage 집계 (`response.done` 파싱·누적은 484~491)

**수정 필요 (높은 결합)**:
- 392~400 `_mic_callback`, 613~620 `sd.InputStream` → WebRTC AudioTrack 소스
- 439~461 `play_audio`, 602~605 `sd.RawOutputStream` → WebRTC 아웃 트랙
- 401~409 `_mic_suppressed` → 브라우저 AEC 대체 (플래그화)
- 164~230 `pick_devices`, 727~748 → 서버 모드 바이패스
- 750~780 flock 락 (`qwen_realtime_voice.py` 내) / voice_process.py → **단일 사용자 선점형 세션 매니저**로 확장

**절대 유지**: DashScope WebSocket 프로토콜 규격, 에이전트 브리지 호출 시퀀스, SessionDied 타임아웃·재연결 제어

## 8. 용어
* **aiortc**: Python asyncio 기반 WebRTC/ORTC 구현체
* **PyAV**: FFmpeg C 라이브러리 Python 래퍼 (지연 없는 리샘플링)
* **AEC/AGC/NS**: 음향 반향 제거 / 자동 감도 제어 / 잡음 억제
* **Tailscale Serve**: 로컬 웹 서비스를 Tailnet 사설 도메인에 HTTPS 역방향 프록시로 게시

## 9. 리뷰어 검토 결과 (2026-09-14, 분석이 독립 리뷰)
* **최종 심사평**: PASS-WITH-FIXES — 조건부 승인 (아래 P1 반영 후 M0 착수 권고)
* **P1 (차단성, 반영 완료)**:
  1. Tailscale TLS 인증서 미활성 500 에러 → Admin Console 활성화 선행 + mkcert 폴백 (M0)
  2. WebRTC DataChannel 이벤트 채널(자막/VAD/위임 상태) 아키텍처 추가 (2.2절·M2)
  3. aiortc 무음 프레임 + PTS(960s/20ms) 타이밍 설계 (M2)
  4. Barge-in 시 WebRTC 송출 큐 flush (M2)
  5. 시그널링 API 인증 토큰(BRIDGE_AUTH_TOKEN) 추가 (5절)
* **P2 (주요 보완, 반영 완료)**: 단일 사용자 선점형 세션 정책, STUN+DERP 폴백, 통화 종료 시 에이전트 서브프로세스 취소 루틴, 공수 16~18h 현실화
* **P3 (문구/표기, 반영 완료)**: 토큰 파싱 행 번호(484~491) 정정, flock 파일명 정정, iOS 잠금 시 WebRTC 일시 중단 UX 안내

## 10. 진행 상태 (야간 자율 개발용)

| 마일스톤 | 상태 | 완료 시각 | 비고 |
| :---: | :---: | :--- | :--- |
| M0 | ✅ 완료 | 2026-09-14 23:40 | venv·aiortc·resampler 검증 완료 / 커널 TUN 모드(GUI 앱 `utun6`, 100.103.115.13) 정상 동작 실측 확인. WebRTC UDP 바인딩 및 폰 ping 통과. M0 인프라 통과. 상세: `m0/TAILSCALE_MODE_BLOCKER.md` |
| M1 | ✅ 완료 | 2026-09-15 00:25 KST | 코어 세션 엔진(`core/session.py`, `persona.py`, `adapter.py`, `pipeline.py`) 구현. 원본 CLI 페르소나·상수 100% 일치. 27/27 단위 테스트 통과 + 마이크 없는 실서버 라운드트립 2종(일반 음성 7.04s, 에이전트 브리지 위임 1.84s) 실측 통과. |
| M2 | ✅ 완료 | 2026-09-15 01:32 KST | WebRTC AudioIO 어댑터(`server/webrtc_adapter.py`) 및 FastAPI 시그널링 서버(`server/app.py`) 구현. 무음 프레임 규칙(20ms/960smp/1920B), 양방향 리샘플링(48k↔24k), 선점형 싱글턴 세션, DataChannel JSON 이벤트 파이프라인. 단위 테스트 10/10 통과 + 무마이크 E2E 실측 17/17 검증 통과(SDP교환 5.0s, ICE 50ms, VAD 음성왕복 7.5s, 전사 확인, 다운링크 9.46s RMS 0.024, PTS연속, 선점교체 100% 정상). |
| M3 | ✅ 완료 | 2026-09-15 02:35 KST | 단일 파일 반응형 WebRTC SPA(`static/index.html`, 무의존성/외부CDN 0B) 완성. WakeLock 화면 유지, AudioContext 언락, 실시간 오디오 레벨 바, 실시간 자막 버블, DataChannel 양방향 이벤트(cmd:text/hangup), 턴/토큰 대시보드 연동. JSDOM 모의 환경 58/58 전원 통과 + Headless Chrome 실 브라우저 E2E 11/11 전원 통과(SDP 협상 0.8s, WebRTC Opus 음성 송출 118패킷/6.8KB 수신, 텍스트 주입 자막 DOM 렌더, 통화 종료 및 리소스 해제 완결). |
| M4 | ✅ 완료 | 2026-09-15 03:22 KST | 실브라우저 E2E 내구성·지연·끼어들기(Barge-in)·유휴복구 자동 검증(`tests/m4_durability_e2e.py`, `tests/m4_browser_audio.py`) 완료. Headless Chrome + 실음성 WebAudio WebRTC 주입 기반 20/20 전원 통과: (1) 실음성 무마이크 음성왕복 (사용자 전사 + AI 한국어 음성 5.3KB 수신) (2) 지연 계측 (WebRTC RTT 1.0ms, response_started→서버첫오디오 224.9ms) (3) 스피커 모드 에코 게이트 (AI 발화 중 코어 유입 0B 완벽 차단) (4) 끼어들기 flush 및 턴 재개 (5) 180초+ 유휴 후 185.1초에 DashScope 세션 1→2 자동 재연결 및 재연결 후 발화 복구 (6) 214.9초 장기 통화 중 PeerConnection 'connected' 100% 유지. |
| M5 | ⏸ 보류 (2단계) | - | 사용자 결정 필요 |

> 야간 크론(KST 23:00~05:00)이 매 실행마다 **미완료 중 가장 앞선 항목 1개**를 진행하고 이 표를 갱신한다.
> ⚠️ **M0-3은 사용자 결정 대기(인프라 변경)** — 야간 잡은 M0-3을 건너뛰고 **M1(DashScope 세션 관리자 분리)부터 진행**한다.
> 블로커 상세: `m0/TAILSCALE_MODE_BLOCKER.md` (tailscaled userspace 모드 → 100.x UDP 바인딩 불가).
> 진행 로그: `PROGRESS.md` (append 전용)

### 야간 자율 개발 크론 (등록 완료 2026-09-14)
- **잡 ID**: `019e9fb80092` — "WebRTC 브리지 야간 자율 개발 (23:00-05:00)"
- **스케줄**: `0 23,0,1,2,3,4 * * *` (KST 23:00 / 00:00 / 01:00 / 02:00 / 03:00 / 04:00 — 야간 6회)
- **모델 고정**: `alibaba-token-plan-intl/deepseek-v4.1-flash` (provider `opencodex`)
- **Continuity**: on (이전 실행 출력이 다음 실행 프롬프트에 주입 → 이어서 진행)
- **Workdir**: `/Users/shinsamkyun/hermes-webrtc-bridge`, Skill: `dashscope-realtime-voice-chat`
- **전달**: telegram (홈채널 8654542438)
- 제어: `hermes cron pause|resume|run|remove 019e9fb80092`, 백업: `~/.hermes/backups/jobs.json.bak_*`

## 11. M0 실측 반영 (2026-09-14 야간 크론) — 계획 보정 사항

상세 근거: `m0/M0_FINDINGS.md`, 원문 로그: `m0/out/*.txt`

**실측 확정치**
* 리샘플러: PyAV `AudioResampler` 48k→16k / 24k→48k 각각 **p95 0.0065ms / 0.0064ms** → PyAV 채택 (soxr 대비 6배 빠름).
* aiortc 로컬 루프백: 연결 수립 **51.9ms**, 단방향 **p95 85ms**, 왕복 **p95 168.6ms**, 프레임 유실 0건, 440Hz 완전 보존.
  → 단방향 83ms 는 aiortc 수신 지터 버퍼 지배적. M4 예산 600ms 대비 여유 있으나 M2 에서 버퍼 지연 단독 계측 필요.

**M2 로 넘기는 필수 반영 항목 (4건)**
1. `frame.to_ndarray()` 는 스테레오에서 packed interleaved `(1, 2N)` 반환 → 반드시 `reshape(-1, nch)[:, 0]` 디인터리브 (안 하면 피치가 1/2 로 보인다).
2. 하나의 `MediaStreamTrack` 에 소비자는 **1개만** — `recv()` 는 큐 pop 이라 2개 붙이면 프레임을 절반씩 나 갖는다. Sink 1개 + fan-out 구조로.
3. WebRTC 로컬 송출용 커스텀 소스 트랙은 **실시간 페이스(20ms) 직접 유지** 필수. 페이스가 없으면 수천 프레임이 순식간에 방출된다(5초에 17,390 프레임 실측).
4. 업링크 다운믹스: aiortc Opus 인코더의 mono→stereo 업믹스에서 **−3.01dB** 손실(Opus 자체는 −0.05dB 로 투명). 스테레오 수신을 채널 0 만 취해 DashScope 로 올리면 ASR 입력이 3dB 낮아지므로 **+3dB 보정 또는 명시적 다운믹스** 필요.
   추가: PyAV 리샘플러는 프라이밍 16샘플 지연이 있어 세션 종료 시 `resample(None)` 플러시 필요.

**신규 리스크 (아래 §6 리스크 표에 준함)**
| 리스크 요인 | 기술적 영향 | 완화 방안 |
| :--- | :--- | :--- |
| **macOS 로컬 네트워크 권한 차단** | 에이전트가 운 프로세스에서 비-루프백 주소(192.168.x / Tailscale 100.x)로 나가는 UDP 가 조용히 드롭 → ICE 가 `checking` 에서 정지, WebRTC 연결 자체가 수립되지 않음 (127.0.0.1 은 정상) | ① M2/M4 브라우저 실연결 전에 서버 프로세스에 로 네트워크 권한 승인 ② 동일 호스트 검증은 127.0.0.1 루프백 경로 사용 ③ 테스트 하네스는 `force_loopback_ice()` 로 host 후보를 127.0.0.1 로 고정 |

**M0 잔여(항목3)**: 테넌트 `CertDomains: None`(HTTPS 인증서 미활성) + `mkcert` 미설치 → Admin Console 활성화 또는 `brew install mkcert` 중 사용자 선택 필요. `tailscale serve` 변경은 시스템 설정 변경에 해당해 에이전트가 임의 실행하지 않음.