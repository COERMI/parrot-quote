#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
羽栖 · 可编辑报价器后端
本地运行，支持手机/电脑浏览器打开、上传图片、保存配置。
运行：python server.py  （默认端口 8000）
访问：http://localhost:8000
手机同网段访问：http://<电脑IP>:8000
"""
import os
import json
import base64
import ssl
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE, "uploads")
STATIC_DIR = os.path.join(BASE, "static")
DATA_FILE = os.path.join(BASE, "data.json")
SUBMISSIONS_FILE = os.path.join(BASE, "submissions.jsonl")

# 后台查看密码：访问 /admin 或 /api/submissions 时需带 ?key= 校验
# 未设置时默认 admin 密码为 123456，建议通过 ADMIN_KEY 环境变量自定义
ADMIN_KEY = os.environ.get("ADMIN_KEY", "123456")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default_data()

def load_submissions():
    """读取所有已提交的清单(每行一条 JSON)。"""
    subs = []
    if os.path.exists(SUBMISSIONS_FILE):
        try:
            with open(SUBMISSIONS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            subs.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
    return subs

def default_data():
    return {
        "shop": "羽栖 · 精致鹦鹉生活馆",
        "subtitle": "两只和尚鹦鹉 · 完整饲养套装报价单",
        "cages": [
            {"id": "cage_white", "name": "达洋 B6047SS 白色", "price": 438, "color": "#FFFFFF"},
            {"id": "cage_pink", "name": "达洋 B6047SS 粉色", "price": 438, "color": "#F4C0D1"}
        ],
        "plans": [
            {"id": "simple", "name": "简单版", "desc": "已断奶 · 基础配置", "items": []},
            {"id": "full", "name": "复杂版", "desc": "未断奶 · 含保温箱+奶粉+喂养工具", "items": []}
        ],
        "items": []
    }

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body) if isinstance(body, (bytes, bytearray)) else len(body.encode("utf-8"))))
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _serve_file(self, filepath):
        ext = os.path.splitext(filepath)[1].lower()
        with open(filepath, "rb") as f:
            return self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = {}
        if parsed.query:
            for kv in parsed.query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    qs[unquote(k)] = unquote(v)

        if path == "/":
            path = "/index.html"
        # 后台查看页(需 key)
        if path == "/admin":
            if qs.get("key") != ADMIN_KEY:
                return self._send(401, "未授权：请在网址后加 ?key=密码", "text/plain; charset=utf-8")
            return self._serve_file(os.path.join(BASE, "admin.html"))
        # API
        if path == "/api/data":
            return self._send(200, json.dumps(load_data(), ensure_ascii=False))
        # 查看已提交的清单(给自己看，需 key)
        if path == "/api/submissions":
            if qs.get("key") != ADMIN_KEY:
                return self._send(401, json.dumps({"ok": False, "error": "unauthorized"}, ensure_ascii=False))
            return self._send(200, json.dumps(load_submissions(), ensure_ascii=False))
        # 静态文件
        filepath = os.path.normpath(os.path.join(BASE, path.lstrip("/")))
        if os.path.isfile(filepath):
            return self._serve_file(filepath)
        return self._send(404, "Not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/save":
            try:
                body = self._read_body()
                data = json.loads(body.decode("utf-8"))
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                # 解析简单 multipart
                content_type = self.headers.get("Content-Type", "")
                boundary = content_type.split("boundary=")[-1].strip()
                b_boundary = ("--" + boundary).encode()
                parts = raw.split(b_boundary)
                for part in parts:
                    if b"filename=" in part:
                        header, _, data = part.partition(b"\r\n\r\n")
                        data = data.rsplit(b"\r\n", 1)[0]
                        # 提取原始文件名和扩展名
                        import re as _re
                        m = _re.search(rb'filename="([^"]*)"', header)
                        orig_name = m.group(1).decode("utf-8", "ignore") if m else "upload"
                        ext = os.path.splitext(orig_name)[1].lower()
                        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                            ext = ".jpg"
                        # 用 uuid 保证文件名绝对唯一，杜绝覆盖
                        fname = "img_" + uuid.uuid4().hex + ext
                        fpath = os.path.join(UPLOAD_DIR, fname)
                        with open(fpath, "wb") as f:
                            f.write(data)
                        return self._send(200, json.dumps({"ok": True, "url": "/uploads/" + fname}, ensure_ascii=False))
                return self._send(400, json.dumps({"ok": False, "error": "no file"}))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/submit":
            try:
                body = self._read_body()
                data = json.loads(body.decode("utf-8"))
                data["submittedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
                data["id"] = uuid.uuid4().hex[:10]
                with open(SUBMISSIONS_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
                return self._send(200, json.dumps({"ok": True, "id": data["id"]}, ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}))
        return self._send(404, "Not found", "text/plain")

def main():
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    # 可选 HTTPS:设置 SSL=1 且提供 SSL_CERT / SSL_KEY 时启用
    ssl_on = os.environ.get("SSL") == "1"
    cert = os.environ.get("SSL_CERT")
    key = os.environ.get("SSL_KEY")
    if ssl_on and cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        proto = "https"
    else:
        proto = "http"

    print("=" * 50)
    print("羽栖 · 可编辑报价器 已启动")
    print("本机访问: %s://localhost:%d" % (proto, port))
    print("手机访问: 同一WiFi下，用电脑IP + 端口 %d" % port)
    print("按 Ctrl+C 停止")
    print("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()

if __name__ == "__main__":
    main()
