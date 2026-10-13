# -*- coding: utf-8 -*-
"""
吞吐量测量与全局统计模块

实验要求"能够显示文件上传与下载的吞吐量"。本模块提供两个类：

    ThroughputMeter —— 单次传输的吞吐量测量
        * add(n)：每传输 n 个字节调用一次；
        * instant()：滑动窗口（约 1 秒）内的瞬时速率；
        * avg()：从开始到现在的平均速率（总字节 / 总耗时）。

    GlobalStats —— 服务器全局统计（线程安全）
        * 累计传输次数 / 成功数 / 失败数；
        * 累计传输字节数 / 累计重传次数；
        * 峰值并发传输数。
        Web 界面的统计卡片数据来源于此。
"""

import threading
import time
from collections import deque
from typing import Optional

# 滑动窗口长度（秒）：瞬时速率在最近该时长内统计
WINDOW_SECONDS = 1.0


class ThroughputMeter:
    """
    单次传输的吞吐量测量器。

    实现原理：维护一个 (时间戳, 累计字节) 的采样队列，
    瞬时速率 = （当前累计字节 - 窗口起点累计字节）/（当前时间 - 窗口起点时间），
    窗口起点不断前移（滑出 WINDOW_SECONDS 之外的旧采样被丢弃）。
    """

    def __init__(self) -> None:
        self.start_time = time.time()   # 传输开始时刻
        self.end_time: Optional[float] = None  # 冻结的计时终点（实际传输结束时刻）
        self.total_bytes = 0            # 累计传输字节数
        # 采样队列：(时间戳, 该时刻的累计字节数)
        self._samples: deque = deque()
        self._samples.append((self.start_time, 0))

    def add(self, num_bytes: int) -> None:
        """记录本次新传输的字节数（每收到/发出一个 DATA 块调用一次）。"""
        self.total_bytes += num_bytes
        now = time.time()
        self._samples.append((now, self.total_bytes))
        # 丢弃窗口之外的旧采样（保留至少 1 个作为窗口起点）
        while len(self._samples) > 2 and now - self._samples[0][0] > WINDOW_SECONDS:
            self._samples.popleft()

    def instant(self) -> float:
        """瞬时吞吐量（字节/秒）：最近一个滑动窗口内的平均速率。"""
        if len(self._samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        elapsed = t1 - t0
        if elapsed <= 0:
            return 0.0
        return (b1 - b0) / elapsed

    def freeze(self) -> None:
        """
        冻结计时终点：最后一个 DATA 块被确认时调用。

        此后 avg() 以冻结时刻为终点——服务器收尾工作（如 WRQ 的末块
        确认宽限期）只是协议层面的善后等待，不应计入传输耗时与平均速率，
        否则 Web 界面显示的耗时会比客户端实际测得的时间长。
        """
        if self.end_time is None:
            self.end_time = time.time()

    def avg(self) -> float:
        """平均吞吐量（字节/秒）：总字节 / 总耗时（冻结后不含收尾时间）。"""
        end = self.end_time if self.end_time is not None else time.time()
        elapsed = end - self.start_time
        if elapsed <= 0:
            return 0.0
        return self.total_bytes / elapsed


class GlobalStats:
    """
    服务器全局统计计数器（线程安全）。

    各计数器由会话线程 / 监听线程在不同时刻更新，
    用一把锁保护全部自增操作，读取通过 snapshot() 拿一致性快照。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._transfers_total = 0      # 累计传输次数（会话启动即计数）
        self._transfers_ok = 0         # 成功次数
        self._transfers_failed = 0     # 失败次数
        self._bytes_total = 0          # 累计传输字节（成功 + 失败的实际字节数）
        self._retransmits_total = 0    # 累计重传次数（含重复报文处理）
        self._peak_concurrent = 0      # 峰值并发传输数
        self._started_at = time.time() # 服务器启动时刻（计算运行时长）

    # ------------------------------------------------------------------
    # 更新接口（会话线程 / 服务器线程调用）
    # ------------------------------------------------------------------
    def on_transfer_start(self) -> None:
        """会话启动：累计次数 +1。"""
        with self._lock:
            self._transfers_total += 1

    def on_transfer_success(self, num_bytes: int) -> None:
        """会话成功结束：成功数 +1，累计字节累加。"""
        with self._lock:
            self._transfers_ok += 1
            self._bytes_total += num_bytes

    def on_transfer_failure(self, num_bytes: int) -> None:
        """会话失败结束：失败数 +1，累计字节累加（已传部分）。"""
        with self._lock:
            self._transfers_failed += 1
            self._bytes_total += num_bytes

    def on_retransmit(self) -> None:
        """发生一次重传（或重复报文处理）：累计重传 +1。"""
        with self._lock:
            self._retransmits_total += 1

    def update_concurrency(self, current_active: int) -> None:
        """服务器在会话启动时调用：刷新峰值并发数。"""
        with self._lock:
            if current_active > self._peak_concurrent:
                self._peak_concurrent = current_active

    # ------------------------------------------------------------------
    # 读取接口
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """返回全部统计的一致性快照（Web 界面统计卡片）。"""
        with self._lock:
            return {
                'transfers_total': self._transfers_total,
                'transfers_ok': self._transfers_ok,
                'transfers_failed': self._transfers_failed,
                'bytes_total': self._bytes_total,
                'retransmits_total': self._retransmits_total,
                'peak_concurrent': self._peak_concurrent,
                'uptime_s': round(time.time() - self._started_at, 1),
                'started_at': self._started_at,
            }
