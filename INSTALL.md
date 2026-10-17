# 安装与运维指南

**一键安装（推荐）**：

```bash
curl -fsSL https://raw.githubusercontent.com/JuLuogo/eo/main/install.sh | sudo sh
```

装完就能用。依赖（Python / curl / git / venv / pip 包）脚本会自己搞定，你只需要回答
两个问题：**这台机器是哪个运营商**、**Web 用哪个端口**。

---

## 目录

- [一、一键安装](#一一键安装推荐)
- [二、安装后做什么](#二安装后做什么)
- [三、常用命令速查](#三常用命令速查)
- [四、查看日志](#四查看日志)
- [五、完全卸载](#五完全卸载)
- [六、Docker 安装](#六docker-安装最省事推荐)
- [七、手动安装](#七手动安装不用一键脚本也不装-docker)
- [八、三台机器分工](#八三台机器联通电信移动怎么部署)
- [九、境内网络问题](#九境内网络问题github--镜像拉取)
- [十、常见问题](#十常见问题)
- [十一、安全与隐患清单](#十一安全与隐患清单)

---

## 一、一键安装（推荐）

### 1.1 Docker（推荐）

镜像由 GitHub Actions 构建好，服务器上**不需要编译**。两步。

**第 1 步：拿到镜像**（三选一，能通 `ghcr.io` 就用第一种）

```bash
# ① 直接拉（服务器能连 ghcr.io 时最快）
docker pull ghcr.io/juluogo/eo:latest

# ② 拉不动 ghcr.io：下离线镜像包（走国内 GitHub 文件代理，55MB，实测 2~27s）
curl -fL https://gh-proxy.com/https://github.com/JuLuogo/eo/releases/download/image-latest/eo-image.tar.gz \
  | gunzip | docker load

# ③ 已经有 tar 文件在本地
gunzip -c eo-image.tar.gz | docker load
```

②③ 加载完得到的镜像名与 ① 完全一样：`ghcr.io/juluogo/eo:latest`
（另外还带一个短名 `edge-optimizer:latest`）。**镜像内容同源，下面的运行命令通用。**

> 离线包由工作流在每次推送到 main 时自动更新，地址固定：
> `https://github.com/JuLuogo/eo/releases/download/image-latest/eo-image.tar.gz`
> 首个请求可能较慢（代理回源取 GitHub），之后有缓存会很快。

**第 2 步：运行**（环境变量按你的情况改）

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
| `EO_NETWORK` | **这台机器的运营商**：`lt`=联通 / `dx`=电信 / `yd`=移动（决定 `-lt/-dx/-yd` 三套平台与 DNS 记录） |
| `-p 8787:8787` | 宿主机端口:容器端口，两边必须一致。三台机器分别用 8787 / 8788 / 8789 |
| `EO_WEB_TOKEN` | 网页访问令牌，自己定；留空会自动生成并打印在日志里 |
| `EO_RATE` | 全平台合计 TCP 连接上限/秒（默认 100）；`0`=不限速 |
| `EO_INTERVAL` | 每轮间隔秒数（默认 300，网页上也能随时改，下一轮生效） |
| `EO_WINDOW` | 时间窗口，如 `00:00-07:00`；留空=全天 |
| `EO_DNS_SYNC` | `none` 只优选 / `dry-run` 只预览 / `push` 真下发（还需 `EO_CONFIRM_DNS=yes`） |
| `HUAWEI_AK` / `HUAWEI_SK` | 自动下发 DNS 才需要；不填也能正常优选 |
| `-v .../targets` | 网段目录，**空目录会自动从镜像内置补齐**，不用手动准备 |
| `-p 127.0.0.1:8787:8787` | 只想本机访问就改成这样（再配 SSH 隧道），公网不可达 |

启动后日志里会有访问地址与令牌：

```bash
docker logs -f edge-optimizer
```

```
  ==========================================================
    Web 控制台已启动
  ==========================================================
    监听地址   : http://0.0.0.0:8787
    访问令牌   : xxxxxxxxxxxxxxxxxxxxxxxx
    带令牌访问 : http://<服务器IP>:8787/?token=xxxxxxxxxxxxxxxxxxxxxxxx
```

常用操作：

```bash
docker logs -f edge-optimizer       # 实时日志
docker stop edge-optimizer          # 优雅停止（SIGTERM，约 5 秒，退出码 0）
docker restart edge-optimizer       # 重启
docker rm -f edge-optimizer         # 删除容器（挂载目录里的数据不受影响）
```

**改环境变量**必须先删容器再 `docker run`（Docker 不支持修改已运行容器的环境变量）：

```bash
docker rm -f edge-optimizer && docker run -d ...   # 重新执行上面的 docker run
```

**自动下发 DNS**：把 `HUAWEI_AK` / `HUAWEI_SK` 填进 `docker run` 的 `-e`，
先设 `EO_DNS_SYNC=dry-run` 验证记录名与网段，确认无误后再改 `push` +
`EO_CONFIRM_DNS=yes`。详见 [十一、安全与隐患清单](#十一安全与隐患清单)。

**想用 Compose 管理**：仓库里有 `docker-compose.yml`，`cd` 进去 `docker compose up -d` 即可，
配置写在同目录 `.env` 里。注意：

> ⚠️ **别用 `docker compose up -d --build`**：Compose 的 build 要求 buildx ≥ 0.17，
> 而 Debian / Ubuntu 源里的 `docker.io` 自带的多是 0.13，会直接失败：
> `compose build requires buildx 0.17.0 or later`。
> 需要在服务器上编译时用 `./rebuild.sh`（经典 `docker build` + `up -d --no-build`）。

> 另一种交互式脚本（会自动识别本机运营商、问你端口、生成 `.env`）：
>
> ```bash
> DIR=/opt/eo EO_NETWORK=lt HUAWEI_AK=... HUAWEI_SK=... \
> bash -c "$(curl -fsSL https://raw.githubusercontent.com/JuLuogo/eo/main/onekey.sh)"
> ```
>
> 支持 `USE_GHCR=1 IMAGE=ghcr.io/juluogo/eo:latest` 走预构建镜像。
> 不过 `onekey.sh` 从 GitHub raw 拉取，境内可能要先过一层文件代理。

**网段文件**：`targets/` 是挂载目录。空的也没关系 —— 容器启动时会从镜像内置的种子补齐
`eo.txt` / `esa.txt` / `cft.txt`，**已有文件不会被覆盖**；想关掉就设 `EO_SEED_TARGETS=0`。

### 1.2 裸机安装（不用 Docker）

```bash
curl -fsSL https://raw.githubusercontent.com/JuLuogo/eo/main/install.sh | sudo sh
```

依赖（Python / curl / git / venv / pip 包）脚本会自己搞定，你只需要回答两个问题：
**这台机器是哪个运营商**、**Web 用哪个端口**。

输出示例：

```
[install] 检查系统依赖…
[install] Python: 3.13.5               ← 缺 Python/curl/git/venv 会自动安装
[install] 拉取代码到 /opt/edge-optimizer
[install] 正在识别本机运营商（联通/电信/移动）…
    公网 IP : 60.3.169.44
    运营商  : 联通（lt）
    ISP/AS  : CNC Group CHINA169 Hebei Province Network / AS4837 CHINA UNICOM

这台机器是哪个运营商？
  1) 联通 (lt)  <= 识别结果
  2) 电信 (dx)
  3) 移动 (yd)
请输入 1/2/3（回车用 lt 联通）:

Web 控制台监听端口（回车用 8787）:
[install] 安装完成！运营商=联通(lt)  端口=8787
```

### 1.3 可选参数

```bash
# 跳过华为云 DNS SDK（纯优选，装得更快、不受 PyPI 网络影响）
curl -fsSL .../install.sh | sudo sh -s -- --no-dns

# 非交互批量部署
EO_NETWORK=lt EO_WEB_PORT=8787 ./install.sh --yes

# 指定安装目录 / 分支 / 仓库
DIR=/opt/eo BRANCH=main REPO=https://github.com/JuLuogo/eo.git ./install.sh

# 私有仓库（用私人令牌）
GITEE_USER=用户名 GITEE_TOKEN=令牌 ./install.sh

# 指定 pip 镜像源（境内网络建议）
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple ./install.sh
```

### 1.4 已经 clone 了？

```bash
cd /opt/edge-optimizer
sudo ./install.sh          # 一键安装（本地版）
sudo ./deploy.sh install   # 或者只做部署那一步
```

---

## 二、安装后做什么

```bash
cd /opt/edge-optimizer

# 1) 填华为云 DNS 密钥（只有「自动下发 DNS」才需要，纯优选可跳过）
vi .env                    # HUAWEI_AK / HUAWEI_SK

# 2) 环境体检 —— 强烈建议做，能发现本机链路是否被代理接管
./deploy.sh doctor

# 3) 启动并查看访问地址
sudo ./deploy.sh start
./deploy.sh status
```

`./deploy.sh status` 输出：

```
  ----------------------------------------------------------
   本机运营商 : 联通 (lt)
   Web 端口   : 8787
  ----------------------------------------------------------
   本机访问   : http://127.0.0.1:8787/?token=xxxxx
   对外访问   : http://<公网IP>:8787/?token=xxxxx
   访问令牌   : xxxxx
  ----------------------------------------------------------
   配置数据   : /opt/edge-optimizer/config/config.json
   结果目录   : /opt/edge-optimizer/results/
  ----------------------------------------------------------
  健康检查 : OK
```

浏览器打开带 token 的地址进入控制台。**首屏「常用变量」面板**会显示每一项的生效值
及其来源（网页设置 / `.env` / 默认值）。

---

## 三、常用命令速查

所有操作都在项目目录下执行（默认 `/opt/edge-optimizer`）：

| 命令 | 作用 |
| --- | --- |
| `sudo ./deploy.sh install` | 安装 / 重新安装（交互式问运营商与端口） |
| `sudo ./deploy.sh start` | 启动服务 |
| `sudo ./deploy.sh stop` | 停止服务 |
| `sudo ./deploy.sh restart` | 重启服务 |
| `./deploy.sh status` | 状态 + 运营商 + 端口 + 访问地址 + 令牌 + 健康检查 |
| `./deploy.sh logs` | **实时跟踪日志**（Ctrl+C 退出） |
| `./deploy.sh logs 200` | 查看最近 200 行后退出 |
| `./deploy.sh logs-err` | 只看错误级别日志 |
| `./deploy.sh logs-save` | 导出全部日志到 `edge-optimizer.log` |
| `./deploy.sh doctor` | 环境体检（链路是否可信、host 是否配对） |
| `./deploy.sh isp` | 识别本机运营商 |
| `./deploy.sh scan` | 按本机运营商手动跑一轮优选（抽样） |
| `./deploy.sh config` | 改运营商 / 端口，自动重启 |
| `./deploy.sh run` | 前台直接跑（调试，Ctrl+C 退出） |
| `./deploy.sh uninstall` | 卸载（保留配置与结果） |
| `sudo ./deploy.sh uninstall --purge` | **完全卸载**（连配置、结果、密钥一起删） |

不开网页也能用的命令行工具：

```bash
.venv/bin/python run.py list --all-platforms    # 看 9 个平台变体
.venv/bin/python run.py scan --network lt -q    # 跑一轮联通优选
.venv/bin/python run.py scan -p eo-lt -q        # 只跑指定平台
.venv/bin/python run.py doctor                  # 体检
.venv/bin/python run.py dns-check -p eo-lt      # 只读查看 DNS 当前解析
.venv/bin/python run.py ranges --source cloudfront --output targets/cft.txt   # 更新 CFT 网段
.venv/bin/python run.py esa-ranges --output targets/esa.txt                   # 更新 ESA 网段
.venv/bin/python run.py selftest                # 离线自检
```

### 升级到最新版

```bash
cd /opt/edge-optimizer
git pull
sudo ./deploy.sh restart
```

---

## 四、查看日志

程序日志由 systemd 收集（`journalctl`），查看方式：

```bash
./deploy.sh logs              # 实时跟踪（最常用）
./deploy.sh logs 500          # 最近 500 行
./deploy.sh logs-err          # 只看错误
./deploy.sh logs-save         # 导出到 edge-optimizer.log
```

等价的原始命令（自定义过滤时用）：

```bash
journalctl -u edge-optimizer -f                    # 实时
journalctl -u edge-optimizer -n 200 --no-pager     # 最近 200 行
journalctl -u edge-optimizer --since "10 min ago"  # 最近 10 分钟
journalctl -u edge-optimizer --since today         # 今天的
journalctl -u edge-optimizer -p err                # 只看错误
journalctl -u edge-optimizer --disk-usage          # 日志占多少磁盘
sudo journalctl --vacuum-size=100M                 # 清理旧日志
```

Web 控制台里也有**实时日志面板**（内存环形缓冲，默认 500 条，不落盘），
可在页面上直接看、清空、调整保留条数。

> 两处日志的分工：`journalctl` 记录进程的完整输出（含启动信息、异常栈），
> 网页日志面板是程序运行时事件流（含每个平台的进度与结果），两者互补。

---

## 五、完全卸载

**方式一：只卸载服务，保留数据**（以后还想重装用这个）

```bash
sudo ./deploy.sh uninstall
```

做的事：停止服务、取消开机自启、删除 `/etc/systemd/system/edge-optimizer.service`、
删除 `.venv` 虚拟环境。**保留** `.env`、`config/`、`results/`。

**方式二：完全卸载，痕迹全清**

```bash
sudo ./deploy.sh uninstall --purge
```

在方式一基础上，额外删除：

| 删掉的东西 | 内容 |
| --- | --- |
| `.env` | 运营商、端口、访问令牌、华为云 AK/SK |
| `config/` | Web 控制台保存的所有配置 |
| `results/` | 优选结果与历史记录 |
| `edge-optimizer.log` | 导出的日志（如果有） |

**方式三：连代码目录一起删（彻底清理）**

```bash
sudo ./deploy.sh uninstall --purge
cd /
sudo rm -rf /opt/edge-optimizer
```

**手动彻底清理**（如果脚本已丢失或不适用）：

```bash
# 1. 停服务并删除 systemd 单元
sudo systemctl stop edge-optimizer
sudo systemctl disable edge-optimizer
sudo rm -f /etc/systemd/system/edge-optimizer.service
sudo systemctl daemon-reload
sudo systemctl reset-failed          # 清掉失败的记录

# 2. 删项目目录（含 .env / config / results / .venv）
sudo rm -rf /opt/edge-optimizer

# 3. 确认没有残留
systemctl status edge-optimizer      # 应显示 Unit ... could not be found
ls /etc/systemd/system/ | grep edge  # 应无输出
ss -ltnp | grep 8787                 # 端口应已释放
```

Docker 部署的卸载：

```bash
docker compose down            # 停止并删除容器
docker compose down -v         # 连数据卷一起删（谨慎）
docker rmi edge-optimizer:latest
sudo rm -rf /opt/edge-optimizer
```

---

## 六、Docker 安装（最省事，推荐）

Docker 会把 Python、venv、pip、依赖全部封装进镜像，**不受服务器环境影响**，
也不会遇到 `ensurepip`、`ConnectionResetError` 这类问题。

### 7.1 装 Docker（如果还没有）

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
docker --version && docker compose version
```

### 7.2 拉代码 + 构建 + 启动（三条命令）

```bash
# 1. 拉代码
git clone https://github.com/JuLuogo/eo.git /opt/edge-optimizer
cd /opt/edge-optimizer

# 2. 交互式生成 .env（问运营商和端口，自动识别本机运营商）
./docker-setup.sh

# 3. 构建并启动
./rebuild.sh
docker compose logs -f          # 启动日志里有访问地址和令牌
```

**想一步到位**：

```bash
./docker-setup.sh --up          # 配置 + 构建 + 启动
```

> **遇到 `./docker-setup.sh: Permission denied`？**（某些 git 配置会忽略文件权限位）
>
> ```bash
> chmod +x *.sh                 # 或直接用 sh 运行：sh docker-setup.sh
> ```
>
> **遇到 `env file /opt/edge-optimizer/.env not found`？** 说明上一步没跑成功，
> 先生成配置再构建：
>
> ```bash
> cp .env.example .env
> vi .env                       # 至少改 EO_NETWORK=lt（或 dx/yd）
> ./rebuild.sh
> ```
>
> 仓库里这几个脚本已标记为可执行（`100755`），正常 `git clone` 出来就能直接运行；
> 若你的环境忽略了权限位，用上面的 `sh` 方式最省事。

**不想用配置脚本，纯手工**：

```bash
cd /opt/edge-optimizer
cp .env.example .env
vi .env                         # 改 EO_NETWORK=lt/dx/yd、EO_WEB_PORT
./rebuild.sh
```

> **不需要任何镜像仓库**。镜像是你本机 `docker build` 出来的，`./rebuild.sh`
> 这一条就直接构建并运行了。Gitee 只存代码，不存 Docker 镜像。

### 7.3 常用命令

```bash
cd /opt/edge-optimizer

docker compose ps                                   # 状态（含健康检查）
docker compose logs -f                              # 实时日志
docker compose logs -f --tail 200                   # 最近 200 行
docker compose restart                              # 重启
docker compose down                                 # 停止并删除容器（保留数据卷挂载的目录）
./rebuild.sh                                        # 改代码后重建并启动（不要用 compose --build）

docker compose run --rm optimizer doctor            # 环境体检
docker compose run --rm optimizer scan -p eo-lt -q  # 手动跑一轮优选
docker compose run --rm optimizer list --all-platforms   # 看平台清单
docker compose run --rm optimizer dns-check -p eo-lt     # 只读查看 DNS 解析
docker compose run --rm optimizer logs-save              # （容器内无 journalctl，日志看 compose）
```

### 7.4 停止与重启（收到 SIGTERM 会优雅退出）

`docker stop` / `docker compose down` / `docker compose restart` 发的是 **SIGTERM**。
容器里的进程收到后会：

1. 立刻停止接受新请求，并请求当前这一轮扫描停下来；
2. 给正在跑的一轮最多 `EO_SHUTDOWN_GRACE` 秒（默认 5）收尾；
3. 打印 `Web 控制台已关闭` 后以**退出码 0** 正常退出。

实测：空转时约 1 秒退出；正在扫描时约 6 秒退出（均在 docker 默认 10 秒宽限期内，
不会被 SIGKILL）。

```bash
docker compose stop          # 优雅停止（走上面的流程）
docker compose restart       # 优雅停止后重启
docker stop eo               # 直接对容器操作也一样
```

如果你的场景需要「正在下发的 DNS 必须做完」，把宽限期调大，同时把 docker 的
等待时间也调大（否则到 10 秒还是会被强杀）：

```bash
# .env 里加 EO_SHUTDOWN_GRACE=20，然后
docker stop -t 30 eo
```

> 怎么判断有没有优雅退出：`docker inspect -f '{{.State.ExitCode}}' <容器名>`
> 返回 `0` 表示自行退出；返回 `137` 表示被 SIGKILL（说明宽限期不够）。
> 日志里应能看到 `收到 SIGTERM，正在优雅退出…` 与 `Web 控制台已关闭`。

结果文件是**原子替换**写入的（先写临时文件再 rename），所以即使某一轮在写入时被
中断，`results/*.txt` 也不会变成半截列表 —— 要么是上一轮的完整内容，要么是新一轮的。

### 7.5 挂载目录权限（已自动处理，无需手动干预）

容器启动时会以 root 修正 `config/`、`results/` 的归属，再**降权到非 root 用户**
（UID 10001）执行程序。原因：`deploy.sh` / `onekey.sh` 都以 root 创建这两个目录，
若直接以非 root 启动，会因无法写入而立即崩溃：

```
PermissionError: [Errno 13] Permission denied: '/app/config/config.tmp'
```

**所以不需要手动 chown。** 副作用是宿主机上这两个目录的属主变为 `10001:10001`
（容器内用户），直接 `ls` 看不到内容、需要 sudo —— 这是正常现象。

想改变这个行为，可在 `docker-compose.yml` 里调整：

```yaml
services:
  optimizer:
    environment:
      EO_APP_UID: "1000"     # 改成与宿主机你的用户 UID 一致
      EO_APP_GID: "1000"
```

或者让容器内以 root 运行（会失去非 root 的安全边界，不推荐）：

```yaml
    user: "0:0"
```

### 7.6 国内网络优化（拉不动基础镜像时）

基础镜像 `python:3.12-slim` 默认从 Docker Hub 拉，境内可能很慢或失败。两种办法：

**办法一：给 Docker 配镜像加速（推荐，一次配好长期有效）**

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://mirror.ccs.tencentyun.com"
  ]
}
EOF
sudo systemctl restart docker
```

**办法二：构建时直接指定可用的基础镜像**

```bash
docker build \
  --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  -t edge-optimizer:latest .
docker compose up -d          # 镜像已存在，直接启动
```

> Dockerfile 里的 pip 源**默认已用清华镜像**，所以不会再出现你之前那种
> `ConnectionResetError`。想换源可以 `--build-arg PIP_INDEX=...`。

### 7.7 不想装华为云 SDK（纯优选）

```bash
docker build --build-arg INSTALL_DNS_SDK=0 -t edge-optimizer:latest .
# 或在 compose 的 build.args 里加 INSTALL_DNS_SDK: "0"
```

### 7.7 Docker 卸载

```bash
cd /opt/edge-optimizer
docker compose down                  # 停容器
docker rmi edge-optimizer:latest     # 删镜像
sudo rm -rf /opt/edge-optimizer      # 删目录（含 config/ results/ .env）
```

### 7.8 三台机器用 Docker

每台机器 `./docker-setup.sh` 时选自己那个运营商即可（端口也自动错开）。
如果想**只构建一次、三台共用**，可以把镜像导出再传过去：

```bash
# 联通机上构建后导出
docker save edge-optimizer:latest | gzip > edge-optimizer.tar.gz
scp edge-optimizer.tar.gz user@电信机:/tmp/

# 电信机上导入
docker load < /tmp/edge-optimizer.tar.gz
cd /opt/edge-optimizer && ./docker-setup.sh --up      # 会直接复用已有镜像
```

或者推到镜像仓库（华为云 SWR 支持与 Gitee 联动自动构建，见
[Gitee 帮助文档](https://help.gitee.com/devops/connect/HUAWEI-Cloud-SWR/)）：

```bash
docker tag edge-optimizer:latest swr.cn-north-4.myhuaweicloud.com/<你的组织>/edge-optimizer:latest
docker push swr.cn-north-4.myhuaweicloud.com/<你的组织>/edge-optimizer:latest
```

---

## 七、手动安装（不用一键脚本，也不装 Docker）

```bash
# 1. 系统依赖
sudo apt update && sudo apt install -y python3 python3-venv python3-pip curl git
# CentOS/RHEL: sudo yum install -y python3 python3-pip curl git
# Alpine:      sudo apk add python3 py3-pip curl git

# 2. 拉代码
git clone https://github.com/JuLuogo/eo.git /opt/edge-optimizer
cd /opt/edge-optimizer

# 3. 虚拟环境
python3 -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    huaweicloudsdkcore huaweicloudsdkdns     # 只有自动下发 DNS 才需要

# 4. 配置
cp .env.example .env
vi .env           # EO_NETWORK=lt/dx/yd、EO_WEB_PORT、HUAWEI_AK/SK

# 5. 体检 + 运行
.venv/bin/python run.py doctor
.venv/bin/python run.py web --port 8787
```

前台跑起来后想常驻，用 `nohup` 或自己写 systemd 单元（可参考 `deploy.sh install` 生成的）。
**推荐还是用一键脚本**，它是幂等的，重复执行只会更新代码并复用已有环境。

---

## 八、三台机器（联通/电信/移动）怎么部署

每台机器跑同样的安装，只是**运营商选自己**：

```bash
# 联通机 → 选 1；电信机 → 选 2；移动机 → 选 3
curl -fsSL https://raw.githubusercontent.com/JuLuogo/eo/main/install.sh | sudo sh
```

脚本会把运营商写进 `.env`（`EO_NETWORK`）并自动配好对应平台：

| 机器 | 端口（默认） | 启用的平台 | 下发到 |
| --- | --- | --- | --- |
| 联通 | 8787 | `eo-lt,esa-lt,cft-lt` | `*-lt` 记录 |
| 电信 | 8788 | `eo-dx,esa-dx,cft-dx` | `*-dx` 记录 |
| 移动 | 8789 | `eo-yd,esa-yd,cft-yd` | `*-yd` 记录 |

**为什么必须分开**：延迟只能从该运营商网络内部测。在联通机器上跑电信的优选没有意义。

每台机器的 `results/`、`config/config.json` 都是本地的，互不影响；
DNS 记录是共享的，但各写各的运营商记录，不会撞车。

非交互：

```bash
EO_NETWORK=dx EO_WEB_PORT=8788 ./install.sh --yes
```

---

## 九、境内网络问题（GitHub / 镜像拉取）

境内服务器直连 GitHub 与 Docker Hub 经常不稳定，项目里已内置规避手段。

### 9.1 代码获取：Gitee 为主，GitHub 为备

`onekey.sh` 默认从 **Gitee** 拉代码（境内稳定），失败自动切 GitHub，再失败用压缩包：

```bash
# 默认走 Gitee；想强制 GitHub：
REPO=https://github.com/JuLuogo/eo.git bash -c "$(curl -fsSL .../onekey.sh)"
```

| 源 | 地址 |
| --- | --- |
| Gitee（境内快） | `https://gitee.com/peroe/eo.git` |
| GitHub | `https://github.com/JuLuogo/eo.git` |
| Gitee 压缩包 | `https://gitee.com/peroe/eo/repository/archive/main.tar.gz` |
| GitHub 压缩包 | `https://github.com/JuLuogo/eo/archive/refs/heads/main.tar.gz` |

### 9.2 脚本/文件下载：走 GitHub 文件代理

`onekey.sh` 内置了实测可用的文件代理，按速度顺序自动尝试：

```
https://gh-proxy.com/      <- 最快（实测约 360ms）
https://gh.llkk.cc/        <- 约 460ms
https://ghproxy.net/       <- 约 830ms
https://ghfast.top/        <- 约 1500ms
https://cdn.jsdelivr.net/gh/JuLuogo/eo@main/   <- jsDelivr 兜底
直连 raw.githubusercontent.com                  <- 最后兜底
```

用法：在任意 GitHub raw 地址前拼代理前缀即可：

```bash
curl -fsSL "https://gh-proxy.com/https://raw.githubusercontent.com/JuLuogo/eo/main/onekey.sh"
```

> 实测（2026-10）：上述 4 个代理 + jsDelivr + 直连均返回 200；
> 而 `mirror.ghproxy.com`、`ghproxy.cc`、`raw.gitmirror.com`、`raw.fastgit.org`、
> `gitclone.com` **已失效**，脚本里没有采用。

### 9.3 容器镜像加速

**方式一：让脚本写入加速配置**（推荐，一次性）

```bash
SET_REGISTRY_MIRROR=1 DIR=/opt/edge-optimizer EO_NETWORK=lt \
HUAWEI_AK=... HUAWEI_SK=... \
bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/JuLuogo/eo/main/onekey.sh)"
```

它会往 `/etc/docker/daemon.json` 写入并重启 Docker：

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://docker.1ms.run",
    "https://hub.rat.dev"
  ]
}
```

**方式二：手动配置**

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://docker.1ms.run",
    "https://hub.rat.dev"
  ]
}
EOF
sudo systemctl restart docker
docker info | grep -A5 "Registry Mirrors"
```

**方式三：拉取时直接指定加速地址**（不改配置）

```bash
docker pull docker.m.daocloud.io/library/python:3.12-slim
docker tag  docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim

docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
             -t edge-optimizer:latest .
```

> **关于 GHCR（ghcr.io）**：`ghcr.io` 在境内连通性差，而且它**不是** Docker Hub 镜像，
> 普通 registry-mirrors **代理不了它**；各类「ghcr 镜像站」实测也已失效。
> 镜像名是 `ghcr.io/juluogo/eo:latest`。拉不动就用**离线镜像包**（最省事）：
>
> ```bash
> curl -fL https://gh-proxy.com/https://github.com/JuLuogo/eo/releases/download/image-latest/eo-image.tar.gz \
>   | gunzip | docker load
> ```
>
> 详见 [11.5](#115-镜像拉不下来怎么办ghcr-在国内的实际情况)。也可以改本机构建（第六节）
> 或把镜像推到国内仓库（见 9.4）。

### 9.4 想彻底避开 GHCR：推到国内镜像仓库

阿里云 / 腾讯云的个人版容器镜像服务都免费，境内拉取很快。以阿里云为例：

```bash
# 1. 在阿里云「容器镜像服务」创建命名空间与仓库，得到形如
#    registry.cn-hangzhou.aliyuncs.com/<命名空间>/eo 的地址
# 2. 在能访问 GitHub 的机器上（或本地构建后）转推
docker pull ghcr.io/juluogo/eo:latest      # 拉不动就 gunzip -c eo-image.tar.gz | docker load
docker tag  ghcr.io/juluogo/eo:latest registry.cn-hangzhou.aliyuncs.com/<命名空间>/eo:latest
docker login registry.cn-hangzhou.aliyuncs.com
docker push registry.cn-hangzhou.aliyuncs.com/<命名空间>/eo:latest

# 3. 服务器上直接用国内地址
USE_GHCR=1 IMAGE=registry.cn-hangzhou.aliyuncs.com/<命名空间>/eo:latest \
DIR=/opt/eo EO_NETWORK=lt HUAWEI_AK=... HUAWEI_SK=... \
bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/JuLuogo/eo/main/onekey.sh)"
```

### 9.5 最省事的组合（完全不用访问 GitHub）

代码走 Gitee、构建走镜像加速：

```bash
SET_REGISTRY_MIRROR=1 DIR=/opt/edge-optimizer EO_NETWORK=lt EO_WEB_PORT=8787 \
HUAWEI_AK=... HUAWEI_SK=... \
bash -c "$(curl -fsSL https://gitee.com/peroe/eo/raw/main/onekey.sh)"
```

`onekey.sh` 在 Gitee 上也有，所以连脚本本身都不需要从 GitHub 取。

---

## 十、常见问题

**Q：装依赖时报 `ConnectionResetError: [Errno 104] 连接被对方重置`**

境内访问 PyPI 被重置。装脚本会自动改用清华 / 阿里云 / 腾讯云 / 中科大镜像重试；
全部失败也**不中断安装**（优选只用标准库 + curl，不需要这些包），只影响自动下发 DNS。

```bash
# 手动补装
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    huaweicloudsdkcore huaweicloudsdkdns

# 或者一开始就跳过
curl -fsSL https://raw.githubusercontent.com/JuLuogo/eo/main/install.sh | sudo sh -s -- --no-dns
```

**Q：报 `ensurepip is not available` / `Failing command: .../.venv/bin/python3`**

Debian/Ubuntu 默认不带 `python3-venv`。装脚本会自动安装（注意只测 `import venv`
是查不出来的，必须看 `ensurepip`）。若自动安装失败：

```bash
python3 -V                        # 先看版本，比如 3.13
sudo apt install -y python3.13-venv
rm -rf /opt/edge-optimizer/.venv  # 清掉失败残留（新版脚本会自动清理）
```

**Q：反复提示 `Username for 'https://gitee.com'`**

仓库是公开的，不需要账号。是服务器上存了过期凭据：

```bash
git config --global --unset credential.helper
rm -f ~/.git-credentials
```

或直接用压缩包（不需要 git）：

```bash
mkdir -p /opt/edge-optimizer && cd /opt/edge-optimizer
curl -fL https://github.com/JuLuogo/eo/archive/refs/heads/main.tar.gz | tar xz --strip-components=1
sudo ./install.sh
```

**Q：`sudo: 未找到命令`**

你本来就是 root，去掉 `sudo` 直接执行即可：

```bash
apt install -y python3.13-venv
./deploy.sh install
```

**Q：忘记访问令牌**

```bash
./deploy.sh status                 # 会打印
grep EO_WEB_TOKEN .env             # 或直接从 .env 读
```

**Q：端口被占用 / 想换端口**

```bash
sudo ./deploy.sh config            # 重新交互配置，自动重启
ss -ltnp | grep 8787               # 查占用者
```

**Q：`doctor` 报「链路不可信」**

本机（或所在网络）有代理/透明网关在接管 HTTPS，此时优选结果无意义，程序会**拒绝下发 DNS**。
请换到干净网络的服务器，或排查本机代理后重试。

**Q：不想对外暴露网页端口**

```bash
# /opt/edge-optimizer/.env 里设 EO_WEB_HOST=127.0.0.1 后重启
sudo ./deploy.sh restart
# 然后用 SSH 隧道访问
ssh -L 8787:127.0.0.1:8787 user@服务器
# 本地浏览器打开 http://127.0.0.1:8787/?token=...
```

**Q：`.env` 里改了值，网页上还是旧的？**

分两种情况：

- **端口 / 运营商**：属于启动参数，改完要 `sudo ./deploy.sh restart`；
- **并发上限、轮询间隔等运行参数**：改完立即生效（下一轮扫描用新值），不用重启。

另外：网页上改过的值优先于 `.env`。控制台「常用变量」面板会明确标出每项当前
生效值的来源，一眼能看出是谁在生效。

---

## 十一、安全与隐患清单

部署前请逐条过一遍。这些都不是理论风险 —— 大部分是实际踩过的坑。

### 11.1 控制台默认对公网开放

`docker compose` 默认把端口发布到 `0.0.0.0`，任何扫到端口的人都能看到登录页。
虽然有令牌保护，但**令牌会出现在 URL 里**（`?token=xxx`），可能留在浏览器历史、
反向代理日志、以及截图里。

收敛办法（任选）：

```bash
# A) 只监听本机，用 SSH 隧道访问（最省事、最安全）
#    /opt/eo/.env 里改：EO_BIND=127.0.0.1
docker compose up -d
ssh -L 8787:127.0.0.1:8787 user@服务器     # 本地打开 http://127.0.0.1:8787/?token=...

# B) 只允许自己的 IP（注意：必须用 DOCKER-USER 链，见 11.2）
# C) 前面挂 Nginx/Caddy 做 HTTPS + 基本认证
```

`EO_BIND` 控制**宿主机**监听地址，`EO_WEB_HOST` 控制**容器内**监听地址，两个都支持
`127.0.0.1`。

### 11.2 ⚠️ ufw / firewalld 挡不住 Docker 发布的端口

这是最容易误判的一条。Docker 直接往 iptables 里写 DNAT 规则，**绕过 ufw 的 INPUT 链**：

```bash
sudo ufw deny 8787          # ← 看起来挡住了
curl http://<公网IP>:8787/  # ← 实际还能访问
```

要么用 `EO_BIND=127.0.0.1` 从根上不发布到公网，要么把规则加进 `DOCKER-USER` 链
（该链在 Docker 规则之前生效），要么装 `ufw-docker`。

### 11.3 访问令牌 = 控制台全部权限

拿到令牌就能改配置、触发扫描、**下发 DNS**。它存在 `config/config.json`（权限 600）。

轮换令牌（令牌泄露后立刻做）：

```bash
curl -s -X POST -H "Authorization: Bearer <旧令牌>" -H 'Content-Type: application/json' \
     -d '{"rotate_web_token":true}' http://127.0.0.1:8787/api/config
# 新令牌在 config/config.json 的 web_token 字段里
sudo grep -o '"web_token": "[^"]*"' config/config.json
```

> 注意：轮换后旧令牌立即失效，**只有服务器上的 config.json 里有新令牌**，
> 别把自己锁在外面。也可以在 `.env` 里显式设 `EO_WEB_TOKEN=<自己定>` 再重启。

### 11.4 凭据放在哪、会不会进镜像/仓库

| 凭据 | 位置 | 权限 |
| --- | --- | --- |
| `HUAWEI_AK` / `HUAWEI_SK` / `CLOUDFLARE_API_TOKEN` | 服务器 `.env`，或网页表单（写进 `config/config.json`） | `onekey.sh` / `deploy.sh` / `docker-setup.sh` 都会 `chmod 600 .env`；手动创建的记得自己改。`config.json` 固定 600 |
| 访问令牌 `web_token` | `config/config.json` | 600 |
| GitHub / GHCR 令牌 | 只应放在服务器本地或 CI secrets | 不要写进仓库 |

`.env` 已被 `.gitignore` 与 `.dockerignore` 排除，且 Dockerfile 只 COPY 指定文件，
**不会被打进镜像**。网页接口回显密钥时一律打码（`/api/config` 只给 `web_token_set`）。

### 11.5 镜像拉不下来怎么办（GHCR 在国内的实际情况）

镜像名是 **`ghcr.io/juluogo/eo:latest`**（GitHub 用户名 `JuLuogo` → 镜像名转小写）。
注意是 `juluogo` 而不是 `julugo`，写错会报 `manifest unknown`。

实测结论（2026-10，国内家庭宽带 + 海外代理）：

| 通道 | 结果 |
| --- | --- |
| `ghcr.io` 匿名取 token | **403 DENIED**（连公开包 `cli/cli` 也一样，是 GitHub 侧的拒绝，不是墙） |
| `ghcr.nju.edu.cn` / `ghcr.dockerproxy.net` / `ghcr.1ms.run` 等镜像站 | 对 `cli/cli` 都返回 `manifest unknown`，**已失效** |
| GitHub Release 离线包（走 `gh-proxy.com`） | **55MB，2~27 秒下完** ✅ |

所以最可靠的是**离线镜像包**：

```bash
curl -fL https://gh-proxy.com/https://github.com/JuLuogo/eo/releases/download/image-latest/eo-image.tar.gz \
  | gunzip | docker load
```

加载出来的镜像名和 `docker pull` 得到的一模一样（`ghcr.io/juluogo/eo:latest`），
后续 `docker run` 不用改。这个包由工作流在每次推送 main 时自动重建覆盖，地址固定。

**如果服务器能连 ghcr.io**，直接 `docker pull ghcr.io/juluogo/eo:latest` 更省事。
包确实是公开的；若你后来改成私有，拉取前需要：

```bash
echo <GitHub令牌> | docker login ghcr.io -u JuLuogo --password-stdin   # 令牌勾选 read:packages
```

**实在都不行**（比如服务器完全连不上 GitHub）：在任一台能上网的机器上跑
`docker build`，然后 `docker save | gzip` 传到服务器 `docker load`；
或者在服务器上 `./rebuild.sh` 本机构建（apt/pip 源已默认国内镜像）。

若 GHCR 在境内拉不动（普通 registry 镜像加速代理不了 ghcr.io），让本机构建：

```bash
cd /opt/eo && ./rebuild.sh
```

> 注意：**不要用 `docker compose up -d --build`** —— Compose 的 build 依赖
> buildx ≥ 0.17（Debian/Ubuntu 自带的常只有 0.13），会报
> `compose build requires buildx 0.17.0 or later`。`./rebuild.sh` 用的是经典
> `docker build`，不依赖 buildx。

镜像目前**只构建了 linux/amd64**。ARM 服务器（如华为云鲲鹏）需要本机构建，或改用
`docker buildx build --platform linux/arm64`。

### 11.6 目录属主是 10001（不是你的用户）

容器启动时会把 `config/`、`results/`、`targets/` 的属主改成容器内用户 `10001`，
所以你用普通用户 `ls` 会「看不到内容」、也**改不了 `targets/` 里的文件**：

```bash
sudo ls -la /opt/eo/results/          # 正常现象，不是权限坏了
sudo vi /opt/eo/targets/eo.txt        # 改网段要 sudo

# 想让属主变成你自己（宿主机 UID 1000）：
#   docker run 时加 -e EO_APP_UID=1000 -e EO_APP_GID=1000（compose 则写进 .env）
```

### 11.7 结果与历史的磁盘占用

- 容器日志：compose 已限制 `max-size=10m` / `max-file=3`，最多 30MB，不会写满磁盘。
- 网页实时日志：内存环形缓冲 500 条，**不落盘**。
- `results/*.txt`：覆盖写，固定大小。
- `results/history/*.jsonl`：**每轮追加一条**（含当轮全部可用 IP）。间隔 300 秒时
  大约每天 288 条；按每个平台 50 个 IP 估算，一年约几百 MB。长期跑建议定期清理：

```bash
sudo truncate -s 0 /opt/eo/results/history/*.jsonl     # 清空历史
# 或加一条 logrotate / cron 定期删旧文件
```

### 11.8 DNS 下发是危险操作（已做双重保险）

默认 `EO_DNS_SYNC=none`（只优选、不碰解析）。真要下发必须同时满足：

1. `EO_DNS_SYNC=push`；
2. `EO_CONFIRM_DNS=yes`（Web 模式下还要在页面上勾选确认）。

另外内置**解析量骤降保护**：新 IP 数远少于现有记录时会直接中止（阈值见
`_shrunk_too_much`），只有显式 `--force` 才允许缩容。首次真实下发前建议先
`docker compose run --rm optimizer scan -p eo-lt --dns --dry-run` 预览。

### 11.9 ⚠️ `docker compose up -d --build` 在老版本 Docker 上会直接失败

Compose 的 `build` 子命令要求 **buildx ≥ 0.17**。Debian / Ubuntu 官方源里的
`docker.io` 包自带的 buildx 往往只有 **0.13**，于是：

```
compose build requires buildx 0.17.0 or later
```

结果就是「构建并启动」这一步直接断掉，容器起不来（而 `docker pull` 与此无关，
所以拉现成镜像的路径不受影响）。判断与解决：

```bash
docker buildx version          # 看版本；< 0.17 就会踩这个坑
cd /opt/eo && ./rebuild.sh     # 用经典 docker build，绕过 buildx

# 或者升级 docker 到官方源版本（buildx 插件会一起装上）
curl -fsSL https://get.docker.com | sh
```

`onekey.sh` 与 `docker-setup.sh` 已经改成用经典 `docker build` + `up -d --no-build`，
不会再踩这个坑。

### 11.10 ⚠️ 小内存机器（1~2GB）会被自己的并发压垮

**症状**：跑起来以后 CPU 100%、内存 95%+、swap 100%、负载飙到 50 以上，SSH 卡住、
网页打不开，看着像"崩了"。

**原因**：**旧版本**每个探测都 fork 一个 `curl` 进程（含 TLS 上下文，约 12MB）。
默认 `workers=80`，三个平台并行 → **最多 240 个 curl 同时在跑 ≈ 2GB 内存**。
1.85GB 的机器（比如 4 核 / 1.85G 的轻量云主机）当场就被打满。

> ✅ **现在探测已改成进程内实现**（Python `socket` + `ssl`，等价于
> `curl --resolve` + `--insecure` + `--http1.1`），每个并发只占几十 KB，
> 不再是 12MB 一个进程。同样的 240 并发，内存从约 2.9GB 降到约 24MB。
> 老的 curl 实现保留为回退：`EO_PROBE=curl`。

> 注意：`EO_RATE=100` 限制的是**每秒新建多少连接**，不是并发数。
> 并发 ≈ 速率 × 单个探测耗时 —— 网络差、超时 3 秒时，100/s × 3s = 300 个在飞，
> 这就是为什么"限速设了 100 还是崩"。

**现在已经自动处理**：开跑前会按「可用内存 + CPU 核数」压一次总并发，
并在日志里打印实际值，例如：

```
⚠ 并发已按机器规格下调：240 -> 30 个并发探测（每平台最多 10 个；可用内存约 1400MB、4 核）
```

大致对应关系（自动计算，无需手填）：

| 机器 | 自动并发上限 | 3 个平台时每个平台 |
| --- | --- | --- |
| 1 核 / 1GB | ~16 | ~5 |
| 4 核 / 1.85GB | ~32 | ~10 |
| 4 核 / 4GB | ~32 | ~10 |
| 8 核 / 8GB | ~64 | ~21 |
| 16 核 / 16GB | ~128 | ~42 |

想手动控制（覆盖自动值）：

```bash
-e EO_MAX_CONCURRENT=16   # 同时存在的探测数上限（最直接；设 1 = 完全串行）
-e EO_WORKERS=8           # 单平台 worker 数（也会被总上限再压一次）
-e EO_RATE=30             # 每秒新建连接数；小机器建议 20~50
```

> 并发还是要适当留一些：**太少会慢到不可用**。6 万个 IP 大致是
> 8 并发 25 分钟~2 小时、4 并发 50 分钟~4 小时、1 并发 3~17 小时。

### 11.11 只保留又快又稳的节点（延迟门槛 / 取前 N）

默认行为是「通过的全写进结果文件」。如果想只要一小撮最优的：

```bash
# 只要 120ms 以内、最快的 10 个
python run.py scan -n lt --max-ms 120 --top 10

# 容器里用环境变量
-e EO_MAX_MS=120 -e EO_TOP=10
```

- `--max-ms MS`：连接耗时超过这个毫秒数就**丢弃**
- `--top N`：只保留**最快的 N 个**（结果文件与 DNS 下发都只用这 N 个）
- 排序规则：**延迟升序**；延迟相同时，**重试过才连上的排后面**（稳定性更差）
- 日志会说明丢了多少，例如：
  ```
  [EdgeOne·联通] 丢弃延迟 > 120ms 的节点 37 个，剩 9 个
  [EdgeOne·联通] 只保留最快 10 个节点（丢弃 2 个）
  ```

> ⚠️ 注意：工具**不测 ICMP 丢包率**。稳定性只看「是否需要重试」。
> 真要丢包率，得靠外部监控（比如 Komari 面板）配合。

Docker 再兜一层，避免它把整台机器拖垮（被 OOM kill 总比 SSH 卡死好）：

```bash
docker run -d --memory 512m --memory-swap 512m ...   # 加在 docker run 上
```

**已经崩了怎么救**（SSH 还能进就先救，进不去就重启实例）：

```bash
# 1) 看残留：崩溃时可能留了一堆 curl
ps aux | grep -c '[c]url'
sudo pkill -f 'curl.*--resolve'      # 清掉本工具残留的探测进程

# 2) 让 swap 回血
sudo swapoff -a && sudo swapon -a

# 3) 停掉容器，改小并发再启动
docker rm -f edge-optimizer
docker run -d --name edge-optimizer --memory 512m ... -e EO_MAX_CONCURRENT=16 ... <镜像>

# 4) 确认
uptime            # 负载应逐步回落到个位数
free -m           # available 应恢复
```

**建议**：这类机器一次只跑一个平台（`EO_PLATFORMS=eo-lt`），或者干脆只跑 EO —— 
240 路并发在 1.8GB 上本来就不现实。

### 11.12 部署前检查清单

```bash
cd /opt/eo
docker compose ps                       # 容器是否 running / healthy
docker compose logs --tail=50           # 有没有报错、令牌是多少
docker compose run --rm optimizer doctor    # ★ 链路体检（有代理会报「链路不可信」）
docker compose run --rm optimizer list --all-platforms   # 平台是否按运营商正确展开
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/healthz   # 期望 200
```

`doctor` 报「链路不可信」说明本机 HTTPS 被代理接管，此时优选结果无意义且程序会
拒绝下发 DNS —— 换干净网络的服务器。

### 11.13 备份与恢复

需要备份的只有三样（都在挂载目录里，重建镜像不丢）：

```bash
sudo tar czf eo-backup-$(date +%F).tar.gz -C /opt/eo .env config results
# 恢复：解回 /opt/eo 后 docker compose up -d
```

`targets/` 不用备份：镜像内置了一份种子，缺文件时启动会自动补齐
（`EO_SEED_TARGETS=0` 可关掉）。

