#!/usr/bin/env bash
# 边缘节点优选 —— 裸机部署脚本（不需要 Docker）
#
#   ./deploy.sh install     交互式配置 + 安装（会问运营商和端口，依赖自动装）
#   ./deploy.sh config      重新交互式配置（改端口/运营商）并重启服务
#   ./deploy.sh isp         识别本机运营商（联通/电信/移动）
#   ./deploy.sh start       启动服务
#   ./deploy.sh stop        停止服务
#   ./deploy.sh restart     重启服务
#   ./deploy.sh status      状态 + 运营商 + 端口 + 访问地址 + 令牌 + 健康检查
#   ./deploy.sh logs        实时跟踪日志（Ctrl+C 退出）
#   ./deploy.sh logs 200    查看最近 200 行后退出
#   ./deploy.sh logs-err    只看错误级别日志
#   ./deploy.sh logs-save   导出全部日志到文件（便于排查）
#   ./deploy.sh doctor      环境体检（确认链路可信）
#   ./deploy.sh scan        按本机运营商手动跑一轮优选
#   ./deploy.sh uninstall   卸载：停服务、删 systemd 单元与虚拟环境（保留配置和结果）
#   ./deploy.sh uninstall --purge   完全卸载：连 .env / config / results 一起删
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
SKIP_DNS_SDK="${SKIP_DNS_SDK:-0}"

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

# 带超时执行命令（没有 timeout 命令时直接执行）
run_with_timeout() {
    local seconds="$1"; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$@"
    else
        "$@"
    fi
}

# 安装 Python 包：PyPI 在境内经常连不上，失败后自动换国内镜像重试。
# 返回 0 表示装上，1 表示彻底失败（调用方决定是否致命）。
PIP_MIRRORS="
https://pypi.tuna.tsinghua.edu.cn/simple
https://mirrors.aliyun.com/pypi/simple/
https://mirrors.cloud.tencent.com/pypi/simple/
https://mirrors.ustc.edu.cn/pypi/simple/
"
PIP_INDEX="${PIP_INDEX:-}"

install_python_packages() {
    local pkg_args="$*" first=1 index log_file rc
    log_file="$(mktemp)"

    # 首选：用户指定的 PIP_INDEX，其次默认 PyPI，最后依次尝试国内镜像
    for index in $PIP_INDEX "" $PIP_MIRRORS; do
        if [ -z "$index" ]; then
            [ "$first" = "1" ] || continue          # 空 index 只当作默认源试一次
            log "安装依赖（默认 PyPI 源）：$pkg_args"
            # shellcheck disable=SC2086
            if run_with_timeout 240 "$VENV_DIR/bin/pip" install --quiet --disable-pip-version-check \
                   --retries 2 --timeout 25 $pkg_args >"$log_file" 2>&1; then
                rm -f "$log_file"; return 0
            fi
            first=0
        else
            log "默认源失败，改用镜像重试：$index"
            # shellcheck disable=SC2086
            if run_with_timeout 240 "$VENV_DIR/bin/pip" install --quiet --disable-pip-version-check \
                   --retries 2 --timeout 25 -i "$index" $pkg_args >"$log_file" 2>&1; then
                log "已从镜像安装成功：$index"
                rm -f "$log_file"; return 0
            fi
        fi
    done

    warn "Python 包安装失败，最后一次错误摘要："
    tail -5 "$log_file" 2>/dev/null | sed 's/^/    /' || true
    rm -f "$log_file"
    return 1
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

    # ---- 可选：现在就配好华为云 DNS 密钥（回车即跳过）----
    if [ "$tty" = "1" ] && [ "${SKIP_DNS_SDK:-0}" != "1" ]; then
        local cur_ak cur_sk
        cur_ak="$(env_get HUAWEI_AK '')"
        cur_sk="$(env_get HUAWEI_SK '')"
        if [ -n "$cur_ak" ]; then
            log "已检测到 HUAWEI_AK（${cur_ak:0:4}****），跳过密钥配置"
        else
            printf '\n'
            printf '是否现在配置「自动下发 DNS」（华为云）？\n'
            printf '  需要华为云的 AccessKey，且该密钥要有 DNS 权限。\n'
            printf '  不配也能正常优选，只是结果需要你自己手动填到 DNS。\n'
            local want
            want="$(ask "现在配置吗？[y/N]（回车跳过）: ")"
            case "$want" in
                y|Y|yes|YES)
                    local ak sk
                    ak="$(ask "AccessKey ID（回车跳过）: ")"
                    if [ -n "$ak" ]; then
                        sk="$(ask "AccessKey Secret: ")"
                        if [ -n "$sk" ]; then
                            env_set HUAWEI_AK "$ak"
                            env_set HUAWEI_SK "$sk"
                            env_set EO_DNS_SYNC "dry-run"
                            log "已保存密钥，DNS 模式先设为 dry-run（只预览，不下发）"
                            log "确认无误后在 Web 控制台把模式改成「执行」并勾选确认即可真正下发"
                        else
                            warn "Secret 为空，已跳过密钥配置"
                        fi
                    else
                        log "已跳过密钥配置（之后可编辑 .env 或到 Web 控制台填写）"
                    fi
                    ;;
                *)
                    log "已跳过；之后可编辑 .env 或在 Web 控制台「DNS 下发」里填写"
                    ;;
            esac
        fi
    fi

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

    # pip 本体升级失败不致命（旧版也能装包）
    run_with_timeout 120 "$VENV_DIR/bin/pip" install --quiet --upgrade pip \
        >/dev/null 2>&1 || warn "升级 pip 失败，继续使用自带版本"

    # 华为云 DNS SDK 只有在需要下发 DNS 时才用得上。
    # 用 --no-dns 可以跳过（优选功能完全不受影响）。
    if [ "${SKIP_DNS_SDK:-0}" = "1" ]; then
        log "按参数要求跳过华为云 DNS SDK（纯优选模式）"
        return 0
    fi

    if install_python_packages huaweicloudsdkcore huaweicloudsdkdns; then
        log "华为云 DNS SDK 安装完成"
        return 0
    fi

    # 不致命：优选本身只用标准库 + curl
    warn "════════════════════════════════════════════════════════════"
    warn "华为云 DNS SDK 未能安装，但【优选功能不受影响】，安装继续。"
    warn "影响范围：只影响把结果自动下发到华为云 DNS 这一步。"
    warn "可以稍后手动补装（国内网络建议指定镜像）："
    warn "  $VENV_DIR/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \\"
    warn "      huaweicloudsdkcore huaweicloudsdkdns"
    warn "或者用环境变量长期指定镜像：export PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"
    warn "════════════════════════════════════════════════════════════"
    return 0
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
        # 支持参数：--yes（非交互）、--no-dns（跳过华为云 SDK，纯优选）
        for _arg in "$@"; do
            case "$_arg" in
                --yes) ASSUME_YES=1 ;;
                --no-dns) SKIP_DNS_SDK=1 ;;
            esac
        done
        need_python; need_curl
        ensure_env
        install_deps
        ask_setup
        mkdir -p results/history config
        write_service
        log "安装完成。下一步："
        echo "    1) 编辑 .env 填 HUAWEI_AK / HUAWEI_SK（下发 DNS 必需，纯优选可跳过）"
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
        # 默认跟随；./deploy.sh logs 200 看最近 200 行后退出
        if [ -n "${2:-}" ]; then
            journalctl -u "$APP_NAME" -n "$2" --no-pager
        else
            journalctl -u "$APP_NAME" -f -n 100
        fi
        ;;
    logs-err)
        journalctl -u "$APP_NAME" -p err --no-pager -n "${2:-100}"
        ;;
    logs-save)
        # 导出全部日志到文件，便于发出来排查
        _out="${2:-${SCRIPT_DIR}/edge-optimizer.log}"
        journalctl -u "$APP_NAME" --no-pager > "$_out" 2>/dev/null || {
            die "journalctl 不可用，日志请直接看：./deploy.sh logs"; }
        log "日志已导出：$_out（$(wc -l < "$_out") 行）"
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
        # 默认：只卸载服务与虚拟环境，保留 results/ config/ .env（便于以后重装）
        # --purge : 连配置、结果、日志一起删干净（完全卸载）
        PURGE=0
        for _arg in "$@"; do
            [ "$_arg" = "--purge" ] && PURGE=1
        done

        log "停止并禁用服务…"
        systemctl stop "$APP_NAME" 2>/dev/null || true
        systemctl disable "$APP_NAME" 2>/dev/null || true
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload 2>/dev/null || true
        log "已移除 systemd 服务：$SERVICE_FILE"

        # 虚拟环境（可重建，直接删）
        if [ -d "$VENV_DIR" ]; then
            rm -rf "$VENV_DIR"
            log "已删除虚拟环境：$VENV_DIR"
        fi
        rm -f "${SCRIPT_DIR}/.deploy-state"

        if [ "$PURGE" = "1" ]; then
            rm -rf "${SCRIPT_DIR}/results" "${SCRIPT_DIR}/config" "${SCRIPT_DIR}/.venv"
            rm -f "${SCRIPT_DIR}/.env" "${SCRIPT_DIR}/edge-optimizer.log"
            log "已删除配置与结果：.env / config/ / results/"
            warn "如需保留代码目录请自行 rm -rf ${SCRIPT_DIR}"
        else
            echo
            echo "  保留内容（想彻底删干净请加 --purge）："
            echo "    .env            —— 运营商、端口、令牌、密钥"
            echo "    config/         —— Web 控制台的配置"
            echo "    results/        —— 优选结果与历史"
            echo
            echo "  重新安装：sudo ./deploy.sh install"
            echo "  完全卸载：sudo ./deploy.sh uninstall --purge"
        fi
        ;;
    *)
        awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 && !/^#/ {exit}' "$0"
        exit 1
        ;;
esac
