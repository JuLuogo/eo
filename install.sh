#!/bin/sh
# 边缘节点优选 —— 一键安装引导脚本
#
# 用法一（推荐，服务器上直接跑，不需要先 clone）：
#   curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh
#
# 用法二（已 clone 到本地）：
#   ./install.sh
#
# 用法三（指定目录 / 分支 / 仓库）：
#   REPO=https://gitee.com/peroe/eo.git DIR=/opt/eo BRANCH=main ./install.sh
#
# 用法四（仓库改成私有后，用令牌拉取）：
#   GITEE_TOKEN=你的令牌 ./install.sh
#   GITEE_USER=你的用户名 GITEE_TOKEN=你的令牌 ./install.sh
#
# 脚本会：检查依赖 -> 拉取代码 -> 建立虚拟环境 -> 安装 Python 包
#         -> 调用 deploy.sh install（交互式问运营商和端口）-> 打印后续步骤
#
# 用 POSIX sh 编写，Ubuntu/Debian/CentOS/Alpine 都能直接跑。
set -eu

REPO="${REPO:-https://gitee.com/peroe/eo.git}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/edge-optimizer}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
# 私有仓库时可用：GITEE_USER + GITEE_TOKEN
GITEE_USER="${GITEE_USER:-}"
GITEE_TOKEN="${GITEE_TOKEN:-}"

say()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# 1. 依赖检查
# --------------------------------------------------------------------------
say "检查系统依赖…"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    die "找不到 $PYTHON_BIN，请先安装 Python 3.10+：
    Ubuntu/Debian : sudo apt update && sudo apt install -y python3 python3-venv python3-pip curl git
    CentOS/RHEL   : sudo yum install -y python3 python3-pip curl git
    Alpine        : sudo apk add python3 py3-pip curl git"
fi

"$PYTHON_BIN" - <<'PY' || die "Python 版本过低，需要 3.10 及以上"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
say "Python: $("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"

command -v curl >/dev/null 2>&1 || die "缺少 curl：
    Ubuntu/Debian : sudo apt install -y curl
    CentOS/RHEL   : sudo yum install -y curl
    Alpine        : sudo apk add curl"

if command -v apt-get >/dev/null 2>&1 && ! "$PYTHON_BIN" -c 'import ensurepip' >/dev/null 2>&1; then
    warn "缺少 venv/ensurepip 支持（Debian/Ubuntu 常见），尝试自动安装…"
    PYVER="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    SUDO=""
    [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
    $SUDO apt-get update -qq >/dev/null 2>&1 || true
    for pkg in "python${PYVER}-venv" "python3-venv"; do
        if $SUDO apt-get install -y "$pkg" >/dev/null 2>&1; then
            say "已安装 $pkg"
            break
        fi
    done
    if ! "$PYTHON_BIN" -c 'import ensurepip' >/dev/null 2>&1; then
        die "venv 支持仍不可用，请手动安装后重试：
    sudo apt install -y python${PYVER}-venv     # 或 python3-venv"
    fi
fi

# --------------------------------------------------------------------------
# 2. 获取代码
# --------------------------------------------------------------------------
# 组装拉取地址：默认公开仓库直接匿名拉取，并禁用交互式认证询问。
# 关键：很多服务器上存着过期的 Gitee 凭据，git 会因此弹出「Username for ...」
# 反复失败。这里显式关掉询问：匿名能拉就拉，拉不了就给出明确指引。
build_clone_url() {
    if [ -n "$GITEE_TOKEN" ]; then
        if [ -n "$GITEE_USER" ]; then
            echo "https://${GITEE_USER}:${GITEE_TOKEN}@gitee.com/peroe/eo.git"
        else
            echo "https://${GITEE_TOKEN}@gitee.com/peroe/eo.git"
        fi
    else
        echo "$REPO"
    fi
}

git_quiet() {
    # GIT_TERMINAL_PROMPT=0 : 禁止弹出用户名/密码询问
    # GCM_INTERACTIVE=never : 禁止 Git Credential Manager 弹窗
    # -c credential.helper=  : 忽略本机已保存（可能已过期）的凭据
    GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never \
        git -c credential.helper= "$@"
}

# 带超时执行（没有 timeout 命令时直接执行）
run_with_timeout() {
    local seconds="$1"; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$@"
    else
        "$@"
    fi
}

clone_hint() {
    cat >&2 <<'EOF'

拉取失败。仓库当前是【公开】的，正常应该能匿名拉取，请按顺序排查：

  1) 服务器上存着过期的 Gitee 凭据（最常见）
     安装脚本已经禁用凭据询问，但如果你的 git 版本仍弹窗，请清掉旧凭据：
       git config --global --unset credential.helper
       rm -f ~/.git-credentials
       git config --global --list | grep -i credential     # 确认已清干净

  2) 网络到 Gitee 不通（尤其是海外机器）
     先测：curl -sI https://gitee.com | head -1

  3) 不想折腾 git，直接用压缩包
       见下面「下载压缩包」给出的两条命令，或重跑本脚本，它会自动尝试。

  4) 仓库若已改为私有，用令牌拉取
       GITEE_USER=你的用户名 GITEE_TOKEN=你的私人令牌 \
       curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh

EOF
}

# 压缩包兜底：git 拉不动时（凭据过期、网络策略等）改用归档下载，无需认证
fetch_via_archive() {
    local url="$1" dest="$2" tmp
    tmp="$(mktemp -d)"
    say "改用压缩包下载（无需认证）：$url"
    if ! curl -fL --connect-timeout 15 --max-time 300 "$url" -o "$tmp/archive.tar.gz"; then
        rm -rf "$tmp"
        return 1
    fi
    mkdir -p "$dest"
    # Gitee 归档解压后是 <仓库名-分支>/ 一层目录，用 --strip-components=1 摊平
    if ! tar xzf "$tmp/archive.tar.gz" -C "$dest" --strip-components=1; then
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$tmp"
    return 0
}

if [ -f "./run.py" ] && [ -f "./deploy.sh" ]; then
    say "当前目录已是项目根目录，跳过拉取"
    TARGET_DIR="$(pwd)"
else
    command -v git >/dev/null 2>&1 || die "需要 git 来拉取代码，请先安装 git"
    CLONE_URL="$(build_clone_url)"

    if [ -d "$DIR/.git" ]; then
        say "目录已存在，执行更新：$DIR"
        # 加超时：部分服务器到 Gitee 的连接会长时间挂住
        if ! run_with_timeout 60 git_quiet -C "$DIR" fetch origin "$BRANCH" 2>/dev/null; then
            warn "fetch 失败或超时（网络/权限问题），继续使用当前版本"
        else
            git_quiet -C "$DIR" checkout "$BRANCH" 2>/dev/null || true
            if ! run_with_timeout 60 git_quiet -C "$DIR" pull --ff-only origin "$BRANCH" 2>/dev/null; then
                warn "pull 失败或超时，继续使用当前版本"
                warn "手动更新：cd $DIR && git fetch origin $BRANCH && git merge --ff-only origin/$BRANCH"
                warn "（git 更新不影响本次安装；脚本本体已是服务器上的版本）"
            fi
        fi
    else
        say "拉取代码到 $DIR"
        mkdir -p "$(dirname "$DIR")"
        if ! git_quiet clone --branch "$BRANCH" "$CLONE_URL" "$DIR"; then
            warn "git 拉取失败，可能是本机凭据过期或网络限制，自动改用压缩包…"
            ARCHIVE_URL="https://gitee.com/peroe/eo/repository/archive/${BRANCH}.tar.gz"
            if fetch_via_archive "$ARCHIVE_URL" "$DIR"; then
                say "压缩包下载成功（这种方式不带 .git，之后升级请用 git clone）"
            else
                rm -rf "$DIR"
                clone_hint
                exit 1
            fi
        fi
    fi
    TARGET_DIR="$DIR"
fi

cd "$TARGET_DIR"
say "项目目录：$TARGET_DIR"

# --------------------------------------------------------------------------
# 3. 交给 deploy.sh 完成安装与配置
# --------------------------------------------------------------------------
if [ ! -f "./deploy.sh" ]; then
    die "在 $TARGET_DIR 里找不到 deploy.sh，仓库可能不完整"
fi
chmod +x ./deploy.sh 2>/dev/null || true

if [ "$(id -u)" = "0" ]; then
    say "以 root 运行，开始安装（会询问运营商与端口）…"
    say "提示：不想装华为云 DNS SDK（纯优选）可加 --no-dns"
    ./deploy.sh install "$@"
else
    if command -v sudo >/dev/null 2>&1; then
        say "需要 root 权限写 systemd 服务，改用 sudo 执行…"
        sudo ./deploy.sh install "$@"
    else
        die "当前不是 root 且没有 sudo，请用 root 执行：
    curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh"
    fi
fi

# --------------------------------------------------------------------------
# 4. 收尾提示
# --------------------------------------------------------------------------
PORT="$(grep -E '^EO_WEB_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' \"' || true)"
CARRIER="$(grep -E '^EO_NETWORK=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' \"' || true)"
case "$CARRIER" in
    lt) CARRIER_CN="联通" ;; dx) CARRIER_CN="电信" ;; yd) CARRIER_CN="移动" ;; *) CARRIER_CN="未设置" ;;
esac

printf '\n'
say "安装完成！运营商=${CARRIER_CN}(${CARRIER:-?})  端口=${PORT:-8787}"
printf '\n接下来三步：\n'
printf '  1) 填密钥（下发 DNS 必需）：\n'
printf '       cd %s && vi .env        # 填 HUAWEI_AK / HUAWEI_SK\n' "$TARGET_DIR"
printf '  2) 体检（确认本机链路没被代理接管）：\n'
printf '       cd %s && ./deploy.sh doctor\n' "$TARGET_DIR"
printf '  3) 启动并查看访问地址：\n'
printf '       cd %s && sudo ./deploy.sh start && ./deploy.sh status\n' "$TARGET_DIR"
printf '\n'
