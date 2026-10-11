#!/usr/bin/env bash
# 边缘节点优选 —— 裸机部署脚本（不需要 Docker）
#
#   ./deploy.sh install     安装依赖、建目录、装 systemd 服务（不自动启动）
#   ./deploy.sh start       启动服务
#   ./deploy.sh stop        停止服务
#   ./deploy.sh restart     重启服务
#   ./deploy.sh status      查看状态 + 访问地址 + 访问令牌
#   ./deploy.sh logs        跟踪日志
#   ./deploy.sh doctor      跑一次环境体检（确认链路可信）
#   ./deploy.sh scan        在服务器上手动跑一轮联通优选（抽样）
#   ./deploy.sh uninstall   卸载 systemd 服务（保留数据）
#   ./deploy.sh run         前台直接跑（调试用，Ctrl+C 退出）
#
# 三台机器（联通/电信/移动）各跑各的时：
#   每台机器上先编辑 .env 里的 EO_PLATFORMS / EO_WEB_PORT（建议 8787/8788/8789），
#   控制台里再把自己那一套运营商的 enabled 打开即可。
set -euo pipefail

APP_NAME="edge-optimizer"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
ENV_FILE="${SCRIPT_DIR}/.env"
VENV_DIR="${SCRIPT_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

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

# 读取 .env（KEY=VALUE，忽略注释），带默认值
env_get() {
    local key="$1" default="${2:-}"
    if [ -f "$ENV_FILE" ]; then
        local value
        value="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
        [ -n "$value" ] && { echo "$value"; return; }
    fi
    echo "$default"
}

detect_ip() {
    local ip
    ip="$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || true)"
    if [ -z "$ip" ]; then
        ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    echo "${ip:-127.0.0.1}"
}

ensure_env() {
    if [ ! -f "$ENV_FILE" ]; then
        if [ -f "${SCRIPT_DIR}/.env.example" ]; then
            cp "${SCRIPT_DIR}/.env.example" "$ENV_FILE"
            log "已从 .env.example 生成 .env，请按需修改（尤其是密钥与 EO_PLATFORMS）"
        else
            : > "$ENV_FILE"
        fi
    fi
    # 给一个固定的网页令牌，避免每次重启变化
    if ! grep -qE '^EO_WEB_TOKEN=' "$ENV_FILE" 2>/dev/null; then
        local token
        token="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(18))')"
        printf '\n# 由 deploy.sh 自动生成\nEO_WEB_TOKEN=%s\n' "$token" >> "$ENV_FILE"
        log "已生成网页访问令牌并写入 .env: $token"
    fi
    chmod 600 "$ENV_FILE"
}

install_deps() {
    log "创建虚拟环境：$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    # 只有需要下发华为云 DNS 时才必须装这两个包
    log "安装依赖：huaweicloudsdkcore huaweicloudsdkdns"
    "$VENV_DIR/bin/pip" install --quiet huaweicloudsdkcore huaweicloudsdkdns
}

write_service() {
    [ -w /etc/systemd/system ] || die "需要 root 权限写 systemd 服务，请用 sudo 执行"
    local port interval rate platforms
    port="$(env_get EO_WEB_PORT 8787)"
    interval="$(env_get EO_INTERVAL 300)"
    rate="$(env_get EO_RATE 100)"
    platforms="$(env_get EO_PLATFORMS '')"

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Edge Optimizer（多平台边缘节点优选 + Web 控制台）
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
Environment=EO_INTERVAL=${interval}
Environment=EO_RATE=${rate}
${platforms:+Environment=EO_PLATFORMS=${platforms}}
ExecStart=${VENV_DIR}/bin/python ${SCRIPT_DIR}/run.py web --port ${port} --host-bind 0.0.0.0
Restart=always
RestartSec=5

# 连接数保护：优选会开很多并发连接，别把文件描述符打满
LimitNOFILE=16384

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    log "已写入 systemd 服务：$SERVICE_FILE"
}

show_url() {
    local port token ip
    port="$(env_get EO_WEB_PORT 8787)"
    token="$(env_get EO_WEB_TOKEN '')"
    ip="$(detect_ip)"
    echo
    echo "  本机访问 : http://127.0.0.1:${port}/?token=${token}"
    echo "  对外访问 : http://${ip}:${port}/?token=${token}"
    echo "  访问令牌 : ${token}"
    echo "  配置文件 : ${SCRIPT_DIR}/config/config.json"
    echo
}

case "${1:-}" in
    install)
        need_python; need_curl
        ensure_env
        install_deps
        mkdir -p results/history config
        write_service
        log "安装完成。下一步："
        echo "    1) 编辑 .env 填 HUAWEI_AK / HUAWEI_SK，按需改 EO_PLATFORMS / EO_WEB_PORT"
        echo "    2) ./deploy.sh doctor      # 先确认链路可信"
        echo "    3) sudo ./deploy.sh start  # 启动 Web 控制台"
        echo "    4) ./deploy.sh status      # 拿访问地址与令牌"
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
            local port
            port="$(env_get EO_WEB_PORT 8787)"
            if curl -sf --max-time 3 "http://127.0.0.1:${port}/healthz" >/dev/null; then
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
        [ -x "$VENV_DIR/bin/python" ] && PY="$VENV_DIR/bin/python" || PY="$PYTHON_BIN"
        "$PY" run.py doctor
        ;;
    scan)
        need_python
        [ -x "$VENV_DIR/bin/python" ] && PY="$VENV_DIR/bin/python" || PY="$PYTHON_BIN"
        net="$(env_get EO_NETWORK lt)"
        "$PY" run.py scan --network "$net" -q
        ;;
    run)
        need_python
        [ -x "$VENV_DIR/bin/python" ] && PY="$VENV_DIR/bin/python" || PY="$PYTHON_BIN"
        port="$(env_get EO_WEB_PORT 8787)"
        token="$(env_get EO_WEB_TOKEN '')"
        [ -n "$token" ] && export EO_WEB_TOKEN="$token"
        exec "$PY" run.py web --port "$port" --host-bind 0.0.0.0
        ;;
    uninstall)
        systemctl stop "$APP_NAME" 2>/dev/null || true
        systemctl disable "$APP_NAME" 2>/dev/null || true
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload
        log "已卸载 systemd 服务（results/ config/ .env 都保留了）"
        ;;
    *)
        sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
