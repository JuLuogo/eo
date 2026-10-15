# 测试说明

本目录是项目的回归测试套件，覆盖限速、日志、调度、多运营商配置与 Web 控制台。
**不需要网络、不需要 Docker**（除 `docker_integration.sh`），可在任何机器上跑。

## 快速运行

```bash
cd /opt/edge-optimizer          # 仓库根目录
python tests/run_all.py         # 全部套件
python tests/run_all.py ratelimit   # 只跑名字含 ratelimit 的
```

也可以用 pytest（若能安装）：

```bash
pip install pytest && pytest tests/ -v
```

## 套件一览

| 文件 | 覆盖内容 | 对应需求 |
| --- | --- | --- |
| `test_ratelimit.py` | 令牌桶：单线程/8 线程下任意 1 秒窗口不超过 rate；空闲后不突发；边界值 | **每秒 TCP 连接不超过 100** |
| `test_ratelimit_e2e.py` | 真实探测路径上验证限速生效（用保留地址 + 低 rate 实测） | 同上（端到端） |
| `test_logbuffer.py` | 500 条环形缓冲：写 5 万条仍只留 500；动态淘汰最旧；运行时改容量；多线程安全 | **日志只保留 500 条、动态删除** |
| `test_scheduler.py` | 循环轮次、间隔热更新、下次执行时间、停止响应、时间窗口、并发启动保护 | **定时任务 / 间隔可配** |
| `test_carriers.py` | 9 个平台变体、`EO_NETWORK` 推导、`EO_PLATFORMS` 优先级、DNS 记录互不覆盖 | **三台机器各跑各的（联通/电信/移动）** |
| `test_web_console.py` | 鉴权、变量来源、配置读写、DNS 面板条目、进度与阶段、停止、间隔热更新、日志 API | **网页管理配置/进度/日志/DNS/任务状态** |
| `docker_integration.sh` | 构建镜像、容器启动与降权、挂载写入、Web 接口、真实扫描、停止 | **Docker 部署**（需要 Docker） |

## Docker 集成测试

```bash
bash tests/docker_integration.sh
```

会在本机构建镜像并起容器，测试完自动删除容器。
需要本机可拉取基础镜像（境内可用 `SET_REGISTRY_MIRROR=1` 配合 `docker.1ms.run` 等加速）。

## 说明

* 测试全部使用**保留地址**（`192.0.2.0/24`、`198.51.100.0/24`）或本地服务，
  不会真的去扫线上节点，也不会改任何 DNS 解析。
* `test_web_console.py` 会在本地起一个临时 Web 服务（端口 18801）并自动关闭。
* 若失败，输出里会列出具体失败项，便于定位。
