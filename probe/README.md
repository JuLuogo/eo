# Komari 节点探针（komari-node-probe）

给 Komari 的 node-collector 插件喂数据的轻量探针：采集 **CPU 温度** 与
**IPv4/IPv6 分地址族的 TCP/UDP 连接数**（Komari 协议里没有这两项）。

- linux-amd64 / linux-arm64 / linux-386：静态二进制，纯 Go 标准库，零第三方依赖
- komari-node-probe.service：systemd 单元
- 采样 1 秒、批量 10 秒上报；默认 HTTP，可加 `-ws` 走 WebSocket 长连接（断线自动回落 HTTP）

安装：在 Komari 后台 -> 插件管理 -> 节点探针 里，对目标节点点「二进制」，
复制生成好的一键命令到该节点执行即可（UUID 与令牌会自动填好）。

不向面板上报，与官方 agent 完全独立、互不干扰。
