# -*- coding: utf-8 -*-
"""支持 Range 的静态文件服务 —— 把本机文件经临时隧道提供给云服务器拉取。

用途（见 README「传输大文件到服务器」）：
  知识库等大文件不在 Git 仓库里，更新时要同步到云服务器；而服务器直连 GitHub/公网很慢，
  瓶颈实际在**本机上行**。所以由本机起服务、服务器主动来拉，实测比直连快两个数量级。

用法：
    python deploy/http_range_server.py [端口=8080] [目录=当前目录]
    # 另开终端建临时隧道（二选一）：
    #   cloudflared tunnel --url http://localhost:8080   ← 推荐，实测约 2.9 MB/s
    #   cpolar http 8080                                 ← 免费版约 130 KB/s 且单连接限流

为什么不直接用现成方案：
  · `python -m http.server` **不支持 Range** → 无法断点续传 / 分段并发；
  · `rangehttpserver` 第三方模块实测在 8 路并发 Range 请求下会崩溃退出。
本实现用 ThreadingMixIn + 断连静默处理，扛得住多路并发拉取。
"""
import http.server
import os
import re
import socketserver
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
ROOT = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        return   # 静默：并发下载会刷爆日志

    def do_GET(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().do_GET()
        if not os.path.isfile(path):
            self.send_error(404, 'Not Found')
            return

        size = os.path.getsize(path)
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        if rng:
            m = re.match(r'bytes=(\d+)-(\d*)', rng)
            if not m:
                self.send_error(416, 'Bad Range')
                return
            start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
        if start >= size:
            self.send_error(416, 'Range Not Satisfiable')
            return

        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        if rng:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.end_headers()

        with open(path, 'rb') as f:
            f.seek(start)
            remain = length
            while remain > 0:
                chunk = f.read(min(262144, remain))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return    # 客户端断开：静默结束，不抛栈
                remain -= len(chunk)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == '__main__':
    with Server(('0.0.0.0', PORT), RangeHandler) as httpd:
        print('serving %s on 0.0.0.0:%d' % (ROOT, PORT), flush=True)
        httpd.serve_forever()
