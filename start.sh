#!/bin/sh
# 同时拉起桥接(18180)与控制台(18190)；任一进程退出则容器退出交给 restart 策略
trap 'kill 0' TERM INT
python /app/miloco-hls-bridge.py &
P1=$!
python /app/miloco-console.py &
P2=$!
wait $P1 $P2
