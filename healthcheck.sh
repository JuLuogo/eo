#!/bin/sh
# 容器健康检查（Dockerfile HEALTHCHECK 调用）。
#
# 为什么不用 `python run.py selftest`：那会每 2 分钟起一个完整 Python 解释器
# 并 import 整个 eo 包，在 1~2 核的小机器上是明显的 CPU/内存尖峰；而且一旦
# 自检里有任何重活（曾经有个超大网段展开的 bug），就会把容器直接 OOM 掉。
# 这里只发一个 HTTP 请求，开销可以忽略。
#
# 非 web 模式没有监听端口，视为健康（容器活着本身就是正常的）。
set -eu

case "${EO_MODE:-web}" in
  web)
    port="${EO_WEB_PORT:-8787}"
    host="${EO_WEB_HEALTHCHECK_HOST:-127.0.0.1}"
    curl -fsS --max-time 8 "http://${host}:${port}/healthz" >/dev/null
    ;;
  *)
    exit 0
    ;;
esac
