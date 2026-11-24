# miloco 摄像头控制台容器
# 构建: docker build -t miloco-console .
# 运行: docker run -d --name miloco-console -p 18190:18190 \
#   -v ~/.openclaw/miloco/config.json:/config/config.json:ro \
#   -e MILOCO_CONFIG=/config/config.json \
#   -e MILOCO_BASE=http://host.docker.internal:1810 \
#   miloco-console
FROM python:3.12-slim

RUN pip install --no-cache-dir aiohttp
COPY miloco-console.py /app/miloco-console.py

ENV CONSOLE_HOST=0.0.0.0 \
    CONSOLE_PORT=18190

EXPOSE 18190
CMD ["python", "/app/miloco-console.py"]
