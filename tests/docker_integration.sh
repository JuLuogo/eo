#!/usr/bin/env bash
# Docker 集成测试：构建 -> 运行 -> 验证（含 EO_NETWORK 推导、真实扫描）
set -uo pipefail
LOCAL=/mnt/c/Users/juluo/Desktop/eo
REPO="$HOME/eotest/repo"
STAGE="$HOME/eotest/docker"
PORT=18810
TOKEN=dockertest123456
pass=0; fail=0
ok()  { printf '  [OK]   %s\n' "$*"; pass=$((pass+1)); }
bad() { printf '  [FAIL] %s\n' "$*"; fail=$((fail+1)); }
export GIT_TERMINAL_PROMPT=0

echo "############ 0. 同步代码与清理 ############"
cd "$REPO" || exit 1
for f in eo/platforms.py eo/webui.py eo/runner.py eo/varview.py eo/pages.py eo/config_store.py \
         eo/core.py eo/dns.py eo/cli.py Dockerfile docker-entrypoint.sh docker-compose.yml; do
    [ -f "$LOCAL/$f" ] && cp -f "$LOCAL/$f" "$f"
done
echo "  已同步本地改动"
docker rm -f eo-dk >/dev/null 2>&1 || true
docker rmi eo-dk:latest >/dev/null 2>&1 || true
rm -rf "$STAGE"; mkdir -p "$STAGE"

echo
echo "############ 1. 构建镜像 ############"
# 本机网络拉不动 Docker Hub，确保有本地基础镜像
if ! docker image inspect python:3.12-slim >/dev/null 2>&1; then
    docker pull -q docker.1ms.run/library/python:3.12-slim >/dev/null 2>&1 && \
        docker tag docker.1ms.run/library/python:3.12-slim python:3.12-slim
fi
if docker build -t eo-dk:latest . > "$STAGE/build.log" 2>&1; then
    ok "构建成功"
    grep -q "镜像自检通过" "$STAGE/build.log" && ok "构建期断言通过" || echo "     (未捕获断言输出)"
else
    bad "构建失败"; tail -25 "$STAGE/build.log" | sed 's/^/    /'; exit 1
fi

echo
echo "############ 2. 镜像内容 ############"
docker run --rm --entrypoint sh eo-dk:latest -c 'test -f /app/eo/__init__.py' \
    && ok "eo/__init__.py 在镜像内" || bad "缺 __init__.py"
docker run --rm --entrypoint sh eo-dk:latest -c 'test ! -e /app/eo.py' \
    && ok "无遗留 eo.py" || bad "仍有 eo.py"
docker run --rm --entrypoint python eo-dk:latest run.py selftest >/dev/null 2>&1 \
    && ok "镜像内 selftest 通过" || bad "selftest 失败"

echo
echo "############ 3. 只设 EO_NETWORK=dx（验证新修复）############"
mkdir -p "$STAGE/v1/config" "$STAGE/v1/results" "$STAGE/v1/targets"
cp targets/*.txt "$STAGE/v1/targets/"
chown -R root:root "$STAGE/v1" 2>/dev/null || true
chmod 755 "$STAGE/v1/config" "$STAGE/v1/results"

docker run -d --name eo-dk \
    -e EO_MODE=web -e EO_WEB_PORT=$PORT -e EO_WEB_TOKEN=$TOKEN \
    -e EO_NETWORK=dx -e EO_RATE=100 -e EO_DNS_SYNC=dry-run \
    -e TZ=Asia/Shanghai -p 127.0.0.1:$PORT:$PORT \
    -v "$STAGE/v1/config:/app/config" -v "$STAGE/v1/results:/app/results" \
    -v "$STAGE/v1/targets:/app/targets" eo-dk:latest >/dev/null
sleep 8

ST=$(docker inspect -f '{{.State.Status}}' eo-dk); RC=$(docker inspect -f '{{.RestartCount}}' eo-dk)
[ "$ST" = "running" ] && ok "容器运行中（重启 $RC）" || {
    bad "容器未运行"; docker logs eo-dk 2>&1 | tail -20 | sed 's/^/    /'; exit 1; }

docker exec eo-dk sh -c 'grep -E "^Uid:" /proc/1/status' | grep -qE "^Uid:\s*10001" \
    && ok "PID 1 降权到 10001" || bad "未降权"

echo "  ≥ 平台推导（只给了 EO_NETWORK=dx）："
docker exec eo-dk python -c "
from eo.webui import make_service
ks = sorted(p.key for p in make_service().build_platforms())
print('     ', ks)
assert ks == ['cft-dx','eo-dx','esa-dx'], ks
" && ok "自动推导出电信三件套" || bad "平台推导错误"

echo "  ≥ config 落盘："
docker exec eo-dk test -f /app/config/config.json && ok "config.json 已生成" || bad "config 缺失"

echo
echo "############ 4. Web 接口 ############"
BASE="http://127.0.0.1:$PORT"
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"; }
[ "$(code "$BASE/healthz")" = "200" ] && ok "/healthz" || bad "/healthz"
[ "$(code "$BASE/api/status")" = "401" ] && ok "无令牌 401" || bad "无令牌未拦"
for ep in status vars logs platforms config; do
    curl -s --max-time 20 -H "Authorization: Bearer $TOKEN" "$BASE/api/$ep" | grep -q . \
        && ok "/api/$ep" || bad "/api/$ep"
done

echo "  ≥ DNS 面板条目（新修复）："
curl -s --max-time 20 -H "Authorization: Bearer $TOKEN" "$BASE/api/config" | python3 -c "
import json,sys
d = json.load(sys.stdin)['config']
dns = d.get('dns', {})
print('     ', sorted(dns))
assert dns, 'dns 为空'
print('      eo-dx record:', dns.get('eo-dx',{}).get('record_name'))
" && ok "DNS 面板有可配置项" || bad "DNS 面板为空"

echo
echo "############ 5. 变量来源（容器内是进程环境变量）############"
curl -s --max-time 20 -H "Authorization: Bearer $TOKEN" "$BASE/api/vars" | python3 -c "
import json,sys
v = {x['key']: x for x in json.load(sys.stdin)['vars']}
r = v['rate']
print(f\"      rate: 生效={r['effective']} 来源={r['source']} 细分={r.get('env_origin')}\")
assert r['source'] == 'env', r
assert r.get('env_origin') == '进程环境变量', r
" && ok "环境变量来源识别正确（关键修复）" || bad "来源识别错误"

echo
echo "############ 6. 真实扫描（短时）############"
curl -s --max-time 20 -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" -d '{}' "$BASE/api/control/run-once" | sed 's/^/      /'
echo
for i in $(seq 1 12); do
    sleep 5
    S=$(curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" "$BASE/api/status")
    PH=$(echo "$S" | python3 -c "import json,sys;print((json.load(sys.stdin).get('progress') or {}).get('phase','?'))" 2>/dev/null)
    DN=$(echo "$S" | python3 -c "
import json,sys
p=json.load(sys.stdin).get('progress') or {}
print(f\"{p.get('done',0)}/{p.get('total',0)}\")" 2>/dev/null)
    printf '      [%02d] phase=%-9s %s\n' "$i" "$PH" "$DN"
    [ "$PH" = "scanning" ] && [ "${DN%%/*}" != "0" ] && break
done

echo "      ≥ 停止任务（验证新修复）："
curl -s --max-time 20 -X POST -H "Authorization: Bearer $TOKEN" "$BASE/api/control/stop" | sed 's/^/        /'
echo
for i in $(seq 1 15); do
    sleep 3
    R=$(curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" "$BASE/api/status" \
        | python3 -c "import json,sys;print(json.load(sys.stdin)['runner']['running'])" 2>/dev/null)
    [ "$R" = "False" ] && break
done
[ "$R" = "False" ] && ok "扫描被成功停止（耗时约 $((i*3))s）" || bad "停止未生效"

echo
echo "  ≥ 停止后可启动循环："
curl -s --max-time 20 -X POST -H "Authorization: Bearer $TOKEN" "$BASE/api/control/start" | sed 's/^/        /'
echo
sleep 2
curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" "$BASE/api/status" | python3 -c "
import json,sys
r = json.load(sys.stdin)['runner']
print(f\"        running={r['running']} interval={r['interval']} state={r['state']}\")
assert r['running'] is True, r
" && ok "循环可启动（不再被 409 挡）" || bad "循环启动失败"
curl -s --max-time 20 -X POST -H "Authorization: Bearer $TOKEN" "$BASE/api/control/stop" >/dev/null

echo
echo "  ≥ 日志与限速："
curl -s --max-time 15 -H "Authorization: Bearer $TOKEN" "$BASE/api/logs" | python3 -c "
import json,sys
logs = [x['line'] for x in json.load(sys.stdin)['logs']]
print(f'        日志 {len(logs)} 条')
for l in logs[:4]: print('          ', l[:80])
assert any('限速' in l for l in logs), '未记录限速'
assert any('停止' in l for l in logs), '未记录停止'
" && ok "日志含限速与停止记录" || bad "日志缺失关键记录"

echo
echo "############ 7. 容器稳定性 ############"
sleep 5
ST=$(docker inspect -f '{{.State.Status}}' eo-dk); RC=$(docker inspect -f '{{.RestartCount}}' eo-dk)
[ "$ST" = "running" ] && ok "全程零崩溃（重启 $RC 次）" || bad "容器已退出"

echo
echo "############ 结果: 通过 $pass / 失败 $fail ############"
docker rm -f eo-dk >/dev/null 2>&1 || true
[ "$fail" -eq 0 ] || exit 1
