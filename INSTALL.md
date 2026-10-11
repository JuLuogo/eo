# 安装与运维指南

**一键安装（推荐）**：

```bash
curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh
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
- [九、常见问题](#九常见问题)

---

## 一、一键安装（推荐）

服务器上直接跑，不需要先 clone：

```bash
curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh
```

流程与输出：

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

### 可选参数

```bash
# 跳过华为云 DNS SDK（纯优选，装得更快、不受 PyPI 网络影响）
curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh -s -- --no-dns

# 非交互批量部署
EO_NETWORK=lt EO_WEB_PORT=8787 ./install.sh --yes

# 指定安装目录 / 分支 / 仓库
DIR=/opt/eo BRANCH=main REPO=https://gitee.com/peroe/eo.git ./install.sh

# 私有仓库（用私人令牌）
GITEE_USER=你的用户名 GITEE_TOKEN=你的令牌 ./install.sh

# 指定 pip 镜像源（境内网络建议）
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple ./install.sh
```

### 已经 clone 了？

```bash
cd /opt/edge-optimizer
sudo ./install.sh          # 一键安装（本地版）
# 或者只做部署那一步：
sudo ./deploy.sh install
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
git clone https://gitee.com/peroe/eo.git /opt/edge-optimizer
cd /opt/edge-optimizer

# 2. 交互式生成 .env（问运营商和端口，自动识别本机运营商）
./docker-setup.sh

# 3. 构建并启动
docker compose up -d --build
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
> docker compose up -d --build
> ```
>
> 仓库里这几个脚本已标记为可执行（`100755`），正常 `git clone` 出来就能直接运行；
> 若你的环境忽略了权限位，用上面的 `sh` 方式最省事。

**不想用配置脚本，纯手工**：

```bash
cd /opt/edge-optimizer
cp .env.example .env
vi .env                         # 改 EO_NETWORK=lt/dx/yd、EO_WEB_PORT
docker compose up -d --build
```

> **不需要任何镜像仓库**。镜像是你本机 `docker build` 出来的，`docker compose up -d --build`
> 这一条就直接构建并运行了。Gitee 只存代码，不存 Docker 镜像。

### 7.3 常用命令

```bash
cd /opt/edge-optimizer

docker compose ps                                   # 状态（含健康检查）
docker compose logs -f                              # 实时日志
docker compose logs -f --tail 200                   # 最近 200 行
docker compose restart                              # 重启
docker compose down                                 # 停止并删除容器（保留数据卷挂载的目录）
docker compose up -d --build                        # 改代码后重建并启动

docker compose run --rm optimizer doctor            # 环境体检
docker compose run --rm optimizer scan -p eo-lt -q  # 手动跑一轮优选
docker compose run --rm optimizer list --all-platforms   # 看平台清单
docker compose run --rm optimizer dns-check -p eo-lt     # 只读查看 DNS 解析
docker compose run --rm optimizer logs-save              # （容器内无 journalctl，日志看 compose）
```

### 7.4 国内网络优化（拉不动基础镜像时）

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

### 7.5 不想装华为云 SDK（纯优选）

```bash
docker build --build-arg INSTALL_DNS_SDK=0 -t edge-optimizer:latest .
# 或在 compose 的 build.args 里加 INSTALL_DNS_SDK: "0"
```

### 7.6 Docker 卸载

```bash
cd /opt/edge-optimizer
docker compose down                  # 停容器
docker rmi edge-optimizer:latest     # 删镜像
sudo rm -rf /opt/edge-optimizer      # 删目录（含 config/ results/ .env）
```

### 7.7 三台机器用 Docker

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
git clone https://gitee.com/peroe/eo.git /opt/edge-optimizer
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
curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh
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

## 九、常见问题

**Q：装依赖时报 `ConnectionResetError: [Errno 104] 连接被对方重置`**

境内访问 PyPI 被重置。装脚本会自动改用清华 / 阿里云 / 腾讯云 / 中科大镜像重试；
全部失败也**不中断安装**（优选只用标准库 + curl，不需要这些包），只影响自动下发 DNS。

```bash
# 手动补装
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    huaweicloudsdkcore huaweicloudsdkdns

# 或者一开始就跳过
curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh -s -- --no-dns
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
curl -fL https://gitee.com/peroe/eo/repository/archive/main.tar.gz | tar xz --strip-components=1
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
