#!/bin/sh
# 容器入口：把环境变量翻译成 run.py 的参数，方便在 compose 里做配置管理。
#
# 支持的环境变量：
#   EO_MODE         web | monitor | scan | doctor | selftest（默认 web）
#   EO_WEB_PORT     Web 控制台端口（默认 8787）
#   EO_WEB_TOKEN    网页访问令牌（默认自动生成并写入 config/config.json）
#   EO_PLATFORMS    平台列表，空格或逗号分隔，如 "eo esa cft"；空=全部已启用
#   EO_INTERVAL     monitor 轮询间隔秒数（默认 300）
#   EO_WINDOW       时间窗口，如 00:00-07:00
#   EO_RATE         全平台合计连接速率上限（默认 100，0=不限速）
#   EO_DNS_SYNC     none | dry-run | push（默认 none）
#   EO_WORKERS      并发数覆盖（可选）
#   EO_TIMEOUT      单次探测超时秒数（可选）
#   EO_EXTRA_ARGS   追加任意参数，如 "--ping --retries 2"
#   其余（TZ / HUAWEI_AK / HUAWEI_SK / CLOUDFLARE_API_TOKEN / EO_WEBHOOK）由程序自己读取
#
# 注意：web 模式下所有配置（限速、间隔、网段、DNS 服务商与令牌）都在网页上改，
# 会持久化到 config/config.json，因此这里的相关环境变量只作为首次部署的默认值。
set -e

MODE="${EO_MODE:-web}"

case "$MODE" in
  web)
    set -- web --port "${EO_WEB_PORT:-8787}" --host-bind "${EO_WEB_HOST:-0.0.0.0}"
    # 令牌可以直接用环境变量指定，留空则由程序生成并写入配置文件
    ;;
  selftest|doctor|list)
    set -- "$MODE"
    ;;
  *)
    # scan / monitor：走完整的命令行参数
    RATE="${EO_RATE:-100}"
    DNS_SYNC="${EO_DNS_SYNC:-none}"
    set -- "$MODE" --rate "$RATE" --dns-sync "$DNS_SYNC"

    if [ -n "$EO_PLATFORMS" ]; then
        PLATS=$(echo "$EO_PLATFORMS" | tr ',' ' ')
        set -- "$@" -p $PLATS
    fi

    if [ "$MODE" = "monitor" ]; then
        set -- "$@" --interval "${EO_INTERVAL:-300}"
        [ -n "$EO_WINDOW" ] && set -- "$@" --window "$EO_WINDOW"
        [ -n "$EO_CYCLES" ] && set -- "$@" --cycles "$EO_CYCLES"
    fi

    if [ "$DNS_SYNC" = "push" ] && [ "${EO_CONFIRM_DNS:-no}" = "yes" ]; then
        set -- "$@" --yes
    fi

    [ -n "$EO_WORKERS" ] && set -- "$@" --workers "$EO_WORKERS"
    [ -n "$EO_TIMEOUT" ] && set -- "$@" --timeout "$EO_TIMEOUT"
    [ -n "$EO_WEBHOOK" ] && set -- "$@" --webhook "$EO_WEBHOOK"
    ;;
esac

if [ -n "$EO_EXTRA_ARGS" ]; then
    # 允许额外参数（有意做分词，方便写 "--ping --retries 2"）
    # shellcheck disable=SC2086
    set -- "$@" $EO_EXTRA_ARGS
fi

exec python run.py "$@"
