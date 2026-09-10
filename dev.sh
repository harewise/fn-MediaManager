#!/usr/bin/env bash
# 调试工具：两种调试方式，均不影响生产容器 tmdb-provider（127.0.0.1:38080）
#
#   bash dev.sh up         # 方式一：起调试容器（源码 bind mount，改代码 restart 即生效，无需 rebuild）
#   bash dev.sh restart    #         改完代码后让调试容器生效
#   bash dev.sh logs       #         看调试容器日志
#   bash dev.sh down       #         停掉调试容器
#
#   bash dev.sh host       # 方式二：宿主机直跑（前台，Ctrl-C 停止），可下断点/加 print，
#                          #         Python 3.11 与镜像同版本、ffmpeg 已装，功能完全一致
#
#   bash dev.sh test       # 离线单测（最快反馈，秒级）：逻辑改动先跑这个，不用起任何服务
#   bash dev.sh fixtures   # 重新录制 TMDB fixture（联网，仅测试数据变化时才需要）
# 调试实例跑在 127.0.0.1:38081，缓存/日志隔离在 cache-debug/ logs-debug/
# 单测完全离线：fixture 回放 TMDB 响应，缓存落到临时目录，不碰生产实例
set -euo pipefail
cd "$(dirname "$0")"

DBG_PORT=38081
DBG_URL="http://127.0.0.1:${DBG_PORT}"

mkdir_debug_dirs() { mkdir -p cache-debug logs-debug; }

healthz() {
    for _ in $(seq 1 15); do
        curl -sf "${DBG_URL}/healthz" >/dev/null && { echo "调试实例就绪：${DBG_URL}"; return 0; }
        sleep 1
    done
    echo "警告：${DBG_URL} 未就绪，查看日志排查"; return 1
}

case "${1:-}" in
up)
    mkdir_debug_dirs
    docker compose -f docker-compose.debug.yml up -d
    healthz
    echo "验证示例：curl -s -X POST ${DBG_URL}/meta/diff -d '{\"dataVersion\":\"\",\"category\":\"episode\",\"language\":\"zh-CN\",\"trimId\":\"tm65942\",\"seasonNumber\":4,\"episodeNumber\":81}'"
    ;;
restart)
    docker compose -f docker-compose.debug.yml restart
    healthz
    ;;
logs)
    docker logs -f tmdb-provider-debug
    ;;
test)
    exec python3 tests/run_tests.py
    ;;
fixtures)
    exec python3 tests/record_fixtures.py
    ;;
down)
    docker compose -f docker-compose.debug.yml down
    ;;
host)
    mkdir_debug_dirs
    set -a; . ./.env; set +a          # 读 TMDB_API_KEY
    # 端口必须用 TMDB_PORT 覆盖：tmdb_config.json 里的 port 优先级高于 --port 参数
    TMDB_PORT="${DBG_PORT}" \
    TMDB_BIND=127.0.0.1 \
    TMDB_LOG_FILE="$PWD/logs-debug/requests.log" \
    TMDB_CACHE_DIR="$PWD/cache-debug" \
    exec python3 tmdb_provider.py
    ;;
*)
    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 1
    ;;
esac
