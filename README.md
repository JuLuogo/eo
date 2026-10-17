# 多平台边缘节点优选工具

在原单平台脚本的基础上通用化：**一次运行可同时为 EO / ESA / CFT 等多个平台筛选可用节点**，
并可直接把结果批量下发到各自的 DNS 记录。支持按运营商（联通/电信/移动）分开优选。

---

## 🚀 快速开始

### 方式一：Docker（推荐）

镜像由 GitHub Actions 构建好，服务器上**不需要编译**。两步。

**第 1 步：拿到镜像**（三选一，能通 GitHub 就用第一种）

```bash
# ① 能连 ghcr.io：直接拉
docker pull ghcr.io/juluogo/eo:latest

# ② 拉不动 ghcr.io：下离线包（GitHub 文件走国内代理，55MB）
curl -fL https://gh-proxy.com/https://github.com/JuLuogo/eo/releases/download/image-latest/eo-image.tar.gz \
  | gunzip | docker load

# ③ 已经有 tar 文件：gunzip -c eo-image.tar.gz | docker load
```

②③ 加载完同样得到 `ghcr.io/juluogo/eo:latest`（还有一个短名 `edge-optimizer:latest`），
所以下面的运行命令三种方式通用。

**第 2 步：运行**（下面的环境变量按你的情况改，注释里写了含义）

```bash
docker run -d --name edge-optimizer \
  --restart unless-stopped \
  -p 8787:8787 \
  -e EO_NETWORK=lt \
  -e EO_WEB_PORT=8787 \
  -e EO_WEB_TOKEN=改成你自己的随机串 \
  -e EO_RATE=100 \
  -e EO_INTERVAL=300 \
  -e EO_DNS_SYNC=none \
  -e EO_CONFIRM_DNS=no \
  -e HUAWEI_AK= \
  -e HUAWEI_SK= \
  -e TZ=Asia/Shanghai \
  -v /opt/eo/config:/app/config \
  -v /opt/eo/results:/app/results \
  -v /opt/eo/targets:/app/targets \
  ghcr.io/juluogo/eo:latest
```

| 变量 | 说明 |
| --- | --- |
| `EO_NETWORK` | **这台机器的运营商**：`lt`=联通 / `dx`=电信 / `yd`=移动 |
| `-p 8787:8787` | 宿主机端口:容器端口，两边要一致。三台机器用 8787/8788/8789 |
| `EO_WEB_TOKEN` | 网页访问令牌，自己定一个（留空会自动生成并打印在日志里） |
| `EO_WEB_AUTH` | `off` = **关闭令牌校验**（内网/单机可关，任何能访问端口的人都能操作）；默认开启 |
| `EO_RATE` | 全平台合计 TCP 连接上限/秒（默认 100）。注意这是**新建速率**，不是并发数 |
| `EO_MAX_CONCURRENT` | **同时运行的探测数上限**。留空自动按内存+核数算（4 核约 32）。小机器被跑满就设 `16`；设 `1` 就是完全串行 |
| `EO_MAX_MS` | 只留连接耗时低于该毫秒数的节点（等于 `--max-ms`） |
| `EO_TOP` | 只保留最快的 N 个节点（等于 `--top`） |
| `EO_INTERVAL` | 每轮间隔秒数（默认 300，网页上也能改） |
| `EO_DNS_SYNC` | `none` 只优选 / `dry-run` 预览 / `push` 真下发（还要 `EO_CONFIRM_DNS=yes`） |
| `HUAWEI_AK/SK` | 自动下发 DNS 才需要；不填也能做优选 |
| `-v ...:/app/targets` | 网段目录。**空目录会自动补齐**，不用手动准备 |
| `-p 127.0.0.1:8787:8787` | 只想本机访问就改成这样（再配 SSH 隧道），公网不可达 |

然后：

```bash
docker logs -f edge-optimizer                 # 实时日志（访问地址与令牌都在里面）
docker restart edge-optimizer                 # 重启（改环境变量后需要重建容器，见下）
docker stop edge-optimizer                    # 优雅停止（SIGTERM，约 5 秒，退出码 0）
docker rm -f edge-optimizer                   # 删除容器（挂载目录里的配置和结果不受影响）
```

改环境变量需要先删容器再 `docker run`（docker 不支持改已运行容器的环境变量）：

```bash
docker rm -f edge-optimizer   # 然后重新执行上面的 docker run
```

> 打开 `http://<服务器IP>:8787/?token=<你设的令牌>`。
> 想用 compose 管理就 `docker compose up -d`（仓库里有 `docker-compose.yml`）。
> ⚠️ 但**不要用 `docker compose up -d --build`**：Compose 的 build 要求 buildx ≥ 0.17，
> Debian/Ubuntu 自带的常常只有 0.13，会直接报 `compose build requires buildx 0.17.0 or later`。
> 本机要重建编译用 `./rebuild.sh`。

### 方式二：裸机 systemd（不用 Docker）

```bash
curl -fsSL https://raw.githubusercontent.com/JuLuogo/eo/main/install.sh | sudo sh
```

会依次完成：检查依赖 → 拉取代码 → 建虚拟环境 → 装 Python 包 → 装 systemd 服务。
过程中只问你两个问题：

```
这台机器是哪个运营商？
  1) 联通 (lt)  <= 自动识别结果
  2) 电信 (dx)
  3) 移动 (yd)
Web 控制台监听端口（回车用 8787）:
```

然后三步收尾：

```bash
cd /opt/edge-optimizer
vi .env                       # 填 HUAWEI_AK / HUAWEI_SK（下发 DNS 必需）
./deploy.sh doctor            # ★ 环境体检：确认本机链路没被代理接管
sudo ./deploy.sh start && ./deploy.sh status   # 启动并打印访问地址 + 令牌
```

浏览器打开 `http://<服务器IP>:8787/?token=<令牌>` 进入 Web 控制台。

> 📖 **完整安装说明见 [INSTALL.md](INSTALL.md)** —— 含手动安装、Docker 安装、
> 三台机器（联通/电信/移动）分工、常见问题排查。
> ⚠️ **部署前请看一眼 [INSTALL.md 的安全与隐患清单](INSTALL.md#十一安全与隐患清单)**
> （控制台对外暴露、ufw 挡不住 Docker 端口、凭据放哪等）。

### 已经 clone 了？

```bash
sudo ./install.sh            # 一键安装（本地版）
# 或者只做部署那一步：
sudo ./deploy.sh install     # 交互式配置 + 装 systemd
./deploy.sh status           # 看运营商、端口、访问地址、令牌
```

### 常用运维命令

```bash
./deploy.sh install                  # 安装（依赖自动装，只问运营商和端口）
./deploy.sh start / stop / restart   # 启动 / 停止 / 重启
./deploy.sh status                   # 状态 + 运营商 + 端口 + 访问地址 + 令牌
./deploy.sh logs                     # 实时日志（logs 200 = 最后 200 行）
./deploy.sh logs-err                 # 只看错误日志
./deploy.sh logs-save                # 导出全部日志到文件
./deploy.sh doctor                   # 环境体检
./deploy.sh scan                     # 按本机运营商跑一轮优选
./deploy.sh isp                      # 识别本机运营商
./deploy.sh config                   # 改运营商 / 端口（自动重启）
./deploy.sh uninstall                # 卸载（保留配置与结果）
sudo ./deploy.sh uninstall --purge   # 完全卸载（连配置、结果、密钥一起删）
```

日志与卸载的完整说明见 **[INSTALL.md](INSTALL.md)**（含 `journalctl` 原始命令、
Docker 卸载、手动彻底清理）。

### 装不动时看这里

| 报错 | 原因与解法 |
| --- | --- |
| `ensurepip is not available` / `Failing command: .venv/bin/python3` | Debian/Ubuntu 缺 `python3-venv`。脚本会自动装，失败就手动：`sudo apt install -y python3.13-venv`（版本号换成 `python3 -V` 显示的） |
| 反复提示 `Username for 'https://gitee.com'` | 仓库是公开的，不需要账号。是服务器上存了过期凭据，清掉即可：`git config --global --unset credential.helper && rm -f ~/.git-credentials` |
| `ConnectionResetError` / `Connection broken`（下载依赖时） | 境内访问 PyPI 被重置。脚本会自动换清华/阿里云/腾讯云/中科大镜像重试；全失败也不影响优选（只影响自动下发 DNS）。可加 `--no-dns` 直接跳过 |
| `./docker-setup.sh: Permission denied` | 文件权限位没带出来。`chmod +x *.sh`，或直接 `sh docker-setup.sh` |
| `env file .../.env not found` | 还没生成配置。先 `cp .env.example .env` 并改 `EO_NETWORK=lt`（或 dx/yd） |
| 拉取失败 | 脚本会自动改用压缩包下载；也可手动：`curl -fL https://github.com/JuLuogo/eo/archive/refs/heads/main.tar.gz \| tar xz --strip-components=1` |

更多见 [INSTALL.md 常见问题](INSTALL.md)。

### 自检与回归测试

```bash
python run.py selftest       # 离线自检（不需要网络，14 项）
python tests/run_all.py      # 跑全部回归套件（限速/日志/调度/运营商/Web）
```

`tests/` 覆盖：**每秒连接上限**、**日志 500 条环形缓冲**、**调度与间隔热更新**、
**三运营商配置**、**Web 控制台全接口**。测试全部使用保留地址与本地服务，
不会真的去扫线上节点、也不会改任何 DNS 解析。详见 [tests/README.md](tests/README.md)。

Docker 方式见 [INSTALL.md 第六节](INSTALL.md)。

---

## 一、原来的优选方法（改造前）

原项目其实是一个「**Anycast 边缘节点优选**」脚本，判定一个 IP 是否可用分两步：

1. **ICMP 预筛选**：`ping -n 1 -w 300 <ip>`，延迟超过 300ms 或不通直接淘汰；
2. **SNI 绑定探测**：用 `curl --resolve pcb.060730.xyz:443:<ip>` 把目标域名强行解析到该 IP，
   发真实 HTTPS 请求，**HTTP 返回 200 才算可用**。

通过的 IP 写入 `yes.txt`，再由 `updatedns.py` 覆盖写入华为云 DNS 记录
`eo-lt.1.060730.xyz`（删除旧记录集 → 按 50 个一批重建），从而实现「挑最快/可用节点做解析」。

```
eo.txt(网段) ──ping+curl──> yes.txt(可用IP) ──华为云API──> eo-lt.1.060730.xyz (A记录)
```

### 原实现的问题

| 问题 | 说明 |
| --- | --- |
| 只支持一个平台 | 目标域名、输入输出、DNS 记录名全部硬编码为 EO 那一套 |
| 平台判定不可复用 | 换 ESA/CFT 必须复制一份脚本再改代码 |
| ping 预筛选全是假阴性风险 | 机房屏蔽 ICMP，**实测会丢掉约 40% 的可用节点**（1065 → 625），且原脚本预筛选全空时就直接 0 结果 |
| 只看 ping 延迟 | DNS 实际走的是 TCP/TLS，ping 低不代表连接快 |
| 无重试 | 大并发下边缘节点偶发丢包/限流，一次失败就被永久淘汰 |
| 无历史记录 | 无法观察节点漂移 |
| DNS 下发逻辑内嵌在脚本 | 与优选耦合，无法按平台分别下发 |
| **emoji 会让脚本崩掉** | 控制台按 GBK 编码时 `✅/❌` 抛 `UnicodeEncodeError`，整轮优选白跑 |
| DNS 下发「先删光再重建」 | 删旧建新之间域名有一段时间**完全没有解析** |
| 改解析没有确认与熔断 | 网段填错/网络抖动时会把几百个 IP 的记录直接缩成几个 |

---

## 二、改造后的结构

```
eo/                                    ← 项目根目录
├── run.py                  ← 统一入口（web / scan / monitor / test / list / dns-check / doctor / selftest / ranges）
├── updatedns.py            ← DNS 下发入口（兼容老用法，支持按平台）
├── eo/                     ← 核心包
│   ├── core.py             平台无关引擎：网段展开 / ping / curl 探测 / 并发扫描 / 落盘 / 链路自检
│   ├── platforms.py        ★ 平台配置：host、网段文件、输出文件、DNS 记录（改这里加平台）
│   ├── runner.py           可复用运行层：RunConfig / 单轮执行 / 可中断的循环（CLI 与 Web 共用）
│   ├── config_store.py     运行配置持久化（config/config.json，密钥掩码）
│   ├── webui.py            Web 服务：配置 API、运行控制、实时日志、SSE 推送
│   ├── pages.py            Web 页面（内嵌 HTML/JS，无外部依赖）
│   ├── dns.py              华为云 DNS 批量下发（就地更新、按平台独立、缩容保护）
│   ├── dns_cloudflare.py   Cloudflare DNS 下发（CloudFront 场景常用）
│   ├── ranges.py           官方网段拉取（CloudFront / Cloudflare）
│   ├── esa_ranges.py       ESA 网段发现（HE ASN + RIPEstat 前缀 − APNIC 中国大陆）
│   ├── ratelimit.py        全局令牌桶限速（默认 100 连接/秒，全平台共用）
│   ├── notify.py           异常通知（飞书 / 企业微信 / 通用 webhook）
│   ├── cli.py              命令行编排
│   └── __init__.py         对外 API
├── targets/                ← 各平台要扫描的网段
│   ├── eo.txt              手工维护
│   ├── esa.txt             ESA：esa-ranges 自动生成（2191 个境外网段）
│   └── cft.txt             CloudFront：ranges 自动生成（211 个官方网段）
├── results/                ← 各平台结果，互不覆盖
│   ├── eo.txt  esa.txt  cft.txt
│   └── history/*.jsonl     每轮快照，可追踪节点漂移
├── config/config.json      ← Web 控制台保存的配置（密钥 0600，勿提交）
├── Dockerfile              ← 容器镜像（含 curl、时区、非 root 运行、healthcheck）
├── docker-compose.yml      ← 一键部署（端口 / 限速 / 卷挂载 / 重启策略）
├── deploy.sh               ← 裸机部署脚本（systemd：install/start/status/logs/doctor）
├── docker-entrypoint.sh    ← 把环境变量翻译成命令行参数
├── .env.example            ← 密钥与运行参数模板（复制成 .env）
└── README.md
```

**核心改动**：把「目标站点」从代码里抽成 `Platform` 配置对象，
于是同一批 IP 可以用不同平台的域名并行探测 —— 这就是多平台同时优选的关键。

```python
@dataclass
class Platform:
    key: str            # eo-lt / esa-dx / cft-yd（平台 + 运营商）
    name: str           # 显示名
    network: str        # ★ 运营商：lt=联通 / dx=电信 / yd=移动
    host: str           # ★ 校验域名（CNAME 到该平台边缘节点）
    port / scheme / path / status_codes / timeout
    ping_timeout_ms / precheck_ping / retries / workers
    sample_per_24       # 稀疏大网段的粗扫抽样粒度
    targets_file        # 该平台扫描哪些网段
    output_file         # 结果写到哪
    history_file        # 历史记录
    dns                 # 该平台的 zone / 记录名 / AK
```

---

## 三、快速开始

```bash
python run.py list                 # 看平台清单（含启用状态）
python run.py                      # 扫描全部已启用平台
python run.py -p eo                # 只扫 EO
python run.py scan -p eo esa cft   # 同时扫三个平台
python run.py scan -p eo --json out.json --with-latency
python run.py dns-check -p eo      # ★ 只读查看 DNS 记录当前解析（安全，推荐先跑）
python run.py test -p eo --ip 43.174.55.152 43.175.146.78   # 诊断单个 IP
python run.py selftest             # 离线自检（不联网，验证核心与 DNS 逻辑）
```

输出示例：

```
==============================================================
第 1 轮：同时扫描 3 个平台
==============================================================
  - EdgeOne(eo) -> https://pcb.060730.xyz:443/ 期望 200，超时 3s，重试 1 次
  - ESA(esa) -> https://esa.example.com:443/ 期望 200，超时 3s，重试 1 次
...
● EdgeOne: 8/7366 可用 (213.4s)
  结果 -> ...\results\eo.txt
  最快节点: 43.174.55.152(182ms), 43.174.55.156(229ms), ...
多平台覆盖情况（同一 IP 可被多个平台命中）：
  ★ 通用 43.174.55.152     命中 2/2: eo, esa
```

### 结果文件格式

默认与原来完全一致（一行一个 IP，按延迟从快到慢排序），可直接给 `updatedns.py` 用。
加 `--with-latency` 会写成 `IP<TAB>延迟ms`，方便人工挑选。

---

## 四、按运营商区分（联通 / 电信 / 移动）

同一个平台的节点，在联通、电信、移动网络里的最优选择是不一样的，
所以 DNS 记录也按运营商分开，例如 EdgeOne：

```
eo-lt.1.060730.xyz   ← 联通（原 eo-lt 就是「联通」的意思）
eo-dx.1.060730.xyz   ← 电信
eo-yd.1.060730.xyz   ← 移动
```

代码里每个平台会自动生成三个变体（`eo-lt` / `eo-dx` / `eo-yd`），
它们共用同一套网段文件和校验域名，只有 `network` 和 DNS 记录名不同：

```bash
python run.py list --all-platforms      # 看全部变体与各自的 DNS 记录
python run.py scan --network lt         # 只跑联通那一套
python run.py scan --network dx yd      # 电信 + 移动（可多选）
python run.py scan -p eo-lt esa-dx      # 精确指定某个变体
```

> 被**明确点名**的平台一律视为启用：`-n dx`、`-p esa-dx`、`EO_NETWORK=dx`、
> `EO_PLATFORMS=eo-dx` 都会直接开跑，不受代码里 `enabled` 默认值影响。
> 只有「什么都没指定」时才回退到代码里默认启用的 `lt` 三件套。

### ★ 关键：运营商不是代码区分的，而是「在哪台机器上跑」

延迟必须从**该运营商网络内部**测量才有意义。所以正确用法是：

| 机器 | 命令 | 结果下发到 |
| --- | --- | --- |
| 联通服务器 | `python run.py scan --network lt --dns-sync push --yes` | `eo-lt` / `esa-lt` / `cft-lt` |
| 电信服务器 | `python run.py scan --network dx --dns-sync push --yes` | `eo-dx` / `esa-dx` / `cft-dx` |
| 移动服务器 | `python run.py scan --network yd --dns-sync push --yes` | `eo-yd` / `esa-yd` / `cft-yd` |

在联通机器上跑 `--network dx` 是没意义的 —— 测出来的还是联通视角的延迟。

用一键脚本部署时不用管这些：`EO_NETWORK` 会被写进 `.env`，程序据此自动选用
对应运营商的三件套（`EO_PLATFORMS` 只在想精确指定组合时才需要）。

> 想在一个更细的粒度上合并（比如「电信+联通」写进同一条记录），直接改
> `record_name` 即可，例如把 `eo-dx` 和 `eo-yd` 都指向同一条记录。

---

## 五、加一个新平台

### 5.1 Amazon CloudFront（CFT）—— 网段官方公开

```bash
python run.py ranges --source cloudfront --output targets/cft.txt
# cloudfront: 已写入 211 个网段 -> targets/cft.txt
python run.py ranges --source list          # 看支持哪些源
```

CloudFront 有个好性质：**边缘节点对所有分配域名一视同仁**，没有「高级/低级域名」
之分，所以随便用一个真实存在的 CloudFront 域名就能当校验目标。
默认用的是 AWS 自己的静态站点 `d1.awsstatic.com`（长期稳定），
你有自己的分配域名时替换掉 `CFT_PROBE_HOST` 即可。

配置在 `eo/platforms.py` 里，三个运营商变体会自动生成，只需 `enabled` 打开：

```python
CFT_PROBE_HOST = "d1.awsstatic.com"        # 或你自己的 xxx.cloudfront.net
```

> 官方网段里有 `/15`、`/16` 这种大段（总计 422 万地址），
> 所以 CFT 平台默认 `sample_per_24=1`，抽样后约 **2.2 万个 IP**，一轮约 4 分钟。

### 5.2 阿里云 ESA —— 网段靠 BGP 推导 + 排除中国大陆

ESA **不公布**节点网段，所以用「阿里云对外宣告的 BGP 前缀」减去「中国大陆地址段」：

```bash
python run.py esa-ranges --output targets/esa.txt
```

实测输出：

```
1) 发现阿里云 ASN …
  HE 搜索到 16 个 ASN，合并内置列表后共 17 个
2) 拉取各 ASN 宣告的前缀 …
  AS24429: 648 个 IPv4 前缀      AS37963: 791 个
  AS45102: 1149 个               AS134963: 260 个
  合计去重后 2800 个前缀
3) 拉取中国大陆地址段并排除 …
  APNIC 委派数据：中国大陆共 8793 段 IPv4 分配记录（合并后 4115 段）
  剔除中国大陆前缀 590 个
  跳过 19 个超大网段（>65536 地址）
4) 结论：可用于 ESA 优选的境外网段 2191 个，约 1419 万个 IP
```

三层数据源全部是公开接口，不依赖任何第三方数据库：

| 步骤 | 来源 | 说明 |
| --- | --- | --- |
| 发现 ASN | `bgp.he.net/search?search[]=alibaba` | 解析页面里的 ASN 列表（阿里云有十几个 ASN） |
| ASN → 前缀 | `stat.ripe.net/data/announced-prefixes` | RIPEstat 官方接口，稳定 JSON |
| 排除中国大陆 | `ftp.apnic.net/stats/apnic/delegated-apnic-latest` | APNIC 官方 RIR 委派文件，权威 |

前面给的启发式链接（`bgp.he.net/search?search[]=alibaba`）我也照做了，只是把
「ASN → 前缀」这一步换成了 RIPEstat，因为 HE 的页面是 HTML 表格、结构随时会变，
而 RIPEstat 有正式 JSON 接口。要改回 HE 也能用 `--asn` 手动指定。

**关键：1400 万 IP 必须抽样**，所以有 `--sample-per-24`：

```bash
# 粗扫：每 /24 取 1 个代表 IP -> 3.2 万个 IP，约 5~9 分钟一轮
python run.py scan -p esa --sample-per-24 1

# 每 /24 取 4 个（更不容易漏节点）-> 12.8 万个 IP，约 21~35 分钟一轮
python run.py scan -p esa --sample-per-24 4

# 深扫：以上一轮命中的 /24 为范围做全量扫描（第二轮用）
python run.py scan -p esa --deep
```

抽样规则（三条路径实测一致）：

| 网段大小 | 处理方式 |
| --- | --- |
| /16、/17…/23 | 切成 /24，每个 /24 取 N 个代表 IP（默认取 .1 / .64 / .128 / .192） |
| /24 | 取 N 个代表 IP |
| /25 ~ /32（深扫范围） | **整段保留**，不再抽样，避免丢掉目标 IP |

> 抽样是「找出哪些 /24 里有节点」的粗筛，所以第一轮务必用 `--sample-per-24`，
> 第二轮再对命中的 /24 用 `--deep` 全量扫。直接全量扫 1400 万 IP 按 100 连接/秒
> 要跑 40 小时，不现实。

**第 3 步**：把 `eo/platforms.py` 里 `esa` 的 `host` 换成你的 ESA 加速域名并按需
`enabled=True`（ESA 在国内一般也能访问，所以这条命令同样要跑 `doctor` 确认链路可信）。

### 4.3 其它平台

复制一段 dict，改 `key / name / host / targets_file / output_file` 就行。
也可以直接在 **Web 控制台** 里改，不用动代码（见第八节）。

---

## 六、持续监控

```bash
# 每 5 分钟一轮，只在 00:00-07:00 生效，跑 12 轮后退出
python run.py monitor --interval 300 --cycles 12 --window 00:00-07:00

# 每轮结果自动下发 DNS（危险操作需要 --yes 明确确认）
python run.py monitor --interval 600 --dns --yes

# 先看看会下发什么，不动线上解析
python run.py scan --dns --dry-run
```

* 每轮结果快照写入 `results/history/<平台>.jsonl`，可回溯节点变化；
* 可用节点少于 3 个会打印告警（`MIN_IPS_WARN`，在 `eo/cli.py` 中调整）；
* 突发扫描较耗带宽，`--window` 可把大动作放到低峰期。

---

## 七、DNS 下发

```bash
python updatedns.py --platform eo               # 读 results/eo.txt 下发到 eo-lt.1.060730.xyz
python updatedns.py --platform eo esa cft       # 每个平台各自下发到自己的记录
python updatedns.py -f yes.txt --record xxx.1.060730.xyz   # 老式单记录用法
python updatedns.py --platform eo --dry-run     # 预览（安全）
python updatedns.py --platform eo --yes         # 真正执行
```

支持的服务商：

| 服务商 | 说明 |
| --- | --- |
| `huawei` | 华为云 DNS，支持**就地更新记录集**（无解析空窗期），单记录集上限 50 个 IP |
| `cloudflare` | Cloudflare DNS，同名的多条 A 记录实现多 IP；需要 API Token（Zone.DNS 编辑权限） |
| `none` | 只优选不下发 |

安全设计（相比原 `updatedns.py` 的改进）：

* **就地更新，不产生解析空窗期**：原来的做法是「先把同名记录集全部删除，再重新创建」，
  删光和建好之间域名是**没有解析**的；现在优先用 `UpdateRecordSet` 原地替换记录集内容，
  只有记录集数量不够时才新建，最后才删除多余的那几个 —— 全程域名始终有解析；
* **解析量骤降保护**：例如线上现有 530 个 IP，而本次只选出 3 个（多半是网段填错或网络抖动），
  程序会直接中止并提示，需显式加 `--force` 才允许缩容；
* **未加 `--yes` 绝不修改线上解析**，只提示；
* 只处理**主机名完全相同**的记录，同 zone 下其他记录一律不动；
* IP 列表为空时**拒绝下发**（原脚本会先把旧记录删光再发现没 IP，导致解析中断）；
* `batch_size` 强制 1~50（华为云单记录集上限）；
* 更新/新建失败时打印旧记录内容，便于回滚；
* AK/SK 支持 `HUAWEI_AK` / `HUAWEI_SK` 环境变量覆盖，建议改用子账号密钥。

> ⚠ 首次使用前请先 `pip install huaweicloudsdkcore huaweicloudsdkdns`
> （本机原先并未安装，老 `updatedns.py` 一跑就会 `ModuleNotFoundError`）。
> 建议先跑只读的 `python run.py dns-check -p eo` 确认密钥与记录都对得上。

---

## 八、命令行参数速查

| 参数 | 说明 |
| --- | --- |
| `-p/--platform` | 选择平台，可多个；默认全部已启用（如 `eo-lt` / `esa-dx`） |
| `-n/--network` | ★ 按运营商筛选：`lt`=联通 / `dx`=电信 / `yd`=移动，可多选 |
| `--all-platforms` | 连 `enabled=False` 的平台一起处理 |
| `--targets FILE` | 统一网段文件，覆盖各平台自己的 targets_file |
| `--host` | 临时覆盖校验域名（调试新平台很方便） |
| `--status 200 301` | 放宽/收紧可接受的状态码 |
| `-w/--workers` | 并发数（默认 80） |
| `--rate N` | ★ 全平台合计 TCP 连接上限（默认 100/秒，0=不限速） |
| `--timeout` | 单次 HTTPS 探测超时秒数 |
| `--ping` | 启用 ICMP 预筛选（**默认关闭**，见「关于 ICMP」） |
| `--retries N` | 探测失败重试次数（默认 1） |
| `--sample-per-24 N` | ★ 粗扫抽样：每个 /24 只取 N 个代表 IP（ESA 必备，可把 1400 万压到 3.2 万） |
| `--deep` | 深扫：以上一轮命中 IP 所在的 /24 为范围做全量扫描 |
| `--max-ms MS` | **延迟门槛**：连接耗时超过这个毫秒数就丢弃（也可用 `EO_MAX_MS`） |
| `--top N` | **只保留最快的 N 个**；结果文件与 DNS 下发都只用这 N 个（也可用 `EO_TOP`） |
| `--with-latency` | 结果写成 `IP<TAB>延迟` |
| `--no-write` | 不写结果与历史（纯测试） |
| `--json FILE` | 额外输出 JSON 汇总，便于对接其它程序 |
| `--webhook URL` | 异常时推送通知（也可用环境变量 `EO_WEBHOOK`） |
| `--notify-always` | 每轮都推送通知，而不只在异常时 |
| `-q/--quiet` | 只输出汇总 |
| `--dns-sync push --yes` | 扫描后下发 DNS（必须显式确认） |
| `--dns-sync dry-run` | 只预览要下发什么 |
| `--force` | 忽略解析量骤降保护，允许大幅缩容 |

子命令：`scan`（默认）、`monitor`、`web`、`test`、`list`、`dns-check`、`doctor`、`selftest`、`ranges`。

调新平台的快捷姿势（先不写配置就能试）：

```bash
python run.py scan -p esa --host your-esa-domain.example --targets targets/esa.txt --no-write -q
```

---

## 九、同时监控多平台
一次运行可以同时为多个平台优选，各平台结果、历史、DNS 记录互不干扰：

```bash
python run.py scan -p eo esa cft        # 三个平台并行扫描
python run.py monitor -p eo esa         # 持续监控两个平台
python run.py scan                      # 不带 -p = 全部已启用平台
```

* 平台之间是**并行**的（每个平台一个线程池，共用一个全局限速桶），
  实测 254 个 IP 跑 3 个平台用时 17s，与单平台同量级，**不会线性叠加**；
* 结果分别写入 `results/<平台>.txt`，历史写入 `results/history/<平台>.jsonl`；
* 多平台时额外打印「同一 IP 命中几个平台」，方便决定是否复用节点：

```
多平台覆盖情况（同一 IP 可被多个平台命中）：
  ★ 通用 43.174.55.152     命中 3/3: eo, esa, cft
           43.174.55.179    命中 1/3: eo
```

---

## 十、部署到 Linux 服务器

三台机器（联通/电信/移动）**共用同一份代码/镜像，各跑各的**，互不干扰。

### 10.0 该用 Docker 还是脚本？

| | **脚本 + systemd**（推荐） | **Docker + compose** |
| --- | --- | --- |
| 首次部署 | `sudo ./deploy.sh install && ./deploy.sh start` | `./rebuild.sh` |
| 服务器依赖 | python3 + curl + venv | 只要 Docker |
| 升级 | `git pull && ./deploy.sh restart` | `git pull && ./rebuild.sh` |
| 看日志 | `./deploy.sh logs`（journalctl） | `docker compose logs -f` |
| 改配置 | 控制台 + `.env`，改完重启 | 控制台 + `.env` |
| 排查问题 | 直接看进程/文件，最直观 | 多一层容器，偶发问题要多绕一圈 |
| 资源占用 | 无额外开销 | 镜像几百 MB、多一层网络栈 |
| 隔离性 | 进程级 | 容器级（更强） |

**结论：三台机器各跑各的、只跑一个 Python 进程，用脚本 + systemd 更省事**——
`git pull` 一句就是升级，systemd 自带开机自启、崩溃重启、日志轮转。
Docker 的价值主要在「环境隔离」和「镜像分发」，你这个场景两者都不太需要。

两种方式在仓库里都备好了，可以随时换：

```bash
# 方式 A：脚本 + systemd（推荐）
sudo ./deploy.sh install      # 建 venv、装依赖、生成 .env、装 systemd 服务
vi .env                       # 填 HUAWEI_AK/SK，改 EO_PLATFORMS / EO_WEB_PORT
./deploy.sh doctor            # ★ 先确认链路可信（被代理接管会拒绝下发）
sudo ./deploy.sh start
./deploy.sh status            # 打印访问地址 + 令牌 + 健康检查

# 方式 B：Docker
cp .env.example .env && vi .env
./rebuild.sh && docker compose logs -f
```

### 10.1 三台机器怎么分工

```bash
# 每台机器上：
#   1) .env 里 EO_WEB_PORT 分别用 8787 / 8788 / 8789
#   2) 控制台里把自己那一套运营商的 enabled 打开（联通机开 *-lt，电信机开 *-dx，移动机开 *-yd）
#   3) EO_NETWORK 设成自己这台机器的运营商，供 ./deploy.sh scan 使用
```

```bash
./deploy.sh scan        # 等价于 python run.py scan --network lt -q
```

每台机器的 `results/` 与 `config/config.json` 都是本地的，互不影响；
只有 DNS 记录是共享的（各写各的运营商记录，不会撞车）。

### 10.2 用 Docker 的话

```bash
cp .env.example .env
./rebuild.sh
docker compose logs -f            # 启动日志里有访问地址和令牌
```

`docker-compose.yml` 里关键项：`EO_RATE`（限速）、`EO_INTERVAL`（间隔）、
`EO_WEB_PORT`（端口）、`config/`+`results/`+`targets/` 三个卷挂载、
`restart: unless-stopped`、`TZ`（时间窗口按本地时区判断）、健康检查。

---

## 十一、Web 控制台（推荐用这个管理）

```bash
sudo ./deploy.sh start                  # 脚本部署：启动服务
./deploy.sh status                      # 打印访问地址 + 令牌 + 健康检查

# 或直接前台跑（调试）
python run.py web --port 8787
```

打开 `http://<服务器IP>:8787/?token=<令牌>` 后可以管理：

| 区域 | 能做什么 |
| --- | --- |
| **常用变量**（置顶） | 并发上限、轮询间隔、时间窗口、并发数、超时、重试、日志条数、DNS 模式、通知地址。每项都标出**当前生效值来自哪里**（网页设置 / `.env` / 默认值），从此不用猜「我改了 .env 怎么没生效」 |
| **运行参数（细分）** | 同上字段的完整表单，外加 ICMP 开关、逐 IP 日志、每轮通知、启动即循环 |
| **平台设置** | 逐平台（含运营商变体）开关、校验域名、协议 http/https、网段文件、结果路径、期望状态码、粗扫抽样 |
| **DNS 下发** | 下发模式（关闭/预览/执行）、确认开关、DNS 服务商、域名、记录名、TTL、批量大小、AccessKey |
| **任务状态** | 运行状态、本轮类型（循环/手动）、已完成轮数、当前间隔、上轮结束/下轮开始时间、**倒计时**、时间窗口、当前平台、最近错误 |
| **测试进度** | 总进度条 + 每平台分项进度（已测/总共/可用） |
| **实时日志** | 增量推送的日志流，可暂停自动滚动、可清空，条数上限可调 |
| **运行控制** | 开始循环 / 停止 / 立刻跑一轮 / 预览下发 / 立即下发 |

### 11.1 定时任务

* **间隔在线可改**：循环在每轮开始前重新读取配置，控制台上把 300 秒改成 60 秒，
  **下一轮就按 60 秒走**，不用重启也不用停循环（正在跑的那一轮不受影响）；
* **时间窗口**：填 `00:00-07:00` 就只在低峰跑，支持跨零点（如 `23:00-06:00`）；
* **同时只跑一个任务**：循环运行期间点「立刻跑一轮」会被明确拒绝
  （提示「已有循环任务在执行」），不会排队堆积重复执行；
* **一次性任务**：空闲时点「立刻跑一轮」，任务状态里的「本轮类型」会显示
  「手动触发的一次性任务」，跑完自动回到空闲。

### 11.2 环境变量与网页的优先级

`.env`（或 compose 的 `environment`）里的 `EO_*` 变量**只在首次启动、
`config.json` 里还没有该字段时生效**；一旦你在网页上改过并存档，就以网页为准，
重启不会被环境变量覆盖回去。这样既能「用 .env 做初始配置」，又能「用网页随时调」。

支持的环境变量：

| 变量 | 作用 |
| --- | --- |
| `EO_MODE` | `web`（带控制台）/ `monitor`（纯后台循环）/ `scan`（跑一次） |
| `EO_WEB_PORT` | 控制台端口（三台机器建议 8787 / 8788 / 8789） |
| `EO_WEB_TOKEN` | 控制台访问令牌（留空自动生成并写入 config.json） |
| `EO_PLATFORMS` | 平台列表，如 `eo-lt,esa-lt,cft-lt` |
| `EO_NETWORK` | 本机运营商（供 `deploy.sh scan` 使用）：`lt` / `dx` / `yd` |
| `EO_INTERVAL` | 每轮间隔秒数 |
| `EO_WINDOW` | 时间窗口，如 `00:00-07:00` |
| `EO_RATE` | 全平台合计 TCP 连接上限（默认 100） |
| `EO_WORKERS` / `EO_TIMEOUT` / `EO_RETRIES` | 并发数 / 超时 / 重试 |
| `EO_LOG_BUFFER` | 实时日志保留条数（默认 500） |
| `EO_DNS_SYNC` | `none` / `dry-run` / `push` |
| `EO_AUTO_RUN` | 启动后是否自动开始循环 |
| `HUAWEI_AK` / `HUAWEI_SK` | 华为云 DNS 密钥 |
| `EO_WEBHOOK` | 异常通知地址 |

> 安全：控制台所有接口都要 Bearer Token；密钥回显为 `******`，只有填新值才覆盖；
> `config/config.json` 权限 600；改 DNS 需要「模式选执行」+「勾选确认」两个条件同时满足。
> 公网机器建议只监听 `127.0.0.1` 再用 SSH 隧道：
> `ssh -L 8787:127.0.0.1:8787 user@server`。

---

## 十二、连接限速（重要）

```bash
python run.py scan -p eo --rate 100      # 全平台合计 100 连接/秒（默认值）
python run.py scan -p eo --rate 30       # 更保守
python run.py scan -p eo --rate 0        # 不限速（不推荐在共享出口上使用）
```

实现方式是**令牌桶**，而不是「每秒重置计数器」：

* 令牌按 `1/rate` 秒均匀发放，因此**任意 1 秒滑动窗口内的连接数都不超过 rate**；
* 所有平台共用同一个桶，`--rate 100` 限的是**多平台合计**，不是每个平台各 100；
* 重试产生的连接同样计入限速，不会绕过；
* 实测精度：`rate=100` 下 8 线程抢 200 个令牌耗时 2.06s（≈97 连接/秒，符合预期）。

另外容器里还额外放了两道保险（`ulimit` / `nofile`），防止极端情况下打满文件描述符：

* `docker-compose.yml` 里 `ulimits.nofile` 设成 8192/16384；
* 裸机上可用 `ulimit -n 8192` 或 `LimitNOFILE=8192`（systemd）。

---

## 十三、异常通知（无人值守必备）

```bash
python run.py monitor --webhook 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxx'
# 或者用环境变量：export EO_WEBHOOK='...'
```

* 触发条件：某平台可用节点少于 3 个、扫描失败、DNS 下发失败；
* 加 `--notify-always` 则每轮都汇报（默认只在异常时发）；
* 已适配飞书自定义机器人、企业微信机器人和通用 `{"text": "..."}` 形态的 webhook；
* 顺便一提：本机装有 `dsh-feishu` 插件，需要的话也能从飞书机器人反向推送/查询。

---

## 十四、准确性保护：先跑 doctor（重要）

```bash
python run.py doctor
```

优选的原理是「把域名钉到指定 IP 去请求」。如果本机装了代理、透明网关，
或者运营商做了 TLS 劫持，钉过去的请求会被中间设备接管 —— 结果是
**任何 IP（哪怕是保留地址）都返回同样的 200**，优选就完全失去意义。

因此工具内置了两道防线：

1. **保留地址探针**：用 RFC5737 的保留地址（`192.0.2.1`、`198.51.100.7`，
   正常永远不可达）去请求目标域名。若它们竟然返回 200，说明链路被接管，
   程序会打印醒目告警，并且**拒绝下发 DNS**。
2. **结果合理性判断**：通过率 > 95% 且中位延迟 < 5ms 时判定结果不可信
   （真实环境一个 /24 往往只有个位数节点可用），同样跳过 DNS 下发。

`doctor` 会逐项检查：curl 是否可用、网段文件是否为空、链路是否可信、
目标域名用默认解析是否返回 200、ICMP 是否可用。

> **本机实测：这台 Windows 机器的链路就是不可信的** —— 把 `pcb.060730.xyz`
> 钉到 `203.0.113.7`（保留地址）竟然返回 `200`，连接耗时 1.8ms，
> `tracert 1.1.1.1` 第一跳就是 1.1.1.1。所以本机跑出来的
> 「4094/4094 可用、延迟 1ms」是假的，**不要拿它去改线上解析**。
> 请把容器部署到干净的 Linux 服务器上，用 `docker compose run --rm optimizer doctor`
> 确认链路可信后再启用 `EO_DNS_SYNC=push`。

排查建议：

```bash
# 1) 本机是否有代理在跑
ss -ltnp | grep -E '7890|1080|8080'      # 常见代理端口
env | grep -i proxy

# 2) 容器内确认（容器走宿主机网络，宿主机代理同样会影响容器）
docker compose run --rm optimizer doctor

# 3) 手工验证：用保留地址钉域名，正常应该是连接失败
curl -s -o /dev/null -w '%{http_code} %{remote_ip}\n' --max-time 5 \
  --resolve pcb.060730.xyz:443:203.0.113.7 https://pcb.060730.xyz
```

---

## 十五、注意事项

* **判断 host 是否正确**：本机用默认 DNS 访问 `https://<host>` 必须返回 200；
  如果返回 404/403，说明该域名不是这个平台的校验域名，优选结果会假阴性。
* **关于 ICMP（重要，已改变默认值）**：实测这些边缘节点对 ping 的响应极不稳定 ——
  同一个 IP 在连续三次测试里 ping 结果时通时不通，而 HTTPS 三次都是 200。
  2000 个 IP 的对照实验：`ping+TLS` 只找到 4 个可用节点，`纯 TLS` 找到 13 个，
  且纯 TLS 反而**更快**（ping 预筛选本身花了 724s，纯 TLS 全量只要 213s）。
  因此 **ping 预筛选默认关闭**，需要时用 `--ping` 显式打开。
* 实测边缘节点会返回 `418/404/000`，只有真正为该域名提供服务的节点才回 `200`，
  所以「只有个位数/个位数百分比通过」是正常现象。
* 华为云 DNS 的 `TTL` 默认 1 秒，方便快速切换；若担心解析抖动可调大。
* 大规模扫描（7000+ IP）单平台约 4 分钟；多平台并行总耗时接近单平台，不线性叠加。
* 中文控制台（GBK）下所有输出都做了编码兜底，`✅/❌` 会降级成 `?/!` 而**不会中断**优选。

---

## 十六、改造后实测数据（本机）

| 项目 | 结果 |
| --- | --- |
| EO 全量扫描（纯 TLS） | 7366 个 IP，链路正常时约 600~1100 个可用，耗时 230~420s |
| 单 /24 扫描 | 254 个 IP → 约 2~3 个可用，耗时约 10~15s |
| 三平台并行（同 host 验证） | 254 个 IP 用时 17s，与单平台同量级 |
| 线上 DNS 现状 | `eo-lt.1.060730.xyz` 现有 **11 个记录集 / 530 个 IP** |
| ICMP 对照实验（2000 IP） | ping+TLS 4 个 / 纯 TLS 13 个；ping 阶段额外耗时 724s |
| 参数基准（1400 IP） | `t3 r1 w80` 检出 6 个 / 98s；`t2 r0 w80` 检出 4 个 / 36s |
| 令牌桶精度 | `rate=100`，8 线程抢 200 令牌 → 2.06s（≈97 连接/秒） |

结论：**默认 `timeout=3 / retries=1 / workers=80 / 不启用 ping`**，
比原逻辑多检出约 **70%** 的可用节点（1065 vs 625），
代价是全量耗时从 227s 增加到 422s。追求速度可加
`--timeout 2 --retries 0`（约快 3 倍，但会漏掉一部分节点）；
想省时间又想用 ping 预筛选，可加 `--ping`（快，但会丢掉约 40% 的可用节点）。

