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

# --------------------------------------------------------------------------
# 启动自检：确认 eo 包在镜像里（而不是只有一个同名文件）
# 症状：ModuleNotFoundError: No module named 'eo.core'; 'eo' is not a package
# 成因：构建时 eo/ 目录没被 COPY 进镜像（常见于 .dockerignore 把它排除了）。
# 这里提前给出人话报错，而不是刷屏的 traceback。
# --------------------------------------------------------------------------
if ! python -c '
import sys, pathlib
try:
    import eo
except Exception as exc:
    print(f"[entrypoint] 无法导入 eo 包：{type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
p = pathlib.Path(eo.__file__)
if p.name != "__init__.py":
    print(f"[entrypoint] eo 被 {p} 遮蔽（不是包），镜像构建有误", file=sys.stderr)
    sys.exit(1)
' 2>/dev/null; then
    echo "[entrypoint] 启动自检失败：镜像里的 eo 包不完整。" >&2
    echo "  请重新构建镜像（代码里已加构建期与运行期检查）：" >&2
    echo "    cd /opt/edge-optimizer && docker compose up -d --build --force-recreate" >&2
    echo "  若仍失败，请执行并反馈输出：" >&2
    echo "    docker run --rm --entrypoint sh edge-optimizer:latest -c 'ls -la /app && ls -la /app/eo'" >&2
    exit 1
fi

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

# --------------------------------------------------------------------------
# 修正挂载目录权限后降权运行
#
# 问题：容器以 optimizer(10001) 运行，但宿主机的 ./config、./results 由 root 创建
# （deploy.sh / onekey.sh 都以 root 执行），挂载进来后非 root 用户无法写入，
# 表现为启动即崩：
#   PermissionError: [Errno 13] Permission denied: '/app/config/config.tmp'
#
# 做法：入口以 root 启动 -> 只对"需要写入"的目录做归属修正 -> 用 setpriv 降权，
# 程序本身仍以非 root 运行（保留容器安全边界）。
# --------------------------------------------------------------------------
APP_UID="${EO_APP_UID:-10001}"
APP_GID="${EO_APP_GID:-10001}"

fix_ownership() {
    local dirs="/app/config /app/results /app/history /app/targets" d
    if [ "$(id -u)" != "0" ]; then
        # 非 root 时无法 chown，只能检测并给出可操作的提示
        for d in $dirs; do
            [ -d "$d" ] || continue
            if ! touch "$d/.wtest" 2>/dev/null; then
                echo "[entrypoint] 无法写入 $d —— 容器内以非 root 运行，但该目录不属于 UID $APP_UID。" >&2
                echo "  请任选一种方式修复后重启：" >&2
                echo "    sudo chown -R $APP_UID:$APP_GID $(dirname $d)/$(basename $d)" >&2
                echo "    或在 docker-compose.yml 的 services.optimizer 下加： user: \"0:0\"" >&2
                echo "    或删掉该挂载（配置只存在容器内，重建镜像会丢）" >&2
                exit 1
            fi
            rm -f "$d/.wtest"
        done
        return 0
    fi

    # root：统一归属，避免宿主 root 目录导致写入失败
    for d in $dirs; do
        [ -d "$d" ] || continue
        if [ -n "$(find "$d" ! -uid "$APP_UID" -print -quit 2>/dev/null)" ]; then
            chown -R "$APP_UID:$APP_GID" "$d" 2>/dev/null || true
        fi
        chmod u+rwx "$d" 2>/dev/null || true
    done
    # /app 下可能被挂载覆盖的其它可写位置
    for d in /app/config /app/results; do
        [ -d "$d" ] && chmod 700 "$d" 2>/dev/null || true
    done
    return 0
}

fix_ownership

# 降权执行（以 root 启动时）；已经是非 root 则直接 exec
if [ "$(id -u)" = "0" ] && command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups python run.py "$@"
fi

exec python run.py "$@"
