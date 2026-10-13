# Python 代码导读

这份说明面向第一次阅读本项目 Python 代码的同学。项目只使用 Python 标准库，
建议按下面的顺序阅读：先看“程序怎么启动”，再看“一个请求如何变成一次传输”，
最后再阅读报文和异常网络处理的细节。

## 1. 先记住四层结构

```text
server.py
  ├─ tftp/server.py       监听 UDP、分发请求、管理会话
  ├─ tftp/session.py      一次 RRQ/WRQ 传输的状态机
  ├─ tftp/packets.py      TFTP 报文的编码与解析
  ├─ tftp/netascii.py     文本模式的换行转换
  ├─ tftp/registry.py     给 Web 页面看的活动/历史记录
  ├─ tftp/stats.py        吞吐量与全局计数
  └─ tftp/logger.py       控制台、文件、网页日志

web/app.py                浏览器页面和 REST API
```

每个模块只负责一类事情。阅读时可以先跳过不属于当前问题的模块，例如调试报文
格式时不必先读 `web/app.py`。

## 2. 程序启动顺序

入口是 `server.py` 的 `main()`：

1. `parse_args()` 读取命令行参数。
2. `build_config()` 读取 `config.json`，再用命令行参数覆盖同名字段。
3. 创建日志、登记表、统计对象。
4. 创建 `TFTPServer` 和 `WebUI`，把这些对象互相传入。这个过程叫“依赖注入”，
   好处是每个类不用自己创建全局对象。
5. `tftp.start()` 绑定 UDP 端口；`web.start()` 启动 Web 线程。
6. 主线程等待 Ctrl+C 或 SIGTERM，最后按 Web、TFTP、日志的顺序清理资源。

## 3. 一次下载和上传怎样运行

`tftp/server.py` 的监听线程只处理端口上的第一份 RRQ/WRQ：

```text
客户端 -> UDP 监听端口 -> PacketParser.parse()
                     -> 检查路径/并发/重复请求
                     -> 创建 TransferSession
                     -> 会话线程使用临时 UDP 端口通信
```

`TransferSession.run()` 是一次传输的总入口：

1. `_precheck()` 检查模式、路径、文件存在性和覆盖策略。
2. RRQ 进入 `_run_read()`，服务器发送 DATA，等待 ACK。
3. WRQ 进入 `_run_write()`，服务器发送 ACK(0)，等待 DATA。
4. 每个数据块都更新 `TransferRecord`、吞吐量和 MD5。
5. 成功走 `_finish_success()`，失败走 `_finish_failure()`。
6. `finally` 始终执行 `_cleanup()`，关闭文件和 Socket，归还并发名额。

不要把 `seq` 和报文里的 `block` 混为一谈：`seq` 是服务器内部的无界计数，
`block` 是线上 16 位块号，所以大文件在 65535 后会回到 0。

## 4. 阅读报文代码的顺序

建议按以下顺序打开 `tftp/packets.py`：

1. 先看常量：`OP_RRQ`、`OP_WRQ`、`BLOCK_SIZE`。
2. 再看 `TftpPacket` 抽象基类：所有报文都有 `to_bytes()` 和 `from_bytes()`。
3. 然后看 `RequestPacket`、`DataPacket`、`AckPacket`、`ErrorPacket`。
4. 最后看 `PacketParser.parse()`，它根据前两个字节的 opcode 选择报文类。

报文层只负责“字节和对象之间的转换”，不负责发包、重传或文件读写。

## 5. Web 层怎样拿到数据

`web/app.py` 使用 Python 标准库的 `ThreadingHTTPServer`。浏览器每秒轮询：

- `/api/state`：服务状态、统计、活动传输和历史记录；
- `/api/logs?after_id=N`：只取编号大于 `N` 的新日志；
- `/api/files`：列出根目录文件；
- `/api/config`：保存设置；
- `/api/server/start` 和 `/api/server/stop`：控制 TFTP 监听。

Web 层只读取 `registry`、`stats`、`logger` 的快照，不直接参与 TFTP 报文收发。

## 6. 修改代码时的安全边界

下列部分属于协议正确性的关键路径，改动后应立即运行协议测试：

- `tftp/session.py` 的超时、重传、重复 ACK/DATA、末块宽限；
- `tftp/packets.py` 的报文字节格式；
- `tftp/server.py` 的 TID、请求去重和并发信号量；
- `tftp/netascii.py` 的跨块 CR/LF 状态。

日志文字、注释、Web 展示字段和代码排版可以独立调整。若需要重构关键路径，
一次只改一个状态，并用下载、上传、netascii 和丢包场景分别验证。

## 7. 推荐的本地检查命令

在仓库根目录执行：

```bash
python3 -m compileall -q tftp-server
python3 调试脚本/test_packets.py
python3 调试脚本/test_netascii_e2e.py
```

如果要验证真实传输，再启动 `server.py`，使用 `curl tftp://...` 下载和上传，
最后比较文件内容或 MD5。这样可以同时覆盖代码检查和实际 Socket 路径。
