import struct
from abc import ABC, abstractmethod
from typing import ClassVar

# 一、协议常量

# 五种报文的操作码（RFC 1350 第 5 节）
OP_RRQ = 1    # 读请求（客户端 -> 服务器，请求下载）
OP_WRQ = 2    # 写请求（客户端 -> 服务器，请求上传）
OP_DATA = 3   # 数据块
OP_ACK = 4    # 确认
OP_ERROR = 5  # 错误

# 两种传输模式
MODE_NETASCII = 'netascii'  # 文本模式
MODE_OCTET = 'octet'        # 二进制模式
VALID_MODES = (MODE_NETASCII, MODE_OCTET)

# 每个 DATA 报文携带的数据块大小
BLOCK_SIZE = 512

# TFTP 报文的最大长度：4 字节头部 + 512 字节数据
MAX_PACKET_SIZE = BLOCK_SIZE + 4


class PacketFormatError(ValueError):
    """报文格式非法时抛出的异常"""


def decode_text(raw: bytes) -> str:
    """
    将报文中的文本字段解码为字符串。

    RFC 1350 规定这些字段使用 netascii（即 7 位 ASCII）。但实际客户端可能
    传输含中文的文件名（其在 Windows 上常以 GBK 编码），因此按以下优先级尝试：
    UTF-8 -> GBK -> Latin-1。
    """
    for encoding in ('utf-8', 'gbk'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:     # 表示捕获解码失败异常
            continue
    return raw.decode('latin-1')
# 遍历编码列表，依次尝试解码，若解码成功，则返回解码结果，若不成功，则尝试下一种解码方式

def encode_text(text: str) -> bytes:
    """
    将字符串编码为报文中的文本字节串。
    纯 ASCII 内容按 netascii 原样编码；含中文时使用 UTF-8。
    """
    try:
        return text.encode('ascii')
    except UnicodeEncodeError:
        return text.encode('utf-8')
# 先尝试ascii编码，若遇中文尝试编码失败，则再尝试utf-8编码

# 二、报文类的抽象基类

class TftpPacket(ABC):
    """
    所有 TFTP 报文类的抽象基类。
     子类必须：
      * 定义类属性 ``opcode``（操作码）；
      * 实现 :meth:`to_bytes`（对象 -> 线上字节串）与 :meth:`from_bytes`
        （线上字节串 -> 对象，类方法）。

    这样设计的好处：协议格式集中在一个类族中描述，会话层只面向
    ``TftpPacket`` 对象编程，不接触任何 struct 细节。
    """

    # opcode：本报文类型的操作码
    opcode: ClassVar[int] = 0

    # 如果一个子类没有实现要求的抽象方法，那么不能直接创建它的实例
    @abstractmethod
    def to_bytes(self) -> bytes:   # 是一个实例方法，需要读取这个具体报文实例所保存的数据
        """  作用：将报文对象序列转化为可直接发送的字节串。  """

    @classmethod
    @abstractmethod
    def from_bytes(cls, raw: bytes) -> 'TftpPacket':
        # 调用这个方法时，还没有对应的子类实例，只有收到的字节串
        # 因此子类必须提供一种从字节创建报文实例的能力
        """ 格式非法时抛出 PacketFormatError。"""

    def __repr__(self) -> str:
        """用于给对象生成适合调试的字符串表示"""
        # vars：返回属性字典
        # sorted：按照属性名称进行排序
        # for：解包，k对应block，v对应block值
        # !r表示用repr()形式表示变量
        fields = ', '.join(f'{k}={v!r}' for k, v in sorted(vars(self).items()))
        return f'{type(self).__name__}({fields})'


# 三、五种具体报文

class RequestPacket(TftpPacket):
    """
    WRQ和RRQ的线上格式完全一致，仅 opcode 不同，因此合并为一个类。
    """

    # 初始化
    def __init__(self, opcode: int, filename: str, mode: str):
        # 检查opcode
        if opcode not in (OP_RRQ, OP_WRQ):
            raise PacketFormatError(f'非法的请求 opcode: {opcode}')
        self.opcode = opcode        # 1=RRQ 2=WRQ
        self.filename = filename    # 文件名
        self.mode = mode            # 传输模式

    def to_bytes(self) -> bytes:
        # opcode(2B) + filename + 0 + mode + 0
        # struct：二进制处理模块
        # pack负责把数值编码成字节
        # !：大端序  H：16位，即 2字节
        return (struct.pack('!H', self.opcode)
                + encode_text(self.filename) + b'\x00'  # 用00表示当前字符串字段结束
                + encode_text(self.mode) + b'\x00')

    @classmethod
    def from_bytes(cls, raw: bytes) -> 'RequestPacket':
        """
        解析请求报文。
        """
        # 至少需要 "opcode(2) + 文件名(>=1字节，含0) + mode(>=1字节，含0)" 共 6 字节
        # 检查报文长度
        if len(raw) < 6:
            raise PacketFormatError('请求报文长度不足')
        # 解析opcode
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
    DATA 数据块报文
    """

    opcode: ClassVar[int] = OP_DATA

    def __init__(self, block: int, data: bytes):
        self.block = block
        self.data = data

    def to_bytes(self) -> bytes:
        # opcode(2B) + block(2B) + data
        return struct.pack('!HH', self.opcode, self.block & 0xFFFF) + self.data
    # self.block & 0xFFFF通过按位与运算，实现块号回滚，即通过block对0xFFFF取模，例如当达到第65537块时
    # 其原先块号为65536，通过取模运算将其块号重新置为0，实现块号回滚

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
    ACK 确认报文
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
    """

# 采用类属性字典保存公共错误信息，再使用实例属性保存本次错误
# 从而使所有实例都可以通过类属性查找错误信息

    opcode: ClassVar[int] = OP_ERROR

    # 错误码(英文原文, 中文释义)；0~7 为 RFC 1350 定义
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
        self.code = code
        self.message = message

    @property
    # 用于根据键获取值
    def description(self) -> str:
        """返回便于展示的错误描述：英文原文 / 中文释义 + 附加信息。"""
        en, zh = self.ERROR_MESSAGES.get(self.code, ('Unknown', '未知错误码'))
        text = f'{en} / {zh}'  # 生成文本
        if self.message:    # 如果存在附加错误信息
            text += f'（{self.message}）'
        return text

    def to_bytes(self) -> bytes:
        return (struct.pack('!HH', self.opcode, self.code)
                + encode_text(self.message) + b'\x00')

    @classmethod
    def from_bytes(cls, raw: bytes) -> 'ErrorPacket':
        if len(raw) < 5:
            raise PacketFormatError('ERROR 报文长度不足')
        code = struct.unpack('!H', raw[2:4])[0]
        # 错误信息以 0 结尾；若缺失结束符则取剩余全部字节
        msg_raw = raw[4:].split(b'\x00')[0]
        return cls(code, decode_text(msg_raw))


# 四、报文解析
class PacketParser:
    """
    根据报文头 2 字节的 opcode 把字节串分派给对应报文类的 ``from_bytes``
    """
    # 每个报文类必须要有一个opcode，表明自己是哪种报文
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
        将一段字节串解析为具体的报文对象，创建并返回报文实例
        """
        if len(raw) < 2:
            raise PacketFormatError('报文长度不足 2 字节，无法读取 opcode')
        opcode = struct.unpack('!H', raw[:2])[0]
        # 根据opcode查找类
        packet_cls = cls._CLASSES.get(opcode)
        if packet_cls is None:
            raise PacketFormatError(f'未知的 opcode: {opcode}')
        return packet_cls.from_bytes(raw)
