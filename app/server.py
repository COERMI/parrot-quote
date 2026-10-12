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
PURCHASES_FILE = os.path.join(BASE, "purchases.json")   # 采购记录:勾选状态 + 实际采购价
PURCHASE_LOCK = threading.Lock()

# 后台查看密码：访问 /admin 或 /api/submissions 时需带 ?key= 校验
# 未设置时默认 admin 密码为 123456，建议通过 ADMIN_KEY 环境变量自定义
ADMIN_KEY = os.environ.get("ADMIN_KEY", "123456")

# 演示模式：DEMO=1 时把数据文件指向 demo/ 下的样例数据，不碰真实的提交记录
# 与采购记录。用途是把仓库 clone 下来、手上还没有自己的数据时，也能直接
# 打开后台看到完整效果。
# 注意：演示模式下在后台点的勾选/改价会写进 demo/purchases.json，
#       想恢复成仓库里的原样，执行 `git checkout app/demo` 即可。
DEMO = os.environ.get("DEMO") == "1"
if DEMO:
    SUBMISSIONS_FILE = os.path.join(BASE, "demo", "submissions.jsonl")
    PURCHASES_FILE = os.path.join(BASE, "demo", "purchases.json")

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

PURCHASES_VER = 3


def _norm_purchase(v):
    """把一条采购项规范化,顺便丢弃畸形数据。"""
    if not isinstance(v, dict):
        return None
    src = v.get("source")
    if not isinstance(src, list):
        src = []
    src = [str(x) for x in src if isinstance(x, (str, int))]
    out = {
        "label": (str(v["label"]) if v.get("label") else None),
        # 合并项的显示名;为空则由前端用被覆盖的报价行名兜底
        "checked": bool(v.get("checked", False)),
        "actual": v.get("actual", None),
        "source": src,          # 覆盖了哪些报价行 key;赠品为空数组
        "gift": bool(v.get("gift", False)),
        "qty": v.get("qty", 1),
        # 手工填的"客户报价"(给客户看的价):
        #   None  -> 用被覆盖报价行的价格求和(普通项);赠送项则视为真赠品(计 0)
        #   数字  -> 以这个价计入"给客户的清单"
        "quoted": v.get("quoted", None),
        # 手工新增项(赠送/补充项)挂在哪个客户的提交下;None = 全局
        "sub": (str(v["sub"]) if v.get("sub") else None),
    }
    try:
        out["qty"] = max(1, int(out["qty"]))
    except Exception:
        out["qty"] = 1
    try:
        if out["quoted"] in (None, ""):
            out["quoted"] = None
        else:
            out["quoted"] = float(out["quoted"])
            if out["quoted"] < 0:
                out["quoted"] = None
    except Exception:
        out["quoted"] = None
    return out


def _norm_sub(v):
    """一条客户结算信息。

    paid    客户实际转账金额(float 或 None)
    adj     抹零那一行的自定义名称,如"抹零优惠"
    done    交易是否完成:True = 手动标记完成 / False = 手动标记未完成 / None = 自动判定
    doneAt  完成时间(手动标记时由前端写入,仅作展示)
    """
    if not isinstance(v, dict):
        return None
    paid = v.get("paid", None)
    try:
        paid = None if paid in (None, "") else float(paid)
    except Exception:
        paid = None
    done = v.get("done", None)
    if done is not None:
        done = bool(done)
    return {
        "paid": paid,
        "adj": (str(v["adj"]) if v.get("adj") else None),
        "done": done,
        "doneAt": (str(v["doneAt"]) if v.get("doneAt") else None),
    }


def load_purchases():
    """读取采购记录,并把旧版(v1/v2)结构自动升级到 v3。

    v1(最旧): { "<提交id>|<组>|<行>": {"checked": true, "actual": 120} }
              —— 一个报价行就是一个采购项,无法合并、无法加赠品
    v2:       {"ver": 2, "items": {...}}   —— 报价行与采购项解耦,支持合并/赠品
    v3(当前): {"ver": 3, "items": {...}, "subs": {"<提交id>": {"paid": 800, "done": true}}}
              —— 增加"客户实付金额",用于把清单凑平到客户实际转账的数;
                 以及"交易是否完成"(done),用于把整单标成已结算
    """
    raw = {}
    if os.path.exists(PURCHASES_FILE):
        try:
            with open(PURCHASES_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    subs = {}
    if isinstance(raw.get("subs"), dict):
        for k, v in raw["subs"].items():
            sv = _norm_sub(v)
            if sv is not None:
                subs[str(k)] = sv

    if raw.get("ver") != PURCHASES_VER:
        items = {}
        # 兼容 v1 的扁平结构:每个顶层 key 就是一个"只覆盖自己"的采购项
        for k, v in raw.items():
            if k in ("ver", "items", "subs"):
                continue
            it = _norm_purchase(v)
            if it is None:
                continue
            if not it["source"] and not it["gift"]:
                it["source"] = [k]
            items[str(k)] = it
        # v2 的 items 结构与 v3 一致,直接沿用
        if isinstance(raw.get("items"), dict):
            for k, v in raw["items"].items():
                it = _norm_purchase(v)
                if it is not None:
                    items[str(k)] = it
        return {"ver": PURCHASES_VER, "items": items, "subs": subs}

    if not isinstance(raw.get("items"), dict):
        raw["items"] = {}
    raw["subs"] = subs
    return raw


def save_purchases(upsert=None, remove=None, subs=None):
    """合并写入采购记录。upsert: {id: {...}};remove: [id, ...];subs: {提交id: {...}}。

    写入采用"临时文件 + 原子替换",避免进程被杀时留下半截 JSON。
    """
    with PURCHASE_LOCK:
        cur = load_purchases()
        items = cur.get("items") or {}
        submap = cur.get("subs") or {}
        for k in (remove or []):
            items.pop(str(k), None)
        for k, v in (upsert or {}).items():
            k = str(k)
            it = _norm_purchase(v)
            if it is None:
                continue
            # 兼容旧的单条写法(不带 source):视为"只覆盖自己"。
            # 赠品项本来就没有报价来源,必须保持空数组,否则会被前端当成普通项。
            if not it["source"] and not it["gift"]:
                it["source"] = [k]
            items[k] = it
        for k, v in (subs or {}).items():
            k = str(k)
            sv = _norm_sub(v)
            if sv is None:
                submap.pop(k, None)
            else:
                submap[k] = sv
        out = {"ver": PURCHASES_VER, "items": items, "subs": submap}
        tmp = PURCHASES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PURCHASES_FILE)
        return out


def parse_qs(raw):
    qs = {}
    if raw:
        for kv in raw.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                qs[unquote(k)] = unquote(v)
    return qs


# 允许对外公开的静态资源扩展名(图片/样式等,不含 .py/.json/.jsonl 等数据文件)
PUBLIC_EXTS = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}
# 允许直接访问的根级文件(admin.html 不在此列，只能通过带 key 的 /admin 访问)
PUBLIC_ROOT_FILES = {"index.html", "favicon.ico", "robots.txt"}


def is_public_path(relpath):
    """静态文件白名单校验。

    安全背景:早期实现是把请求路径直接拼到项目目录下,导致 /server.py、
    /submissions.jsonl、/data.json、/purchases.json 这些后端文件可以被任何人
    直接下载(朋友的提交数据、源码全部泄露)。这里改为白名单:只放行前端页面
    与 static/uploads 下的图片资源,其余一律 404。
    """
    rel = str(relpath).replace("\\", "/").lstrip("/")
    if rel.startswith("../") or rel == ".." or "/../" in rel:
        return False
    if rel in PUBLIC_ROOT_FILES:
        return True
    if rel.startswith("static/") or rel.startswith("uploads/"):
        return os.path.splitext(rel)[1].lower() in PUBLIC_EXTS
    return False


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
    # HTTP/1.1 + keep-alive:手机端一次要加载 30 多张图,复用同一条连接会快很多
    protocol_version = "HTTP/1.1"
    # 连接超时:空闲或慢速连接 75 秒后自动断开(比 Nginx 的上游空闲超时略长,
    # 保证由 Nginx 先关连接,避免复用到已被关闭的连接)
    timeout = 75

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache)
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
        # 静态资源可缓存:图片/字体等长期不变,html 短缓存(方便改动后快速生效)
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico"):
            cache = "public, max-age=604800"  # 图片缓存 7 天
        else:
            cache = "no-cache"  # html/js/css 每次校验,改动后及时生效
        with open(filepath, "rb") as f:
            return self._send(200, f.read(), MIME.get(ext, "application/octet-stream"), cache)

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
        # 采购记录:勾选状态 + 实际采购价(需 key)
        if path == "/api/purchases":
            if qs.get("key") != ADMIN_KEY:
                return self._send(401, json.dumps({"ok": False, "error": "unauthorized"}, ensure_ascii=False))
            return self._send(200, json.dumps(load_purchases(), ensure_ascii=False))
        # 静态文件(白名单:只放行前端页面 + static/uploads 下的图片)
        filepath = os.path.normpath(os.path.join(BASE, path.lstrip("/")))
        if os.path.isfile(filepath) and is_public_path(os.path.relpath(filepath, BASE)):
            return self._serve_file(filepath)
        return self._send(404, "Not found", "text/plain")

    def do_HEAD(self):
        """HEAD 请求:只返回响应头、不返回正文。

        链接预览(微信/短信/iMessage 抓标题)、爬虫、健康检查常用 HEAD。
        Python 默认不实现 do_HEAD,会返回 501,这里补上标准行为。
        """
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

        def head_only(code, ctype, length, cache=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if cache:
                self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(length))
            self.end_headers()

        if path == "/admin":
            if qs.get("key") != ADMIN_KEY:
                return head_only(401, "text/plain; charset=utf-8", 0)
            fp = os.path.join(BASE, "admin.html")
            return head_only(200, "text/html; charset=utf-8", os.path.getsize(fp), "no-cache")
        if path == "/api/submissions":
            if qs.get("key") != ADMIN_KEY:
                return head_only(401, "application/json; charset=utf-8", 0)
            body = json.dumps(load_submissions(), ensure_ascii=False)
            return head_only(200, "application/json; charset=utf-8", len(body.encode("utf-8")), "no-store")
        if path == "/api/data":
            body = json.dumps(load_data(), ensure_ascii=False)
            return head_only(200, "application/json; charset=utf-8", len(body.encode("utf-8")), "no-store")

        filepath = os.path.normpath(os.path.join(BASE, path.lstrip("/")))
        if os.path.isfile(filepath) and is_public_path(os.path.relpath(filepath, BASE)):
            ext = os.path.splitext(filepath)[1].lower()
            if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico"):
                cache = "public, max-age=604800"
            else:
                cache = "no-cache"
            return head_only(200, MIME.get(ext, "application/octet-stream"), os.path.getsize(filepath), cache)
        return head_only(404, "text/plain", 0)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/api/purchases":
            # 保存采购记录(需 key)。支持三种入参:
            #   新批量 {"upsert": {id: {...}}, "remove": [id, ...], "subs": {提交id: {...}}}
            #   旧批量 {"items": {id: {...}}}
            #   旧单条 {"k": "...", "checked": true, "actual": 45}
            if qs.get("key") != ADMIN_KEY:
                return self._send(401, json.dumps({"ok": False, "error": "unauthorized"}, ensure_ascii=False))
            try:
                data = json.loads(self._read_body().decode("utf-8"))
                if not isinstance(data, dict):
                    return self._send(400, json.dumps({"ok": False, "error": "bad payload"}, ensure_ascii=False))
                upsert, remove, subs = {}, [], {}
                if isinstance(data.get("items"), dict):
                    upsert.update(data["items"])
                if isinstance(data.get("upsert"), dict):
                    upsert.update(data["upsert"])
                if isinstance(data.get("remove"), list):
                    remove.extend(str(x) for x in data["remove"])
                if isinstance(data.get("subs"), dict):
                    subs.update(data["subs"])
                if data.get("k"):                       # 旧单条格式
                    k = str(data["k"])
                    upsert[k] = {"checked": data.get("checked", False), "actual": data.get("actual")}
                if not upsert and not remove and not subs:
                    return self._send(400, json.dumps({"ok": False, "error": "nothing to save"}, ensure_ascii=False))
                cur = save_purchases(upsert, remove, subs)
                return self._send(200, json.dumps({"ok": True, "count": len(cur["items"])}, ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
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

class SSLThreadingHTTPServer(ThreadingHTTPServer):
    """自带 HTTPS 的服务端:把 TLS 握手放到子线程里做,并加超时。

    为什么必须这样:如果在 accept 主循环里做 TLS 握手,任何"连上就不说话"
    的客户端(扫描器、中途断网的手机)都会把主线程永久卡死,导致整个 443
    入口不再接受任何新连接。这是线上真实发生过的故障,所以握手必须隔离+限时。
    """

    ssl_context = None
    handshake_timeout = 15

    def get_request(self):
        sock, addr = self.socket.accept()
        sock.settimeout(self.handshake_timeout)
        return sock, addr

    def process_request(self, request, client_address):
        try:
            request = self.ssl_context.wrap_socket(request, server_side=True)
        except Exception:
            try:
                request.close()
            except Exception:
                pass
            return
        super().process_request(request, client_address)


def main():
    port = int(os.environ.get("PORT", 8000))

    # 可选 HTTPS:设置 SSL=1 且提供 SSL_CERT / SSL_KEY 时启用
    ssl_on = os.environ.get("SSL") == "1"
    cert = os.environ.get("SSL_CERT")
    key = os.environ.get("SSL_KEY")
    if ssl_on and cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server = SSLThreadingHTTPServer(("0.0.0.0", port), Handler)
        server.ssl_context = ctx
        proto = "https"
    else:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        proto = "http"

    print("=" * 50)
    print("羽栖 · 可编辑报价器 已启动")
    if DEMO:
        print("⚠ 演示模式:数据取自 demo/(样例数据,可随意点着玩)")
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
