# 多平台边缘节点优选 —— 容器镜像
#
# 在项目根目录构建（最常用，不需要任何镜像仓库）：
#   docker build -t edge-optimizer:latest .
# 或直接后台跑起来：
#   docker compose up -d --build
#
# 国内网络如果拉不动基础镜像，可换镜像源：
#   docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
#                -t edge-optimizer:latest .
#
# 构建参数（都有默认值，一般不用改）：
#   BASE_IMAGE    基础镜像，默认 python:3.12-slim
#   APT_MIRROR    系统包镜像源，默认用系统自带（国内可设 mirrors.aliyun.com 等）
#   PIP_INDEX     Python 包镜像源，默认清华（避开境内访问 PyPI 被重置的问题）
#   INSTALL_DNS_SDK 是否装华为云 SDK：1=装（默认），0=不装（纯优选，构建更快）
#
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# 让构建过程也能用镜像源，避免境内拉包被重置
ARG APT_MIRROR=
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG INSTALL_DNS_SDK=1

# curl 是优选探测的必需依赖；tzdata 让时间窗口按本地时区判断
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
        # 把 debian 源换成指定镜像（国内建议 mirrors.aliyun.com / mirrors.tuna.tsinghua.edu.cn）
        if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
            sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
        fi; \
        if [ -f /etc/apt/sources.list ]; then \
            sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
        fi; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl tzdata ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# 华为云 DNS SDK：只有「自动下发 DNS」才需要
RUN if [ "$INSTALL_DNS_SDK" = "1" ]; then \
        pip install --no-cache-dir --disable-pip-version-check \
            -i "$PIP_INDEX" --retries 3 --timeout 30 \
            huaweicloudsdkcore huaweicloudsdkdns; \
    else \
        echo "跳过华为云 DNS SDK（INSTALL_DNS_SDK=0，纯优选模式）"; \
    fi

WORKDIR /app

# 代码
COPY eo/ ./eo/
COPY run.py eo.py updatedns.py README.md INSTALL.md docker-entrypoint.sh ./

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

# 入口脚本把环境变量翻译成命令行参数（见 INSTALL.md）
ENTRYPOINT ["./docker-entrypoint.sh"]

HEALTHCHECK --interval=2m --timeout=30s --start-period=40s \
    CMD python run.py selftest > /dev/null 2>&1 || exit 1

LABEL org.opencontainers.image.title="edge-optimizer" \
      org.opencontainers.image.description="多平台边缘节点优选（EdgeOne / ESA / CloudFront，按运营商分 lt/dx/yd）" \
      org.opencontainers.image.source="https://gitee.com/peroe/eo"
