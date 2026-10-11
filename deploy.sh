#!/usr/bin/env bash
# 边缘节点优选 —— 裸机部署脚本（不需要 Docker）
#
#   ./deploy.sh install     交互式配置 + 安装 systemd 服务（会问端口和运营商）
#   ./deploy.sh config      重新交互式配置（改端口/运营商）并重启服务
#   ./deploy.sh isp         识别本机运营商（联通/电信/移动）
#   ./deploy.sh start       启动服务
#   ./deploy.sh stop        停止服务
#   ./deploy.sh restart     重启服务
#   ./deploy.sh status      查看状态 + 运营商 + 访问地址 + 令牌
#   ./deploy.sh logs        跟踪日志
#   ./deploy.sh doctor      跑一次环境体检（确认链路可信）
#   ./deploy.sh scan        按本机运营商手动跑一轮优选
#   ./deploy.sh uninstall   卸载 systemd 服务（保留数据）
#   ./deploy.sh run         前台直接跑（调试用，Ctrl+C 退出）
#
# 非交互安装（适合批量部署）：
#   EO_NETWORK=lt EO_WEB_PORT=8787 ./deploy.sh install --yes
#
# 三台机器（联通/电信/移动）各跑各的：
#   install 时会自动识别本机运营商并让你确认，结果写入 .env；
#   对应的平台变体（eo-lt / esa-lt / cft-lt 等）也会一并配好。
set -euo pipefail

APP_NAME="edge-optimizer"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
ENV_FILE="${SCRIPT_DIR}/.env"
VENV_DIR="${SCRIPT_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ASSUME_YES=0

log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# 优先用 venv 里的 python
py() {
    if [ -x "$VENV_DIR/bin/python" ]; then "$VENV_DIR/bin/python" "$@"; else "$PYTHON_BIN" "$@"; fi
}

need_python() {
    command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "找不到 $PYTHON_BIN，请先安装 python3"
    local ver
    ver="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    log "使用 Python $ver（$PYTHON_BIN）"
    "$PYTHON_BIN" - <<'PY' || die "需要 Python 3.10+"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
}

need_curl() {
    command -v curl >/dev/null 2>&1 || die "找不到 curl，请先安装：apt install curl / yum install curl"
}

# --------------------------------------------------------------------------
# 虚拟环境依赖：Debian/Ubuntu 上 python3-venv 可能没装
# 症状：创建 venv 时报 "ensurepip is not available / Failing command: .venv/bin/python3"
# 注意：只测 `import venv` 是测不出来的，必须看 ensurepip。
# --------------------------------------------------------------------------
venv_module_ok() {
    "$PYTHON_BIN" -c 'import ensurepip, venv' >/dev/null 2>&1
}

pip_in_venv() {
    [ -x "$VENV_DIR/bin/pip" ] && "$VENV_DIR/bin/pip" --version >/dev/null 2>&1
}

# 需要 root（或 sudo）才能装系统包；这里返回可用的提权前缀。
# 若当前既不是 root、sudo 又需要密码且没有终端，则返回 "unavailable"，
# 避免脚本卡在密码提示上（无人值守/管道执行时会死锁）。
sudo_prefix() {
    if [ "$(id -u)" = "0" ]; then
        echo ""
        return 0
    fi
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        echo "sudo"
        return 0
    fi
    if command -v sudo >/dev/null 2>&1 && is_interactive; then
        echo "sudo"          # 有终端，可以正常输密码
        return 0
    fi
    echo "unavailable"
    return 1
}

# 按发行版安装 python venv 支持包
install_venv_package() {
    local prefix pyver pkg
    if ! prefix="$(sudo_prefix)"; then
        warn "需要 root 权限安装 venv 支持包，但当前无法提权（非 root 且 sudo 需密码）"
        return 1
    fi
    pyver="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

    if command -v apt-get >/dev/null 2>&1; then
        warn "缺少 ensurepip，尝试安装 venv 支持包…"
        # 优先装与当前解释器版本匹配的包（Debian 12+/Ubuntu 23+ 需要），再试通用包
        for pkg in "python${pyver}-venv" "python3-venv"; do
            $prefix env DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
            if $prefix env DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg" >/dev/null 2>&1; then
                log "已安装 $pkg"
                return 0
            fi
        done
    elif command -v dnf >/dev/null 2>&1; then
        warn "缺少 ensurepip，尝试安装 python3-pip…"
        $prefix dnf install -y python3-pip >/dev/null 2>&1 && return 0
    elif command -v yum >/dev/null 2>&1; then
        warn "缺少 ensurepip，尝试安装 python3-pip…"
        $prefix yum install -y python3-pip >/dev/null 2>&1 && return 0
    elif command -v apk >/dev/null 2>&1; then
        warn "缺少 ensurepip，尝试安装 py3-pip / python3…"
        $prefix apk add --no-cache py3-pip >/dev/null 2>&1 && return 0
    fi
    return 1
}

# --------------------------------------------------------------------------
# .env 读写：交给 Python 处理，能正确剥离行尾注释
# （用 grep/sed 取值会把 "8787   # 说明" 整个当成端口，导致莫名报错）
# --------------------------------------------------------------------------
env_get() {
    local key="$1" default="${2:-}" value
    value="$(py -m eo.deploytool env get "$key" --file "$ENV_FILE" 2>/dev/null || true)"
    if [ -n "$value" ]; then echo "$value"; else echo "$default"; fi
}

env_set() {
    py -m eo.deploytool env set "$1" "$2" --file "$ENV_FILE"
}

# 从终端读一行（stdin 被管道占用时用 /dev/tty，保证交互仍可用）
ask() {
    local prompt="$1" answer=""
    if [ -r /dev/tty ]; then
        read -r -p "$prompt" answer < /dev/tty || true
    elif [ -t 0 ]; then
        read -r -p "$prompt" answer || true
    fi
    printf '%s' "$answer"
}

is_interactive() {
    [ -r /dev/tty ] || [ -t 0 ]
}

detect_ip() {
    local ip
    ip="$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || true)"
    if [ -z "$ip" ]; then
        ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    echo "${ip:-127.0.0.1}"
}

# 识别本机运营商（联通/电信/移动），失败回显 unknown
detect_carrier() {
    py -m eo.deploytool isp --quiet 2>/dev/null || echo "unknown"
}

carrier_label() {
    case "$1" in
        lt) echo "联通" ;;
        dx) echo "电信" ;;
        yd) echo "移动" ;;
        *)  echo "未知" ;;
    esac
}

carrier_platforms() {
    echo "eo-$1,esa-$1,cft-$1"
}

ensure_env() {
    if [ ! -f "$ENV_FILE" ]; then
        if [ -f "${SCRIPT_DIR}/.env.example" ]; then
            cp "${SCRIPT_DIR}/.env.example" "$ENV_FILE"
            log "已从 .env.example 生成 .env"
        else
            : > "$ENV_FILE"
        fi
    fi
    chmod 600 "$ENV_FILE"
}

# --------------------------------------------------------------------------
# 交互式配置：问运营商、问端口
# --------------------------------------------------------------------------
ask_setup() {
    local tty=0
    is_interactive && tty=1

    local carrier port default_port token

    # ---- 运营商 ----
    if [ -n "${EO_NETWORK:-}" ]; then
        carrier="$EO_NETWORK"
        log "使用环境变量指定的运营商：$(carrier_label "$carrier")"
    elif [ "$tty" = "1" ]; then
        printf '\n'
        log "正在识别本机运营商（联通/电信/移动）…"
        local detected
        detected="$(detect_carrier)"
        if [ "$detected" != "unknown" ]; then
            log "识别结果：$(carrier_label "$detected")"
            py -m eo.deploytool isp 2>/dev/null | sed 's/^/    /' || true
        else
            warn "无法自动识别本机运营商（可能是境外/中转 IP，或识别服务不可达）"
        fi

        local hint="lt"
        [ "$detected" != "unknown" ] && hint="$detected"
        printf '\n这台机器是哪个运营商？\n'
        printf '  1) 联通 (lt)%s\n' "$([ "$hint" = lt ] && echo '  <= 识别结果' || true)"
        printf '  2) 电信 (dx)%s\n' "$([ "$hint" = dx ] && echo '  <= 识别结果' || true)"
        printf '  3) 移动 (yd)%s\n' "$([ "$hint" = yd ] && echo '  <= 识别结果' || true)"
        local choice
        choice="$(ask "请输入 1/2/3（回车用 ${hint} $(carrier_label "$hint")）: ")"
        case "$choice" in
            1) carrier="lt" ;;
            2) carrier="dx" ;;
            3) carrier="yd" ;;
            "") carrier="$hint" ;;
            lt|dx|yd) carrier="$choice" ;;
            *) warn "输入无法识别，使用 $(carrier_label "$hint")"; carrier="$hint" ;;
        esac
    else
        carrier="lt"
        warn "非交互模式且未指定 EO_NETWORK，默认使用联通 (lt)"
    fi

    # ---- 端口（按运营商给不同默认值，三台机器天然错开）----
    # 运营商必须是 lt/dx/yd 之一：自动识别失败时（境外/中转 IP）兜底为联通，
    # 绝不能留 unknown —— 否则会拼出 eo-unknown 这种不存在的平台
    case "$carrier" in
        lt|dx|yd) : ;;
        *)
            warn "运营商未确定，默认按联通 (lt) 处理；可用 ./deploy.sh config 修改"
            carrier="lt"
            ;;
    esac

    case "$carrier" in
        dx) default_port=8788 ;;
        yd) default_port=8789 ;;
        *)  default_port=8787 ;;
    esac
    local existing_port
    existing_port="$(env_get EO_WEB_PORT '')"

    if [ -n "${EO_WEB_PORT:-}" ]; then
        port="$EO_WEB_PORT"
        log "使用环境变量指定的端口：$port"
    elif [ "$tty" = "1" ]; then
        local default_used="${existing_port:-$default_port}"
        port="$(ask "Web 控制台监听端口（回车用 ${default_used}）: ")"
        port="${port:-$default_used}"
    else
        port="${existing_port:-$default_port}"
    fi
    port="$(echo "$port" | tr -d '[:space:]')"
    case "$port" in
        ''|*[!0-9]*) die "端口必须是数字，收到：[$port]" ;;
    esac
    if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        die "端口超出范围：$port"
    fi
    if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${port} "; then
        warn "端口 ${port} 已被占用，继续安装但可能启动失败（用 ss -ltnp 查看占用者）"
    fi

    # ---- 访问令牌（太短的当作无效值重新生成）----
    token="$(env_get EO_WEB_TOKEN '')"
    if [ -z "$token" ] || [ "${#token}" -lt 12 ]; then
        token="$(py -c 'import secrets; print(secrets.token_urlsafe(18))')"
        log "已生成网页访问令牌"
    fi

    # ---- 落盘 ----
    env_set EO_NETWORK "$carrier"
    env_set EO_WEB_PORT "$port"
    env_set EO_WEB_TOKEN "$token"
    env_set EO_PLATFORMS "$(carrier_platforms "$carrier")"

    log "配置完成：运营商=$(carrier_label "$carrier")  端口=$port"
}

install_deps() {
    # 先确认 venv 真的可用（Debian/Ubuntu 经常缺 python3-venv）
    if ! venv_module_ok; then
        PYVER_SHORT="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        install_venv_package || die "无法自动安装 venv 支持包，请手动执行后重试：
    Debian/Ubuntu : sudo apt install -y python3-venv
    CentOS/RHEL   : sudo yum install -y python3-pip
    Alpine        : sudo apk add py3-pip
    （Debian 12+/Ubuntu 23+ 若说找不到 python3-venv，请装同版本包：
      sudo apt install -y python${PYVER_SHORT}-venv ）"
        venv_module_ok || die "venv 支持包已安装但仍不可用，请检查 $PYTHON_BIN 是否完整"
    fi

    # 上一次失败可能留下不完整的 .venv（例如 ensurepip 缺失时创建到一半），先清掉
    if [ -d "$VENV_DIR" ] && ! pip_in_venv; then
        warn "$VENV_DIR 存在但不可用（可能是上次失败残留），清理后重建"
        rm -rf "$VENV_DIR"
    fi

    if [ ! -d "$VENV_DIR" ]; then
        log "创建虚拟环境：$VENV_DIR"
        if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
            rm -rf "$VENV_DIR"
            die "创建虚拟环境失败。请先安装 venv 支持包后重试：
    Debian/Ubuntu : sudo apt install -y python3-venv
    CentOS/RHEL   : sudo yum install -y python3-pip
    Alpine        : sudo apk add py3-pip"
        fi
    else
        log "复用已有虚拟环境：$VENV_DIR"
    fi

    pip_in_venv || die "虚拟环境里没有可用的 pip，请删掉 $VENV_DIR 后重试"

    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    # 只有下发华为云 DNS 才必须装这两个包，装了也不影响纯扫描
    log "安装依赖：huaweicloudsdkcore huaweicloudsdkdns"
    if ! "$VENV_DIR/bin/pip" install --quiet huaweicloudsdkcore huaweicloudsdkdns; then
        warn "依赖安装失败（网络问题？）。优选功能仍可用，但无法下发华为云 DNS。"
        warn "稍后可手动重试：$VENV_DIR/bin/pip install huaweicloudsdkcore huaweicloudsdkdns"
    fi
}

write_service() {
    [ -w /etc/systemd/system ] || die "需要 root 权限写 systemd 服务，请用 sudo 执行"
    local port interval rate platforms carrier
    port="$(env_get EO_WEB_PORT 8787)"
    interval="$(env_get EO_INTERVAL 300)"
    rate="$(env_get EO_RATE 100)"
    platforms="$(env_get EO_PLATFORMS '')"
    carrier="$(env_get EO_NETWORK lt)"

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Edge Optimizer（多平台边缘节点优选 + Web 控制台，$(carrier_label "$carrier")）
Documentation=file://${SCRIPT_DIR}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SUDO_USER:-$(id -un)}
WorkingDirectory=${SCRIPT_DIR}
EnvironmentFile=-${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=EO_MODE=web
Environment=EO_WEB_PORT=${port}
Environment=EO_NETWORK=${carrier}
Environment=EO_INTERVAL=${interval}
Environment=EO_RATE=${rate}
${platforms:+Environment=EO_PLATFORMS=${platforms}}
ExecStart=${VENV_DIR}/bin/python ${SCRIPT_DIR}/run.py web --port ${port} --host-bind 0.0.0.0
Restart=always
RestartSec=5

# 优选会开很多并发连接，别把文件描述符打满
LimitNOFILE=16384

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    log "已写入 systemd 服务：$SERVICE_FILE"
}

show_url() {
    local port token ip carrier
    port="$(env_get EO_WEB_PORT 8787)"
    token="$(env_get EO_WEB_TOKEN '')"
    carrier="$(env_get EO_NETWORK lt)"
    ip="$(detect_ip)"
    echo
    echo "  ----------------------------------------------------------"
    echo "   本机运营商 : $(carrier_label "$carrier") (${carrier})"
    echo "   Web 端口   : ${port}"
    echo "  ----------------------------------------------------------"
    echo "   本机访问   : http://127.0.0.1:${port}/?token=${token}"
    echo "   对外访问   : http://${ip}:${port}/?token=${token}"
    echo "   访问令牌   : ${token}"
    echo "  ----------------------------------------------------------"
    echo "   配置数据   : ${SCRIPT_DIR}/config/config.json"
    echo "   结果目录   : ${SCRIPT_DIR}/results/"
    echo "  ----------------------------------------------------------"
    echo
    if [ -n "${SSH_CONNECTION:-}" ]; then
        echo "  提示：可用 SSH 隧道访问，无需对外暴露端口："
        echo "    ssh -L ${port}:127.0.0.1:${port} $(whoami)@${ip}"
        echo "  然后本地浏览器打开 http://127.0.0.1:${port}/?token=${token}"
        echo
    fi
}

case "${1:-}" in
    install)
        [ "${2:-}" = "--yes" ] && ASSUME_YES=1
        need_python; need_curl
        ensure_env
        install_deps
        ask_setup
        mkdir -p results/history config
        write_service
        log "安装完成。下一步："
        echo "    1) 编辑 .env 填 HUAWEI_AK / HUAWEI_SK（下发 DNS 必需）"
        echo "    2) ./deploy.sh doctor      # 先确认链路可信"
        echo "    3) sudo ./deploy.sh start  # 启动 Web 控制台"
        echo "    4) ./deploy.sh status      # 拿访问地址与令牌"
        echo
        echo "    端口 $(env_get EO_WEB_PORT 8787) 与运营商已写入 .env；"
        echo "    要改端口/运营商，执行 ./deploy.sh config"
        ;;
    config)
        need_python
        ensure_env
        ask_setup
        if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
            log "服务正在运行，重启以应用新配置…"
            write_service
            systemctl restart "$APP_NAME"
            sleep 1
        fi
        show_url
        ;;
    isp)
        need_python
        py -m eo.deploytool isp || true
        ;;
    start)
        systemctl start "$APP_NAME"
        sleep 1
        systemctl --no-pager --lines=0 status "$APP_NAME" || true
        show_url
        ;;
    stop)
        systemctl stop "$APP_NAME"
        log "已停止"
        ;;
    restart)
        systemctl restart "$APP_NAME"
        sleep 1
        show_url
        ;;
    status)
        systemctl --no-pager --lines=0 status "$APP_NAME" || true
        show_url
        if command -v curl >/dev/null 2>&1; then
            _port="$(env_get EO_WEB_PORT 8787)"
            if curl -sf --max-time 3 "http://127.0.0.1:${_port}/healthz" >/dev/null; then
                echo "  健康检查 : OK"
            else
                echo "  健康检查 : 失败（服务可能还没起来，看 ./deploy.sh logs）"
            fi
        fi
        ;;
    logs)
        journalctl -u "$APP_NAME" -f -n 100
        ;;
    doctor)
        need_python
        py run.py doctor
        ;;
    scan)
        need_python
        net="$(env_get EO_NETWORK lt)"
        log "按本机运营商（$(carrier_label "$net")）跑一轮优选"
        py run.py scan --network "$net" -q
        ;;
    run)
        need_python
        port="$(env_get EO_WEB_PORT 8787)"
        token="$(env_get EO_WEB_TOKEN '')"
        [ -n "$token" ] && export EO_WEB_TOKEN="$token"
        exec "$( [ -x "$VENV_DIR/bin/python" ] && echo "$VENV_DIR/bin/python" || echo "$PYTHON_BIN" )" \
            run.py web --port "$port" --host-bind 0.0.0.0
        ;;
    uninstall)
        systemctl stop "$APP_NAME" 2>/dev/null || true
        systemctl disable "$APP_NAME" 2>/dev/null || true
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload
        log "已卸载 systemd 服务（results/ config/ .env 都保留了）"
        ;;
    *)
        sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
