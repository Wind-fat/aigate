#!/usr/bin/env bash
# 本机起一个 Redis（Windows 没有官方 redis-server，用便携版 zip）。
#
#   ./scripts/redis.sh up      启动（后台），并验证 PONG
#   ./scripts/redis.sh status  查端口与版本
#   ./scripts/redis.sh down    停掉
#
# 为什么不用 Docker：本机没有 Docker，也没有管理员权限装 WSL2。
# 生产环境请用 compose 里的 redis:7-alpine（见 DEPLOY.md）。
set -uo pipefail
cd "$(dirname "$0")/.."

DIR="tools/redis"
PORT="${AIGATE_REDIS_PORT:-6379}"
URL="https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip"

case "${1:-up}" in
  up)
    if [ ! -x "$DIR/redis-server.exe" ]; then
      echo "下载便携版 Redis …"
      mkdir -p "$DIR"
      curl -sL --max-time 120 -o "$DIR/redis.zip" "$URL" || {
        echo "下载失败：试试代理 curl -x http://127.0.0.1:7897"; exit 1; }
      (cd "$DIR" && MSYS_NO_PATHCONV=1 powershell -NoProfile -Command "Expand-Archive -Path 'redis.zip' -DestinationPath '.' -Force")
    fi
    "$DIR/redis-cli.exe" -p "$PORT" ping >/dev/null 2>&1 && { echo "已在运行 ($PORT)"; exit 0; }
    # 用 PowerShell 的 Start-Process 拉起，而不是 bash 的 `&`：
    # 在 Windows + git-bash 下，脚本里 `&` 起的原生 exe 会让调用方的管道一直挂着
    # （表现为「./scripts/redis.sh up 卡住不返回」）。Start-Process 是真正脱离。
    MSYS_NO_PATHCONV=1 powershell -NoProfile -Command \
      "Start-Process -WindowStyle Hidden -FilePath '$(pwd -W)/$DIR/redis-server.exe' -ArgumentList '--port','$PORT','--save','\"\"','--appendonly','no'" \
      || { echo "启动失败（见 /tmp/aigate-redis.log）"; exit 1; }
    for _ in $(seq 1 20); do
      "$DIR/redis-cli.exe" -p "$PORT" ping >/dev/null 2>&1 && break
      sleep 0.3
    done
    "$DIR/redis-cli.exe" -p "$PORT" ping
    ;;
  status)
    "$DIR/redis-cli.exe" -p "$PORT" ping 2>/dev/null || echo "not running"
    "$DIR/redis-cli.exe" -p "$PORT" info server 2>/dev/null | grep -E "redis_version|os:" | head -2
    ;;
  down)
    "$DIR/redis-cli.exe" -p "$PORT" shutdown nosave 2>/dev/null || true
    echo "stopped"
    ;;
  *)
    echo "usage: $0 up|status|down"; exit 2;;
esac
