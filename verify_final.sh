#!/usr/bin/env bash
# 最终验证：全新 clone（含全部已推送修复）-> 构建 -> 运行 -> 全功能 + 错误路径
set -uo pipefail
STAGE=/tmp/eo-verify-final
PORT=18795
TOKEN=finalcheck123456
pass=0; fail=0
ok()  { printf '  [OK]   %s\n' "$*"; pass=$((pass+1)); }
bad() { printf '  [FAIL] %s\n' "$*"; fail=$((fail+1)); }
export GIT_TERMINAL_PROMPT=0

echo "############ 0. 清理 ############"
docker rm -f eo-vf >/dev/null 2>&1 || true
docker rmi eo-vf:latest >/dev/null 2>&1 || true
rm -rf "$STAGE"; mkdir -p "$STAGE"
echo "  已清理"

echo
echo "############ 1. 全新 clone（验证仓库完整性）############"
cd "$STAGE"
timeout 120 git clone --quiet --branch main https://gitee.com/peroe/eo.git repo || { echo "  clone 失败"; exit 1; }
cd repo
HEAD=$(git rev-parse --short HEAD)
echo "  HEAD: $HEAD"
[ "$HEAD" = "5105d2c" ] && ok "clone 到目标提交" || echo "  注意: 期望 5105d2c, 实际 $HEAD"

echo "  ≥ 必备文件（全新 clone 视角）："
MISSING=0
for f in eo/__init__.py eo/core.py eo/cli.py eo/dns.py eo/webui.py eo/runner.py eo/varview.py \
         run.py Dockerfile docker-compose.yml docker-entrypoint.sh onekey.sh install.sh \
         deploy.sh docker-setup.sh .env.example targets/eo.txt targets/esa.txt targets/cft.txt; do
    [ -f "$f" ] || { echo "      缺失 $f"; MISSING=$((MISSING+1)); }
done
[ "$MISSING" -eq 0 ] && ok "全部必备文件存在" || bad "$MISSING 个文件缺失"

echo "  ≥ 可执行位："
EXECOK=0
for f in onekey.sh install.sh deploy.sh docker-setup.sh docker-entrypoint.sh; do
    [ -x "$f" ] && EXECOK=$((EXECOK+1))
done
[ "$EXECOK" -eq 5 ] && ok "5 个脚本均可执行" || bad "仅 $EXECOK/5 可执行"

echo
echo "############ 2. Python 层（宿主无华为云 SDK）############"
export PYTHONIOENCODING=utf-8
python3 -c "import huaweicloudsdkcore" 2>/dev/null && echo "  SDK: 已装" || echo "  SDK: 未装（正是要验证的场景）"
python3 -c "from eo.dns import sdk_available; print('  sdk_available():', sdk_available())"
python3 run.py selftest > "$STAGE/st.log" 2>&1 \
    && ok "selftest 通过（$(grep -c '✅' "$STAGE/st.log") 项，无 SDK 环境）" \
    || { bad "selftest 失败"; tail -12 "$STAGE/st.log" | sed 's/^/    /'; }
python3 run.py list --all-platforms > /dev/null 2>&1 && ok "run.py list 正常" || bad "list 异常"

echo
echo "  ≥ 网段缺失不再裸崩（三平台）："
python3 - <<'PY'
import dataclasses
from eo.core import scan_platform
from eo.platforms import PLATFORMS
bad = []
for key in ("eo-lt", "esa-lt", "cft-lt"):
    p = dataclasses.replace(PLATFORMS[key], targets_file="targets/__nope__.txt")
    try:
        out = scan_platform(p, targets=None, log=lambda *_: None, quiet=True)
        if not out.error:
            bad.append(f"{key}: 无错误信息")
        else:
            print(f"    {key}: 返回错误 OK")
    except Exception as e:
        bad.append(f"{key}: 抛异常 {type(e).__name__}")
for b in bad:
    print("    FAIL:", b)
raise SystemExit(1 if bad else 0)
PY
[ $? -eq 0 ] && ok "三平台均优雅报错" || bad "仍有平台崩溃"

echo
echo "############ 3. 构建镜像 ############"
if ! docker image inspect python:3.12-slim >/dev/null 2>&1; then
    if docker pull -q docker.1ms.run/library/python:3.12-slim >/dev/null 2>&1; then
        docker tag docker.1ms.run/library/python:3.12-slim python:3.12-slim
        echo "  基础镜像经 docker.1ms.run 加速"
    fi
fi
if docker build -t eo-vf:latest . > "$STAGE/b.log" 2>&1; then
    ok "构建成功"
    grep -q "镜像自检通过" "$STAGE/b.log" && ok "构建期断言通过（eo 是包）" || echo "  （未捕获到断言输出，不影响）"
else
    bad "构建失败"; tail -20 "$STAGE/b.log" | sed 's/^/    /'; exit 1
fi

echo
echo "############ 4. 镜像内容 ############"
docker run --rm --entrypoint sh eo-vf:latest -c 'test -f /app/eo/__init__.py' && ok "eo/__init__.py 在镜像内" || bad "缺 __init__.py"
docker run --rm --entrypoint sh eo-vf:latest -c 'test ! -e /app/eo.py' && ok "无遗留 eo.py" || bad "仍有 eo.py"
docker run --rm --entrypoint python eo-vf:latest run.py selftest > /dev/null 2>&1 && ok "镜像内 selftest 通过" || bad "镜像内 selftest 失败"
docker run --rm --entrypoint python eo-vf:latest -c "import huaweicloudsdkcore" >/dev/null 2>&1 && ok "SDK 已内置" || bad "缺 SDK"

echo
echo "############ 5. 真实运行（root 所有的挂载目录）############"
mkdir -p "$STAGE/v/config" "$STAGE/v/results" "$STAGE/v/targets"
cp targets/*.txt "$STAGE/v/targets/"
chown -R root:root "$STAGE/v" 2>/dev/null || true
chmod 755 "$STAGE/v/config" "$STAGE/v/results"

docker run -d --name eo-vf \
    -e EO_MODE=web -e EO_WEB_PORT=$PORT -e EO_WEB_TOKEN=$TOKEN \
    -e EO_NETWORK=lt -e EO_PLATFORMS=eo-lt -e EO_RATE=100 -e EO_DNS_SYNC=dry-run \
    -e TZ=Asia/Shanghai -p 127.0.0.1:$PORT:$PORT \
    -v "$STAGE/v/config:/app/config" -v "$STAGE/v/results:/app/results" \
    -v "$STAGE/v/targets:/app/targets" eo-vf:latest >/dev/null
sleep 8

ST=$(docker inspect -f '{{.State.Status}}' eo-vf); RC=$(docker inspect -f '{{.RestartCount}}' eo-vf)
[ "$ST" = "running" ] && ok "容器运行中，重启 $RC 次（权限问题已解决）" \
    || { bad "未运行"; docker logs eo-vf 2>&1|tail -20|sed 's/^/    /'; exit 1; }

docker exec eo-vf sh -c 'grep -E "^Uid:" /proc/1/status' | grep -qE "^Uid:\s*10001" \
    && ok "PID 1 降权到 10001（非 root）" || bad "未降权"
docker exec eo-vf test -f /app/config/config.json && ok "config 落盘" || bad "config 未落盘"

docker exec eo-vf python -c "
from eo.webui import make_service
ks = [p.key for p in make_service().build_platforms()]
print('    生效平台:', ks)
assert ks == ['eo-lt'], ks
" >/dev/null 2>&1 && ok "EO_PLATFORMS 过滤生效（仅 eo-lt）" || bad "平台过滤失效"

echo
echo "############ 6. Web 接口 ############"
BASE="http://127.0.0.1:$PORT"
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"; }
[ "$(code "$BASE/healthz")" = "200" ] && ok "/healthz 200" || bad "/healthz"
[ "$(code "$BASE/api/status")" = "401" ] && ok "无令牌 401" || bad "无令牌未拦截"
for ep in status vars logs platforms; do
    curl -s --max-time 20 -H "Authorization: Bearer $TOKEN" "$BASE/api/$ep" | grep -q . \
        && ok "/api/$ep" || bad "/api/$ep"
done
PAGE=$(curl -s --max-time 20 -H "Authorization: Bearer $TOKEN" "$BASE/")
for kw in "常用变量" "运行参数" "平台设置" "DNS 下发" "任务状态" "实时日志"; do
    echo "$PAGE" | grep -q "$kw" && ok "面板「$kw」" || bad "缺面板「$kw」"
done

echo
echo "############ 7. 错误路径（容器内，空 targets）############"
docker run --rm --entrypoint python \
    -e EO_NETWORK=lt -e EO_PLATFORMS=cft-lt \
    -v "$STAGE/v2/config:/app/config" -v "$STAGE/v2/targets:/app/targets" \
    eo-vf:latest run.py scan --network lt -q > "$STAGE/miss.log" 2>&1
mkdir -p "$STAGE/v2/config" "$STAGE/v2/targets"
docker run --rm --entrypoint python \
    -e EO_NETWORK=lt -e EO_PLATFORMS=cft-lt \
    -v "$STAGE/v2/config:/app/config" -v "$STAGE/v2/targets:/app/targets" \
    eo-vf:latest run.py scan --network lt -q > "$STAGE/miss.log" 2>&1
grep -q "网段文件不存在" "$STAGE/miss.log" && ok "quiet 模式下错误可见" || { bad "quiet 吞掉错误"; cat "$STAGE/miss.log"|sed 's/^/    /'; }
grep -q "run.py ranges --source cloudfront" "$STAGE/miss.log" && ok "包含可操作修复命令" || bad "缺修复命令"
grep -qE "Traceback|FileNotFoundError" "$STAGE/miss.log" && bad "有裸崩溃" || ok "无裸崩溃"

echo
echo "############ 结果 ############"
echo "  通过: $pass   失败: $fail"
docker rm -f eo-vf >/dev/null 2>&1 || true
[ "$fail" -eq 0 ] || exit 1
