# -*- coding: utf-8 -*-
"""
UDP 损伤代理（D 组测试用，等价于 clumsy 的注入能力，但可脚本化精确控制）

拓扑:  tftp.exe(客户端) ──> 127.0.0.1:69 (本代理) ──> 127.0.0.1:1069 (TFTP 服务器)

损伤类型（每个方向独立配置，概率按包计）:
    drop     丢包
    lag_ms   延迟（毫秒，定时后发出）
    dup      重复（同一包发两份）
    reorder  乱序（扣住一个包，等下一个包先走再释放 -> 先到者后至）

profile.json 格式:
    {"c2s": {"drop":0.1}, "s2c": {}}
代理每个包到来时重新读取 profile，因此可以在传输过程中动态增删损伤。
"""
import socket
import select
import json
import threading
import time
import os
import random

BASE = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(BASE, "profile.json")
STATS_PATH = os.path.join(BASE, "proxy_stats.json")
LOG_PATH = os.path.join(BASE, "proxy.log")

LISTEN_ADDR = ("127.0.0.1", 69)
SERVER_ADDR = ("127.0.0.1", 1069)

_lock = threading.Lock()
_stats = {
    "c2s": {"fwd": 0, "drop": 0, "dup": 0, "lag": 0, "reorder": 0},
    "s2c": {"fwd": 0, "drop": 0, "dup": 0, "lag": 0, "reorder": 0},
}


def log(msg):
    line = time.strftime("[%H:%M:%S] ") + msg
    with _lock:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        print(line, flush=True)


def stat(dkey, kind):
    with _lock:
        _stats[dkey][kind] += 1


def save_stats():
    with _lock:
        try:
            with open(STATS_PATH, "w", encoding="utf-8") as f:
                json.dump(_stats, f)
        except OSError:
            pass


def load_profile():
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {"c2s": {}, "s2c": {}}


class DamageProxy:
    def __init__(self, sock):
        self.sock = sock
        self.hlock = threading.Lock()
        self.held = None  # (payload, dst) 被乱序扣住的包

    # ---------- 底层发送 ----------
    def _safe_send(self, payload, dst):
        try:
            self.sock.sendto(payload, dst)
        except OSError:
            pass

    # ---------- 乱序 ----------
    def _release_later(self, held, delay):
        def _send():
            self._safe_send(held[0], held[1])
            log("    [reorder] 迟到的包已释放")
        threading.Timer(delay, _send).start()

    def _take_held(self):
        with self.hlock:
            h = self.held
            self.held = None
            return h

    # ---------- 单包处理 ----------
    def process(self, data, addr, dst, dkey, label):
        prof = load_profile().get(dkey, {})
        reorder = float(prof.get("reorder", 0))
        lag_ms = float(prof.get("lag_ms", 0))
        dup = float(prof.get("dup", 0))
        drop = float(prof.get("drop", 0))

        # 先释放上一个被扣住的包（延迟一点点，保证"后到者先至"）
        h = self._take_held()
        if h is not None:
            self._release_later(h, 0.15)

        # 乱序：本次包被扣住，等下一个包触发释放
        if reorder and random.random() < reorder:
            stat(dkey, "reorder")
            with self.hlock:
                self.held = (data, dst)
            log(f"    [reorder] {label} 被扣住")
            return

        # 延迟
        if lag_ms > 0:
            stat(dkey, "lag")
            threading.Timer(lag_ms / 1000.0, lambda: self._safe_send(data, dst)).start()
            return

        # 重复：先发一份，随后再发一份
        if dup and random.random() < dup:
            stat(dkey, "dup")
            self._safe_send(data, dst)
            self._safe_send(data, dst)
            log(f"    [dup] {label} 重复发送")
            return

        # 丢包
        if drop and random.random() < drop:
            stat(dkey, "drop")
            log(f"    [drop] {label} 丢弃")
            return

        stat(dkey, "fwd")
        self._safe_send(data, dst)

    def flush_held(self):
        h = self._take_held()
        if h is not None:
            self._safe_send(h[0], h[1])
            log("    [reorder] flush 滞留包")


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(LISTEN_ADDR)
    proxy = DamageProxy(sock)
    log(f"损伤代理启动: 监听 {LISTEN_ADDR}  ->  后端 {SERVER_ADDR}")

    client = None
    server_tid = SERVER_ADDR

    while True:
        try:
            r, _, _ = select.select([sock], [], [], 0.5)
        except OSError:
            continue
        if not r:
            save_stats()
            continue
        try:
            data, addr = sock.recvfrom(4096)
        except OSError:
            continue

        op = int.from_bytes(data[:2], "big") if len(data) >= 2 else 0

        if op in (1, 2):  # RRQ / WRQ：新请求（可能来自新的客户端端口）
            client = addr
            server_tid = SERVER_ADDR
            log(f"新请求 op={op} 来自 {addr}，{len(data)} 字节")
            proxy.process(data, addr, SERVER_ADDR, "c2s", f"客户端请求 op={op}")
        elif addr == client:
            proxy.process(data, addr, server_tid, "c2s", f"客户端包 op={op} {len(data)}B")
        else:
            server_tid = addr  # 服务器会话 TID（每个新会话都会变）
            if client is not None:
                proxy.process(data, addr, client, "s2c", f"服务器包 op={op} {len(data)}B")
        save_stats()


if __name__ == "__main__":
    try:
        main()
    finally:
        save_stats()
