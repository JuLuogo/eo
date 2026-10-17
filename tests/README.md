# 测试说明

本目录是项目的回归测试套件，覆盖限速、日志、调度、多运营商配置、Web 控制台、
DNS 下发安全性、通知与网段解析。

**除 Docker 集成测试外，全部不需要网络、不需要 Docker**，可在任何机器上跑。
测试只用保留地址（`192.0.2.0/24` 等）与本地服务，**不会扫线上节点、不会改 DNS**。

## 快速运行

```bash
cd /opt/edge-optimizer          # 仓库根目录
python tests/run_all.py         # 全部套件（约 4 分钟）
python tests/run_all.py dns ratelimit   # 只跑名字含这些关键字的套件
```

也可以单独跑某一个：

```bash
python tests/test_dns_sync.py
```

用 pytest（若能安装）：

```bash
pip install pytest && pytest tests/ -v
```

## 套件一览

| 文件 | 覆盖内容 | 对应需求 |
| --- | --- | --- |
| `test_ratelimit.py` | 令牌桶：单线程/8 线程下任意 1 秒窗口不超过 rate；空闲后不突发；边界值 | **每秒 TCP 连接不超过 100** |
| `test_ratelimit_e2e.py` | 真实探测路径上验证限速生效（保留地址 + 低 rate 实测） | 同上（端到端） |
| `test_logbuffer.py` | 500 条环形缓冲：写 5 万条仍只留 500；动态淘汰最旧；运行时改容量；多线程安全 | **日志只保留 500 条、动态删除** |
| `test_scheduler.py` | 循环轮次、间隔热更新、下次执行时间、停止响应、时间窗口、并发启动保护 | **定时任务 / 间隔可配** |
| `test_carriers.py` | 9 个平台变体、`EO_NETWORK` 推导、`EO_PLATFORMS` 优先级、DNS 记录互不覆盖 | **三台机器各跑各的（联通/电信/移动）** |
| `test_web_console.py` | 鉴权、变量来源、配置读写、DNS 面板条目、进度与阶段、停止、间隔热更新、日志 API | **网页管理配置/进度/日志/DNS/任务状态** |
| `test_frontend_static.py` | 内嵌 JS 语法（node --check）、`getElementById` 是否都有对应元素、JS 调用的 API 是否都有服务端路由、按钮是否绑定事件 | **网页点了要有反应** |
| `test_dns_sync.py` | 批量拆分、就地更新优先（无空窗期）、删除多余、骤降保护阈值、参数校验、dry-run 不调 API、**网络异常不误改线上**、分页、名称精确匹配 | **DNS 下发不能写坏线上解析** |
| `test_dns_cloudflare.py` | Cloudflare 分支（与华为云是两套独立代码）：HTTP/业务错误映射、先建后删、**新建失败绝不删旧记录**、分页取全、重复/空 IP 去重、删除失败上报、provider 路由、dry-run 零请求、骤降保护 | 同上（Cloudflare 服务商） |
| `test_notify.py` | 飞书/企业微信/通用格式识别、业务错误码（HTTP 200 但 code!=0）、超时与非法 URL | 告警能真正送达 |
| `test_ranges.py` | CloudFront/Cloudflare 网段解析、ESA BGP 前缀与中国大陆剔除（含区间合并） | 网段拉取正确 |
| `test_cli_e2e.py` | scan 落盘、monitor 多轮、时间窗口、`--targets` 校验、退出码、运营商选择（`-n`/`-p`/`EO_NETWORK`）、`-p`/`-n` 冲突处理 | **命令行可用** |
| `test_shutdown.py` | 真起进程发 SIGTERM/SIGINT：monitor 与 web 都要在宽限期内**退出码 0**（不是被强杀）、端口释放；结果文件**原子替换**（模拟中断后旧文件完好） | **docker stop 不能把结果写坏** |
| `test_resources.py` | 按可用内存/核数计算并发上限、`EO_MAX_CONCURRENT` 覆盖、按平台数摊分、接线到 `scan_platforms`（平台对象不被就地修改） | **小内存机器不能被自己的并发压垮** |

## Docker 集成测试

```bash
bash tests/docker_integration.sh
```

在本机构建镜像、起容器、跑一轮真实扫描，测试完自动删除容器、镜像与临时目录。
脚本用自身位置推导仓库根，在任意目录都能执行。需要本机可拉取基础镜像
（境内可先 `SET_REGISTRY_MIRROR=1`，脚本也会自动尝试 `docker.1ms.run` 等加速源）。

## 说明

* `test_frontend_static.py` 会调用 `node --check` 校验页面内嵌 JS 的语法；没有 node 时自动跳过该步
  （其余检查照常执行）。查找顺序：环境变量 `NODE_BIN` → `PATH` 里的 `node`/`nodejs` →
  常见位置（`/usr/local/bin/node`、WSL 下各盘符 `/mnt/<盘>/Program Files/nodejs/node.exe` 等）。
  想强制启用就 `NODE_BIN=/path/to/node python3 tests/test_frontend_static.py`。
  该步自带反向自测：会用一段故意写错的 JS 确认校验器真的能报错，避免「永远通过」的假绿。
* `test_web_console.py` 会在本地起一个临时 Web 服务（端口 18801）并自动关闭。
* `test_cli_e2e.py` 会真的调用 `run.py`，结果文件写在仓库的 `results/`（已被 gitignore）；
  用 `--no-write` 的用例不落盘。
* `test_dns_sync.py`、`test_dns_cloudflare.py` 与 `test_notify.py` 全部使用假客户端 /
  假 HTTP 层 / 本地 HTTP 接收端，**不会向任何真实 DNS 或 webhook 发请求**。
* `test_shutdown.py` 会真的起 `run.py` 子进程并发送 SIGTERM/SIGINT，因此**只在
  Linux / WSL / 容器里有意义**；Windows 上会自动跳过（SIGTERM 等于强制终止）。
* 失败时输出会列出具体失败项，便于定位。

## 测试本身是否可信

「全绿」也可能是测试失效导致的假绿。为排除这一点，套件做过**故障注入**验证：
把代码故意改坏（还原历史 bug、破坏限速、去掉日志封顶、让运营商过滤失效等），
确认对应套件确实会变红并返回非零退出码。已覆盖：

| 注入的故障 | 被哪个套件抓到 |
| --- | --- |
| 还原「先取默认启用再按运营商筛」→ `-n dx` 得空列表 | `test_carriers.py`（8 条失败） |
| 让 `-n` 运营商过滤整体失效（返回全部 9 个） | `test_carriers.py`（7 条失败） |
| HTML 缺少 JS 引用的 id | `test_frontend_static.py` |
| 内嵌 JS 语法错误 | `test_frontend_static.py` |
| 日志环形缓冲不封顶（无限增长） | `test_logbuffer.py`（8 条失败） |
| 限速器不生效 / 允许突发 | `test_ratelimit.py`（8 条失败） |
| DNS 查询失败时返回空列表（历史上会写重复记录） | `test_dns_sync.py`（4 条失败） |
| Cloudflare 分支：新建失败仍删旧记录（会把解析清空） | `test_dns_cloudflare.py` |
| Cloudflare 分支：去掉分页（250 条只读到 100 条 → 重复解析） | `test_dns_cloudflare.py` |
| Cloudflare 分支：重复/空 IP 不去重 | `test_dns_cloudflare.py` |

注：`test_carriers.py` 第 6 节刻意**不传** `include_disabled=True`，
因为真实 CLI 走的是默认值；传了会让测试通过但掩盖 `-n dx` 返回空列表的 bug。

## 已知未覆盖（诚实说明）

* **真实 DNS 下发从未对线上记录执行过**。全部验证都走假客户端 / 假 HTTP 层，
  只证明了「逻辑与参数正确」，没有证明「对你的域名和密钥确实能改成功」。
  首次真实下发请先用 `--dry-run` 或 Web 上的「DNS 预览」。
* 网段拉取（`test_ranges.py`）只验证**离线解析**正确，不验证线上接口返回的内容是否变过。

