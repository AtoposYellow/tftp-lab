# -*- coding: utf-8 -*-
"""
TFTP 服务器程序入口

启动方式（在项目根目录下执行）：
    python server.py                     # 按 config.json 启动
    python server.py --port 1069         # 临时改用 UDP 1069 端口（macOS 调试）
    python server.py --root ./files      # 临时指定根目录

启动后：
    * TFTP 服务常驻 UDP 69 端口（可用标准 TFTP 客户端上传/下载）；
    * Web 管理台位于 http://127.0.0.1:8080（浏览器打开即监控与配置）；
    * 控制台实时打印彩色日志；Ctrl+C 优雅停机。

本模块只做"组装与生命周期管理"，协议逻辑见 tftp/ 包，界面逻辑见 web/ 包。
"""

import argparse
import os
import signal
import sys
import time

# 保证以相对路径找到项目内的 tftp/ 与 web/ 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tftp.logger import EventLog                      # noqa: E402
from tftp.registry import TransferRegistry            # noqa: E402
from tftp.server import ServerConfig, TFTPServer      # noqa: E402
from tftp.stats import GlobalStats                    # noqa: E402
from tftp import VERSION                              # noqa: E402
from web.app import WebUI                             # noqa: E402

# 默认配置文件路径（与 server.py 同目录）
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')


def parse_args() -> argparse.Namespace:
    """解析命令行参数：所有参数均可覆盖 config.json 中的同名配置。"""
    parser = argparse.ArgumentParser(description='TFTP 服务器（Socket 编程实验）')
    parser.add_argument('--port', type=int, help='TFTP 监听的 UDP 端口（默认 69）')
    parser.add_argument('--web-port', type=int, help='Web 管理台 TCP 端口（默认 8080）')
    parser.add_argument('--root', help='服务器根目录（上传/下载限制在该目录内）')
    parser.add_argument('--timeout', type=float, help='收包超时秒数（默认 2.0）')
    parser.add_argument('--retries', type=int, help='重传次数上限（默认 6）')
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                        help='控制台日志级别（默认 INFO）')
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> ServerConfig:
    """加载 config.json 并应用命令行覆盖项。"""
    config = ServerConfig.load(CONFIG_PATH)
    if args.port is not None:
        config.tftp_port = args.port
    if args.web_port is not None:
        config.web_port = args.web_port
    if args.root is not None:
        config.root_dir = args.root
    if args.timeout is not None:
        config.timeout = args.timeout
    if args.retries is not None:
        config.retries = args.retries
    if args.log_level is not None:
        config.log_level = args.log_level
    return config


def print_banner(config: ServerConfig) -> None:
    """控制台启动横幅（提示关键入口信息）。"""
    root_abs = os.path.abspath(config.root_dir)
    print('=' * 62)
    print(f'  TFTP 服务器 v{VERSION}  ——  《计算机通信与网络》Socket 编程实验')
    print('=' * 62)
    print(f'  TFTP 服务 : udp://0.0.0.0:{config.tftp_port}  (RFC 1350)')
    print(f'  Web 管理台: http://127.0.0.1:{config.web_port}')
    print(f'  根目录    : {root_abs}')
    print(f'  参数      : 超时 {config.timeout}s / 重传 {config.retries} 次 / '
          f'并发上限 {config.max_concurrent} / '
          f'覆盖已存在文件 {"开" if config.overwrite else "关"}')
    print('=' * 62)


def main() -> int:
    args = parse_args()
    config = build_config(args)

    # ---- 组装各组件（依赖注入：组件间只通过构造参数耦合）----
    log = EventLog(log_dir=config.log_dir, console_level=config.log_level)
    registry = TransferRegistry()
    stats = GlobalStats()
    tftp = TFTPServer(config, log, registry, stats)
    web = WebUI(config, tftp, registry, stats, log, config_path=CONFIG_PATH)

    print_banner(config)

    # ---- 启动 TFTP 监听与 Web 界面 ----
    try:
        tftp.start()
    except OSError as exc:
        log.error(f'TFTP 端口 {config.tftp_port} 绑定失败: {exc}')
        print(f'\n[错误] 无法绑定 UDP {config.tftp_port} 端口：{exc}')
        print('  - macOS/Linux 调试请使用非特权端口，如: python server.py --port 1069')
        print('  - 或确认端口未被其他 TFTP 服务占用')
        return 1
    web.start()

    # ---- 注册信号：Ctrl+C (SIGINT) / kill (SIGTERM) 优雅停机 ----
    def _handle_signal(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, AttributeError):
        pass  # Windows 不支持 SIGTERM，忽略

    # ---- 主线程保持存活，等待退出信号 ----
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n收到退出信号，正在优雅停机……')
    finally:
        web.stop()
        tftp.stop()
        log.system('程序退出')
    return 0


if __name__ == '__main__':
    sys.exit(main())
