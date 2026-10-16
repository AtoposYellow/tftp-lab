"""

    ServerConfig —— 服务器配置（dataclass），支持 JSON 持久化；
    PathGuard    —— 路径安全守卫：把客户端给出的文件名限制在服务器根目录内；
    TFTPServer   —— 服务器主体：
        * 监听线程常驻 UDP 69 端口（TFTP 约定端口）等待 RRQ / WRQ；
        * 每个合法请求创建一个 TransferSession（新线程 + 新 TID）；
        * 请求去重：客户端在收不到响应时会向 69 端口重发同一请求，
          必须识别并忽略，否则会为同一客户端重复建立会话；
        * 并发上限：超过 max_concurrent 的请求回 ERROR(0) 并拒绝；
        * start()/stop() 优雅启停。

线程模型总览：
    主线程          —— 程序入口，负责启动/停止与信号处理；
    监听线程        —— TFTPServer._listen_loop，只做"收请求 + 派发"；
    会话线程    —— TransferSession.run，每个客户端请求一个；
    Web 服务线程    —— web/app.py 的 HTTP 服务（独立于 TFTP 监听）。
"""

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from posixpath import normpath
from typing import Dict, Optional, Set, Tuple

from tftp.packets import (MAX_PACKET_SIZE, OP_RRQ, ErrorPacket,
                          PacketFormatError, PacketParser, RequestPacket)
from tftp.session import TransferSession


@dataclass # 可以自动生成初始化方法
# 配置Config
class ServerConfig:
    """
    服务器全部可配置项（对应项目根目录 config.json）。
    """

    tftp_port: int = 69              # TFTP 监听的 UDP 端口
    web_port: int = 8080             # Web 管理界面的 HTTP 端口
    root_dir: str = 'files'          # 服务器根目录：所有上传/下载都限制在该目录内
    log_dir: str = 'logs'            # 日志文件目录
    log_level: str = 'INFO'          # 控制台日志级别
    timeout: float = 2.0        # 收包超时秒数
    retries: int = 6                 # 最大重传次数
    max_concurrent: int = 32         # 最大并发传输数
    grace_seconds: int = 5           # 末块宽限秒数（WRQ最终ACK后
    overwrite: bool = True           # 是否覆盖同名文件

    @classmethod
    def load(cls, path: str) -> 'ServerConfig':
        """从 JSON 文件加载配置；文件不存在或字段缺失时用默认值补齐。"""
        cfg = cls()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for key, value in data.items():
                # 只读取 dataclass 中已有的公开字段；类型以默认值为准。
                # 这样 config.json 可以只写需要修改的几项，新增未知字段也会被忽略。
                if hasattr(cfg, key) and not key.startswith('_'):
                    setattr(cfg, key, type(getattr(cfg, key))(value))
        except (OSError, ValueError):
            pass  # 配置文件损坏时静默回退到默认值（首次运行时本就无文件）
        return cfg

    def save(self, path: str) -> None:
        """把当前配置写回 JSON 文件（Web 端修改配置时调用）。"""
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)


# 路径安全守卫
class PathGuard:
    """
    把客户端提供的文件名安全地解析为服务器根目录内的绝对路径。
    """

    # Windows 保留设备名（不区分大小写、可带扩展名，如 "NUL.txt"）
    _WIN_RESERVED = re.compile(
        r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$', re.IGNORECASE)

    def __init__(self, root_dir: str):
        # 根目录统一转为绝对路径；不存在时由调用方（TFTPServer）负责创建
        self.root = Path(root_dir).resolve()

    def resolve(self, filename: str) -> Path:
        """
        解析文件名为根目录内的绝对路径。

        :raises ValueError: 文件名非法或试图越出根目录时抛出，
            会话层将其转换为 ERROR(2) Access violation 回给客户端。
        """
        if not filename or '\x00' in filename:
            raise ValueError('空文件名或含非法字节')

        name = filename.strip().replace('\\', '/')

        # 规则 2：绝对路径与 Windows 盘符
        if name.startswith('/') or re.match(r'^[A-Za-z]:', name):
            raise ValueError(f'不允许绝对路径: {filename}')

        # 规则 4：Windows 保留设备名（按每一段检查，如 "sub/CON"）
        for part in name.split('/'):
            if self._WIN_RESERVED.match(part):
                raise ValueError(f'Windows 保留设备名: {part}')

        # 规则 3：posix 规范化后检查 ".."
        normalized = normpath(name)
        if normalized.startswith('..') or '/..' in normalized:
            raise ValueError(f'不允许目录穿越: {filename}')
        if normalized in ('.', ''):
            raise ValueError('空文件名')

        # 规则 5：拼接后必须仍位于根目录内（双保险）
        resolved = (self.root / normalized).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError(f'路径越出根目录: {filename}')
        return resolved


# 服务器主体
class TFTPServer:
    """
    TFTP 服务器主体：监听线程 + 会话线程管理 + 请求去重 + 并发控制
    """

    def __init__(self, config: ServerConfig, log, registry, stats):
        self.config = config
        self.log = log
        self.registry = registry
        self.stats = stats

        # 停机标志：会话线程在各循环点检查，置位后尽快终止
        self.stopping = threading.Event()

        # 监听 Socket 与监听线程（start() 中创建）
        self._listen_sock: Optional[object] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None

        # 并发控制信号量：每个会话占用 1 个名额，超限时拒绝新请求
        self._slots = threading.BoundedSemaphore(config.max_concurrent)

        # 会话线程跟踪集合（stop 时逐个 join / 关闭 Socket）
        self._sessions: Set[TransferSession] = set()
        self._sessions_lock = threading.Lock()

        # 请求去重表：客户端地址 -> (请求指纹, 时间戳)
        # 客户端在超时收不到响应时会向 69 端口重发同一 RRQ/WRQ，
        # 去重表保证同一客户端的同一请求在短时间内只建立一个会话。
        self._recent_requests: Dict[Tuple[str, int], Tuple[bytes, float]] = {}
        self._DEDUP_TTL = 15.0  # 去重窗口（秒），超过后允许重新建立会话

        # 路径安全守卫（根目录不存在则创建）
        Path(config.root_dir).mkdir(parents=True, exist_ok=True)
        self.path_guard = PathGuard(config.root_dir)

    # 启动 / 停止
    def start(self) -> None:
        """
        启动 TFTP 服务：绑定 UDP 端口并启动监听线程。

        :raises OSError: 端口被占用或无权限绑定时抛出（由入口/Web 层提示）。
        """
        # 判断监听线程是否已经运行
        if self._listen_thread is not None and self._listen_thread.is_alive():
            return  # 已在运行
        self.stopping.clear()   # 清除停止标志

        # 创建 UDP Socket
        sock = _make_udp_socket()
        # 并绑定服务器监听端口
        sock.bind(('0.0.0.0', self.config.tftp_port))
        self._listen_sock = sock
        self._started_at = time.time()

        # 创建监听线程，指定其执行_listen_loop
        self._listen_thread = threading.Thread(
            target=self._listen_loop, name='tftp-listen', daemon=True)
        self._listen_thread.start()
        self.log.system(f'TFTP 服务器已启动：UDP 端口 {self.config.tftp_port}，'
                        f'根目录 "{os.path.abspath(self.config.root_dir)}"')

    # 停止服务器运行
    def stop(self) -> None:
        """
        优雅停机：先停止收新请求，再关闭所有会话线程，最后释放资源。
        （幂等：重复调用安全；Web 端的"停止服务"按钮也调用本方法。）
        """
        # 设置 self.stopping，通知监听线程和会话线程停止
        if not self.stopping.is_set():
            self.stopping.set()
            self.log.system('TFTP 服务器正在停止……')

        # 1. 关闭监听 Socket：阻塞在 recvfrom 的监听线程立即解除阻塞
        if self._listen_sock is not None:
            try:
                self._listen_sock.close()
            except OSError:
                pass
            self._listen_sock = None

        # 2. 等待监听线程退出
        if self._listen_thread is not None:
            self._listen_thread.join(timeout=3.0)
            self._listen_thread = None

        # 3. 关闭全部会话 Socket（会话线程随即终止并自行清理登记表）
        with self._sessions_lock:
            sessions = list(self._sessions)
        for session in sessions:
            session.shutdown()
        for session in sessions:
            session.join(timeout=3.0)
            # 表示当前线程最多等待目标线程约 3 秒结束
        self.log.system('TFTP 服务器已停止')

    @property
    def is_running(self) -> bool:
        """服务器是否正在运行（供 Web 界面状态灯使用）。"""
        return (not self.stopping.is_set()
                and self._listen_thread is not None
                and self._listen_thread.is_alive())

    @property
    def uptime(self) -> float:
        """本次运行时长（秒；未启动为 0）。"""
        return time.time() - self._started_at if self._started_at else 0.0

    # 监听循环：收请求 -> 去重 -> 限流 -> 派发会话线程
    def _listen_loop(self) -> None:
        """
        监听线程主循环（协议约定的 69 端口）。
        只负责接收和分配请求，不负责完整的文件传输
        """
        # 在服务器运行期间不断接收客户端请求
        sock = self._listen_sock
        while not self.stopping.is_set():  # 检查停止标志
            try:     # 等待 UDP 请求，收到数据及客户端地址
                data, addr = sock.recvfrom(MAX_PACKET_SIZE * 4)
            except OSError:
                # Socket 被 stop() 关闭（或系统级错误）-> 检查标志后退出
                if self.stopping.is_set():
                    return
                continue

            # 处理请求
            try:
                self._dispatch(data, addr)
            except Exception as exc:
                # 单个请求的处理异常不影响监听循环
                self.log.error(f'处理来自 {addr[0]}:{addr[1]} 的请求时出错: {exc!r}')

    # 请求-分发算法
    # 收到一个 UDP 报文后，判断是否接受请求，并为合法请求创建新的传输会话
    def _dispatch(self, data: bytes, addr: Tuple[str, int]) -> None:
        """
        处理一个到达 69 端口的数据报：解析 -> 去重 -> 限流 -> 建会话。
        只有 RRQ/WRQ 请求会进入本函数的处理路径
        """
        client = f'{addr[0]}:{addr[1]}'

        # 解析：非 RRQ/WRQ 或畸形包直接忽略（错误请求不必回应）
        try:
            packet = PacketParser.parse(data)
        except PacketFormatError as exc:
            self.log.denied(client, f'非法报文（{exc}）')
            return
        if not isinstance(packet, RequestPacket):
            self.log.denied(client, f'69 端口收到非请求报文 opcode={packet.opcode}')
            return

        opcode_name = 'RRQ(下载)' if packet.opcode == OP_RRQ else 'WRQ(上传)'
        self.log.request(opcode_name, client, packet.filename, packet.mode)

        # 判断重复请求
        if self._is_duplicate_request(data, addr):
            self.log.debug(f'[{client}] 识别为重发的重复请求，忽略'
                           f'（既有会话仍在处理）')
            return
        # acquire() 表示尝试获取一个信号量名额
        # blocking=False ：如果没有名额，不要阻塞等待，而是立即返回 False。
        if not self._slots.acquire(blocking=False):
            self.log.denied(client, f'并发传输数已达上限 {self.config.max_concurrent}')
            try:
                self._listen_sock.sendto(
                    ErrorPacket(0, 'Server busy: too many transfers').to_bytes(), addr)
            except OSError:
                pass
            return

        # 创建会话并启动线程
        session = TransferSession(self, packet, addr)
        with self._sessions_lock:
            self._sessions.add(session)
        session.start()    # 创建新线程，执行 run()

    def _is_duplicate_request(self, data: bytes, addr: Tuple[str, int]) -> bool:
        """
        判断是否为客户端重发的重复请求
        原理：记录每个客户端地址最近一次请求的 (报文指纹, 时间)。
        若同一地址在 TTL 窗口内发来字节完全相同的请求，视为重发。
        """
        now = time.time()
        fingerprint = bytes(data)  # bytes 作 dict 键即天然指纹
        last = self._recent_requests.get(addr)

        # 顺手清理过期表项，防止长期运行时内存缓慢增长
        if len(self._recent_requests) > 256:
            self._recent_requests = {
                a: v for a, v in self._recent_requests.items()
                if now - v[1] < self._DEDUP_TTL}

        # 这个客户端地址之前是否有请求记录
        # 本次请求与上次请求字节相同
        # 两次请求间隔小于 15s
        is_dup = (last is not None and last[0] == fingerprint
                  and now - last[1] < self._DEDUP_TTL)
        # 检查会话是否存活
        if is_dup:
            with self._sessions_lock:
                has_live = any(s.client_addr == addr and s.is_alive()
                               for s in self._sessions)
            if not has_live:
                is_dup = False   # 无存活会话 -> 允许重建会话
        self._recent_requests[addr] = (fingerprint, now)
        return is_dup
    # 第一次请求	                    创建会话
    # 15 秒内相同请求，旧会话仍存活	    忽略重复请求
    # 15 秒内相同请求，但旧会话已结束	允许建立新会话
    # 超过去重时间窗口	            不因该记录被判为重复
    # 请求字节不同	                不判为相同请求

    # 会话生命周期回调
    # 并发控制
    def on_session_end(self, session: TransferSession) -> None:
        """
        会话结束回调（由 TransferSession._cleanup 调用）：
        从跟踪集合移除会话线程并归还并发名额。
        """
        with self._sessions_lock:
            self._sessions.discard(session)    # 移除已完成会话
        try:
            self._slots.release()              # 调用 release()归还一个并发名额
        except ValueError:
            pass  # 防御：重复释放（正常流程不会发生）

    def active_session_count(self) -> int:
        """当前存活的会话线程数（含正在收尾的）。"""
        with self._sessions_lock:
            return len(self._sessions)

    def resize_slots(self, new_limit: int) -> None:
        """
        调整并发传输上限（Web 端修改配置时调用）。

        直接替换信号量：已在进行的传输不受影响，新请求按新上限放行。
        """
        self._slots = threading.BoundedSemaphore(new_limit)

# 工具函数
def _make_udp_socket():
    """创建监听用 UDP Socket，并设置地址复用（快速重启时不受 TIME_WAIT 限制）。"""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return sock
