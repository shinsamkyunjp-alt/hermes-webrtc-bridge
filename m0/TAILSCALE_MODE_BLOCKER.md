# M0 블로커 — Tailscale 커널 TUN 모드 경로 (해소됨)

> 발견: 2026-09-14 23:1x KST (야간 크론 1회차)
> **해소 확인: 2026-09-14 23:38 KST** — 공식 Tailscale GUI 앱이 커널 TUN 모드로 실행 중임을 실측 확인
> 상태: ✅ **RESOLVED** (미디어 경로 확보) / 잔여 2건 별도 추적

## 0. 결론 (요약)
- **P0 블로커 해소**: 이 Mac에는 **커널 모드(utun) Tailscale 노드가 실제로 살아 있다.**
  - `utun6` → `100.103.115.13` (공식 GUI 앱, bundle `io.tailscale.ipn.macos` v1.102.4, App Store 배포)
  - 라우팅 테이블: `100.64/10 → utun6` (커널 L3 경로 정상)
  - **UDP 바인딩 성공** / **UDP 자기 왕복 성공** / **폰까지 ping 왕복 8ms**
- 즉 aiortc가 tailnet IP(`100.103.115.13`)로 UDP 소켓을 열 수 있다 → **WebRTC 미디어(UDP) 원격 경로 성립**.
- 잔여 ①: 테넌트 HTTPS 인증서 미활성(`CertDomains: None`) → 브라우저 마이크용 HTTPS 소스 필요
- 잔여 ②: **중복 노드** — 홈브루 userspace 데몬이 별도 노드로 동시 등록 중 (§4)

## 1. 실측 증거 (해소 시점)

### (1) 커널 인터페이스 존재
```
ifconfig utun6
  flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
  inet 100.103.115.13 --> 100.103.115.13 netmask 0xffffffff

netstat -rn
  100.64/10          link#25   UCS    utun6
  100.103.115.13     100.103.115.13   UH   utun6
```

### (2) 소켓 바인딩 / 왕복 검증
```
UDP bind('100.103.115.13')  → ✅ OK
TCP bind('100.103.115.13')  → ✅ OK
UDP 자기 왕복(100.103.115.13) → ✅ b'webrtc-probe' 수신

# 대조: 기존 userspace 노드 주소는 여전히 불가
UDP bind('100.126.185.45')  → ❌ Errno 49 Can't assign requested address
```

### (3) tailnet 왕복 / 경로 품질
```
tailscale ping z-fold8-1  → pong via 192.168.45.144:50719 in 8ms  (직접 경로)
tailscale netcheck        → UDP: true, PortMapping: UPnP, Nearest DERP: tok 38.7ms
```

### (4) 실행 주체
```
/Applications/Tailscale.app/Contents/MacOS/Tailscale        (PID 10518)
/Applications/Tailscale.app/Contents/PlugIns/IPNExtension.appex/.../IPNExtension (PID 10549)
번들(io.tailscale.ipn.macos) mtime: 2026-09-14 23:32:30  ← 스캔 시점(23:2x) 직후 교체/갱신됨
```

### (5) 오판 정정
- 초기 스캔(23:2x)에서 `/Applications/Tailscale.app`이 보이지 않아 "미설치"로 단정한 것은 **오판**이었다.
- 실제로는 번들이 **23:32:30에 갱신**되었고 프로세스는 **23:32:42에 기동** — 스 창과 교체 시점이 엇갈렸다.
- 교훈: ** 부재를 단정하기 전에 설치 영수증(`/private/var/db/receipts`)·컨테이너·번들 mtime·프로세스를 함께 보고, 사용자 진술을 우선 신뢰**할 것.

## 2. 이전 블로커(userspace 모드) 증거 — 이력 보존
```
ps -p 754
/opt/homebrew/bin/tailscaled --tun=userspace-networking \
  --socks5-server=localhost:1055 --outbound-http-proxy-listen=localhost:1055 \
  --state=~/.tailscale/tailscaled.state --socket=~/.tailscale/tailscaled.sock
→ 이 데몬은 여전히 실행 중이며 노드 sinsamgyun-ui-macmini(100.126.185.45)를 점유
```
`tailscale serve`는 UDP 전달 기능이 없음(`--tcp/--http/--https`만) → userspace 노드로는 WebRTC 미디어 불가.
따라서 **미디어는 반드시 커널 노드(100.103.115.13)를 사용**해야 한다.

## 3. 잔여 ① — 테넌트 HTTPS 인증서
```
tailscale cert macmini.tail091002.ts.net
→ 500 Internal Server Error: your Tailscale account does not support getting TLS certs
CertDomains: None
```
- 필요: 관리자 콘솔(https://login.tailscale.com/admin/dns) → **HTTPS Certificates 활성화**
- 대안: `mkcert` 로컬 인증서 + 기기 신뢰 등록
- 용도: 브라우저 `getUserMedia`(마이크)는 보안 컨텍스트(HTTPS) 필수

## 4. 잔여 ② — 중복 노드 (정리 권장, 사용자 승인 필요)
| 노드 | 주소 | 제공자 | 용도 |
| :--- | :--- | :--- | :--- |
| `macmini` | 100.103.115.13 | 공식 GUI 앱 (커널 TUN) | ✅ WebRTC 미디어 경로 |
| `sinsamgyun-ui-macmini` | 100.126.185.45 | 브루 userspace 데몬 | `tailscale serve` 8767(relay)·9119(dashboard) |

- 같은 기기가 tailnet에 **두 노드**로 뜬다 → 주소 혼선·정책/ACL 관리 중복.
- ⚠️ **userspace 데몬을 즉시 중단하면 안 된다**: `hermes relay start`(8767)와 `hermes dashboard`(9119)가
  현재 그 노드의 serve 뒤에 물려 있다. 중단 시 서비스 접근이 끊긴다.
- 권장 절차(사용자 승인 후): ① GUI 노드로 serve 재설정 → ② 검증 → ③ 브루 데몬 unload → ④ 노드 제거

## 5. 계획 반영
- **M0-3 미디어 경로**: ✅ 완료 (커널 노드 100.103.115.13 사용)
- **M0-3 HTTPS**: ⏳ 사용자 솔 작업 대기 (테넌트 HTTPS Certificates)
- 야간 크론: M0-3의 HTTPS를 건너뛰고 **M1(DashScope 세션 관리자 분리)**부터 진행