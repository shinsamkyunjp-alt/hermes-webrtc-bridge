# Hermes WebRTC Bridge — 개발 진행 로그

[2026-09-14 23:10 KST] M0 | 완료: 전용 venv(Python 3.11.16) 구성 및 aiortc/PyAV/soxr 설치, PyAV 리샘플러 벤치마크(p95 0.0065ms 채택), aiortc 무마이크 오디오 루프백 테스트 하네스(m0/loopback_test.py) 구현 및 실측 | 검증: `.venv/bin/python m0/loopback_test.py` 실행 결과 6/6 PASS (연결 51.9ms, 단방향 p95 85.1ms, 왕복 p95 168.6ms, 440Hz 음정 보존, 패킷 무유실), `.venv/bin/python m0/resample_bench.py` p95 0.0065ms | 다음: M0-3 Tailscale HTTPS 경로 사용자 확인(CertDomains 활성화 또는 mkcert) 대기 및 M1(DashScope 세션 관리자) 구현 착수 | 이슈: macOS 로컬 네트워크 권한 정책으로 비-루프백 UDP 바인딩 드롭 현상 발견(테스트는 loopback mock 처리, 실서비스 배포 시 권한 승인 또는 127.0.0.1 바인딩 필요)


## 2026-09-14 23:25 KST — M0-3 HTTPS 및 미디어 경로 검증 중 P0 블로커 발견
- **상태**: ⚠️ P0 블로커 발견 (사용자 결정 대기)
- **발견 내용**:
  1. `tailscale cert` 실패의 직접 원인: 테넌트 `CertDomains: None` (콘솔 토글 필요)
  2. **더 근본적인 P0 블로커**: 현재 Mac의 tailscaled가 `--tun=userspace-networking` 모드로 실행 중
     - 커널 `utun` 인터페이스 없음 (100.126.185.45 주소가 OS 네트워크 스택에 없음)
     - OS 레벨에서 `100.126.185.45`로 UDP/TCP 바인딩 시 `EADDRNOTAVAIL` 즉시 발생
     - `tailscale serve`는 **TCP(HTTP/HTTPS/TLS) 전용**이며 UDP 리버스 프록시를 미지원
     - 결과: WebRTC 표준 음성 미디어(RTP/UDP)가 Tailscale을 통해 들어올 **OS 네트워크 경로가 완전히 부재**
- **문서화**: `m0/TAILSCALE_MODE_BLOCKER.md` 작성 완료
- **조치**:
  - `PLAN.md` §10 상태표에 블로커 명시
  - 야간 자율 개발 잡이 교착(deadlock)되지 않도록, M0-3을 사용자 결정 대기로 표시하고 **00:00 KST 잡은 M1(DashScope 세션 관리자 분리, 로컬 순수 파이썬 코드)부터 계속 진행**하도록 규칙 추가
  - 삼균 님께 3가지 해결 경로(A/B/C) 보고


## 2026-09-14 23:40 KST — 사용자 A안(커널 TUN 모드 전환) 선택 및 가이드 전달
- **상태**: 사용자 결정 완료 (A안 선택)
- **현재 설정 확인**:
  - 현재 userspace 데몬: `~/Library/LaunchAgents/com.samkyun.tailscaled.plist`
  - socks5/http-proxy: `localhost:1055`
- **조치**:
  - 사용자에게 맥 터미널에서 실행할 1회성 전환 명령어(sudo 기반 system daemon 등록 또는 공식 GUI 앱 설치) 가이드 제공.
  - 전환 완료 시 `ifconfig utun` 및 `100.x` IP 바인딩 정상화 예상.
  - 야간 크론(00:00)은 계획대로 M1(DashScope 세션 관리자 분리)을 계속 진행.
## 2026-09-14 23:38 KST — P0 블로커 해소 확인 (커널 TUN 모드 실증)
- **상태**: ✅ RESOLVED — 미디어(UDP) 경로 확보
- **경위**: 사용자 진술("Mac App Store Tailscale GUI 앱 설치·사용 중")을 우선 신뢰하고 재검증.
  초기 스캔이 놓친 이유는 번들이 **23:32:30에 갱신/교체**되고 프로세스가 23:32:42에 기동되어 스캔 시점과 엇갈렸기 때문.
- **실측 증거**:
  - `/Applications/Tailscale.app` (io.tailscale.ipn.macos v1.102.4, App Store) 실행 중
  - 커널 인터페이스 `utun6 → 100.103.115.13`, 라우팅 `100.64/10 → utun6`
  - `UDP bind 100.103.115.13` ✅ / `UDP 자기 왕복` ✅ / `ping z-fold8-1` **8ms 직접 경로** ✅
  - `netcheck`: UDP true, UPnP 매핑, 최근접 DERP Tokyo 38.7ms
- **남은 작업**:
  1. 테넌트 HTTPS 인증서 미활성(`CertDomains: None`) → 관리자 콘솔에서 HTTPS Certificates 활성화 필요(사용자)
  2. 중복 노드 정리 권장 — 홈브루 userspace 데몬(100.126.185.45)이 별도 노드로 동시 등록.
     ️ 단, 현재 8767(hermes relay)·9119(dashboard)의 `tailscale serve`가 그 노드에 물려 있어 **즉시 중단 금지**.
- **문서**: `m0/TAILSCALE_MODE_BLOCKER.md` 전면 개정(RESOLVED), `PLAN.md` §10 M0 행 갱신
[2026-09-15 00:26 KST] M1 | 완료: 코어 세션 엔진 및 I/O 분리 (core/session.py, persona.py, adapter.py, pipeline.py, extract_persona.py, live_roundtrip.py) | 검증: unittest 27/27 통과 (0.15s), 실서버 manual 왕복 통과 (입력전사 '안녕 헤르메스야 오늘 반도체 시장 관련해서 짧게 인사해줘' -> 답변전사 '안녕하세요. 오늘 반도체 시장은...' 7.04s WAV / total=636토큰), 에이전트 브리지 위임 왕복 통과 (tool_call=1, 'One moment, checking that for you' 1.84s WAV / total=969토큰) | 다음: M2 WebRTC 서버 구현 (server/webrtc_adapter.py, server/app.py - aiortc AudioTrack과 Core Session 양방향 파이프라인 연동) | 이슈: 없음 (turn_detection=None manual 모드 시 서버 기본 VAD 생존 문제 발견 및 명시적 None 패치로 해결)
[2026-09-15 01:34 KST] M2 | 완료: WebRTC AudioIO 어댑터(server/webrtc_adapter.py: QwenAudioTrack·WebRTCAudioAdapter·PCMResampler) 및 FastAPI 시그널링 서버(server/app.py: /api/offer, /api/hangup, /api/status, DataChannel 이벤트 버스, 선점형 세션 교체) 구현 | 검증: `.venv/bin/python -m unittest tests.m2_unit_webrtc -v` 10/10 PASS(1.46s), `.venv/bin/python tests/m2_e2e_webrtc.py` E2E 17/17 검증 전항목 PASS(SDP 교환 5.0s, ICE 연결 50ms, 실 DashScope VAD 응답 7.5s, 사용자 발화 전사 '안녕 르 메스, 오늘 반도체 시장 관련 해서 짧 게 인사 해줘. ' 생성, AI 음성 전사 '안녕하세요. 오늘 반도체 시장은...' 생성, 다운링크 오디오 9.46s RMS 0.0241 수신, PTS 연속 갭 0개, 선점형 2번째 오퍼 연결 시 기존 세션 closed 확인, hangup 정상 정리) | 다음: M3 웹 프론트엔드 통화 UI 구현(static/index.html, app.js: WebRTC 오디오 스트리밍, DataChannel 자막·상태 렌더링, 오디오 비주얼라이저 파형) | 이슈: 없음 (aiortc RTCIceServer 객체 변환 및 setRemoteDescription 이전 on_track 등록 순서 엄격 준수로 해결)

[2026-09-15 02:35 KST] M3 | 완료: 단일 파일 반응형 WebRTC SPA(static/index.html, 무의존성/외부CDN 0B) 완성 (WakeLock 화면유지, AudioContext 언락, 실시간 오디오 레벨 바, 실시간 자막 버블, DataChannel 양방향 이벤트 cmd:text/hangup, 턴/토큰 대시보드) 및 서버 조기 텍스트 주입 레이스 핸들링 보강 | 검증: node tests/m3_dom_test.mjs 58/58 PASS (DOM·이벤트·접근성·AEC 제약식 전원 통과), .venv/bin/python tests/m3_browser_e2e.py Headless Chrome 실 브라우저 E2E 11/11 전원 통과 (SDP 협상 0.8s, WebRTC 실 오디오 다운링크 118패킷/6,866B 수신, 타이핑 입력 -> DashScope -> DOM 자막 버블 'One moment, checking that for you.' 렌더, 통화 종료 후 서버 세션 정상 해제) | 다음: M4 모바일 E2E 실증 (Tailscale Serve HTTPS 환경 실스마트폰 통화 검증, 음질/지연 튜닝) | 이슈: 없음 (초기 DataChannel 오픈 전 텍스트 주입 레이스 발생 -> 세션 웹소켓 연결 대기 로직 적용으로 완벽 해결)
[2026-09-15 03:25 KST] M4 | 완료: 실환경 E2E 내구성·지연·끼어들기(Barge-in)·유휴복구 자동 검증 프레임워크(tests/m4_durability_e2e.py, tests/m4_browser_audio.py) 구현 및 실측 검증 완료 | 검증: .venv/bin/python tests/m4_durability_e2e.py --idle 200 실행 결과 20/20 전원 PASS (out/m4_durability_report.json 생성). ① 실음성 무마이크 음성왕복 (사용자 전사 '안녕하세요, 오늘 기분이 어떠신가요?...' 및 AI 한국어 음성 5,335B 브라우저 수신 완료) ② 지연 계측 (WebRTC RTT 1.0ms, response_started->서버첫오디오 224.9ms) ③ 스피커 모드 에코 게이트 (AI 발화 중 코어 오디오 유입 0B 완벽 차단) ④ 헤드폰 모드 끼어들기 flush 및 턴 정상 재개 ⑤ 180초+ 유휴 후 185.1초에 DashScope 세션 1->2 자동 재연결 및 재연결 후 발화 복구 ⑥ 214.9초 장기 통화 중 PeerConnection 'connected' 100% 유지 및 통화 종료 시 세션 정상 정리 | 다음: M0 Tailscale 인증서(CertDomains) 활성화 후 스마트폰 실기기 브라우저(Android Chrome/Safari) 1회성 마이크 통화 테스트(사용자 확인) | 이슈: Chrome headless fake-audio capture 의 무음 스트림 전달 현상 발견 -> 브라우저 내부 WebAudio MediaStream 주입으로 전환하여 완벽 해결

[2026-09-16 23:30 KST] M4-실기기 | 인프라 개통: tailnet HTTPS 인증서 활성화(사용자 조치) 완료 → `tailscale serve --bg 8000` 으로 https://macmini.tail091002.ts.net/ 프록시 개통. BRIDGE_AUTH_TOKEN 발급·설정(.env, chmod 600), `-m server.app` 기동(PYTHONPATH 이슈 수정: `python server/app.py` 직접 실행 시 ModuleNotFoundError: core). 검증: 토큰 없이 401 / 토큰 있으면 HTTPS 경유 200, /api/status JSON 정상(voice=longanlingxin, agent_bridge=true), 정적 페이지 UI 로드 확인. 원클릭 스크립트 `start-bridge.sh` 생성. 다음: 폰(z-fold8-1) Tailscale 온라인 후 실기기 통화 1회 검증(사용자).

[2026-09-17 21:45 KST] M4-실기기 | 한국어 오전사(중국어/영어) 완전 해결. 원인 3단: ① QWEN_MIC_GAIN=2.5(데스크용)가 폰의 뜨거운 입력을 포화 → 1.0 으로 (`.env`). ② 안드로이드에서 AI 오디오를 WebAudio 로 우회하면 Chrome AEC 참조가 끊겨 에코가 마이크로 유입 → 기본 출력을 `<audio>` 엘리먼트로 되돌림(`?out=webaudio` 로 A/B 가능, 대신 AI 레벨미터 없음). ③ **결정타: 에코 게이트가 '버스트 도착 시각'(output_pump) 기준으로 동작 — 지터 버퍼 6초 때문에 실제 재생 시점엔 게이트가 열려 있어 AI 자기 목소리가 ASR 로 들어갔다.** → `QwenAudioTrack.recv()` 에 on_play 훅을 달아 실제 재생 타임라인으로 gate.note_play_end 갱신. 검증: 폰 업링크 WAV(`out/uplink_last.wav`)를 `qwen_realtime_voice.py --test-file` 로 재생 → '내가 하는 말이 한국어야 일본어야 영어야?' = 완벽 한국어. M2 단위 10/10, M2 E2E 17/17, M3 DOM 58/58 통과. 진단 인프라 추가: last_snapshot(종료 후에도 실측 유지, 2차 close 덮어쓰기 방지), input_rms_avg/input_clip_pct_max, gate.passed/blocked/tail, 업링크 20초 WAV 덤프.

[2026-09-17 22:10 KST] UI | JARVIS HUD 스킨 적용 (`static/index.html` 전면 개편, JS 로직·ID 100% 보존). 레퍼런스 = Iron Man JARVIS scanner ring(Pinterest, oEmbed 로 정체 확인 후 이미지 직접 분석). 팔레트는 레퍼런스 추출(단색 틸: void #03050a / cyan #2ad4d4 / rim #8af5e8 / core #0b2a33), 유일한 예외는 실패 상태용 --alert. 신규: ① Canvas 로브 토러스 스캐너 (3/5/7차 하모닉 변조 + 메시 2패밀리 + 방사 크로스해치 + 가산 블룸 + 정점 파티클 + 코어/고스트 호), 마이크 레벨이 로브·파티클 구동 ② 캔버스 백스토어 = CSS크기×DPR(dpr 3 확인, 헤어라인 선명) ③ 사분면 텔레메트리 + 헤어라인 레벨 바(폭은 JS 인라인, 오른쪽 열은 우측 정렬) ④ 더블 베젤 플레이트 + 버튼-인-버튼 CTA ⑤ 보이드 글로우/스캔라인/비네트. 대비 상향(dim 2.8:1→약 4.6:1, dim2 상향). 검증: M3 DOM 58/58, M2 단위 10/10, M2 E2E 17/17, CDP 실측(390×844@DPR3: 가로 오버플로 0, scrollHeight 844=뷰포트, 코어 중심 195=195, 버튼 높이 54/47/47, 버스 배율 3.0, 미터 62%→108.5px 렌더). 외부 리소스 0 유지.
