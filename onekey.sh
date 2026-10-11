#!/usr/bin/env bash
# 边缘节点优选 —— 一键部署脚本（Docker）
#
# 一条命令完成：拉代码 -> 写配置 -> 构建/拉取镜像 -> 启动 -> 打印访问地址
#
# 用法（把 <...> 换成真实值）：
#   HUAWEI_AK=xxx HUAWEI_SK=yyy EO_NETWORK=lt EO_WEB_PORT=8787 bash -c \
#     "$(curl -fsSL https://raw.githubusercontent.com/JuLuogo/eo/main/onekey.sh)"
#
# 全部可用环境变量（都可选，除 AK/SK 外都有默认值）：
#   HUAWEI_AK        华为云 AccessKey ID（下发 DNS 必需）
#   HUAWEI_SK        华为云 AccessKey Secret
#   EO_NETWORK       本机运营商：lt=联通 / dx=电信 / yd=移动（默认自动识别）
#   EO_WEB_PORT      Web 端口（默认按运营商：lt=8787 dx=8788 yd=8789）
#   EO_RATE          全平台合计连接上限，连接/秒（默认 100）
#   EO_INTERVAL      轮询间隔秒（默认 300）
#   EO_WINDOW        时间窗口，如 00:00-07:00（默认全天）
#   EO_DNS_SYNC      none / dry-run / push（默认 dry-run：只预览不下发）
#   EO_WEB_TOKEN     网页访问令牌（默认自动生成）
#   EO_WEBHOOK       异常通知 webhook（可选）
#   DIR              安装目录（默认 /opt/edge-optimizer）
#   USE_GHCR         1=从 GHCR 拉取现成镜像（默认 0=本机构建）
#   IMAGE            镜像名（USE_GHCR=1 时生效，默认 ghcr.io/julugo/eo:latest）
set -euo pipefail

DIR="${DIR:-/opt/edge-optimizer}"
REPO="${REPO:-https://github.com/JuLuogo/eo.git}"
BRANCH="${BRANCH:-main}"
USE_GHCR="${USE_GHCR:-0}"
IMAGE="${IMAGE:-ghcr.io/julugo/eo:latest}"

say()  { printf '\033[1;34m[onekey]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# 1. Docker 检查
# --------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || {
    say "未检测到 Docker，尝试自动安装…"
    curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 || \
        die "Docker 自动安装失败，请手动安装：curl -fsSL https://get.docker.com | sh"
}
docker info >/dev/null 2>&1 || die "无法访问 Docker 守护进程。请用 root/sudo 运行，或：
    sudo systemctl enable --now docker
    sudo usermod -aG docker \$USER && newgrp docker"

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "缺少 docker compose 插件，请安装：sudo apt install -y docker-compose-v2"
fi
say "Docker: $(docker --version)"

# --------------------------------------------------------------------------
# 2. 拉取/更新代码
# --------------------------------------------------------------------------
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never
if [ -f "./run.py" ] && [ -f "./docker-compose.yml" ]; then
    say "当前目录已是项目根目录"
    DIR="$(pwd)"
else
    if [ -d "$DIR/.git" ]; then
        say "更新已有代码：$DIR"
        git -C "$DIR" fetch --quiet origin "$BRANCH" 2>/dev/null || true
        git -C "$DIR" checkout --quiet "$BRANCH" 2>/dev/null || true
        git -C "$DIR" pull --ff-only --quiet origin "$BRANCH" 2>/dev/null || \
            warn "更新失败，沿用当前版本"
    else
        command -v git >/dev/null 2>&1 || die "需要 git 拉取代码，请先安装 git"
        say "拉取代码到 $DIR"
        mkdir -p "$(dirname "$DIR")"
        git clone --quiet --branch "$BRANCH" "$REPO" "$DIR" || die "拉取失败：$REPO"
    fi
fi
cd "$DIR"

# --------------------------------------------------------------------------
# 3. 识别运营商（未指定时）
# --------------------------------------------------------------------------
detect_carrier() {
    if [ -f "./eo/deploytool.py" ] && command -v python3 >/dev/null 2>&1; then
        python3 -m eo.deploytool isp --quiet 2>/dev/null || echo "unknown"
    else
        echo "unknown"
    fi
}
carrier_label() {
    case "$1" in lt) echo "联通" ;; dx) echo "电信" ;; yd) echo "移动" ;; *) echo "未知" ;; esac
}

CARRIER="${EO_NETWORK:-}"
if [ -z "$CARRIER" ]; then
    say "未指定 EO_NETWORK，尝试自动识别本机运营商…"
    CARRIER="$(detect_carrier)"
    case "$CARRIER" in
        lt|dx|yd) say "识别结果：$(carrier_label "$CARRIER")" ;;
        *) warn "无法识别（境外/中转 IP），默认按联通 (lt)"; CARRIER="lt" ;;
    esac
fi
case "$CARRIER" in lt|dx|yd) : ;; *) die "EO_NETWORK 只能是 lt / dx / yd，收到：$CARRIER" ;; esac

# --------------------------------------------------------------------------
# 4. 写 .env
# --------------------------------------------------------------------------
case "$CARRIER" in dx) DEF_PORT=8788 ;; yd) DEF_PORT=8789 ;; *) DEF_PORT=8787 ;; esac
PORT="${EO_WEB_PORT:-$DEF_PORT}"
case "$PORT" in ''|*[!0-9]*) die "EO_WEB_PORT 必须是数字，收到：$PORT" ;; esac

TOKEN="${EO_WEB_TOKEN:-}"
if [ -z "$TOKEN" ]; then
    TOKEN="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
fi

cat > .env <<EOF
# 由 onekey.sh 生成：$(date '+%Y-%m-%d %H:%M:%S')
EO_NETWORK=$CARRIER
EO_WEB_PORT=$PORT
EO_WEB_TOKEN=$TOKEN
EO_PLATFORMS=eo-$CARRIER,esa-$CARRIER,cft-$CARRIER
EO_RATE=${EO_RATE:-100}
EO_INTERVAL=${EO_INTERVAL:-300}
EO_WINDOW=${EO_WINDOW:-}
EO_DNS_SYNC=${EO_DNS_SYNC:-dry-run}
EO_CONFIRM_DNS=${EO_CONFIRM_DNS:-no}
HUAWEI_AK=${HUAWEI_AK:-}
HUAWEI_SK=${HUAWEI_SK:-}
HUAWEI_REGION=${HUAWEI_REGION:-cn-north-4}
EOF
[ -n "${EO_WEBHOOK:-}" ] && echo "EO_WEBHOOK=$EO_WEBHOOK" >> .env
chmod 600 .env
mkdir -p config results/history

say "配置完成：运营商=$(carrier_label "$CARRIER")  端口=$PORT  DNS模式=${EO_DNS_SYNC:-dry-run}"
if [ -z "${HUAWEI_AK:-}" ]; then
    warn "未提供 HUAWEI_AK / HUAWEI_SK —— 优选可用，但无法自动下发 DNS"
fi

# --------------------------------------------------------------------------
# 5. 启动
# --------------------------------------------------------------------------
if [ "$USE_GHCR" = "1" ]; then
    say "从 GHCR 拉取现成镜像：$IMAGE"
    docker pull "$IMAGE" || die "拉取失败。若镜像是私有的，先执行：echo <你的GitHub令牌> | docker login ghcr.io -u <用户名> --password-stdin"
    docker tag "$IMAGE" edge-optimizer:latest
    $COMPOSE up -d --no-build
else
    say "本机构建并启动（首次约 2~5 分钟）…"
    $COMPOSE up -d --build
fi

# --------------------------------------------------------------------------
# 6. 收尾
# --------------------------------------------------------------------------
sleep 3
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
STATUS="$($COMPOSE ps --format '{{.Status}}' 2>/dev/null | head -1 || true)"

printf '\n'
printf '  ==========================================================\n'
printf '   运营商   : %s (%s)\n' "$(carrier_label "$CARRIER")" "$CARRIER"
printf '   端口     : %s\n' "$PORT"
printf '   访问地址 : http://%s:%s/?token=%s\n' "${IP:-<服务器IP>}" "$PORT" "$TOKEN"
if [ -n "$STATUS" ]; then printf '   容器状态 : %s\n' "$STATUS"; fi
printf '  ==========================================================\n'
printf '\n'
printf '  查看日志   : cd %s && %s logs -f\n' "$DIR" "$COMPOSE"
printf '  重启       : cd %s && %s restart\n' "$DIR" "$COMPOSE"
printf '  停止       : cd %s && %s down\n' "$DIR" "$COMPOSE"
printf '  环境体检   : cd %s && %s run --rm optimizer doctor\n' "$DIR" "$COMPOSE"
printf '  跑一轮优选 : cd %s && %s run --rm optimizer scan --network %s -q\n' "$DIR" "$COMPOSE" "$CARRIER"
printf '\n'
printf '  ⚠ 下一步：确认结果没问题后，在网页上把 DNS 模式从 dry-run 改成「执行」才会真正改解析\n'
printf '\n'
