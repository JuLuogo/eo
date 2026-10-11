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
#   GHCR_TOKEN       GitHub 私人令牌（勾选 read:packages）。GHCR 包为私有时，
#                    带上它即可自动登录拉取，省去在服务器上手动 docker login
#   GHCR_USER        GitHub 用户名（默认 JuLuogo）
#   REPO             代码源，默认 Gitee（境内稳定）；可设为 GitHub 地址
#   SET_REGISTRY_MIRROR  1 = 写入 /etc/docker/daemon.json 配置国内镜像加速
#                    （拉基础镜像用；境内建议开）
set -euo pipefail

DIR="${DIR:-/opt/edge-optimizer}"
# 代码源：默认 Gitee（境内稳定）。想走 GitHub 可用
#   REPO=https://github.com/JuLuogo/eo.git bash -c "$(curl ...)"
REPO="${REPO:-https://gitee.com/peroe/eo.git}"
BRANCH="${BRANCH:-main}"
USE_GHCR="${USE_GHCR:-0}"
IMAGE="${IMAGE:-ghcr.io/julugo/eo:latest}"

# GitHub 文件代理：境内直连 raw.githubusercontent.com 常失败，这里按实测速度排序，
# 逐个尝试，全失败才回退直连。用法：<前缀><完整 raw 地址>
GH_FILE_MIRRORS="
https://gh-proxy.com/
https://gh.llkk.cc/
https://ghproxy.net/
https://ghfast.top/
"
GH_RAW_BASE="https://raw.githubusercontent.com/JuLuogo/eo/${BRANCH}"

# 带代理回退的下载：download <相对路径> <输出文件>
download_from_github() {
    local rel="$1" out="$2" prefix
    for prefix in $GH_FILE_MIRRORS; do
        if curl -fsSL --connect-timeout 10 --max-time 60 \
                "${prefix}${GH_RAW_BASE}/${rel}" -o "$out" 2>/dev/null && [ -s "$out" ]; then
            say "已通过镜像下载 $rel（${prefix%%/*//*}）"
            return 0
        fi
    done
    # jsDelivr 兜底（直接按仓库路径，不是 raw 前缀形式）
    if curl -fsSL --connect-timeout 10 --max-time 60 \
            "https://cdn.jsdelivr.net/gh/JuLuogo/eo@${BRANCH}/${rel}" -o "$out" 2>/dev/null && [ -s "$out" ]; then
        say "已通过 jsDelivr 下载 $rel"
        return 0
    fi
    if curl -fsSL --connect-timeout 10 --max-time 60 \
            "${GH_RAW_BASE}/${rel}" -o "$out" 2>/dev/null && [ -s "$out" ]; then
        say "已直连下载 $rel"
        return 0
    fi
    return 1
}

# 拉取代码：先试主源，再试备用源（Gitee <-> GitHub 互为备份）
clone_or_update() {
    local repo="$1"
    if [ -d "$DIR/.git" ]; then
        say "更新已有代码：$DIR（$repo）"
        git -C "$DIR" remote set-url origin "$repo" 2>/dev/null || true
        if git -C "$DIR" fetch --quiet origin "$BRANCH" 2>/dev/null; then
            git -C "$DIR" checkout --quiet "$BRANCH" 2>/dev/null || true
            git -C "$DIR" merge --ff-only "origin/$BRANCH" --quiet 2>/dev/null || \
                warn "更新失败，沿用当前版本"
            return 0
        fi
        warn "从 $repo 更新失败，尝试备用源…"
        return 1
    fi
    say "克隆代码：$repo -> $DIR"
    git clone --quiet --branch "$BRANCH" "$repo" "$DIR" 2>/dev/null
}

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
# 1.5 境内网络优化：可选写入 Docker 镜像加速
#     SET_REGISTRY_MIRROR=1 时把加速器写进 /etc/docker/daemon.json
#     （拉 python:3.12-slim 这类基础镜像会明显更快/更稳）
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
# 2. 拉取/更新代码（主源失败自动换备用源）
# --------------------------------------------------------------------------
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never
if [ -f "./run.py" ] && [ -f "./docker-compose.yml" ]; then
    say "当前目录已是项目根目录"
    DIR="$(pwd)"
else
    command -v git >/dev/null 2>&1 || die "需要 git 拉取代码，请先安装 git
    （或者直接把项目解压到 $DIR 后，在该目录里重新执行本脚本）"
    mkdir -p "$(dirname "$DIR")"

    if [ -d "$DIR/.git" ]; then
        clone_or_update "$REPO" || clone_or_update "$REPO_FALLBACK" || \
            warn "两个源都更新失败，沿用当前版本"
    else
        # 主源（默认 Gitee）
        if ! clone_or_update "$REPO"; then
            warn "从 $REPO 拉取失败，尝试备用源…"
            rm -rf "$DIR"
            if ! clone_or_update "$REPO_FALLBACK"; then
                rm -rf "$DIR"
                # 最后兜底：压缩包（Gitee / GitHub 的归档）
                say "git 拉取都失败，改用压缩包下载…"
                mkdir -p "$DIR"
                ok=0
                for u in \
                    "https://gitee.com/peroe/eo/repository/archive/${BRANCH}.tar.gz" \
                    "https://github.com/JuLuogo/eo/archive/refs/heads/${BRANCH}.tar.gz"
                do
                    if curl -fL --connect-timeout 15 --max-time 300 "$u" -o /tmp/eo-src.tar.gz 2>/dev/null \
                       && tar xzf /tmp/eo-src.tar.gz -C "$DIR" --strip-components=1 2>/dev/null; then
                        rm -f /tmp/eo-src.tar.gz
                        say "已通过压缩包获取代码：$u"
                        ok=1
                        break
                    fi
                done
                [ "$ok" = "1" ] || die "代码获取失败。请手动下载后放到 $DIR 再执行本脚本：
    Gitee  : https://gitee.com/peroe/eo
    GitHub : https://github.com/JuLuogo/eo"
            fi
        fi
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

    # 私有包需要先登录。提供了 GHCR_TOKEN 就自动登录（令牌需勾选 read:packages）
    if [ -n "${GHCR_TOKEN:-}" ]; then
        if printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-JuLuogo}" --password-stdin >/dev/null 2>&1; then
            say "已登录 ghcr.io（用户 ${GHCR_USER:-JuLuogo}）"
        else
            warn "GHCR 登录失败，继续尝试匿名拉取"
        fi
    fi

    if ! docker pull "$IMAGE"; then
        warn "拉取失败。GHCR 的包默认是【私有】的，两种解决方式："
        warn "  A) 到 GitHub 把包改成公开（一次性，之后免登录）："
        warn "     仓库页 -> 右侧 Packages -> eo -> Package settings"
        warn "     -> Danger Zone -> Change package visibility -> Public"
        warn "  B) 保持私有，用 GitHub 私人令牌（勾选 read:packages）自动登录："
        warn "     重新执行本命令，并加上 GHCR_TOKEN=<你的GitHub令牌>"
        warn "或改用本机构建（去掉 USE_GHCR=1 即可）。"
        exit 1
    fi
    docker tag "$IMAGE" edge-optimizer:latest
    # 强制用拉下来的镜像，绝不触发本地构建
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
