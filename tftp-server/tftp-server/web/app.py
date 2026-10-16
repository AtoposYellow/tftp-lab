"""
Web 管理界面模块（图形界面）

        GET  /api/state            服务器状态 + 全局统计 + 活动/历史传输列表
        GET  /api/logs?after_id=N  增量拉取日志（配合内存环形缓冲）
        GET  /api/files?path=      浏览服务器根目录（防越权，复用 PathGuard）
        POST /api/config           修改配置（端口/根目录/超时/重试/并发/覆盖）
        POST /api/server/start     启动 TFTP 监听
        POST /api/server/stop      停止 TFTP 监听（Web 界面本身保持可用）
"""

import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from tftp import VERSION

# Web 静态资源目录（web/templates 与 web/static）
_WEB_DIR = Path(__file__).resolve().parent


class WebUI:
    """
    Web 管理界面的封装类。
    持有对 TFTP 服务器各组件的引用，通过类属性注入给 HTTP 处理器；
    start()/stop() 控制 HTTP 服务线程（独立于 TFTP 监听的启停）。
    """

    def __init__(self, config, tftp_server, registry, stats, event_log,
                 config_path: str = 'config.json'):
        self.config = config             # 保存服务器配置对象
        self.tftp = tftp_server
        self.registry = registry
        self.stats = stats
        self.log = event_log
        # 以上均为引用
        self.config_path = config_path   # 配置文件绝对路径（保存用，与启动目录无关）

        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

        # 把组件引用挂到处理器类上（ThreadingHTTPServer 每请求实例化一个
        # handler，通过类属性共享上下文是最简洁的注入方式）
        ApiHandler.ui = self

    # 启动 / 停止
    def start(self) -> None:
        """在独立线程中启动 HTTP 服务（daemon：主进程退出时自动结束）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._httpd = ThreadingHTTPServer(('0.0.0.0', self.config.web_port), ApiHandler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name='web-ui', daemon=True)
        self._thread.start()
        self.log.system(f'Web 管理界面已启动：http://127.0.0.1:{self.config.web_port}')

    def stop(self) -> None:
        """停止 HTTP 服务（程序退出时调用）。"""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


class ApiHandler(BaseHTTPRequestHandler):
    """
    HTTP 请求处理器：静态页面 + REST API 路由分发
    """

    # WebUI 实例（由 WebUI.__init__ 注入）
    ui: 'WebUI' = None
    # Server 响应头
    server_version = f'TFTPLabWeb/{VERSION}'

    # GET 路由
    # 当浏览器发送 GET 请求时，标准库 HTTP 服务会调用 do_GET()
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route == '/':
            self._serve_file(_WEB_DIR / 'templates' / 'index.html')
        elif route.startswith('/static/'):
            # 防目录穿越：只允许 static 目录下的直接文件名
            rel = route[len('/static/'):]
            if '/' in rel or '..' in rel:
                self._send_json({'error': '非法路径'}, 400)
                return
            self._serve_file(_WEB_DIR / 'static' / rel)
        elif route == '/api/state':
            self._send_json(self._build_state())
        elif route == '/api/logs':
            after = int(query.get('after_id', ['0'])[0])
            self._send_json({'logs': self.ui.log.buffer.get_after(after)})
        elif route == '/api/files':
            self._send_json(self._list_files(query.get('path', [''])[0]))
        elif route == '/api/dirs':
            self._send_json(self._list_dirs(query.get('path', [''])[0]))
        else:
            self._send_json({'error': '接口不存在'}, 404)

    # POST 路由
    def do_POST(self) -> None:
        route = urlparse(self.path).path
        body = self._read_json()

        if route == '/api/config':
            self._handle_config(body)
        elif route == '/api/server/start':
            self._handle_server_toggle(True)
        elif route == '/api/server/stop':
            self._handle_server_toggle(False)
        else:
            self._send_json({'error': '接口不存在'}, 404)

    # API 实现
    # 将不同模块中的数据汇总，返回给浏览器
    def _build_state(self) -> dict:
        """聚合服务器状态 / 全局统计 / 传输列表，供前端一次轮询取全。"""
        cfg, tftp, stats = self.ui.config, self.ui.tftp, self.ui.stats
        return {
            'version': VERSION,
            'server': {
                'running': tftp.is_running,  # TFTP是否运行
                'tftp_port': cfg.tftp_port,
                'web_port': cfg.web_port,
                'root_dir': str(Path(cfg.root_dir).resolve()),
                'timeout': cfg.timeout,
                'retries': cfg.retries,
                'max_concurrent': cfg.max_concurrent,
                'overwrite': cfg.overwrite,
                'grace_seconds': cfg.grace_seconds,
                'uptime_s': tftp.uptime,
                'active_sessions': tftp.active_session_count(),
            },
            'stats': stats.snapshot(),
            'transfers': {
                'active': self.ui.registry.snapshot_active(),    # 活动传输记录
                'history': self.ui.registry.snapshot_history(),  # 历史传输记录
            },
        }

    # 浏览服务器文件
    def _list_files(self, rel_path: str) -> dict:
        """列出根目录（或其子目录）内容，供前端"文件浏览"面板使用。"""
        try:
            # 复用 PathGuard 做越权防护：只允许根目录内的路径
            base = self.ui.tftp.path_guard.resolve(rel_path) if rel_path \
                else self.ui.tftp.path_guard.root
            if not base.is_dir():
                return {'error': '路径不是目录'}
            entries = []
            for entry in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
                if entry.name.startswith('.'):
                    continue  # 隐藏文件不展示
                stat = entry.stat()
                entries.append({
                    'name': entry.name,
                    'is_dir': entry.is_dir(),
                    'size': stat.st_size if entry.is_file() else 0,
                    'mtime': int(stat.st_mtime),
                })
            return {'path': rel_path, 'entries': entries}
        except (ValueError, OSError) as exc:
            return {'error': str(exc)}

    def _list_dirs(self, raw_path: str) -> dict:
        """
        列出指定绝对路径下的子目录（供设置页"浏览"选择根目录使用）
            * 只返回目录名，不返回文件，也不返回目录内容；
            * 隐藏目录（.开头）不列出；
            * 无权限进入的目录直接过滤，避免点击后报错；
            * parent 为空串表示已到文件系统根（Windows 为盘符根）。
        """
        path = os.path.normpath(raw_path or os.sep)
        if not os.path.isdir(path):
            return {'error': '目录不存在或不可访问'}
        dirs = []
        try:
            for name in sorted(os.listdir(path)):
                if name.startswith('.'):
                    continue
                full = os.path.join(path, name)
                if not os.path.isdir(full):
                    continue
                try:
                    os.listdir(full)          # 可进入才列出
                except OSError:
                    continue
                dirs.append(name)
        except OSError as exc:
            return {'error': str(exc)}
        parent = os.path.dirname(path)
        if parent == path:                    # 已是根（macOS/Linux 的 /，Windows 的盘符根）
            parent = ''
        return {'path': path, 'parent': parent, 'dirs': dirs}

    def _handle_config(self, body: dict) -> None:
        """
        应用前端提交的配置修改。
        """
        cfg, tftp = self.ui.config, self.ui.tftp
        notes = []
        try:
            # 检查是否合法，合法则写入
            # 普通参数更新，如超时、重传次数等
            if 'timeout' in body:
                timeout = float(body['timeout'])
                if not (0.2 <= timeout <= 30):
                    raise ValueError('超时须在 0.2~30 秒之间')
                cfg.timeout = timeout
            if 'retries' in body:
                retries = int(body['retries'])
                if not (1 <= retries <= 30):
                    raise ValueError('重传次数须在 1~30 之间')
                cfg.retries = retries
                # 修改并发上限
            if 'max_concurrent' in body:
                concurrent = int(body['max_concurrent'])
                if not (1 <= concurrent <= 512):
                    raise ValueError('并发上限须在 1~512 之间')
                cfg.max_concurrent = concurrent
                tftp.resize_slots(concurrent)
            if 'overwrite' in body:
                cfg.overwrite = bool(body['overwrite'])
            if 'grace_seconds' in body:
                grace = int(body['grace_seconds'])
                if not (0 <= grace <= 60):
                    raise ValueError('宽限期须在 0~60 秒之间')
                cfg.grace_seconds = grace

            # 根目录：必须存在或可创建
            if 'root_dir' in body and body['root_dir']:
                root = str(body['root_dir'])
                Path(root).mkdir(parents=True, exist_ok=True)
                cfg.root_dir = root
                from tftp.server import PathGuard
                tftp.path_guard = PathGuard(root)

            # TFTP 端口：变更时重启监听线程
            # 修改 TFTP监听端口
            if 'tftp_port' in body:
                new_port = int(body['tftp_port'])
                if not (1 <= new_port <= 65535):
                    raise ValueError('TFTP 端口须在 1~65535 之间')
                if new_port != cfg.tftp_port:
                    was_running = tftp.is_running
                    if was_running:
                        tftp.stop()
                    cfg.tftp_port = new_port
                    if was_running:
                        tftp.start()  # 端口被占用时这里抛异常
                    notes.append(f'TFTP 端口已切换为 {new_port}')

            # Web 端口：仅记录，重启程序后生效
            if 'web_port' in body:
                new_web_port = int(body['web_port'])
                if not (1 <= new_web_port <= 65535):
                    raise ValueError('Web 端口须在 1~65535 之间')
                if new_web_port != cfg.web_port:
                    cfg.web_port = new_web_port
                    notes.append('Web 端口将在下次启动程序后生效')

            cfg.save(self.ui.config_path)
            self.ui.log.system(f'配置已更新: {body}')
            self._send_json({'ok': True, 'notes': notes})
        except (ValueError, OSError) as exc:
            self._send_json({'ok': False, 'error': str(exc)}, 400)

    def _handle_server_toggle(self, start: bool) -> None:
        """启动 / 停止 TFTP 监听（Web 界面本身保持在线）。"""
        try:
            if start:
                self.ui.tftp.stopping.clear()
                self.ui.tftp.start()
            else:
                self.ui.tftp.stop()
            self._send_json({'ok': True, 'running': self.ui.tftp.is_running})
        except OSError as exc:
            self._send_json({'ok': False, 'error': f'端口绑定失败: {exc}'}, 400)

    # HTTP 工具方法
    def _serve_file(self, path: Path) -> None:
        """返回静态文件；不存在时返回 404。"""
        if not path.is_file():
            self._send_json({'error': '页面不存在'}, 404)
            return
        content = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype == 'application/javascript':
            ctype += '; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')  # 调试/演示时始终拿最新页面
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, obj: dict, status: int = 200) -> None:
        """以 JSON 响应请求。"""
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        """
        读取 POST 请求的 JSON 体
        """
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length) or b'{}')
            return body if isinstance(body, dict) else {}
        except (ValueError, TypeError, json.JSONDecodeError):
            return {}

    def log_message(self, fmt: str, *args) -> None:
        """
        把 http.server 默认的 stderr 访问日志改走 EventLog 的 http 通道
        （DEBUG 级、只进控制台/文件，不进入网页日志面板，避免轮询刷屏）
        """
        if self.ui is not None and self.ui.log is not None:
            self.ui.log.http('HTTP ' + fmt % args)
