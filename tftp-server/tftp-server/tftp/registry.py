# -*- coding: utf-8 -*-
"""
传输会话登记表模块

实验的选做功能之一是图形界面，界面需要实时展示"当前有哪些传输在进行、
各自进度如何、结果如何"。本模块提供 :class:`TransferRegistry`：

    * 每个传输会话（TransferSession）启动时在登记表中创建一条
      :class:`TransferRecord`，会话线程运行期间持续更新其中的进度字段；
    * 会话结束后记录被移入历史队列（保留最近 100 条）；
    * Web 层通过 snapshot_active() / snapshot_history() 取快照渲染页面。

线程模型说明：一条 TransferRecord 只由它所属的会话线程写入（单写者），
登记表的锁只保护"活动表 / 历史队列"这两个容器的增删与遍历，字段级别的
读写依赖 CPython 的 GIL，对 UI 展示而言足够安全。
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# 传输方向常量
DIR_UPLOAD = 'upload'      # WRQ：客户端 -> 服务器（上传）
DIR_DOWNLOAD = 'download'  # RRQ：服务器 -> 客户端（下载）

# 传输状态常量
STATE_ACTIVE = 'active'      # 传输进行中
STATE_SUCCESS = 'success'    # 传输成功
STATE_FAILED = 'failed'      # 传输失败

# 传输记录保留的历史条数
HISTORY_LIMIT = 100


@dataclass
class TransferRecord:
    """
    一次文件传输的全部展示信息（Web 界面的数据模型）。

    字段在会话生命周期中的演进：
        created  -> 由登记表 create() 填写基本信息
        running  -> 会话线程更新 block / bytes_transferred / 速率 / 重传数
        finished -> mark_success() 或 mark_failed() 写入结果与统计
    """

    tid: int                          # 传输编号（服务器内单调递增，区别于协议 TID）
    client: str                       # 客户端 "IP:端口"
    direction: str                    # DIR_UPLOAD / DIR_DOWNLOAD
    filename: str                     # 操作的文件名（客户端视角）
    mode: str                         # 传输模式 netascii / octet

    # ---- 进度信息（会话线程更新）----
    state: str = STATE_ACTIVE         # 当前状态
    block: int = 0                    # 当前线上块号（用于观察 65535->0 回绕）
    bytes_transferred: int = 0        # 已传输字节数（线上 DATA 载荷累计）
    total_bytes: Optional[int] = None  # 文件总大小（下载已知；上传未知为 None）
    speed_now: float = 0.0            # 瞬时吞吐量（字节/秒）
    speed_avg: float = 0.0            # 平均吞吐量（字节/秒）
    retransmits: int = 0              # 重传次数（含重复报文处理）

    # ---- 结果信息 ----
    md5: str = ''                     # 文件 MD5（完整性校验）
    error: str = ''                   # 失败原因（成功时为空）
    started_at: float = field(default_factory=time.time)  # 开始时间戳
    finished_at: Optional[float] = None                   # 结束时间戳
    transferred_at: Optional[float] = None  # 实际传输结束时刻（最后一个 DATA 块被确认），
                                            # 之后的会话收尾（末块确认宽限期）不计入耗时

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def direction_name(self) -> str:
        """方向的中文名（上传 / 下载）。"""
        return '上传' if self.direction == DIR_UPLOAD else '下载'

    @property
    def duration(self) -> float:
        """
        已进行 / 总耗时（秒）。

        以"实际传输结束时刻"（transferred_at，最后一个 DATA 块被确认）为
        终点；服务器随后的收尾等待（WRQ 末块确认宽限期）不计入，保证与
        客户端测得的传输时间一致。
        """
        end = self.transferred_at or self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    @property
    def progress(self) -> Optional[float]:
        """
        进度百分比（0~100）。

        下载（RRQ）已知文件总大小，可计算精确进度；
        上传（WRQ）总大小未知，返回 None（前端显示为不定进度）。
        """
        if self.total_bytes:
            return min(100.0, self.bytes_transferred * 100.0 / self.total_bytes)
        return None

    def to_dict(self) -> dict:
        """序列化为 JSON 友好的 dict，供 Web API 返回。"""
        return {
            'tid': self.tid,
            'client': self.client,
            'direction': self.direction,
            'direction_name': self.direction_name,
            'filename': self.filename,
            'mode': self.mode,
            'state': self.state,
            'block': self.block,
            'bytes_transferred': self.bytes_transferred,
            'total_bytes': self.total_bytes,
            'progress': self.progress,
            'speed_now': self.speed_now,
            'speed_avg': self.speed_avg,
            'retransmits': self.retransmits,
            'md5': self.md5,
            'error': self.error,
            'duration': round(self.duration, 2),
        }


class TransferRegistry:
    """
    传输会话登记表（线程安全）。

    容器结构：
        active  —— tid -> TransferRecord，正在进行中的传输；
        history —— deque(maxlen=100)，最近结束的传输（先进先出淘汰）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: Dict[int, TransferRecord] = {}
        self._history: deque = deque(maxlen=HISTORY_LIMIT)
        self._next_tid = 1  # 传输编号发生器

    def create(self, client: str, direction: str, filename: str, mode: str) -> TransferRecord:
        """会话启动时调用：分配传输编号并登记一条活动记录。"""
        with self._lock:
            record = TransferRecord(
                tid=self._next_tid, client=client, direction=direction,
                filename=filename, mode=mode)
            self._next_tid += 1
            self._active[record.tid] = record
            return record

    def finish(self, record: TransferRecord) -> None:
        """会话结束时调用：从活动表移除并放入历史队列。"""
        with self._lock:
            self._active.pop(record.tid, None)
            self._history.appendleft(record)

    def active_count(self) -> int:
        """当前并发传输数（用于峰值并发统计）。"""
        with self._lock:
            return len(self._active)

    def get_active(self, tid: int) -> Optional[TransferRecord]:
        """按编号取活动记录（会话线程更新自身记录时使用）。"""
        with self._lock:
            return self._active.get(tid)

    def snapshot_active(self) -> List[dict]:
        """活动传输列表快照（Web 轮询用）。"""
        with self._lock:
            records = list(self._active.values())
        return [r.to_dict() for r in records]

    def snapshot_history(self) -> List[dict]:
        """历史传输列表快照（新的在前，Web 轮询用）。"""
        with self._lock:
            records = list(self._history)
        return [r.to_dict() for r in records]
