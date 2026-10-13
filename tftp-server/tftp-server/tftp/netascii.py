# -*- coding: utf-8 -*-
"""
netascii 传输模式的流式换行转换模块

RFC 1350 规定 netascii 模式下，数据必须转换为"标准 ASCII + 规定的行结束
表示"后再传输：
    * 行结束符表示为 CR LF（回车 + 换行）两个字节的序列；
    * 数据中真实的 CR 字符（不作为行结束）必须表示为 CR NUL；
    * 数据中不允许出现单独的 LF。

因此发送方（服务器下载 netascii 文件时）需要把【本地文本】转换为
【netascii 线上格式】；接收方（服务器上传 netascii 文件时）反向转换。
本模块用两个流式转换器类实现：

    NetasciiEncoder —— 本地格式 -> netascii（RRQ 下载时使用）
        LF       -> CR LF
        CR       -> CR NUL   （本地文件里的"裸 CR"，与行结束无关）
        其余字节 -> 原样

    NetasciiDecoder —— netascii -> 本地格式（WRQ 上传时使用）
        CR LF    -> os.linesep（Windows 为 CR LF，macOS/Linux 为 LF）
        CR NUL   -> CR        （还原数据中真实的 CR）
        LF       -> os.linesep（容错：把单独的 LF 也视作行结束）
        其余字节 -> 原样

实现方式：两个转换器都是"逐字节状态机"，内部用一个挂起字节 ``_pending``
记录含义待定的字节（CR 的含义取决于下一个字节），因此被 512 字节块边界
截断的换行序列（例如上一块以 CR 结尾、下一块以 LF 开头）依然能被正确
转换——这是 netascii 实现中最容易出错的细节。
"""

import os
from abc import ABC, abstractmethod
from typing import Optional

# 本地平台的文本行结束符（Windows: b"\r\n"；macOS/Linux: b"\n"）
LOCAL_EOL = os.linesep.encode('ascii')

# ASCII 控制字符常量
_CR = 0x0D  # 回车
_LF = 0x0A  # 换行
_NUL = 0x00


class _NetasciiConverter(ABC):
    """
    流式换行转换器的公共基类。

    子类只需实现 :meth:`_step`（单字节状态转移）与 :meth:`_flush_pending`
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

    def feed(self, chunk: bytes) -> bytes:
        """喂入一块原始字节，返回转换后的字节串（流式，可任意分块调用）。"""
        out = bytearray()
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


class NetasciiEncoder(_NetasciiConverter):
    """本地格式 -> netascii（服务器 RRQ 下载 netascii 文件时使用）。"""

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
