# Hermes WebRTC Bridge (Voice Assistant WebRTC Call)

Hermes / Qwen Voice Realtime 양방향 음성 통화를 스마트폰 및 브라우저에서 지연 없이 수행할 수 있도록 지원하는 자체 호스팅 WebRTC 오디오 브리지입니다.

## 🌟 주요 특징

- **초저지연 WebRTC 통화**: 브라우저 ↔ 서버 간 Opus/WebRTC 오디오 파이프라인 (RTT ~1ms, 처리 지연 ~220ms).
- **DashScope Realtime 연동**: `qwen-audio-3.0-realtime` 모델과 양방향 WebSocket 스트리밍 (48kHz WebRTC ↔ 24kHz DashScope 고품질 리샘플링).
- **Tailscale 인프라 최적화**: Tailscale 커널 TUN 모드 및 HTTPS를 활용하여 공인 IP/포트포워딩 없이 사설망 암호화 통화.
- **반응형 웹 SPA**: 모바일/데스크톱 대응 단일 파일 SPA (`static/index.html`), WakeLock 화면 유지, 실시간 음성 비주얼라이저, 자막 버블 및 턴 대시보드.
- **다양한 통화 모드**: 스피커폰 에코 게이트 모드, 헤드폰 모드 (Barge-in / 실시간 끼어들기), 수동 PTT 모드.
- **안정성 & 자동 복구**: 180초+ 유휴 시 DashScope 세션 자동 핫 재연결, 선점형 싱글턴 세션 관리.

## 🏗 시스템 아키텍처

```
[ 스마트폰 / 브라우저 WebRTC ]
            │ (WebRTC AudioTrack: Opus 48kHz / DataChannel)
            ▼
[ FastAPI & aiortc Signaling Server (server/app.py) ]
            │ (PCMResampler 48kHz ↔ 24kHz)
            ▼
[ Core Pipeline & Session Engine (core/session.py) ]
            │ (WebSocket PCM16 Stream)
            ▼
[ DashScope Qwen Audio Realtime API ]
```

## 🚀 빠른 시작

### 1. 환경 설정
```bash
# 가상환경 생성 및 의존성 설치
python3 -m venv .venv
source .venv/bin/activate
pip install --only-binary=:all: -r requirements.txt
```

### 2. 서버 실행
```bash
# 원클릭 실행 스크립트 사용
./start-bridge.sh start

# 또는 모듈 직접 실행 (PYTHONPATH 지정 필수)
export PYTHONPATH=$(pwd)
.venv/bin/python -m server.app --host 127.0.0.1 --port 8000 --mode speaker
```

### 3. 상태 확인 및 종료
```bash
./start-bridge.sh status
./start-bridge.sh stop
```

## 🧪 테스트 & 검증

```bash
# 핵심 단위 테스트 (M1 코어 + M2 WebRTC)
.venv/bin/python -m unittest tests.test_core_unit tests.m2_unit_webrtc

# 웹 프론트엔드 DOM 테스트
node tests/m3_dom_test.mjs

# 내구성 및 E2E 실측 검증
.venv/bin/python tests/m4_durability_e2e.py
```

## 📂 프로젝트 구조

- `core/`: DashScope 양방향 WebSocket 세션, 리샘플러 어댑터, 윈터 페르소나 및 파이프라인.
- `server/`: FastAPI 시그널링 서버 (`app.py`), aiortc WebRTC 트랙 어댑터 (`webrtc_adapter.py`).
- `static/`: 반응형 WebRTC 클라이언트 UI (`index.html`).
- `tests/`: 단위 테스트 및 헤드리스 브라우저 E2E 검증 스위트.
- `PLAN.md`: 마일스톤(M0~M5) 상세 설계 및 구현 계획서.
- `PROGRESS.md`: 자율 개발 진행 로그 및 실측 벤치마크 리포트.
