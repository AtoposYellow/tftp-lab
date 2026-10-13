# -*- coding: utf-8 -*-
"""
TFTP 报文编解码模块（协议层）

本模块按照 RFC 1350 定义，用面向对象的方式封装 TFTP 的五种报文。
全部多字节字段均采用【网络字节序（大端）】，通过 struct 模块的 '!H' 完成打包/解包。

五种报文格式（opcode 为报文第一个 2 字节字段，即"操作码"）：

    RRQ（读请求，opcode=1）/ WRQ（写请求，opcode=2）：
        +--------+----~----+---0---+----~----+---0---+
        | opcode | 文件名  |   0   |  mode   |   0   |
        +--------+----~----+---0---+----~----+---0---+
        文件名与 mode 均为 netascii 字符串，以 1 字节的 0 作为结束符。

    DATA（数据块，opcode=3）：
        +--------+--------+--------+--------~--------+
        | opcode | 块号(2B) |  数据（0~512 字节）      |
        +--------+--------+--------+--------~--------+

    ACK（确认，opcode=4）：
        +--------+--------+
        | opcode | 块号(2B) |
        +--------+--------+
        对 WRQ 的确认块号为 0；对第 k 块 DATA 的确认块号为 k。

    ERROR（错误，opcode=5）：
        +--------+----------+----------+---0---+
        | opcode | 错误码(2B) | 错误信息  |   0   |
        +--------+----------+----------+---0---+

块号字段为 16 位无符号整数（0~65535）。本服务器内部使用无界整数递增计数，
发送/比较时取 `seq & 0xFFFF` 映射到线上块号，从而在 65535 -> 0 回绕处依然
可以正确匹配新旧报文，突破"标准 TFTP 最大 32 MiB（65536 块 x 512 字节）"的限制。
"""

import struct
from abc import ABC, abstractmethod
from typing import ClassVar

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------

# 五种报文的操作码（RFC 1350 第 5 节）
OP_RRQ = 1    # 读请求（客户端 -> 服务器，请求下载）
OP_WRQ = 2    # 写请求（客户端 -> 服务器，请求上传）
OP_DATA = 3   # 数据块
OP_ACK = 4    # 确认
OP_ERROR = 5  # 错误

# 两种传输模式（RFC 1350 第 2.4 节；mail 模式已废弃，本服务器不支持）
MODE_NETASCII = 'netascii'  # 文本模式：换行符按 netascii 规则转换
MODE_OCTET = 'octet'        # 二进制模式：原始 8 位字节，不做任何转换
VALID_MODES = (MODE_NETASCII, MODE_OCTET)

# 每个 DATA 报文携带的数据块大小（RFC 1350 固定为 512 字节）
BLOCK_SIZE = 512

# TFTP 报文的最大长度：4 字节头部 + 512 字节数据（用于接收缓冲区大小的参考）
MAX_PACKET_SIZE = BLOCK_SIZE + 4


class PacketFormatError(ValueError):
    """报文格式非法时抛出的异常（长度不足、缺少 0 结束符、未知 opcode 等）。"""


def decode_text(raw: bytes) -> str:
    """
    将报文中的文本字段（文件名 / mode / 错误信息）解码为字符串。

    RFC 1350 规定这些字段使用 netascii（即 7 位 ASCII）。但实际客户端可能
    传输含中文的文件名（Windows 上常以 GBK 编码），因此按以下优先级尝试：
    UTF-8 -> GBK -> Latin-1（Latin-1 不会失败，作为兜底保证健壮性）。
    """
    for encoding in ('utf-8', 'gbk'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('latin-1')


def encode_text(text: str) -> bytes:
    """
    将字符串编码为报文中的文本字节串。

    纯 ASCII 内容按 netascii 原样编码；含中文时使用 UTF-8
    （主流客户端对 ERROR 信息均按"原样显示"处理，可接受）。
    """
    try:
        return text.encode('ascii')
    except UnicodeEncodeError:
        return text.encode('utf-8')


# ---------------------------------------------------------------------------
# 报文类的抽象基类
# ---------------------------------------------------------------------------

class TftpPacket(ABC):
    """
    所有 TFTP 报文类的抽象基类。

    子类必须：
      * 定义类属性 ``opcode``（该报文类型的操作码）；
      * 实现 :meth:`to_bytes`（对象 -> 线上字节串）与 :meth:`from_bytes`
        （线上字节串 -> 对象，类方法）。

    这样设计的好处：协议格式集中在一个类族中描述，会话层只面向
    ``TftpPacket`` 对象编程，不接触任何 struct 细节。
    """

    # 子类必须覆盖：本报文类型的操作码
    opcode: ClassVar[int] = 0

    @abstractmethod
    def to_bytes(self) -> bytes:
        """将本报文对象序列化为可直接 sendto 的字节串。"""

    @classmethod
    @abstractmethod
    def from_bytes(cls, raw: bytes) -> 'TftpPacket':
        """从字节串反序列化出报文对象；格式非法时抛出 PacketFormatError。"""

    def __repr__(self) -> str:
        """调试用：打印类名与全部字段。"""
        fields = ', '.join(f'{k}={v!r}' for k, v in sorted(vars(self).items()))
        return f'{type(self).__name__}({fields})'


# ---------------------------------------------------------------------------
# 五种具体报文
# ---------------------------------------------------------------------------

class RequestPacket(TftpPacket):
    """
    RRQ / WRQ 请求报文（opcode = 1 / 2）。

    两种请求的线上格式完全一致，仅 opcode 不同，因此合并为一个类，
    通过 ``opcode`` 字段区分方向（1 = 下载请求，2 = 上传请求）。
    """

    def __init__(self, opcode: int, filename: str, mode: str):
        # opcode 只能为 OP_RRQ 或 OP_WRQ，其余值在构造时即拒绝
        if opcode not in (OP_RRQ, OP_WRQ):
            raise PacketFormatError(f'非法的请求 opcode: {opcode}')
        self.opcode = opcode        # 1=RRQ（下载） 2=WRQ（上传）
        self.filename = filename    # 请求操作的文件名（字符串）
        self.mode = mode            # 传输模式字符串（"netascii" / "octet"）

    def to_bytes(self) -> bytes:
        # opcode(2B) + filename + 0 + mode + 0
        return (struct.pack('!H', self.opcode)
                + encode_text(self.filename) + b'\x00'
                + encode_text(self.mode) + b'\x00')

    @classmethod
    def from_bytes(cls, raw: bytes) -> 'RequestPacket':
        """
        解析请求报文。

        严格性说明：RFC 1350 的请求以 mode 字段的 0 结束；若其后还有附加
        字节（RFC 2347 option 协商的扩展字段），本实现按"纯 RFC 1350 服务器"
        处理——直接忽略附加字段，不进行 option 协商。
        """
        # 至少需要 "opcode(2) + 文件名(>=1字节含0) + mode(>=1字节含0)" 共 6 字节
        if len(raw) < 6:
            raise PacketFormatError('请求报文长度不足')
        opcode = struct.unpack('!H', raw[:2])[0]
        if opcode not in (OP_RRQ, OP_WRQ):
            raise PacketFormatError(f'非法的请求 opcode: {opcode}')

        # 按 0x00 切分字符串字段：fields[0]=文件名, fields[1]=mode
        fields = raw[2:].split(b'\x00')
        if len(fields) < 2 or fields[0] == b'' or fields[1] == b'':
            raise PacketFormatError('请求报文缺少以0结尾的文件名或mode字段')
        filename = decode_text(fields[0])
        # mode 在协议中大小写不敏感，统一转为小写比较
        mode = decode_text(fields[1]).strip().lower()
        return cls(opcode, filename, mode)


class DataPacket(TftpPacket):
    """
    DATA 数据块报文（opcode = 3）。

    ``data`` 长度约定：
      * 等于 512 字节 —— 后面还有数据块；
      * 小于 512 字节 —— 这是最后一个数据块（传输结束的判据，可为 0 字节，
        例如文件大小恰好是 512 的整数倍时，需要补发一个 0 字节末块）。
    """

    opcode: ClassVar[int] = OP_DATA

    def __init__(self, block: int, data: bytes):
        self.block = block      # 线上块号（16 位，调用方负责 & 0xFFFF）
        self.data = data        # 数据载荷（0~512 字节）

    def to_bytes(self) -> bytes:
        # opcode(2B) + block(2B) + data
        return struct.pack('!HH', self.opcode, self.block & 0xFFFF) + self.data

    @classmethod
    def from_bytes(cls, raw: bytes) -> 'DataPacket':
        if len(raw) < 4:
            raise PacketFormatError('DATA 报文长度不足')
        block = struct.unpack('!H', raw[2:4])[0]
        data = raw[4:]
        if len(data) > BLOCK_SIZE:
            raise PacketFormatError(f'DATA 载荷超过 512 字节: {len(data)}')
        return cls(block, data)


class AckPacket(TftpPacket):
    """
    ACK 确认报文（opcode = 4）。

    * 对 WRQ 请求本身的确认：block = 0；
    * 对第 k 块 DATA 的确认：block = k（线上 16 位块号）。
    """

    opcode: ClassVar[int] = OP_ACK

    def __init__(self, block: int):
        self.block = block      # 被确认的块号（0 表示对 WRQ 的确认）

    def to_bytes(self) -> bytes:
        return struct.pack('!HH', self.opcode, self.block & 0xFFFF)

    @classmethod
    def from_bytes(cls, raw: bytes) -> 'AckPacket':
        if len(raw) != 4:
            raise PacketFormatError('ACK 报文长度必须为 4 字节')
        block = struct.unpack('!H', raw[2:4])[0]
        return cls(block)


class ErrorPacket(TftpPacket):
    """
    ERROR 错误报文（opcode = 5）。

    类属性 ``ERROR_MESSAGES`` 定义了 RFC 1350 规定的 8 种错误码及其
    英文原文与中文释义，用于日志记录和 Web 界面展示（实验手册表 2.1）。
    """

    opcode: ClassVar[int] = OP_ERROR

    # 错误码 -> (英文原文, 中文释义)；0~7 为 RFC 1350 定义
    ERROR_MESSAGES: ClassVar[dict] = {
        0: ('Not defined, see error message', '未定义错误，见错误信息'),
        1: ('File not found', '文件未找到'),
        2: ('Access violation', '访问被拒绝'),
        3: ('Disk full or allocation exceeded', '磁盘满或超出可分配空间'),
        4: ('Illegal TFTP operation', '非法的 TFTP 操作'),
        5: ('Unknown transfer ID', '未知的传输 ID'),
        6: ('File already exists', '文件已经存在'),
        7: ('No such user', '没有该用户'),
    }

    def __init__(self, code: int, message: str = ''):
        self.code = code            # 0~7
        self.message = message      # 附加的错误描述（可为空串）

    @property
    def description(self) -> str:
        """返回便于展示的错误描述："英文原文 / 中文释义 + 附加信息"。"""
        en, zh = self.ERROR_MESSAGES.get(self.code, ('Unknown', '未知错误码'))
        text = f'{en} / {zh}'
        if self.message:
            text += f'（{self.message}）'
        return text

    def to_bytes(self) -> bytes:
        # opcode(2B) + code(2B) + errmsg + 0
        return (struct.pack('!HH', self.opcode, self.code)
                + encode_text(self.message) + b'\x00')

    @classmethod
    def from_bytes(cls, raw: bytes) -> 'ErrorPacket':
        if len(raw) < 5:
            raise PacketFormatError('ERROR 报文长度不足')
        code = struct.unpack('!H', raw[2:4])[0]
        # 错误信息以 0 结尾；若缺失结束符则取剩余全部字节（提高健壮性）
        msg_raw = raw[4:].split(b'\x00')[0]
        return cls(code, decode_text(msg_raw))


# ---------------------------------------------------------------------------
# 报文解析工厂
# ---------------------------------------------------------------------------

class PacketParser:
    """
    报文解析工厂。

    根据报文头 2 字节的 opcode 把字节串分派给对应报文类的 ``from_bytes``
    完成解析；调用方只需面向 ``TftpPacket`` 对象编程。
    """

    # opcode -> 报文类 的分派表
    _CLASSES = {
        OP_RRQ: RequestPacket,
        OP_WRQ: RequestPacket,
        OP_DATA: DataPacket,
        OP_ACK: AckPacket,
        OP_ERROR: ErrorPacket,
    }

    @classmethod
    def parse(cls, raw: bytes) -> TftpPacket:
        """
        将一段字节串解析为具体的报文对象。

        :raises PacketFormatError: 长度不足或 opcode 未知时抛出，
            调用方（监听循环 / 会话收包循环）应记录日志后忽略该报文。
        """
        if len(raw) < 2:
            raise PacketFormatError('报文长度不足 2 字节，无法读取 opcode')
        opcode = struct.unpack('!H', raw[:2])[0]
        packet_cls = cls._CLASSES.get(opcode)
        if packet_cls is None:
            raise PacketFormatError(f'未知的 opcode: {opcode}')
        return packet_cls.from_bytes(raw)
