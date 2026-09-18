#!/bin/bash
# Hermes WebRTC 브리지 — 원클릭 시작 스크립트
#   사용:  ~/hermes-webrtc-bridge/start-bridge.sh          (기동)
#          ~/hermes-webrtc-bridge/start-bridge.sh status    (상태)
#          ~/hermes-webrtc-bridge/start-bridge.sh stop      (중지)
#
# 주의: server/app.py 를 직접 실행하면 `core` 모듈을 못 찾는다(ModuleNotFoundError).
#       반드시 PYTHONPATH=프로젝트루트 + `-m server.app` 으로 실행할 것.
set -uo pipefail
PROJ="$HOME/hermes-webrtc-bridge"
TS="/Applications/Tailscale.app/Contents/MacOS/tailscale"
PORT=8000
LOG=/tmp/webrtc-bridge.log

cd "$PROJ" || exit 1
export PYTHONPATH="$PROJ"

case "${1:-start}" in
  start)
    # 토큰·설정 로드 (.env — 값은 화면에 출력하지 않음)
    if [ -f .env ]; then set -a; . ./.env; set +a; fi

    if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
      echo "▶ 서버가 이미 실행 중입니다 (포트 $PORT)"
    else
      nohup .venv/bin/python -m server.app --host 127.0.0.1 --port $PORT > "$LOG" 2>&1 &
      echo "▶ 서버 기동 PID=$! (로그: $LOG)"
      sleep 6
    fi

    # Tailscale HTTPS 프록시 (테넌트에 HTTPS 인증서 활성화 전제)
    "$TS" serve --bg $PORT >/dev/null 2>&1
    echo "▶ HTTPS 프록시: https://macmini.tail091002.ts.net/"
    if [ -f phone-link.txt ]; then
      echo "▶ 폰 접속 링크:  cat $PROJ/phone-link.txt   (토큰 포함, chmod 600)"
    fi
    ;;
  stop)
    pkill -f "server.app --host 127.0.0.1" && echo "■ 서버 중지" || echo "■ 실행 중인 서버 없음"
    "$TS" serve --https=443 off >/dev/null 2>&1 && echo "■ HTTPS 프록시 해제"
    ;;
  status)
    echo "--- 포트 $PORT ---"; lsof -nP -iTCP:$PORT -sTCP:LISTEN | head -3 || echo "(미실행)"
    echo "--- tailscale serve ---"; "$TS" serve status 2>&1 | head -10
    echo "--- 최근 로그 ---"; tail -5 "$LOG" 2>/dev/null
    ;;
  *)
    echo "사용법: $0 {start|stop|status}"; exit 1;;
esac