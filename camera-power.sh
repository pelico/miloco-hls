#!/bin/bash
# 摄像头定时开关：调 miloco 的 MIoT 控制接口设置总开关 prop.2.1
# 用法: camera-power.sh <did> <on|off>
DID="$1"
ACTION="$2"
CFG="$HOME/.openclaw/miloco/config.json"
LOG="$HOME/.local/share/miloco-hls/schedule.log"

VAL=false
[ "$ACTION" = "on" ] && VAL=true

TOKEN=$(/usr/bin/python3 -c "import json;print(json.load(open('$CFG'))['server']['token'])" 2>/dev/null)
if [ -z "$TOKEN" ]; then
    echo "$(date '+%m-%d %H:%M:%S') did=$DID -> $ACTION 失败: 读不到 token" >> "$LOG"
    exit 1
fi

RESULT=$(curl -sf -m 10 -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"type\":\"set_property\",\"iid\":\"prop.2.1\",\"value\":$VAL}" \
    "http://127.0.0.1:1810/api/miot/devices/$DID/control")
CODE=$(echo "$RESULT" | /usr/bin/python3 -c "import json,sys;print(json.load(sys.stdin).get('code'))" 2>/dev/null)
echo "$(date '+%m-%d %H:%M:%S') did=$DID -> $ACTION code=$CODE $RESULT" >> "$LOG"
[ "$CODE" = "0" ] || exit 1
