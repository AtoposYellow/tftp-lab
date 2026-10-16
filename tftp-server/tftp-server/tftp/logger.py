"""
三路输出的统一事件日志

实验要求"能够记录日志，对于用户操作、传输成功、传输失败、超时重传等行为
记录日志"。本模块提供 :class:`EventLog`，把日志同时写到三个目的地：
    1. 彩色控制台
       Windows 下通过 ctypes 启用终端的 ANSI 转义序列（VT 模式）着色
    2. 滚动日志文件 —— logs/tftp.log，每天 0 点轮转，保留 14 份历史；
    3. 内存环形缓冲 —— 固定容量的 deque，带自增编号，供 Web 界面
       增量拉取实时日志（/api/logs?after_id=N）。

日志事件按类型（event）分类，便于 Web 端筛选：
    request    收到客户端请求
    success    传输成功
    failure    传输失败（含具体原因）
    retransmit 超时重传 / 重复报文处理
    denied     请求被拒绝（路径越权、并发超限、非法报文等）
    system     服务器生命周期（启动 / 停止 / 配置变更）
    http       Web 管理界面的访问日志（低优先级）
"""

import logging
import logging.handlers
import os
import sys
import threading
import time
from collections import deque
from typing import List, Optional


def _enable_windows_ansi() -> None:
    """
    在 Windows 控制台启用 ANSI 转义序列

    Windows 10 之前的控制台默认不解析 ANSI 颜色码，需要通过 SetConsoleMode
    打开 VT 开关；任何一步失败（旧系统 / 输出重定向）都静默降级为无色输出。
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # -11 = STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass  # 着色失败不影响功能


def _setup_stdout_utf8() -> None:
    """
    尽量把标准输出切换为 UTF-8 编码，避免 Windows 控制台输出中文乱码。

    Python 3.7+ 的 io.TextIOWrapper 支持 reconfigure()；若环境不支持
    （旧解释器 / 输出流被替换）则静默忽略。
    """
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# 内存环形缓冲（Web 界面实时日志的数据源）

# 保存最近的日志，按编号查询
class MemoryBuffer:
    """
    线程安全的日志环形缓冲。

    每条日志保存为 dict：{"id", "time", "level", "event", "message"}，
    其中 id 单调递增，Web 端携带 after_id 即可增量拉取新日志。
    """

    def __init__(self, capacity: int = 2000):
        self._lock = threading.Lock()              # 防止多个线程同时操作日志缓冲区
        self._buf: deque = deque(maxlen=capacity)  # 超出容量自动丢弃最旧条目
        self._next_id = 1                          # 下一条日志的编号

    def add(self, level: str, event: str, message: str) -> None:
        """向缓冲追加一条日志（线程安全）。"""
        with self._lock:
            # 编号和写入必须在同一把锁内完成，否则两个会话线程可能拿到同一个 id。
            entry = {
                'id': self._next_id,
                'time': time.strftime('%H:%M:%S'),
                'level': level,
                'event': event,
                'message': message,
            }
            self._next_id += 1
            self._buf.append(entry)

    # 增量获取日志  只获取编号大于 after_id 的新日志，并限制返回条数
    def get_after(self, after_id: int = 0, limit: int = 500) -> List[dict]:
        """取出编号大于 after_id 的日志（最多 limit 条），供 Web 增量拉取。"""
        with self._lock:
            result = [e for e in self._buf if e['id'] > after_id]
        return result[-limit:] if len(result) > limit else result
        # 只保存最新的若干条日志

# 控制台彩色格式化器


class ColorFormatter(logging.Formatter):
    """按日志级别给控制台输出着色。"""

    # 级别 -> ANSI 颜色代码
    COLORS = {
        logging.DEBUG: '\033[90m',     # 亮灰
        logging.INFO: '\033[36m',      # 青色
        logging.WARNING: '\033[33m',   # 黄色
        logging.ERROR: '\033[31m',     # 红色
        logging.CRITICAL: '\033[35m',  # 紫色
    }
    RESET = '\033[0m'

    def __init__(self, fmt: str, datefmt: Optional[str] = None, use_color: bool = True):
        super().__init__(fmt, datefmt=datefmt)
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self._use_color:
            color = self.COLORS.get(record.levelno)
            if color:
                text = f'{color}{text}{self.RESET}'
        return text


# 事件日志门面
class EventLog:
    """
    TFTP 服务器的统一日志门面。

    会话层 / 服务器层 / Web 层都通过本类提供的语义化方法记录日志，
    内部同时分派到：控制台 + 文件（经由 logging.Logger）与内存环形缓冲。
    """

    # 语义化方法名 -> (logging 级别, Web 端事件类型)
    _EVENTS = {
        'request': (logging.INFO, 'request'),
        'success': (logging.INFO, 'success'),
        'failure': (logging.WARNING, 'failure'),
        'retransmit': (logging.WARNING, 'retransmit'),
        'denied': (logging.WARNING, 'denied'),
        'system': (logging.INFO, 'system'),
        'http': (logging.DEBUG, 'http'),
        'info': (logging.INFO, 'system'),
        'warn': (logging.WARNING, 'system'),
        'error': (logging.ERROR, 'system'),
        'debug': (logging.DEBUG, 'system'),
    }

    def __init__(self, log_dir: str = 'logs', console_level: str = 'INFO',
                 file_level: str = 'DEBUG', buffer_capacity: int = 2000,
                 console_enabled: bool = True):
        _setup_stdout_utf8()
        _enable_windows_ansi()

        self.buffer = MemoryBuffer(buffer_capacity)
        self._logger = logging.getLogger('tftp')
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()   # 避免重复初始化时叠加 handler
        self._logger.propagate = False  # 不向 root logger 传播，避免重复输出

        # 输出 1：彩色控制台
        if console_enabled:
            console = logging.StreamHandler(sys.stdout)
            console.setLevel(getattr(logging, console_level.upper(), logging.INFO))
            console.setFormatter(ColorFormatter(
                '%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'))
            self._logger.addHandler(console)

        # 输出 2：按天滚动的日志文件
        os.makedirs(log_dir, exist_ok=True)
        file_path = os.path.join(log_dir, 'tftp.log')
        file_handler = logging.handlers.TimedRotatingFileHandler(
            file_path, when='midnight', backupCount=14, encoding='utf-8')
        file_handler.setLevel(getattr(logging, file_level.upper(), logging.DEBUG))
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s'))
        self._logger.addHandler(file_handler)

    # 语义化日志方法
    def _emit(self, method: str, message: str) -> None:
        """把一条语义化事件同时写到 logging 三路与内存缓冲。"""
        level, event = self._EVENTS[method]
        self._logger.log(level, message)
        if event != 'http':
            # Web 界面自身的轮询访问日志不进入网页日志面板，避免刷屏
            # 淹没 TFTP 业务日志（控制台/文件仍以 DEBUG 级保留，便于排查）
            self.buffer.add(logging.getLevelName(level), event, message)

    def request(self, opcode_name: str, client: str, filename: str, mode: str) -> None:
        """记录收到客户端请求（用户操作）。"""
        self._emit('request', f'收到 {opcode_name} 请求 [{client}] 文件="{filename}" 模式={mode}')

    def success(self, direction: str, client: str, filename: str,
                size_bytes: int, duration: float, speed: float, md5: str) -> None:
        """记录传输成功：字节数 / 耗时 / 平均吞吐量 / MD5。"""
        self._emit('success',
                   f'{direction}成功 [{client}] 文件="{filename}" '
                   f'{size_bytes} 字节 耗时 {duration:.2f}s '
                   f'平均 {EventLog.fmt_speed(speed)} MD5={md5}')

    def failure(self, direction: str, client: str, filename: str, reason: str) -> None:
        """记录传输失败及具体原因（实验要求展示失败原因）。"""
        self._emit('failure', f'{direction}失败 [{client}] 文件="{filename}" 原因：{reason}')

    def retransmit(self, tid: int, client: str, detail: str, attempt: int) -> None:
        """记录一次超时重传 / 重复报文处理（异常链路可靠传输的观测点）。"""
        self._emit('retransmit',
                   f'[传输#{tid}] {detail} 第 {attempt} 次重传 [{client}]')

    def denied(self, client: str, reason: str) -> None:
        """记录被拒绝的请求（路径越权 / 并发超限 / 非法报文等）。"""
        self._emit('denied', f'拒绝请求 [{client}] 原因：{reason}')

    # 下面这些方法故意写成显式包装，而不是使用 __getattr__ 动态生成。
    # 对阅读代码的同学来说，可以直接看到日志 API；调用方式与之前完全相同。
    def system(self, message: str) -> None:
        """记录服务器生命周期或配置变更。"""
        self._emit('system', message)

    def info(self, message: str) -> None:
        """记录普通信息（归入 Web 的 system 事件）。"""
        self._emit('info', message)

    def warn(self, message: str) -> None:
        """记录警告信息。"""
        self._emit('warn', message)

    def error(self, message: str) -> None:
        """记录错误信息。"""
        self._emit('error', message)

    def debug(self, message: str) -> None:
        """记录调试信息。"""
        self._emit('debug', message)

    def http(self, message: str) -> None:
        """记录 Web 访问日志（不显示在网页业务日志面板）。"""
        self._emit('http', message)

    # 工具方法
    @staticmethod
    def fmt_speed(bytes_per_second: float) -> str:
        """把字节/秒格式化为人类可读的速率字符串。"""
        if bytes_per_second >= 1024 * 1024:
            return f'{bytes_per_second / 1024 / 1024:.2f} MB/s'
        if bytes_per_second >= 1024:
            return f'{bytes_per_second / 1024:.2f} KB/s'
        return f'{bytes_per_second:.0f} B/s'

    @staticmethod
    def fmt_size(num_bytes: int) -> str:
        """把字节数格式化为人类可读的大小字符串。"""
        if num_bytes >= 1024 * 1024:
            return f'{num_bytes / 1024 / 1024:.2f} MB'
        if num_bytes >= 1024:
            return f'{num_bytes / 1024:.1f} KB'
        return f'{num_bytes} B'
