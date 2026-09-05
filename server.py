import http.server
import os
import json
import urllib.parse
import logging
from pathlib import Path

# 配置安全与访问日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(client_ip)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("AppServer")

ROOT = Path(__file__).resolve().parent

API = {
    '/user/api/index/data': 'index_data.json',
    '/user/api/site/info': 'site_info.json',
    '/user/api/index/pay': 'index_pay.json',
    '/user/api/index/commodity': 'index_commodity.json',
    '/user/api/index/commodityDetail': 'commodity_detail.json',
}

# 敏感文件/目录黑名单，禁止任何外部读取
BLOCKED_PATTERNS = {
    '.env', '.git', 'Dockerfile', 'requirements.txt',
    'server.py', '_inspect.py', 'vercel.json'
}

class HardenedHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    @property
    def client_ip(self):
        # 兼容代理标头
        return self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        clean_path = parsed.path

        # 1. 拦截对敏感配置文件与源码的访问 (未授权/信息泄露防护)
        req_filename = Path(clean_path).name
        if req_filename in BLOCKED_PATTERNS or clean_path.startswith('/.'):
            logger.warning(f"BLOCKED SENSITIVE FILE ACCESS: {clean_path}", extra={'client_ip': self.client_ip})
            self.send_error(403, "Access Forbidden")
            return

        # 2. 仿真 API 路由分发 (白名单映射)
        if clean_path in API:
            api_file = ROOT / 'api' / API[clean_path]
            try:
                # 校验路径确保处于 api 目录下 (防止路径遍历)
                api_file = api_file.resolve()
                if not str(api_file).startswith(str(ROOT / 'api')):
                    raise PermissionError("Path traversal detected")

                with open(api_file, 'rb') as f:
                    body = f.read()

                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                logger.info(f"API 200: {clean_path}", extra={'client_ip': self.client_ip})
                return
            except Exception as e:
                logger.error(f"API Error {clean_path}: {e}", extra={'client_ip': self.client_ip})
                self.send_error(500, "Internal Server Error")
                return

        # 3. 静态文件分发 (防止跳出 ROOT 目录的路径穿越)
        try:
            target_path = (ROOT / clean_path.lstrip('/')).resolve()
            if target_path.is_dir():
                target_path = target_path / 'index.html'
            if not str(target_path).startswith(str(ROOT)):
                logger.warning(f"PATH TRAVERSAL ATTEMPT: {clean_path}", extra={'client_ip': self.client_ip})
                self.send_error(403, "Forbidden")
                return
        except Exception:
            self.send_error(400, "Bad Request")
            return

        logger.info(f"STATIC: {clean_path}", extra={'client_ip': self.client_ip})
        return super().do_GET()

    def end_headers(self):
        # 增加关键安全标头防点击劫持、MIME 嗅探和 XSS
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-XSS-Protection', '1; mode=block')
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

    def log_message(self, format, *args):
        # 拦截标准库无格式打印，统一交给 logging 记录
        pass

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8686))
    host = os.environ.get('HOST', '0.0.0.0')
    logger.info(f"Starting hardened server at http://{host}:{port}", extra={'client_ip': 'SYSTEM'})
    with http.server.ThreadingHTTPServer((host, port), HardenedHandler) as httpd:
        httpd.serve_forever()
