# M0 실측 결과 — 기반 환경 및 aiortc 루프백 검증

> 실행: 2026-09-14 23:00~23:2x KST (야간 자율 개발 크론)
> 실행 환경: macOS 26.6.2 / Mac mini (arm64) / Python 3.11.16
> 산출물: `m0/loopback_test.py`, `m0/resample_bench.py`, `m0/diag_audio_frames.py`, `m0/out/*.txt`
> 마이크·스피커 장치 무사용 (세션 장치를 건드리지 않는 검증만 수행)

## 1. 설치 결과 (M0-1) ✅

전용 venv: `/Users/shinsamkyun/hermes-webrtc-bridge/.venv` (homebrew python3.11.16 기반)

| 패키지 | 설치 버전 | 비고 |
| :--- | :--- | :--- |
| aiortc | 1.15.0 | |
| av (PyAV) | 17.1.0 | 번들 FFmpeg (libswresample 6.3.101) |
| fastapi | 0.141.1 | |
| uvicorn[standard] | 0.53.0 | |
| soxr | 1.1.0 | |
| numpy | 2.4.6 | |

⚠️ **설치 함정**: 홈브루에 `ffmpeg 9.0.1` 이 설치 있어 `av`  소스 빌드하면
`use of undeclared identifier 'AVFMT_ALLOW_FLUSH'` 로 실패한다(FFmpeg 9 API 변경).
**`pip install --only-binary=:all:` 로 휠을 강제해야 한다** (wheel 은 번들 FFmpeg 사용).
`requirements.txt` 에 주석으로 명시해 둠.

## 2. 리샘플러 지연 실측 (M0-2a) ✅

`m0/resample_bench.py` (20ms 프레임 × 2000회, p95 기준) — 원문: `m0/out/resample_bench.txt`

| 진 | 경로 | mean | p95 | max |
| :--- | :--- | ---: | ---: | ---: |
| PyAV `AudioResampler` | 48000→16000 (업링크) | 0.0067 ms | **0.0065 ms** | 0.5472 ms |
| PyAV `AudioResampler` | 24000→48000 (다운링크) | 0.0063 ms | **0.0064 ms** | 0.1473 ms |
| soxr | 48000→16000 | 0.0169 ms | 0.0393 ms | 0.1313 ms |
| soxr | 24000→48000 | 0.0136 ms | 0.0138 ms | 0.0405 ms |

* PLAN §6 가정 "0.05ms 미만" → **PyAV 로 통과** (p95 0.0065ms, 여유 8배). soxr  p95 0.0393ms 로
  통과하지만 호출 오버헤드가 커서 **PyAV 를 기본 리샘플러로 채택**.
* ⚠️ 첫 프레임 출력이 304/320 샘플(PyAV 프라이밍 지연 ≈16 입력샘플) — 세션 종료 시
  `resample(None)` 플러시가 필요하다. 총 1,920,000 입력샘플 중 639,984 출력(=640,000-16) 로 확인.

## 3. aiortc 오디오 루프백 (M0-2b) ✅

`m0/loopback_test.py` — 같은 프로세스 안에서 pc1↔pc2 를 SDP 교환으로 연결,
440Hz 사인파(48k, 20ms/프레임)를 pc1→pc2 로 보내고 pc2 가 받은 PCM 을 그대로 되돌려 pc1 로 수신.
원문 JSON: `m0/out/loopback_report.txt`

| 항목 | 실측 |
| :--- | :--- |
| SDP offer+ICE 수집 | 0.61 ms |
| SDP answer+ICE 수집 | 0.61 ms |
| Answer→`connected` | **50.0 ms** |
| 총 연결 수립 | **51.9 ms** |
| SDP 크기 | offer 1,239 B / answer 1,238 B |
| 송신 / pc2 수신 | 253 / 249 프레임 (98.4%) |
| 왕복 수신 | 244 프레임 (96.4%) |
| PTS 갭 | 다운링크 0건 / 왕복 0건 |
| **단방향 지연** (pc1→pc2) | min 79.8 / p50 83.3 / p95 85.1 / max 85.8 ms |
| **왕복 지연** (pc1→pc2→pc1) | min 161.7 / p50 166.6 / p95 168.6 / max 170.7 ms |
| 무결성 | FFT 피크 **440.0 Hz** (목표 일치), 무음 프레임 0%, WAV 저장 |

판정: **6/6 PASS** (`m0/out/loopback_report.txt` 말미 `판정: 통과`).

* 해석: 단방향 83ms ≈ 왕복 166ms 의 절반 — 두 방향 모두 동일 파이프라인임을 교차 확인.
  83ms 는 aiortc 수신측 **지터 버퍼**(≈4프레임)가 지배적이며 Opus/리샘플 자체는 1ms 미만.
  M4 예산(RTT+생성 600ms)에 여유가 있으나, M2 에서 지터 버퍼 지연을 별도로 계측할 것.

## 4. 되짚어 볼 하네스 함정 3건 (M2/M3 구현 시 동일 실수 방지)

1. **`frame.to_ndarray()` 는 packed interleaved 를 준다.**
   aiortc `OpusDecoder` 는 `layout=stereo` 로 디코드하므로 반환 shape 이 `(2, 960)` 이 아니라
   **`(1, 1920)`** (L,R 교차) 이다. 이걸 mono 로 읽으면 스펙트럼 피크가 **440→216Hz**(1/2)로
   보여 "코덱이 피치를 망가뜨렸다"는 오진을 하게 된다. 반드시 `reshape(-1, nch)[:, 0]` 로 디인터리브.
   → 퍼 `frame_to_mono_int16()` 로 고정.
2. **하나의 aiortc 트랙에 소비자를 2개 붙이면 안 된다.** `track.recv()`  큐 pop 이므로 두 태스크가
   읽으면 프레임을 **절반씩** 나눠 갖는다(253 신 → 127 수신, pts 가 1920씩 건너뛰어 마치 50% 패킷
   손실처럼 보임). 소비자는 하나만 두고 fan-out 할 것.
3. **소스 트랙은 실시간 페이스를 직접 유지해야 한다.** 이스(`await asyncio.sleep`)를 빼면 aiortc 가
   원하는 만큼 프레임을 뽑아가 5초에 **17,390 프레임**의 무음이 순식간에 나간다.
   또한 에코 경로에서 pts 를 재작성하면 RTP 타임스탬프가 어긋나므로, 왕복 지연은
   **도착 순서(1:1 미러링)** 로 정렬해야 한다(pts 정렬은 wraparound 오차 발생).

## 5. 신규 발견 — M2/M4 설계에 영향

### (A) macOS 로컬 네트워크 개인정보 보호가 비-루프백 UDP 를 차단 
실측 (동일 프로세스에서 자기 자신의 주소로 UDP 전송):

| 목적지 | 결과 |
| :--- | :--- |
| 127.0.0.1 | **OK** |
| 192.168.45.167 (en1, Wi-Fi) | **DROP** (응답 없음, 에러도 없음) |
| 100.126.185.45 (Tailscale utun) | **DROP** |

* 증상: aioice 가 수집한 `typ host` 후보로 binding request 를 계속 보내지만 응답이 없어
  ICE 가 `checking` 에서 춘다(연결 불가). aioice 로그상 요청은 정상 전송됨.
* 원인 추정: macOS 15+ 의 Local Network 권한이 에이전트가 띄운 python 프로세스에 부여되지 않아
  비-루프백 로컬 트래픽이 조용히 드롭됨(인터넷 STUN 은 정상 → srflx 후보 획득).
* 대응: 로컬 루프백 테스트는 `aioice.ice.get_host_addresses` 를 127.0.0.1 로 monkeypatch
  (`force_loopback_ice()`), 계획의 브라우저 실연결(M2/M3/M4) 전에
  서버 프로세스에 **로컬 네트워크 권한 승인**(시스템 설정 → 개인정보 보호 → 로컬 네트워크)
  또는 127.0.0.1 루프백 경로로 검증할 것을 권고. ← **M4 리스크 표에 추가 필요**

### (B) aiortc Opus 인코더의 mono→stereo 업믹스에서 −3 dB 발생
* 입력 mono RMS 0.2121 → Opus 왕복(디인터리브 후) 0.1492 = **−3.06 dB**
* 분리 실험: 리샘플러(mono→stereo)만 통과시킨 값이 **−3.01 dB** → 손실은 Opus 가 아니라
  업믹스 매트릭스(각 채널 1/√2)에서 발생. Opus 자체는 레벨 투명(−0.05 dB).
* 함의: 브라우저가 보낸 Opus(스테레오)를 서버에서 mono 로 뽑아 DashScope 로 올릴 때
  채널 0 만 취하면 −3dB 손실 → **ASR 입력 레벨 보정(+3dB) 또는 다운믹스 규칙 명시 필요** (M2 항목).

## 6. 미완 항목 (M0-3)  — 사용자 권한 필요

| 확인 | 결과 |
| :--- | :--- |
| `tailscale status --json` → `CertDomains` | **None** (테넌트 HTTPS 인증서 미활성) |
| `tailscale serve status` | 8767/9119 만 매핑, **8443 없음** |
| tailscale 버전 | 1.102.3 |
| MagicDNS 이름 | `sinsamgyun-ui-macmini.tail091002.ts.net` |
| `mkcert` 설치 여부 | **미설치** (백 경로 미확보) |

⇒ PLAN §M0-3 의 사전 함정(테넌트 HTTPS 인증서 미활성 시 `tailscale cert` 500)이 실제로 확인됨.
**Admin Console 에서 HTTPS Certificates 활성화(사용자 작업)** 또는 `brew install mkcert` 백 중
하나를 선택해야 하며, 두 경로 모두 사용자 결정/권한이 필요하므로 이번 실행에서는 대기.
`tailscale serve --https=8443`  테넌트 설정 변경에 해당해 실행하지 않았다(가드: 시스템 설정 변경 금지).