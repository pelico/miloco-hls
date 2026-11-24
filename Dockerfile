# miloco 摄像头整套容器：HLS/Ogg 桥接(18180) + Web 控制台(18190)
# 构建: docker build -t miloco-hls .
# 运行:
#   docker run -d --name miloco-hls --restart unless-stopped \
#     -p 18180:18180 -p 18190:18190 \
#     -v ~/.openclaw/miloco/config.json:/config/config.json:ro \
#     -v ~/.local/share/miloco-hls:/data \
#     -e MILOCO_CONFIG=/config/config.json \
#     -e BRIDGE_CACHE=/data/cache \
#     -e CONSOLE_SCHEDULES=/data/schedules.json \
#     -e CONSOLE_PASS_FILE=/data/console.pass \
#     miloco-hls
# 说明: miloco 本体仍跑在宿主机(:1810)，容器经 host.docker.internal 访问
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir aiohttp av websockets

WORKDIR /app
COPY miloco-hls-bridge.py miloco-console.py start.sh ./
RUN chmod +x start.sh

ENV MILOCO_BASE=http://host.docker.internal:1810 \
    BRIDGE_HOST=0.0.0.0 BRIDGE_PORT=18180 \
    CONSOLE_HOST=0.0.0.0 CONSOLE_PORT=18190

EXPOSE 18180 18190
CMD ["./start.sh"]
