# 安装指南

三种装法，选一种即可。**推荐第一种**。

---

## 一、一键安装（推荐，服务器上直接跑）

不需要先 clone，一行命令搞定依赖检查 + 拉代码 + 建虚拟环境 + 装 systemd 服务：

```bash
curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh
```

或者更快（如果你网络访问 Gitee 慢，先 clone 再跑本地脚本）：

```bash
git clone https://gitee.com/peroe/eo.git /opt/edge-optimizer
cd /opt/edge-optimizer
sudo ./install.sh
```

> 仓库是**公开**的，clone 不需要账号密码。如果你看到 `Username for 'https://gitee.com'`，
> 说明服务器上存了过期的旧凭据 —— 见文末 FAQ「拉代码时一直提示用户名密码」。
> 拉取失败时脚本会自动改用压缩包下载，通常不影响安装。

安装过程中会问你两件事：

```
[install] 正在识别本机运营商（联通/电信/移动）…
    公网 IP : 60.3.169.44
    运营商  : 联通（lt）
    ISP/AS  : CNC Group CHINA169 Hebei Province Network / AS4837 CHINA UNICOM China169 Backbone

这台机器是哪个运营商？
  1) 联通 (lt)  <= 识别结果
  2) 电信 (dx)
  3) 移动 (yd)
请输入 1/2/3（回车用 lt 联通）:

Web 控制台监听端口（回车用 8787）:
```

默认端口按运营商分配，三台机器天然错开：**联通 8787 / 电信 8788 / 移动 8789**。

### 安装后三步

```bash
cd /opt/edge-optimizer

# 1) 填华为云 DNS 密钥（只优选不下发的话可以跳过）
vi .env                       # HUAWEI_AK / HUAWEI_SK

# 2) 环境体检 —— 必须做，确认本机链路没被代理接管
./deploy.sh doctor

# 3) 启动并查看访问地址
sudo ./deploy.sh start
./deploy.sh status
```

`./deploy.sh status` 会打印：

```
  ----------------------------------------------------------
   本机运营商 : 联通 (lt)
   Web 端口   : 8787
  ----------------------------------------------------------
   本机访问   : http://127.0.0.1:8787/?token=xxxxx
   对外访问   : http://<公网IP>:8787/?token=xxxxx
   访问令牌   : xxxxx
  ----------------------------------------------------------
```

浏览器打开带 token 的地址即可进入控制台。

---

## 二、手动安装（不用一键脚本）

```bash
# 1. 装系统依赖
# Ubuntu / Debian
sudo apt update && sudo apt install -y python3 python3-venv python3-pip curl git
# CentOS / RHEL
sudo yum install -y python3 python3-pip curl git
# Alpine
sudo apk add python3 py3-pip curl git

# 2. 拉代码
git clone https://gitee.com/peroe/eo.git /opt/edge-optimizer
cd /opt/edge-optimizer

# 3. 建虚拟环境并装依赖
python3 -m venv .venv
.venv/bin/pip install huaweicloudsdkcore huaweicloudsdkdns   # 只有下发 DNS 才需要

# 4. 配置
cp .env.example .env
vi .env        # 填 EO_NETWORK（lt/dx/yd）、EO_WEB_PORT、HUAWEI_AK/SK

# 5. 体检 + 启动（前台试跑）
.venv/bin/python run.py doctor
.venv/bin/python run.py web --port 8787
```

要长期后台运行，可以用 systemd（参考 `deploy.sh install` 生成的
`/etc/systemd/system/edge-optimizer.service`），或者：

```bash
nohup .venv/bin/python run.py web --port 8787 >> run.log 2>&1 &
```

---

## 三、Docker 安装

```bash
git clone https://gitee.com/peroe/eo.git /opt/edge-optimizer
cd /opt/edge-optimizer
cp .env.example .env
vi .env                       # 填密钥、端口、运营商
docker compose up -d --build
docker compose logs -f        # 启动日志里有访问地址和令牌
```

常用命令：

```bash
docker compose ps                                   # 状态（含健康检查）
docker compose run --rm optimizer doctor            # 体检
docker compose run --rm optimizer scan -p eo-lt -q  # 手动跑一轮
docker compose down                                 # 停止
```

---

## 四、三台机器（联通/电信/移动）怎么部署

每台机器上跑同样的安装，只是**运营商选自己**：

```bash
# 联通机
curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh   # 选 1

# 电信机
curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh   # 选 2

# 移动机
curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh   # 选 3
```

脚本会把运营商写进 `.env`（`EO_NETWORK`）并配好对应平台：
联通 → `eo-lt,esa-lt,cft-lt`，电信 → `eo-dx,esa-dx,cft-dx`，移动 → `eo-yd,esa-yd,cft-yd`。

**为什么必须分开**：延迟只能从该运营商网络内部测。在联通机器上跑电信的优选没有意义。

每台机器的 `results/`、`config/config.json` 都是本地的，互不影响；
只有 DNS 记录是共享的，但各写各的运营商记录，不会撞车。

非交互批量部署：

```bash
EO_NETWORK=lt EO_WEB_PORT=8787 ./install.sh --yes
```

---

## 五、常见问题

**Q：拉代码时一直提示 `Username for 'https://gitee.com'`，输了密码还是 `Authentication failed`**

仓库是**公开**的，正常不需要任何认证。出现这个提示是因为**服务器上存着一份过期的 Gitee 凭据**，
git 会自动拿它去认证，失败后又来问你用户名密码。

装脚本已经禁用了凭据询问并忽略本机凭据，正常情况下不会再弹。如果你想根治（其它 git 操作也不再弹）：

```bash
git config --global --unset credential.helper
rm -f ~/.git-credentials
git config --global --list | grep -i credential     # 确认已清干净，应无输出
```

彻底不碰 git 的装法（推荐给只有这一台机器的场景）：

```bash
mkdir -p /opt/edge-optimizer && cd /opt/edge-optimizer
curl -fL https://gitee.com/peroe/eo/repository/archive/main.tar.gz | tar xz --strip-components=1
sudo ./install.sh
```

如果哪天你把仓库改成了私有，用令牌拉取：

```bash
GITEE_USER=你的用户名 GITEE_TOKEN=你的私人令牌 \
  curl -fsSL https://gitee.com/peroe/eo/raw/main/install.sh | sudo sh
```

**Q：git 拉取失败时脚本会怎么办**

会自动改用压缩包下载（`/repository/archive/main.tar.gz`，无需认证）。
所以即使凭据、网络策略有问题，安装通常也能继续；只是这种方式没有 `.git`，
以后升级要重新下载，或者改成 `git clone` 一次。

**Q：`EO_WEB_PORT` 在 `.env` 里改了，为什么网页上还是旧端口？**
端口是启动参数，改完要重启服务：`sudo ./deploy.sh restart`。
（其他运行参数如并发上限、轮询间隔改完是立即生效的，不用重启。）

**Q：网页上改了值，`.env` 里也写了，到底哪个生效？**
网页优先。控制台的「常用变量」面板会明确标出每一项当前生效值的来源
（网页设置 / .env / 默认值），一眼就能看出。

**Q：端口被占用 / 想换端口**
```bash
sudo ./deploy.sh config      # 重新交互配置运营商与端口，自动重启
```

**Q：忘记访问令牌**
```bash
./deploy.sh status                                # 会打印令牌
grep EO_WEB_TOKEN .env                            # 或直接从 .env 读
```

**Q：`doctor` 报「链路不可信」**
说明本机（或所在网络）有代理/透明网关在接管 HTTPS，此时优选结果无意义，
程序会拒绝下发 DNS。请换到干净网络的服务器，或排查本机代理后重试。

**Q：不想对外暴露网页端口**
只监听本机 + SSH 隧道：
```bash
# .env 里设置 EO_WEB_HOST=127.0.0.1（或 docker-compose 端口写成 127.0.0.1:8787:8787）
ssh -L 8787:127.0.0.1:8787 user@服务器
# 本地浏览器打开 http://127.0.0.1:8787/?token=...
```

**Q：升级到最新版**
```bash
cd /opt/edge-optimizer
git pull
./deploy.sh restart          # Docker 用 docker compose up -d --build
```
