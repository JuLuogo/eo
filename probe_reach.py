#!/usr/bin/env python3
"""先探一下：本机能不能连到真实外网（决定测速校验怎么做）。

之前测过 1.1.1.1 / 8.8.8.8 等全部超时，说明这台机器在透明代理后面。
如果确实连不通外网，延迟校验只能在本地服务器上做（能验证"测得准不准"，
但验证不了"跨公网 RTT 是否可信"——那部分如实说明）。
"""
import socket
import ssl
import time

CASES = [
    ("1.1.1.1", 443, "one.one.one.one"),
    ("8.8.8.8", 443, "dns.google"),
    ("223.5.5.5", 443, "www.aliyun.com"),
    ("119.29.29.29", 443, "www.dnspod.cn"),
    ("180.163.150.1", 443, "www.qq.com"),      # 腾讯 CDN 常见段
    ("101.33.20.1", 443, "cloud.tencent.com"),
]

print(f"{'IP':<18} {'TCP 握手':<12} {'TLS 握手':<12} {'结果'}")
print("-" * 66)
for ip, port, host in CASES:
    tcp_ms = tls_ms = None
    note = ""
    try:
        t0 = time.perf_counter()
        s = socket.create_connection((ip, port), timeout=3)
        tcp_ms = (time.perf_counter() - t0) * 1000
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            s.settimeout(3)
            t1 = time.perf_counter()
            s = ctx.wrap_socket(s, server_hostname=host)
            tls_ms = (time.perf_counter() - t1) * 1000
            note = "可达（含 TLS）"
        except Exception as exc:                                   # noqa: BLE001
            note = f"TCP 通但 TLS 失败: {type(exc).__name__}"
        s.close()
    except Exception as exc:                                       # noqa: BLE001
        note = f"{type(exc).__name__}: {exc}"[:40]
    tcp_s = f"{tcp_ms:.1f}ms" if tcp_ms is not None else "-"
    tls_s = f"{tls_ms:.1f}ms" if tls_ms is not None else "-"
    print(f"{ip:<18} {tcp_s:<12} {tls_s:<12} {note}")
