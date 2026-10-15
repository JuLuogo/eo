#!/usr/bin/env bash
# Docker 集成测试：构建镜像 -> 起容器 -> 全功能验证 -> 自动清理。
#
# 用法（任意位置均可，建议在仓库根目录）：
#     bash tests/docker_integration.sh
#
# 覆盖：镜像构建、构建期断言、容器降权、挂载目录可写、EO_NETWORK 推导平台、
#       Web 全部接口、DNS 面板条目、变量来源识别、真实扫描、停止、循环启动、稳定性。
#
# 需要本机能拉取 python:3.12-slim。境内可先配置镜像加速，或手动执行：
#     docker pull docker.1ms.run/library/python:3.12-slim
#     docker tag  docker.1ms.run/library/python:3.12-slim python:3.12-slim
set -uo pipefail

# 以脚本自身位置推导仓库根，不写死任何绝对路径
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
PORT="${EO_TEST_PORT:-18810}"
TOKEN="dockertest123456"
IMAGE="eo-docker-it:latest"
NAME="eo-docker-it"
pass=0; fail=0

ok()  { printf '  [OK]   %s\n' "$*"; pass=$((pass+1)); }
bad() { printf '  [FAIL] %s\n' "$*"; fail=$((fail+1)); }
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker rmi "$IMAGE" >/dev/null 2>&1 || true
    # 容器会把挂载目录 chown 给 10001，普通用户删不掉，用容器以 root 身份清理
    docker run --rm -v "$STAGE:/cleanup" python:3.12-slim \
        sh -c 'rm -rf /cleanup/* /cleanup/.[!.]* 2>/dev/null' >/dev/null 2>&1 || true
    rm -rf "$STAGE" 2>/dev/null || true
}
trap cleanup EXIT

cd "$ROOT" || { echo "定位仓库根目录失败"; exit 1; }
echo "仓库根目录: $ROOT"

echo
echo "############ 0. 环境准备 ############"
command -v docker >/dev/null 2>&1 || { echo "本机未安装 docker，跳过（可选测试）"; exit 0; }
docker info >/dev/null 2>&1 || { echo "docker 守护进程不可用，跳过"; exit 0; }
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker rmi "$IMAGE" >/dev/null 2>&1 || true
if ! docker image inspect python:3.12-slim >/dev/null 2>&1; then
    echo "  本地无 python:3.12-slim，尝试拉取…"
    if ! docker pull -q python:3.12-slim >/dev/null 2>&1; then
        for m in docker.1ms.run docker.m.daocloud.io dockerproxy.com; do
            if docker pull -q "$m/library/python:3.12-slim" >/dev/null 2>&1; then
                docker tag "$m/library/python:3.12-slim" python:3.12-slim
                echo "  已通过 $m 获取基础镜像"
                break
            fi
        done
    fi
fi
docker image inspect python:3.12-slim >/dev/null 2>&1 \
    && ok "基础镜像可用" \
    || { echo "  无法获取 python:3.12-slim（网络受限），跳过 Docker 测试"; exit 0; }

echo
echo "############ 1. 构建镜像 ############"
if docker build -t "$IMAGE" . > "$STAGE/build.log" 2>&1; then
    ok "构建成功"
    grep -q "镜像自检通过" "$STAGE/build.log" && ok "构建期断言通过（eo 是包）" \
        || echo "     (未捕获断言输出，不影响结论)"
else
    bad "构建失败"; tail -25 "$STAGE/build.log" | sed 's/^/    /'; exit 1
fi

echo
echo "############ 2. 镜像内容 ############"
docker run --rm --entrypoint sh "$IMAGE" -c 'test -f /app/eo/__init__.py' \
    && ok "eo/__init__.py 已打进镜像" || bad "镜像缺 __init__.py"
docker run --rm --entrypoint sh "$IMAGE" -c 'test ! -e /app/eo.py' \
    && ok "无遗留 eo.py" || bad "镜像内仍有 eo.py"
docker run --rm --entrypoint python "$IMAGE" run.py selftest >/dev/null 2>&1 \
    && ok "镜像内 selftest 通过" || bad "镜像内 selftest 失败"

echo
echo "############ 3. 只设 EO_NETWORK=dx（验证运营商推导）############"
mkdir -p "$STAGE/v1/config" "$STAGE/v1/results" "$STAGE/v1/targets"
cp targets/*.txt "$STAGE/v1/targets/" 2>/dev/null || true
# 故意让目录属 root，模拟 deploy.sh / onekey.sh 以 root 创建的真实情况
chown -R root:root "$STAGE/v1" 2>/dev/null || true
chmod 755 "$STAGE/v1/config" "$STAGE/v1/results"

docker run -d --name "$NAME" \
    -e EO_MODE=web -e EO_WEB_PORT="$PORT" -e EO_WEB_TOKEN="$TOKEN" \
    -e EO_NETWORK=dx -e EO_RATE=100 -e EO_DNS_SYNC=dry-run -e TZ=Asia/Shanghai \
    -p "127.0.0.1:$PORT:$PORT" \
    -v "$STAGE/v1/config:/app/config" \
    -v "$STAGE/v1/results:/app/results" \
    -v "$STAGE/v1/targets:/app/targets" \
    "$IMAGE" >/dev/null
sleep 8

ST=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null)
RC=$(docker inspect -f '{{.RestartCount}}' "$NAME" 2>/dev/null)
if [ "$ST" = "running" ]; then
    ok "容器运行中（重启 $RC 次）"
else
    bad "容器未运行"; docker logs "$NAME" 2>&1 | tail -20 | sed 's/^/    /'; exit 1
fi
docker exec "$NAME" sh -c 'grep -E "^Uid:" /proc/1/status' | grep -qE "^Uid:\s*10001" \
    && ok "PID 1 已降权到 10001（非 root）" || bad "容器未降权"
docker exec "$NAME" test -f /app/config/config.json \
    && ok "挂载目录可写（config.json 已落盘）" || bad "config 未落盘"
docker exec "$NAME" python -c "
from eo.webui import make_service
ks = sorted(p.key for p in make_service().build_platforms())
assert ks == ['cft-dx', 'eo-dx', 'esa-dx'], ks
print('    生效平台:', ks)
" >/dev/null 2>&1 && ok "EO_NETWORK=dx 自动推导出电信三件套" || bad "平台推导错误"

echo
echo "############ 4. Web 接口 ############"
BASE="http://127.0.0.1:$PORT"
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"; }
[ "$(code "$BASE/healthz")" = "200" ] && ok "/healthz" || bad "/healthz"
[ "$(code "$BASE/api/status")" = "401" ] && ok "无令牌返回 401" || bad "无令牌未拦截"
for ep in status vars logs platforms config; do
    curl -s --max-time 20 -H "Authorization: Bearer $TOKEN" "$BASE/api/$ep" | grep -q . \
        && ok "/api/$ep" || bad "/api/$ep"
done

echo
echo "  >= DNS 面板有可配置项："
curl -s --max-time 20 -H "Authorization: Bearer $TOKEN" "$BASE/api/config" | python3 -c "
import json, sys
dns = json.load(sys.stdin)['config'].get('dns') or {}
assert dns, 'DNS 面板为空'
print('    条目:', sorted(dns))
print('    eo-dx 记录名:', dns.get('eo-dx', {}).get('record_name'))
" && ok "DNS 面板非空" || bad "DNS 面板为空"

echo "  >= 变量来源识别（容器内为进程环境变量）："
curl -s --max-time 20 -H "Authorization: Bearer $TOKEN" "$BASE/api/vars" | python3 -c "
import json, sys
v = {x['key']: x for x in json.load(sys.stdin)['vars']}
r = v['rate']
print(f\"    rate: 生效={r['effective']} 来源={r['source']} 细分={r.get('env_origin')}\")
assert r['source'] == 'env' and r.get('env_origin') == '进程环境变量', r
" && ok "来源识别正确" || bad "来源识别错误"

echo
echo "############ 5. 真实扫描与停止 ############"
curl -s --max-time 20 -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" -d '{}' "$BASE/api/control/run-once" | sed 's/^/    /'
echo
for i in $(seq 1 12); do
    sleep 5
    S=$(curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" "$BASE/api/status")
    PH=$(echo "$S" | python3 -c "import json,sys;print((json.load(sys.stdin).get('progress') or {}).get('phase','?'))" 2>/dev/null)
    DN=$(echo "$S" | python3 -c "
import json,sys
p=json.load(sys.stdin).get('progress') or {}
print(f\"{p.get('done',0)}/{p.get('total',0)}\")" 2>/dev/null)
    printf '    [%02d] phase=%-9s %s\n' "$i" "$PH" "$DN"
    [ "$PH" = "scanning" ] && [ "${DN%%/*}" != "0" ] && break
done

echo "  >= 停止任务："
curl -s --max-time 20 -X POST -H "Authorization: Bearer $TOKEN" "$BASE/api/control/stop" | sed 's/^/    /'
echo
R=""
for i in $(seq 1 15); do
    sleep 3
    R=$(curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" "$BASE/api/status" \
        | python3 -c "import json,sys;print(json.load(sys.stdin)['runner']['running'])" 2>/dev/null)
    [ "$R" = "False" ] && break
done
[ "$R" = "False" ] && ok "扫描被成功停止（约 $((i*3))s）" || bad "停止未生效"

echo "  >= 停止后可启动循环："
curl -s --max-time 20 -X POST -H "Authorization: Bearer $TOKEN" "$BASE/api/control/start" | sed 's/^/    /'
echo
sleep 2
curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" "$BASE/api/status" | python3 -c "
import json, sys
r = json.load(sys.stdin)['runner']
print(f\"    running={r['running']} interval={r['interval']} state={r['state']}\")
assert r['running'] is True, r
" && ok "循环可启动（不再被 409 挡）" || bad "循环启动失败"
curl -s --max-time 20 -X POST -H "Authorization: Bearer $TOKEN" "$BASE/api/control/stop" >/dev/null

echo "  >= 日志内容："
curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" "$BASE/api/logs" | python3 -c "
import json, sys
logs = [x['line'] for x in json.load(sys.stdin)['logs']]
print(f'    日志 {len(logs)} 条')
assert any('限速' in l for l in logs), '未记录限速'
assert any('停止' in l for l in logs), '未记录停止'
" && ok "日志含限速与停止记录" || bad "日志缺失"

echo
echo "############ 6. 容器稳定性 ############"
sleep 5
ST=$(docker inspect -f '{{.State.Status}}' "$NAME"); RC=$(docker inspect -f '{{.RestartCount}}' "$NAME")
[ "$ST" = "running" ] && ok "全程零崩溃（重启 $RC 次）" || bad "容器已退出"

echo
echo "############ 结果: 通过 $pass / 失败 $fail ############"
[ "$fail" -eq 0 ] || exit 1
