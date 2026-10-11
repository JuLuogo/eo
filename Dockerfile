# 多平台边缘节点优选 —— 容器镜像
#
# 构建：
#   docker build -t edge-optimizer:latest .
# 运行（示例见 docker-compose.yml）：
#   docker run --rm -v ./results:/app/results edge-optimizer scan -p eo -q
#
FROM python:3.12-slim

# curl 是优选探测的必需依赖；tzdata 让 --window 时间窗口按本地时区生效
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 华为云 DNS SDK（只有下发 DNS 时才会用到，装了也不影响纯扫描）
RUN pip install --no-cache-dir huaweicloudsdkcore huaweicloudsdkdns

WORKDIR /app

# 代码
COPY eo/ ./eo/
COPY run.py eo.py updatedns.py README.md docker-entrypoint.sh ./

# 网段与结果目录（实际部署时建议用 volume 挂载，避免重建镜像丢数据）
COPY targets/ ./targets/
RUN mkdir -p results/history config && chmod +x docker-entrypoint.sh

# 以非 root 运行，降低容器逃逸面
RUN useradd --create-home --uid 10001 optimizer \
    && chown -R optimizer:optimizer /app
USER optimizer

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    EO_MODE=web \
    EO_WEB_PORT=8787 \
    EO_RATE=100 \
    EO_INTERVAL=300 \
    EO_DNS_SYNC=none

EXPOSE 8787

# 入口脚本把环境变量翻译成命令行参数（见 README「部署到 Linux 服务器」）
ENTRYPOINT ["./docker-entrypoint.sh"]

HEALTHCHECK --interval=2m --timeout=30s --start-period=40s \
    CMD python run.py selftest > /dev/null 2>&1 || exit 1

LABEL org.opencontainers.image.title="edge-optimizer" \
      org.opencontainers.image.description="多平台边缘节点优选（EO/ESA/CFT 等）"
