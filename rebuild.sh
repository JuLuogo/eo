#!/bin/sh
# 重建镜像并启动（Docker Compose 部署用）。
#
# 为什么不直接 `docker compose up -d --build`：
#   Compose 的 build 依赖 buildx >= 0.17，而 Debian / Ubuntu 源里的 docker.io
#   自带的 buildx 常常只有 0.13，会直接失败：
#       compose build requires buildx 0.17.0 or later
#   经典 `docker build` 不依赖 buildx，任何 Docker 版本都能用。
#
# 用法：
#   ./rebuild.sh              # 用 .env 里的 EO_IMAGE（没写则 edge-optimizer:latest）
#   ./rebuild.sh my-img:tag   # 临时指定镜像名
set -e
cd "$(dirname "$0")"

IMG="$1"
if [ -z "$IMG" ] && [ -f .env ]; then
    IMG="$(sed -n 's/^EO_IMAGE=//p' .env | tail -1 | sed 's/[[:space:]]*#.*$//' | tr -d '"'"'" | sed 's/[[:space:]]*$//')"
fi
[ -n "$IMG" ] || IMG="edge-optimizer:latest"

echo "[rebuild] 构建镜像 $IMG …"
docker build -t "$IMG" .

echo "[rebuild] 启动容器 …"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    EO_IMAGE="$IMG" docker compose up -d --no-build
else
    EO_IMAGE="$IMG" docker-compose up -d --no-build
fi

echo "[rebuild] 完成。状态与日志："
docker compose ps 2>/dev/null || docker-compose ps
echo
echo "  看日志： docker compose logs -f"
echo "  取令牌： docker compose logs | grep 访问令牌"
