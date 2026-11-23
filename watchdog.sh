#!/bin/bash
# miloco-hls 跟随看门狗：
# - miloco 在跑 → 拉起桥接并监督；miloco 掉线 → 杀掉桥接并退出本脚本
# - 配合 launchd WatchPaths（监听 miloco 启动时写的 launchd-stdio.log），
#   miloco 每次启动都会把本看门狗唤醒；miloco 不在时本机无任何残留进程

BASE="$HOME/.local/share/miloco-hls"
BRIDGE="$BASE/miloco-hls-bridge.py"
LOG="$BASE/bridge.log"
MILOCO="http://127.0.0.1:1810/"
MYPORT="http://127.0.0.1:18180/healthz"

up() { curl -sf -m 3 "$1" >/dev/null 2>&1; }

# 已有桥接实例在跑（上一次看门狗还没退），直接让位
if up "$MYPORT"; then
    exit 0
fi

# 等 miloco 就绪（最多 60 秒），起不来就没必要留在这里
for _ in $(seq 1 30); do
    up "$MILOCO" && break
    sleep 2
done
up "$MILOCO" || exit 0

# 启动桥接；若自己崩了就退避重启，最多 5 次
tries=0
while [ "$tries" -lt 5 ]; do
    tries=$((tries + 1))
    uv run --with aiohttp --with av --with websockets "$BRIDGE" >> "$LOG" 2>&1 &
    BR=$!
    # 监督循环：桥活着 && miloco 活着 才继续
    while kill -0 "$BR" 2>/dev/null; do
        sleep 5
        if ! up "$MILOCO"; then
            kill "$BR" 2>/dev/null
            wait "$BR" 2>/dev/null
            exit 0   # miloco 下线，桥接跟随退出
        fi
    done
    wait "$BR" 2>/dev/null
    # 桥接自己退了：miloco 还在就等几秒再拉一次
    up "$MILOCO" || exit 0
    sleep 3
done
