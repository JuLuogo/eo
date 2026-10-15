#!/usr/bin/env bash
# 边缘节点优选 —— Docker 部署配置脚本
#
#   ./docker-setup.sh           交互式：识别运营商、问端口、写好 .env
#   ./docker-setup.sh --up      配置完直接构建并启动
#
# 做的事：检查 Docker -> 识别本机运营商 -> 问端口 -> 生成/更新 .env
#         -> 提示构建命令（或直接用 --up 启动）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
ENV_FILE="$SCRIPT_DIR/.env"
PYTHON_BIN="${PYTHON_BIN:-python3}"

say()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

UP=0
[ "${1:-}" = "--up" ] && UP=1

# --------------------------------------------------------------------------
# 1. 检查 Docker
# --------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "没找到 docker，请先安装：
    curl -fsSL https://get.docker.com | sh
    # 或者用发行版包：
    sudo apt install -y docker.io docker-compose-v2"

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "没找到 docker compose 插件，请安装：sudo apt install -y docker-compose-v2"
fi
say "Docker: $(docker --version)"
say "Compose: $($COMPOSE version --short 2>/dev/null || echo 已就绪)"

if ! docker info >/dev/null 2>&1; then
    die "当前用户无权访问 Docker。请用 sudo 运行本脚本，或把用户加入 docker 组：
    sudo usermod -aG docker \$USER && newgrp docker"
fi

# --------------------------------------------------------------------------
# 2. 运营商识别（复用项目里的工具）
# --------------------------------------------------------------------------
detect_carrier() {
    if [ -f "./eo/deploytool.py" ] && command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        "$PYTHON_BIN" -m eo.deploytool isp --quiet 2>/dev/null || echo "unknown"
    else
        echo "unknown"
    fi
}

carrier_label() {
    case "$1" in
        lt) echo "联通" ;; dx) echo "电信" ;; yd) echo "移动" ;; *) echo "未知" ;;
    esac
}

ask() {
    local prompt="$1" answer=""
    if [ -r /dev/tty ]; then
        read -r -p "$prompt" answer < /dev/tty || true
    elif [ -t 0 ]; then
        read -r -p "$prompt" answer || true
    fi
    printf '%s' "$answer"
}

env_get() {
    local key="$1" default="${2:-}"
    if [ -f "$ENV_FILE" ]; then
        local v
        v="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- \
             | sed 's/[[:space:]]*#.*$//' | tr -d '"' | tr -d "'" || true)"
        [ -n "$v" ] && { echo "$v"; return; }
    fi
    echo "$default"
}

env_set() {
    local key="$1" value="$2"
    [ -f "$ENV_FILE" ] || : > "$ENV_FILE"
    if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
        awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="}
             $1==k {print k"="v; next} {print}' "$ENV_FILE" > "${ENV_FILE}.tmp"
        mv "${ENV_FILE}.tmp" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

# --------------------------------------------------------------------------
# 3. 交互配置
# --------------------------------------------------------------------------
# 先记住 .env 是否本来就有：.env.example 里带着 EO_WEB_PORT=8787，
# 若把它当成「用户已设的端口」，电信/移动机器就永远拿不到 8788/8789 的默认值。
ENV_PREEXISTED=0
[ -f "$ENV_FILE" ] && ENV_PREEXISTED=1

if [ "$ENV_PREEXISTED" = "0" ] && [ -f ".env.example" ]; then
    cp .env.example "$ENV_FILE"
    say "已从 .env.example 生成 .env"
fi

if [ -n "${EO_NETWORK:-}" ]; then
    CARRIER="$EO_NETWORK"
    say "使用环境变量指定的运营商：$(carrier_label "$CARRIER")"
elif [ -r /dev/tty ]; then
    printf '\n'
    say "正在识别本机运营商（联通/电信/移动）…"
    DETECTED="$(detect_carrier)"
    if [ "$DETECTED" != "unknown" ]; then
        say "识别结果：$(carrier_label "$DETECTED")"
        if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
            "$PYTHON_BIN" -m eo.deploytool isp 2>/dev/null | sed 's/^/    /' || true
        fi
    else
        warn "无法自动识别（可能是境外/中转 IP，或识别服务不可达）"
    fi
    HINT="$DETECTED"
    [ "$HINT" = "unknown" ] && HINT="lt"
    printf '\n这台机器是哪个运营商？\n'
    printf '  1) 联通 (lt)%s\n' "$([ "$HINT" = lt ] && echo '  <= 识别结果' || true)"
    printf '  2) 电信 (dx)%s\n' "$([ "$HINT" = dx ] && echo '  <= 识别结果' || true)"
    printf '  3) 移动 (yd)%s\n' "$([ "$HINT" = yd ] && echo '  <= 识别结果' || true)"
    CHOICE="$(ask "请输入 1/2/3（回车用 ${HINT} $(carrier_label "$HINT")）: ")"
    case "$CHOICE" in
        1) CARRIER="lt" ;; 2) CARRIER="dx" ;; 3) CARRIER="yd" ;;
        "") CARRIER="$HINT" ;;
        lt|dx|yd) CARRIER="$CHOICE" ;;
        *) warn "输入无法识别，使用 $(carrier_label "$HINT")"; CARRIER="$HINT" ;;
    esac
else
    CARRIER="$(env_get EO_NETWORK lt)"
    warn "非交互模式，使用运营商：$(carrier_label "$CARRIER")"
fi

# 兜底：必须是 lt/dx/yd
case "$CARRIER" in
    lt|dx|yd) : ;;
    *) warn "运营商未确定，默认按联通 (lt)"; CARRIER="lt" ;;
esac

# 端口：按运营商给不同默认值（联通 8787 / 电信 8788 / 移动 8789，三台机器天然错开）
case "$CARRIER" in
    dx) DEFAULT_PORT=8788 ;; yd) DEFAULT_PORT=8789 ;; *) DEFAULT_PORT=8787 ;;
esac
# 只有 .env 本来就有（用户之前跑过或手动配过）才认为端口是「已设定」的
EXISTING_PORT=""
[ "$ENV_PREEXISTED" = "1" ] && EXISTING_PORT="$(env_get EO_WEB_PORT '')"

if [ -n "${EO_WEB_PORT:-}" ]; then
    PORT="$EO_WEB_PORT"
elif [ -r /dev/tty ]; then
    DEFAULT_USED="${EXISTING_PORT:-$DEFAULT_PORT}"
    PORT="$(ask "Web 控制台监听端口（回车用 ${DEFAULT_USED}）: ")"
    PORT="${PORT:-$DEFAULT_USED}"
else
    PORT="${EXISTING_PORT:-$DEFAULT_PORT}"
fi
PORT="$(echo "$PORT" | tr -d '[:space:]')"
case "$PORT" in
    ''|*[!0-9]*) die "端口必须是数字，收到：[$PORT]" ;;
esac

TOKEN="$(env_get EO_WEB_TOKEN '')"
if [ -z "$TOKEN" ] || [ "${#TOKEN}" -lt 12 ]; then
    TOKEN="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
    say "已生成网页访问令牌"
fi

env_set EO_NETWORK "$CARRIER"
env_set EO_WEB_PORT "$PORT"
env_set EO_WEB_TOKEN "$TOKEN"
env_set EO_PLATFORMS "eo-${CARRIER},esa-${CARRIER},cft-${CARRIER}"
chmod 600 "$ENV_FILE" 2>/dev/null || true

say "配置完成：运营商=$(carrier_label "$CARRIER")  端口=$PORT"

# --------------------------------------------------------------------------
# 4. 启动
# --------------------------------------------------------------------------
if [ "$UP" = "1" ]; then
    say "开始构建并启动（首次构建需要几分钟）…"
    $COMPOSE up -d --build
    sleep 3
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    printf '\n'
    say "已启动。访问地址："
    printf '    http://%s:%s/?token=%s\n\n' "${IP:-<服务器IP>}" "$PORT" "$TOKEN"
    say "看日志：$COMPOSE logs -f"
else
    printf '\n'
    say "下一步执行："
    printf '    %s up -d --build      # 构建并启动\n' "$COMPOSE"
    printf '    %s logs -f            # 看日志（里面有访问地址）\n' "$COMPOSE"
    printf '    ./docker-setup.sh --up   # 或直接跑这一条，一步到位\n\n'
    say "国内拉不动基础镜像时，可指定镜像源后重试："
    printf '    docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \\\n'
    printf '                 -t edge-optimizer:latest .\n\n'
fi
