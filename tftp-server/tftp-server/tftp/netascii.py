import os
from abc import ABC, abstractmethod
from typing import Optional

# 当前操作系统惯用的文本行结束符
LOCAL_EOL = os.linesep.encode('ascii')

# ASCII 控制字符常量
_CR = 0x0D  # 回车
_LF = 0x0A  # 换行
_NUL = 0x00


# 提供转换框架，管理转换状态，不能直接实例化
class _NetasciiConverter(ABC):
    """
    流式换行转换器的公共基类。
    * 子类只需实现 :_step（单字节状态转移）与 _flush_pending
    （流结束时对挂起字节的处理），基类提供 feed()/flush() 的通用骨架。
    """

    def __init__(self) -> None:
        # 挂起字节：None 表示没有等待与下一字节组合判定的字节；
        # CR 需要挂起，因为它是"行结束 CR LF"还是"数据 CR NUL"由下一字节决定。
        self._pending: Optional[int] = None

    @abstractmethod
    def _step(self, byte: int) -> bytes:
        """
        状态机单步：输入一个字节，返回应输出的字节串。
        子类在本方法内部自行读写 self._pending。
        """

    @abstractmethod
    def _flush_pending(self) -> bytes:
        """输入流结束时，若仍有挂起字节，返回其应输出的字节串。"""

    # 程序需要记住第一块末尾的 CR，再结合第二块开头的 LF 进行判断。
    def feed(self, chunk: bytes) -> bytes:
        """接收原始字节，返回转换后的字节串。"""
        out = bytearray()   # 用于保存输出结果
        for byte in chunk:
            out += self._step(byte)
        return bytes(out)

    def flush(self) -> bytes:
        """输入流结束时调用；若仍有挂起字节则按规则输出并清空状态。"""
        if self._pending is None:
            return b''
        tail = self._flush_pending()
        self._pending = None
        return tail

# 本地模式 -> 网络格式
class NetasciiEncoder(_NetasciiConverter):
    """本地格式 -> netascii（服务器下载 netascii 文件时使用）"""

    def _step(self, byte: int) -> bytes:
        # 情形一：上一字节是挂起的 CR，由当前字节决定其含义
        if self._pending is not None:
            self._pending = None
            if byte == _LF:
                # CR LF：本地 CRLF 行结束 -> 原样输出 CR LF
                return b'\r\n'
            # 挂起的 CR 是数据字节 -> 输出 CR NUL；再处理当前字节
            if byte == _CR:
                # 连续 CR：当前 CR 同样含义待定，再次挂起
                self._pending = _CR
                return b'\r\x00'
            return b'\r\x00' + bytes([byte])

        # 情形二：无挂起状态，正常处理当前字节
        if byte == _CR:
            # CR 含义待定，挂起
            self._pending = _CR
            return b''
        if byte == _LF:
            # 单独的 LF（本地 LF 行结束）-> CR LF
            return b'\r\n'
        return bytes([byte])

    def _flush_pending(self) -> bytes:
        # 输入以挂起的 CR 结尾：它是数据字节 -> CR NUL
        return b'\r\x00'

# 网络格式转本地格式
class NetasciiDecoder(_NetasciiConverter):
    """netascii -> 本地格式（服务器 WRQ 上传 netascii 文件时使用）。"""

    def _step(self, byte: int) -> bytes:
        # 情形一：上一字节是挂起的 CR，由当前字节决定其含义
        if self._pending is not None:
            self._pending = None
            if byte == _LF:
                # CR LF：netascii 行结束 -> 本地行结束符
                return LOCAL_EOL
            if byte == _NUL:
                # CR NUL：数据中真实的 CR -> 输出 CR
                return b'\r'
            if byte == _CR:
                # CR + CR：第一个 CR 非法（既非 CR LF 也非 CR NUL），
                # 容错按"行结束"处理；当前 CR 再挂起
                self._pending = _CR
                return LOCAL_EOL
            # CR + 其他字节：容错按"行结束 + 数据字节"处理
            return LOCAL_EOL + bytes([byte])

        # 情形二：无挂起状态，正常处理当前字节
        if byte == _CR:
            # CR 含义待定，挂起
            self._pending = _CR
            return b''
        if byte == _LF:
            # 单独的 LF：容错视作行结束
            return LOCAL_EOL
        return bytes([byte])

    def _flush_pending(self) -> bytes:
        # 输入以挂起的 CR 结尾：容错按行结束处理
        return LOCAL_EOL
