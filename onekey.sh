#!/usr/bin/env bash
# 边缘节点优选 —— 一键部署脚本（Docker）
#
# 一条命令完成：拉代码 -> 写配置 -> 构建/拉取镜像 -> 启动 -> 打印访问地址
#
# 用法（把 <...> 换成真实值）：
#   HUAWEI_AK=xxx HUAWEI_SK=yyy EO_NETWORK=lt bash -c \
#     "$(curl -fsSL https://gitee.com/peroe/eo/raw/main/onekey.sh)"
#
# 全部可用环境变量（除密钥外都有默认值）：
#   HUAWEI_AK        华为云 AccessKey ID（下发 DNS 必需）
#   HUAWEI_SK        华为云 AccessKey Secret
#   （重跑时若不传密钥/端口/令牌，会沿用 .env 里已保存的值，不会被清空）
#   EO_NETWORK       本机运营商：lt=联通 / dx=电信 / yd=移动（默认自动识别）
#   EO_WEB_PORT      Web 端口（默认按运营商：lt=8787 dx=8788 yd=8789）
#   EO_RATE          全平台合计连接上限，连接/秒（默认 100）
#   EO_INTERVAL      轮询间隔秒（默认 300）
#   EO_WINDOW        时间窗口，如 00:00-07:00（默认全天）
#   EO_DNS_SYNC      none / dry-run / push（默认 dry-run：只预览不下发）
#   EO_WEB_TOKEN     网页访问令牌（默认自动生成）
#   EO_WEBHOOK       异常通知 webhook（可选）
#   DIR              安装目录（默认 /opt/edge-optimizer）
#   REPO             代码源（默认 Gitee；可设为 GitHub 地址）
#   USE_GHCR         1=从 GHCR 拉现成镜像（默认 0=本机构建）
#   IMAGE            镜像名（USE_GHCR=1 时生效）
#   GHCR_TOKEN       GitHub 私人令牌（勾选 read:packages），私有包时自动登录
#   GHCR_USER        GitHub 用户名（默认 JuLuogo）
#   SET_REGISTRY_MIRROR  1=写入 Docker 国内镜像加速配置
set -euo pipefail

DIR="${DIR:-/opt/edge-optimizer}"
REPO="${REPO:-https://gitee.com/peroe/eo.git}"
REPO_FALLBACK="${REPO_FALLBACK:-https://github.com/JuLuogo/eo.git}"
BRANCH="${BRANCH:-main}"
USE_GHCR="${USE_GHCR:-0}"
IMAGE="${IMAGE:-ghcr.io/julugo/eo:latest}"

say()  { printf '\033[1;34m[onekey]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# 带超时执行（没有 timeout 命令时直接执行）
run_with_timeout() {
    local seconds="$1"; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$@"
    else
        "$@"
    fi
}

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
# 1.5 可选：写入 Docker 国内镜像加速
# --------------------------------------------------------------------------
configure_registry_mirror() {
    local conf=/etc/docker/daemon.json
    if [ -f "$conf" ] && grep -q "registry-mirrors" "$conf" 2>/dev/null; then
        say "已存在 registry-mirrors 配置，跳过"
        return 0
    fi
    say "写入 Docker 镜像加速配置：$conf"
    mkdir -p /etc/docker
    cat > "$conf" <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://docker.1ms.run",
    "https://hub.rat.dev"
  ]
}
EOF
    if command -v systemctl >/dev/null 2>&1; then
        systemctl restart docker 2>/dev/null && sleep 3 || \
            warn "重启 Docker 失败，请手动执行：systemctl restart docker"
    fi
    say "镜像加速已配置"
}

if [ "${SET_REGISTRY_MIRROR:-0}" = "1" ]; then
    configure_registry_mirror
fi

# --------------------------------------------------------------------------
# 2. 获取代码
#   已有仓库：暂存本地改动后更新；更新失败【直接终止】，绝不用旧代码继续构建
#   全新安装：主源 -> 备用源 -> 压缩包
# --------------------------------------------------------------------------
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never

update_repo() {
    local repo="$1"
    say "更新已有代码：$DIR（$repo）"

    # 本地有改动时先备份并暂存，否则 pull 会被拒绝
    if ! git -C "$DIR" diff --quiet 2>/dev/null || \
       ! git -C "$DIR" diff --cached --quiet 2>/dev/null; then
        local stamp backup
        stamp="$(date '+%Y%m%d-%H%M%S')"
        backup="$DIR/../edge-optimizer-local-$stamp.patch"
        git -C "$DIR" diff HEAD > "$backup" 2>/dev/null || true
        warn "检测到本地未提交的修改，已备份到 $backup"
        if git -C "$DIR" stash push --include-untracked -m "onekey-$stamp" >/dev/null 2>&1; then
            say "已暂存本地修改（git stash），需要时用 git stash pop 找回"
        else
            warn "git stash 失败，改为重置到远端版本"
            git -C "$DIR" checkout --quiet -- . 2>/dev/null || true
        fi
    fi

    git -C "$DIR" remote set-url origin "$repo" 2>/dev/null || true
    git -C "$DIR" fetch --quiet origin "$BRANCH" 2>/dev/null || return 1
    git -C "$DIR" checkout --quiet "$BRANCH" 2>/dev/null || true
    if ! git -C "$DIR" merge --ff-only "origin/$BRANCH" --quiet 2>/dev/null; then
        warn "无法快进合并，重置到远端 $BRANCH"
        git -C "$DIR" reset --hard --quiet "origin/$BRANCH" 2>/dev/null || return 1
    fi
    say "代码已更新至 $(git -C "$DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
    return 0
}

clone_repo() {
    local repo="$1"
    say "克隆代码：$repo -> $DIR"
    run_with_timeout 180 git clone --quiet --branch "$BRANCH" "$repo" "$DIR" 2>/dev/null
}

fetch_archive() {
    local u
    for u in \
        "https://gitee.com/peroe/eo/repository/archive/${BRANCH}.tar.gz" \
        "https://github.com/JuLuogo/eo/archive/refs/heads/${BRANCH}.tar.gz"
    do
        say "尝试压缩包：$u"
        if curl -fL --connect-timeout 15 --max-time 300 "$u" -o /tmp/eo-src.tar.gz 2>/dev/null \
           && tar xzf /tmp/eo-src.tar.gz -C "$DIR" --strip-components=1 2>/dev/null; then
            rm -f /tmp/eo-src.tar.gz
            say "已通过压缩包获取代码"
            return 0
        fi
    done
    rm -f /tmp/eo-src.tar.gz
    return 1
}

if [ -f "./run.py" ] && [ -f "./docker-compose.yml" ]; then
    say "当前目录已是项目根目录"
    DIR="$(pwd)"
elif [ -d "$DIR/.git" ]; then
    if ! update_repo "$REPO"; then
        warn "从 $REPO 更新失败，尝试备用源…"
        update_repo "$REPO_FALLBACK" || die "两个源都更新失败，已中止。
必须拿到最新代码才能构建出正确的镜像，请先手动确认：
    cd $DIR && git status && git pull
或彻底重来：
    rm -rf $DIR 后重新执行本命令"
    fi
else
    command -v git >/dev/null 2>&1 || die "需要 git 拉取代码，请先安装 git"
    mkdir -p "$(dirname "$DIR")"
    if ! clone_repo "$REPO"; then
        warn "从 $REPO 克隆失败，尝试备用源…"
        rm -rf "$DIR"
        if ! clone_repo "$REPO_FALLBACK"; then
            rm -rf "$DIR"
            say "git 克隆都失败，改用压缩包…"
            mkdir -p "$DIR"
            fetch_archive || die "代码获取失败。请手动下载后解压到 $DIR 再执行本脚本：
    Gitee  : https://gitee.com/peroe/eo
    GitHub : https://github.com/JuLuogo/eo"
        fi
    fi
fi

cd "$DIR"
say "使用代码版本：$(git rev-parse --short HEAD 2>/dev/null || echo '未知（压缩包方式）')"

# --------------------------------------------------------------------------
# 3. 识别运营商
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
#    重跑时保留已有值：这里用 cat > .env 整份重写，若不先读回旧值，
#    「不传 HUAWEI_AK 再跑一次（例如只是升级镜像）」就会把上次保存的
#    密钥与访问令牌一起清掉。
# --------------------------------------------------------------------------
env_peek() {
    # 读取现有 .env 里的某个键（去引号、去行尾注释）
    local key="$1" line
    [ -f .env ] || return 0
    line="$(grep -E "^${key}=" .env 2>/dev/null | tail -1)" || return 0
    [ -n "$line" ] || return 0
    line="${line#*=}"
    case "$line" in
        *" #"*) line="${line%% \#*}" ;;
    esac
    printf '%s' "$line" | tr -d '"' | tr -d "'" | sed 's/[[:space:]]*$//'
}

case "$CARRIER" in dx) DEF_PORT=8788 ;; yd) DEF_PORT=8789 ;; *) DEF_PORT=8787 ;; esac
OLD_PORT="$(env_peek EO_WEB_PORT)"
PORT="${EO_WEB_PORT:-${OLD_PORT:-$DEF_PORT}}"
case "$PORT" in ''|*[!0-9]*) die "EO_WEB_PORT 必须是数字，收到：$PORT" ;; esac

TOKEN="${EO_WEB_TOKEN:-$(env_peek EO_WEB_TOKEN)}"
if [ -z "$TOKEN" ]; then
    TOKEN="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
fi

# 密钥优先用本次传入的，其次沿用 .env 里已有的
AK="${HUAWEI_AK:-$(env_peek HUAWEI_AK)}"
SK="${HUAWEI_SK:-$(env_peek HUAWEI_SK)}"
REGION="${HUAWEI_REGION:-$(env_peek HUAWEI_REGION)}"
REGION="${REGION:-cn-north-4}"
OLD_DNS="$(env_peek EO_DNS_SYNC)"

cat > .env <<EOF
# 由 onekey.sh 生成：$(date '+%Y-%m-%d %H:%M:%S')
EO_NETWORK=$CARRIER
EO_WEB_PORT=$PORT
EO_WEB_TOKEN=$TOKEN
EO_PLATFORMS=eo-$CARRIER,esa-$CARRIER,cft-$CARRIER
EO_RATE=${EO_RATE:-100}
EO_INTERVAL=${EO_INTERVAL:-300}
EO_WINDOW=${EO_WINDOW:-}
EO_DNS_SYNC=${EO_DNS_SYNC:-${OLD_DNS:-dry-run}}
EO_CONFIRM_DNS=${EO_CONFIRM_DNS:-no}
HUAWEI_AK=$AK
HUAWEI_SK=$SK
HUAWEI_REGION=$REGION
EOF
[ -n "${EO_WEBHOOK:-}" ] && echo "EO_WEBHOOK=$EO_WEBHOOK" >> .env
chmod 600 .env
mkdir -p config results/history

say "配置完成：运营商=$(carrier_label "$CARRIER")  端口=$PORT  DNS模式=$(env_peek EO_DNS_SYNC)"
if [ -z "$AK" ]; then
    warn "未提供 HUAWEI_AK / HUAWEI_SK —— 优选可用，但无法自动下发 DNS"
fi

# --------------------------------------------------------------------------
# 5. 启动
# --------------------------------------------------------------------------
if [ "$USE_GHCR" = "1" ]; then
    say "从 GHCR 拉取现成镜像：$IMAGE"
    if [ -n "${GHCR_TOKEN:-}" ]; then
        if printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-JuLuogo}" --password-stdin >/dev/null 2>&1; then
            say "已登录 ghcr.io（用户 ${GHCR_USER:-JuLuogo}）"
        else
            warn "GHCR 登录失败，继续尝试匿名拉取"
        fi
    fi
    if ! run_with_timeout 600 docker pull "$IMAGE"; then
        warn "拉取失败。GHCR 在境内连通性差，普通 registry-mirrors 也代理不了它。建议："
        warn "  A) 改用本机构建（去掉 USE_GHCR=1 即可，apt/pip 源已默认国内镜像）"
        warn "  B) 把镜像推到国内仓库后指定 IMAGE=<国内仓库地址>"
        warn "  C) 包为私有时：加 GHCR_TOKEN=<GitHub令牌，勾选 read:packages>"
        exit 1
    fi
    docker tag "$IMAGE" edge-optimizer:latest
    EO_IMAGE="edge-optimizer:latest" $COMPOSE up -d --no-build
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
printf '   代码版本 : %s\n' "$(git rev-parse --short HEAD 2>/dev/null || echo '压缩包方式')"
if [ -n "$STATUS" ]; then printf '   容器状态 : %s\n' "$STATUS"; fi
printf '  ==========================================================\n'
printf '\n'
printf '  查看日志   : cd %s && %s logs -f\n' "$DIR" "$COMPOSE"
printf '  重启       : cd %s && %s restart\n' "$DIR" "$COMPOSE"
printf '  停止       : cd %s && %s down\n' "$DIR" "$COMPOSE"
printf '  环境体检   : cd %s && %s run --rm optimizer doctor\n' "$DIR" "$COMPOSE"
printf '  离线自检   : cd %s && %s run --rm optimizer selftest\n' "$DIR" "$COMPOSE"
printf '  跑一轮优选 : cd %s && %s run --rm optimizer scan --network %s -q\n' "$DIR" "$COMPOSE" "$CARRIER"
printf '\n'
printf '  确认结果没问题后，在网页上把 DNS 模式从 dry-run 改成「执行」才会真正改解析\n'
printf '\n'
