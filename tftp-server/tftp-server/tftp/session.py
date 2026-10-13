# -*- coding: utf-8 -*-
"""
传输会话模块：单次文件传输的状态机（一线程、一 UDP Socket、一 TID）

实验手册 2.3 节给出的会话模型（本模块的对应实现）：
    * 客户端向服务器 69 端口发 RRQ/WRQ 发起一次传输；
    * 服务器为每个请求创建一个新的 UDP Socket（绑定临时端口），该端口即
      本次传输的 TID（Transfer ID），后续所有报文都只在这对 TID 之间往来；
    * WRQ：服务器先从会话 Socket 回 ACK(0)，随后逐块接收 DATA 并逐块 ACK，
      收到长度 < 512 字节的 DATA 即认为传输结束；
    * RRQ：服务器从会话 Socket 直接发送 DATA(1)，客户端逐块 ACK，
      发出 < 512 字节的 DATA 且被 ACK 后传输结束。

可靠传输机制（对应验收的"异常环境可靠传输"25 分项）：
    * 超时重传：等待对端报文超时后重发本端最后一个报文，
      连续重传 cfg.retries 次仍失败则终止会话并记录失败原因；
    * 重复报文处理（丢包导致 ACK/DATA 丢失时的自愈）：
      - 收到重复 DATA（块号 = 上一已确认块）→ 说明我方 ACK 丢失，补发 ACK，
        不重复写入文件；
      - 收到重复 ACK（块号 = 上一已发送 DATA）→ 说明我方 DATA 丢失，立即重发；
    * 末块宽限：WRQ 发送最终 ACK 后保持会话数秒，若再次收到末块 DATA
      （说明最终 ACK 丢失）则补发 ACK（手册 2.3 节明确要求）。

大文件支持（块号回绕）：
    协议块号只有 16 位（0~65535）。本模块内部使用无界整数 seq 递增计数，
    线上报文块号取 seq & 0xFFFF；"新块 / 重复块"的判定都用回绕后的值比较，
    因此 65535 -> 0 回绕后状态机依然正确，文件大小不受 32 MiB 限制。

线程模型：TransferSession 继承 threading.Thread，服务器为每个合法请求
创建一个会话对象并 start()，即"每个客户请求启动一个线程"（选做要求）。
"""

import hashlib
import socket
import threading
import time
from pathlib import Path
from typing import Optional

from tftp.netascii import NetasciiDecoder, NetasciiEncoder
from tftp.packets import (BLOCK_SIZE, AckPacket, DataPacket, ErrorPacket,
                          MODE_NETASCII, OP_RRQ, PacketFormatError, PacketParser,
                          RequestPacket, TftpPacket, VALID_MODES)
from tftp.registry import DIR_DOWNLOAD, DIR_UPLOAD, STATE_FAILED, STATE_SUCCESS
from tftp.stats import ThroughputMeter

# 会话接收缓冲区大小：合法报文最长 516 字节，取余量以发现/丢弃畸形大包
RECV_BUFFER_SIZE = 4096


class TransferError(Exception):
    """
    传输过程中的可预期失败（会话层内部用于控制流的异常）。

    属性说明：
        message      —— 失败原因的中文描述（写入日志、登记表与 Web 界面）；
        code         —— 需要发给客户端的 TFTP 错误码（0~7）；
        notify_peer  —— True 时由会话向客户端发送 ERROR 报文；为 False 表示
                        失败信息已由对端告知（对端发来 ERROR）或无需告知
                        （本机停机），不再回发报文。
    """

    def __init__(self, message: str, code: int = 0, notify_peer: bool = True):
        super().__init__(message)
        self.message = message
        self.code = code
        self.notify_peer = notify_peer


class TransferSession(threading.Thread):
    """
    一次 TFTP 传输会话 = 一个线程 + 一个 UDP Socket（TID）+ 一个状态机。

    服务器（tftp/server.py）收到合法的 RRQ/WRQ 后构造本类对象并 start()。
    会话的生命周期：

        __init__  创建会话 Socket（bind 临时端口 = TID）、登记 TransferRecord
        run       前置检查（模式/路径/文件） -> 状态机收发循环 -> 结果处理
        finally   清理：关闭文件与 Socket、从登记表移入历史、释放并发名额
    """

    def __init__(self, server, request: RequestPacket, client_addr: tuple):
        # ---- 线程初始化（守护线程：主进程退出时自动结束）----
        super().__init__(name=f'tftp-{client_addr[0]}:{client_addr[1]}', daemon=True)

        # ---- 服务器各组件的引用（由 TFTPServer 注入）----
        self.server = server
        self.cfg = server.config
        self.log = server.log
        self.registry = server.registry
        self.stats = server.stats

        # ---- 会话基本信息 ----
        self.request = request
        self.client_addr = client_addr                 # (IP, 端口) 二元组
        self.client = f'{client_addr[0]}:{client_addr[1]}'
        self.is_read = request.opcode == OP_RRQ        # True=RRQ下载 False=WRQ上传
        self.direction = DIR_DOWNLOAD if self.is_read else DIR_UPLOAD
        self.use_netascii = request.mode == MODE_NETASCII

        # ---- 传输过程中的可变状态 ----
        self.md5 = hashlib.md5()                       # 本地文件内容的 MD5
        self.file: Optional[object] = None             # 打开的本地文件句柄
        self.path: Optional[Path] = None               # 本地文件的完整路径
        self.meter = ThroughputMeter()                 # 吞吐量测量器
        self._sock_closed = False                      # Socket 是否已关闭

        # ---- 会话 UDP Socket：绑定系统分配的临时端口，即本会话的 TID ----
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('', 0))
        self.tid = self.sock.getsockname()[1]          # 本端的 TID（端口号）
        self.sock.settimeout(self.cfg.timeout)         # 收包超时 -> 驱动重传

        # ---- 在登记表中注册（Web 界面立即能看到这条传输）----
        self.record = self.registry.create(
            self.client, self.direction, request.filename, request.mode)

    # ==================================================================
    # 线程入口与总体流程
    # ==================================================================
    def run(self) -> None:
        """线程入口：前置检查 -> 状态机 -> 结果登记（异常统一收口）。"""
        self.stats.on_transfer_start()
        self.stats.update_concurrency(self.registry.active_count())
        try:
            self._precheck()                       # 模式 / 路径 / 文件检查
            if self.is_read:
                self._run_read()                   # RRQ：服务器发送文件
            else:
                self._run_write()                  # WRQ：服务器接收文件
            self._finish_success()                 # 正常结束 -> 成功登记
        except TransferError as exc:
            self._finish_failure(exc)              # 可预期失败 -> 失败登记
        except socket.timeout:
            # 防御：正常情况下超时都在内层循环处理，这里兜底防漏网
            self._finish_failure(TransferError('连接超时', notify_peer=False))
        except ConnectionError as exc:
            # 对端连接中断（典型：客户端重传预算耗尽先退出并关闭端口，服务器
            # 后续收发立刻收到 WinError 10054 连接重置反馈）。属于网络中断而
            # 非磁盘/文件问题；对端已不可达，无需回送 ERROR。
            self._finish_failure(TransferError(
                f'客户端中断/连接被重置: {exc}', code=0, notify_peer=False))
        except OSError as exc:
            # 服务器停机会主动关闭会话 Socket，此时阻塞中的 recv 抛出
            # OSError——需与真正的本地磁盘/文件错误区分开
            if self.server.stopping.is_set():
                self._finish_failure(TransferError('服务器正在停止', notify_peer=False))
            else:
                errno_val = getattr(exc, 'errno', 0) or 0
                if 10000 <= errno_val < 20000:
                    # 其余 WinSock 网络层错误（网络不可达/被拦截等），也不是磁盘问题
                    self._finish_failure(TransferError(
                        f'网络错误: {exc}', code=0, notify_peer=False))
                else:
                    # 本地文件 / 磁盘错误（如磁盘满 -> ERROR 3；errno 28 = ENOSPC）
                    code = 3 if errno_val == 28 else 0
                    self._finish_failure(TransferError(
                        f'本地磁盘/文件错误: {exc}', code=code, notify_peer=True))
        except Exception as exc:  # 兜底：任何未预期异常都不能让会话静默消失
            self.log.error(f'[传输#{self.record.tid}] 未捕获异常: {exc!r}')
            self._finish_failure(TransferError(
                f'服务器内部错误: {exc}', code=0, notify_peer=False))
        finally:
            self._cleanup()

    # ==================================================================
    # 前置检查：传输模式、路径安全、文件存在性 / 覆盖策略
    # ==================================================================
    def _precheck(self) -> None:
        """
        会话开始前的静态检查，任何一项不通过都以对应错误码终止会话
        （ERROR 报文从本会话的 TID 发出，符合主流 TFTP 服务器行为）。
        """
        # 1. 传输模式：只支持 netascii / octet（mail 等已废弃模式拒绝）
        if self.request.mode not in VALID_MODES:
            raise TransferError(
                f'不支持的传输模式 "{self.request.mode}"', code=4)

        # 2. 路径安全：文件名必须落在服务器根目录内（防目录穿越攻击）
        try:
            self.path = self.server.path_guard.resolve(self.request.filename)
        except ValueError as exc:
            raise TransferError(f'非法的文件路径: {exc}', code=2)

        if self.is_read:
            # 3a. RRQ：文件必须存在且是普通文件（否则 ERROR 1 File not found）
            if not self.path.is_file():
                raise TransferError('文件未找到 (File not found)', code=1)
            self.file = open(self.path, 'rb')
            self.record.total_bytes = self.path.stat().st_size
            self.log.debug(f'[传输#{self.record.tid}] 下载文件 {self.path} '
                           f'({self.record.total_bytes} 字节)')
        else:
            # 3b. WRQ：按覆盖策略检查（关闭覆盖时同名文件 -> ERROR 6）
            if self.path.exists() and not self.cfg.overwrite:
                raise TransferError('文件已经存在 (File already exists)', code=6)
            try:
                # octet/netascii 统一以二进制方式写入，换行转换由解码器负责
                self.file = open(self.path, 'xb' if not self.cfg.overwrite else 'wb')
            except FileExistsError:
                raise TransferError('文件已经存在 (File already exists)', code=6)

    # ==================================================================
    # RRQ 状态机：服务器发送文件（下载）
    # ==================================================================
    def _run_read(self) -> None:
        """
        下载状态机：读本地文件 -> 组块 -> 发 DATA -> 等 ACK -> 下一块。

        与 WRQ 不同，RRQ 没有 ACK(0)：服务器直接从 DATA 块 1 开始。
        传输结束判据：发出的 DATA 载荷 < 512 字节且被确认。
        """
        f = self.file
        encoder = NetasciiEncoder() if self.use_netascii else None
        outbuf = bytearray()   # netascii 转换后的输出缓冲（转换会改变字节数）
        eof = False            # 本地文件是否已读尽
        seq = 1                # 无界块计数器（线上块号 = seq & 0xFFFF）

        while True:
            self._check_stopping()

            # ---- 第 1 步：组装下一个 DATA 载荷（不足 512 字节时继续读文件）----
            while len(outbuf) < BLOCK_SIZE and not eof:
                raw = f.read(BLOCK_SIZE)
                if raw:
                    # MD5 按本地文件原始内容计算（净荷与客户端下载结果一致）
                    self.md5.update(raw)
                    # netascii 模式先转换；octet 模式原样
                    outbuf += encoder.feed(raw) if encoder else raw
                if not raw or len(raw) < BLOCK_SIZE:
                    # 文件读尽：netascii 需 flush 残留的挂起字节（如结尾 CR）
                    if encoder:
                        outbuf += encoder.flush()
                    eof = True

            # ---- 第 2 步：切出至多 512 字节作为本块载荷 ----
            payload = bytes(outbuf[:BLOCK_SIZE])
            del outbuf[:BLOCK_SIZE]
            # 结束判据：文件已读尽且缓冲已排空（本块即最后一块）
            is_last = eof and not outbuf

            # ---- 第 3 步：发送本块并等待确认（内含超时重传/重复 ACK 处理）----
            self._send_data_wait_ack(DataPacket(seq & 0xFFFF, payload), seq)

            # ---- 第 4 步：进度与吞吐量统计 ----
            self.meter.add(len(payload))
            self._update_progress(seq, len(payload))

            if is_last:
                self._mark_transfer_done()   # 冻结耗时统计（最后一个块已被确认）
                return
            seq += 1  # 无界递增；线上块号在 65535 后自然回绕为 0

    def _send_data_wait_ack(self, pkt: DataPacket, seq: int) -> None:
        """
        发送一个 DATA 并阻塞等待其 ACK（锁步协议的核心）。

        处理三种情况（详见模块 docstring 的可靠传输机制）：
            * ACK 块号 == 本块       -> 确认成功，返回；
            * ACK 块号 == 上一块     -> 重复 ACK：我方 DATA 可能丢失，立即重发；
            * 等待超时               -> 重发本块，超过 retries 次终止。
        """
        attempts = 0
        while True:
            self._check_stopping()
            self._send(pkt)
            try:
                rx = self._recv()
            except socket.timeout:
                # 超时：重传本块（RFC 1350：只在超时后重传）
                attempts += 1
                self._note_retransmit(f'等待 ACK(块{pkt.block}) 超时，重发 DATA',
                                      attempt=attempts)
                if attempts > self.cfg.retries:
                    raise TransferError(
                        f'重传 {self.cfg.retries} 次后仍未收到 ACK，放弃传输')
                continue

            if isinstance(rx, AckPacket):
                if rx.block == pkt.block:
                    return  # 正确确认 -> 状态推进
                if seq > 1 and rx.block == (seq - 1) & 0xFFFF:
                    # 重复 ACK：客户端没有收到我方上一个 DATA —— 立即重发，
                    # 不等超时（提升异常链路下的恢复速度）
                    attempts += 1
                    self._note_retransmit(
                        f'收到重复 ACK(块{rx.block})，立即重发 DATA 块{pkt.block}',
                        attempt=attempts)
                    if attempts > self.cfg.retries:
                        raise TransferError(
                            f'对端反复重复 ACK，重发 {self.cfg.retries} 次后放弃')
                    continue
                # 其他块号的 ACK：不属于本状态，记录后忽略
                self.log.debug(f'[传输#{self.record.tid}] 忽略异常 ACK 块号 {rx.block}')
            elif isinstance(rx, ErrorPacket):
                # 客户端主动报错（如用户中止）-> 记录原因并终止
                raise TransferError(f'客户端返回错误: {rx.description}',
                                    code=rx.code, notify_peer=False)
            else:
                self.log.debug(f'[传输#{self.record.tid}] '
                               f'等待 ACK 时收到意外报文 {type(rx).__name__}，忽略')

    # ==================================================================
    # WRQ 状态机：服务器接收文件（上传）
    # ==================================================================
    def _run_write(self) -> None:
        """
        上传状态机：ACK(0) -> 收 DATA 写盘 -> ACK -> ... -> 末块 < 512。

        结束后的"末块宽限"见 _grace()：最终 ACK 可能丢失，需在数秒内
        继续响应客户端重发的末块 DATA。
        """
        f = self.file
        decoder = NetasciiDecoder() if self.use_netascii else None
        self._send(AckPacket(0))   # 对 WRQ 本身的确认：块号为 0

        seq = 1          # 期望接收的下一个块号（无界计数；线上 = seq & 0xFFFF）
        last_acked = 0   # 最近一次已确认的块号（无界计数）
        attempts = 0     # 连续超时次数
        dup_block = None  # 最近一次连续重复的 DATA 块号
        dup_count = 0    # 该块连续重复次数（当前块的重传计数）

        while True:
            self._check_stopping()
            try:
                rx = self._recv()
            except socket.timeout:
                # 等待数据超时：重发上一个 ACK（RFC 1350：S 等待的新 DATA
                # 是对上一 ACK 的确认；客户端收不到 ACK 会重发 DATA）
                attempts += 1
                self._note_retransmit(
                    f'等待 DATA 块{seq & 0xFFFF} 超时，补发 ACK(块{last_acked & 0xFFFF})',
                    attempt=attempts)
                if attempts > self.cfg.retries:
                    raise TransferError(
                        f'重传 {self.cfg.retries} 次后仍未收到客户端数据，放弃传输')
                self._send(AckPacket(last_acked & 0xFFFF))
                continue

            attempts = 0  # 收到任何合法报文都重置连续超时计数

            if isinstance(rx, DataPacket):
                if rx.block == seq & 0xFFFF:
                    # ---- 新数据块：写入文件并确认 ----
                    written = self._write_payload(rx.data, decoder)
                    self.md5.update(written)
                    last_acked = seq
                    dup_block, dup_count = None, 0   # 新块到达，重复计数复位
                    seq += 1  # 无界递增；线上块号在 65535 后回绕为 0

                    # 进度与吞吐量统计
                    self.meter.add(len(rx.data))
                    self._update_progress(last_acked, len(rx.data))

                    if len(rx.data) < BLOCK_SIZE:
                        # ---- 末块：结束传输 ----
                        if decoder:
                            tail = decoder.flush()   # 还原挂起的 CR 序列
                            if tail:
                                f.write(tail)
                                self.md5.update(tail)
                        f.flush()
                        self._send(AckPacket(last_acked & 0xFFFF))
                        # 实际传输到此结束：先冻结耗时统计，再进入末块确认
                        # 宽限期——宽限等待只服务于"最终 ACK 丢失"的善后，
                        # 不应计入传输耗时与平均速率（否则 Web 显示的耗时会
                        # 比客户端实际测得的时间多出 grace_seconds）。
                        self._mark_transfer_done()
                        self._grace(last_acked & 0xFFFF)
                        return
                    # 非末块：确认后继续等待下一块（每个 DATA 恰好一个 ACK）
                    self._send(AckPacket(last_acked & 0xFFFF))
                elif rx.block == last_acked & 0xFFFF:
                    # ---- 重复 DATA：我方上一 ACK 丢失 -> 补 ACK，不重复写 ----
                    if rx.block == dup_block:
                        dup_count += 1
                    else:
                        dup_block, dup_count = rx.block, 1
                    self._note_retransmit(
                        f'收到重复 DATA 块{rx.block}，补发 ACK(块{last_acked & 0xFFFF})',
                        attempt=dup_count)
                    self._send(AckPacket(last_acked & 0xFFFF))
                else:
                    # 乱序 / 未知块号（如迟到很久的旧块）：忽略并记录
                    self.log.debug(f'[传输#{self.record.tid}] 忽略乱序 DATA 块'
                                   f'{rx.block}（期望 {seq & 0xFFFF}）')
            elif isinstance(rx, ErrorPacket):
                raise TransferError(f'客户端返回错误: {rx.description}',
                                    code=rx.code, notify_peer=False)
            else:
                self.log.debug(f'[传输#{self.record.tid}] '
                               f'等待 DATA 时收到意外报文 {type(rx).__name__}，忽略')

    def _write_payload(self, data: bytes, decoder: Optional[NetasciiDecoder]) -> bytes:
        """
        把一个新 DATA 块写入本地文件。

        octet 模式原样写入；netascii 模式先经解码器转换为本地格式再写入。
        返回实际写入的字节（用于 MD5 计算——MD5 对应的是本地落盘内容）。
        """
        if decoder is not None:
            decoded = decoder.feed(data)
            self.file.write(decoded)
            return decoded
        self.file.write(data)
        return data

    def _grace(self, last_wire_block: int) -> None:
        """
        末块确认宽限期（手册 2.3 节）。

        WRQ 的最后一个报文是服务器的 ACK——若它丢失，客户端会重发末块 DATA。
        因此服务器在回完最终 ACK 后不立即关闭，而是在 grace_seconds 内继续
        监听：再次收到末块 DATA 就补发 ACK；超过宽限期仍无动静则正常关闭。
        """
        grace = getattr(self.cfg, 'grace_seconds', 5)
        if grace <= 0:
            return
        self.log.info(f'[传输#{self.record.tid}] 传输完成')
        deadline = time.time() + grace
        grace_dup = 0              # 末块 DATA 连续重复次数（当前块的宽限重传计数）
        self.sock.settimeout(0.5)   # 短超时轮询，便于检查停止标志与截止时间
        try:
            while time.time() < deadline:
                self._check_stopping()
                try:
                    rx = self._recv()
                except socket.timeout:
                    continue        # 宽限期内安静 -> 继续等到截止时间
                if isinstance(rx, DataPacket) and rx.block == last_wire_block:
                    # 客户端没收到最终 ACK -> 补发（计一次重传事件）
                    grace_dup += 1
                    self._note_retransmit(
                        f'宽限期内收到重复末块 DATA(块{last_wire_block})，补发 ACK',
                        attempt=grace_dup)
                    self._send(AckPacket(last_wire_block))
                # 其余报文一律忽略
        except TransferError:
            # 停机打断了宽限期：数据已完整落盘且最终 ACK 已发出，
            # 本次传输按正常完成处理（不能误报为失败）
            pass
        except ConnectionError:
            # 对端已关闭（如客户端收到最终 ACK 后立即退出）：数据已完整
            # 落盘，宽限被动结束，同样按正常完成处理
            pass
        finally:
            self.sock.settimeout(self.cfg.timeout)

    # ==================================================================
    # 通用收发与统计辅助
    # ==================================================================
    def _send(self, pkt: TftpPacket) -> None:
        """从会话 Socket 向客户端发送一个报文。"""
        if self._sock_closed:
            return
        try:
            self.sock.sendto(pkt.to_bytes(), self.client_addr)
        except OSError as exc:
            # 客户端不可达（已关闭等）：记录后继续，由超时/重试机制收口
            self.log.debug(f'[传输#{self.record.tid}] 发送失败: {exc}')

    def _recv(self) -> TftpPacket:
        """
        从会话 Socket 接收一个【来自本会话客户端】的报文并解析。

        校验规则（RFC 1350 的 TID 机制）：源地址/端口与请求时不符的报文
        一律丢弃并记日志——防止其他进程/会话的报文串扰本会话。

        :raises socket.timeout: 超时未收到有效报文（由调用方触发重传逻辑）；
        :raises OSError: Socket 已被关闭（服务器停机）。
        """
        while True:
            data, addr = self.sock.recvfrom(RECV_BUFFER_SIZE)
            if addr != self.client_addr:
                self.log.debug(f'[传输#{self.record.tid}] 丢弃来自非会话对端 '
                               f'{addr[0]}:{addr[1]} 的报文（TID 不符）')
                continue
            try:
                return PacketParser.parse(data)
            except PacketFormatError as exc:
                # 报文格式错误只影响这一包，记录后继续等待下一包。
                self.log.debug(f'[传输#{self.record.tid}] 丢弃畸形报文: {exc}')

    def _check_stopping(self) -> None:
        """服务器正在停机时抛出终止异常（notify_peer=False：不必告知客户端）。"""
        if self.server.stopping.is_set():
            raise TransferError('服务器正在停止', notify_peer=False)

    def _note_retransmit(self, detail: str, attempt: Optional[int] = None) -> None:
        """统一记录一次重传/重复报文事件：登记表 + 全局统计 + 日志。

        attempt：当前块（当前等待序列）内的第几次重传；缺省时日志回退为整场累计次数。
        """
        self.record.retransmits += 1
        self.stats.on_retransmit()
        shown = attempt if attempt is not None else self.record.retransmits
        self.log.retransmit(self.record.tid, self.client, detail, shown)

    def _update_progress(self, seq: int, num_bytes: int) -> None:
        """每个 DATA 块处理完后刷新登记表中的进度与速率字段。"""
        rec = self.record
        rec.block = seq & 0xFFFF          # 线上块号（Web 可观察 65535->0 回绕）
        rec.bytes_transferred += num_bytes
        rec.speed_now = self.meter.instant()
        rec.speed_avg = self.meter.avg()

    def _mark_transfer_done(self) -> None:
        """记录"实际传输结束"时刻：最后一个 DATA 块被确认之时。

        冻结吞吐量计时器并把该时刻写入登记表；此后服务器进入的收尾阶段
        （WRQ 末块确认宽限期等）不再计入 duration 与平均速率，使 Web 界面
        显示的耗时与客户端测得的时间一致。
        """
        self.meter.freeze()
        self.record.transferred_at = time.time()

    # ==================================================================
    # 结果登记与资源清理
    # ==================================================================
    def _finish_success(self) -> None:
        """传输成功：写登记表、全局统计、成功日志（含吞吐量与 MD5）。"""
        if self.file is not None:
            self.file.flush()
        rec = self.record
        rec.md5 = self.md5.hexdigest()
        rec.speed_avg = self.meter.avg() if self.meter else 0.0
        rec.speed_now = self.meter.instant() if self.meter else 0.0
        rec.state = STATE_SUCCESS
        rec.finished_at = time.time()
        self.stats.on_transfer_success(rec.bytes_transferred)
        self.log.success(rec.direction_name, self.client, rec.filename,
                         rec.bytes_transferred, rec.duration, rec.speed_avg, rec.md5)

    def _finish_failure(self, exc: TransferError) -> None:
        """传输失败：按需回 ERROR 报文，登记失败原因（实验要求展示失败原因）。"""
        if exc.notify_peer and not self._sock_closed:
            self._send(ErrorPacket(exc.code, exc.message))
        rec = self.record
        rec.state = STATE_FAILED
        rec.error = exc.message
        rec.finished_at = time.time()
        self.stats.on_transfer_failure(rec.bytes_transferred)
        self.log.failure(rec.direction_name, self.client, rec.filename, exc.message)

    def _cleanup(self) -> None:
        """会话收尾：关闭文件与 Socket、移入历史、归还并发名额（幂等）。"""
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                pass
            self.file = None
        self._sock_closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        self.registry.finish(self.record)
        self.server.on_session_end(self)

    def shutdown(self) -> None:
        """供服务器停机时调用：立即关闭会话 Socket，令阻塞的 recv 立刻返回。"""
        self._sock_closed = True
        try:
            self.sock.close()
        except OSError:
            pass
