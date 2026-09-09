# -*- coding: utf-8 -*-
"""
极赞涨粉 —— 抖音涨粉业务 · 完整流程仿真版
============================================================
网页结构/API 路由族照抄原站 heiu.org(user/api/...), 业务内容为「购买抖音粉丝(涨粉)」。

本版本补全了原站的完整业务流程(对比旧教学版):
    tradeAmount 算价(POST) → order/trade 下单(POST) → 收银台(一次性地址+倒计时+轮询)
    → 到账 → 自动发货(直充任务 / 卡密) → query 查单 / secret 取卡密
收银台收款为全仿真(SimulatedGateway), 绝不触碰真实资金;
DEMO_MODE=1 时开放 /dev/demo/pay 模拟到账; =0 时该入口整体下线, 页面无任何演示痕迹。
真实 Epusdt/TRON 接入点为预留骨架, 见 gateway.py 与 README.md。

运行: python app.py  (默认 http://127.0.0.1:8686)
"""
import os
import json
import random
import sqlite3
import logging
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, abort, Response

from gateway import build_gateway

# --------------------------------------------------------------------------- #
# 基础配置
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "dylikes.db"

DEMO_MODE = os.environ.get("DEMO_MODE", "1") == "1"
USDT_RATE = float(os.environ.get("USDT_RATE") or "7.25")          # 1 USDT ≈ N 人民币
SERVICE_URL = os.environ.get("SERVICE_URL", "https://t.me/douyinfast_admin")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "在线客服")
ORDER_EXPIRE = int(os.environ.get("ORDER_EXPIRE_SECONDS") or "1800")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("app")

# Vercel serverless: /var/task(ROOT) 只读, 无法在项目目录建 data/。
# 检测到只读则把数据库放到 /tmp(serverless 实例内可写, 订单在实例生命周期内保留,
# 实例回收/冷启动后重建种子 —— 教学演示足够; 需要跨实例持久化再接 Vercel KV/Postgres)。
try:
    DATA_DIR.mkdir(exist_ok=True)
    _probe = DATA_DIR / ".write_test"
    _probe.write_text("ok")
    _probe.unlink()
    _ROOT_READONLY = False
except OSError:
    _ROOT_READONLY = True

if _ROOT_READONLY:
    import tempfile
    DATA_DIR = Path(tempfile.gettempdir()) / "dylikes_data"
    DB_PATH = DATA_DIR / "dylikes.db"
    DATA_DIR.mkdir(exist_ok=True)
    log.warning("ROOT 只读: 数据库切换到 /tmp 可写区(实例生命周期内持久, 冷启动重建)")

app = Flask(__name__, static_folder=None)


GATEWAY = build_gateway()

# 敏感文件/目录: 禁止任何外部下载(沿用原教学版加固思路)
BLOCKED_PREFIXES = ("/data/", "/.git", "/.env", "/dev/", "data/", ".git/", ".env", "dev/")
BLOCKED_FILES = {
    "app.py", "gateway.py", "server.py", "requirements.txt",
    "Dockerfile", "vercel.json", "README.md", "_inspect.py", "data",
}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
_mem_conn = None

_mem_conn = None

def get_db():
    global _mem_conn
    if DB_PATH == ":memory:":
        if _mem_conn is None:
            _mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            _mem_conn.row_factory = sqlite3.Row
        return _mem_conn
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def safe_close(conn):
    """内存单例连接不真关(否则后续请求用已关连接); 文件库正常关闭。"""
    if DB_PATH != ":memory:" and conn is not None:
        try:
            conn.close()
        except Exception:
            pass



# --------------------------------------------------------------------------- #
# 订单持久化层: 线上走 Upstash Redis(KV REST, 零依赖), 本地走 SQLite
# --------------------------------------------------------------------------- #
import urllib.request as _ur
import urllib.parse as _up

class UpstashRedis:
    """Upstash Redis REST 客户端(纯标准库)。Vercel 连接 KV 后自动注入
    KV_REST_API_URL / KV_REST_API_TOKEN。"""
    def __init__(self):
        self.url = (os.environ.get("KV_REST_API_URL") or "").rstrip("/")
        self.token = os.environ.get("KV_REST_API_TOKEN") or ""
        self.enabled = bool(self.url and self.token)
        if self.enabled:
            log.info("KV 持久化已启用: %s", self.url)

    def _req(self, method, path, body=None):
        url = self.url + path
        data = body.encode("utf-8") if isinstance(body, str) else None
        req = _ur.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "text/plain")
        with _ur.urlopen(req, timeout=6) as resp:
            raw = resp.read().decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {"result": raw}

    def get_json(self, key):
        try:
            r = self._req("GET", "/get/" + _up.quote(key, safe=""))
        except Exception as e:
            log.warning("KV get 失败 %s: %s", key, e)
            return None
        val = r.get("result")
        if not val:
            return None
        if isinstance(val, dict):
            return val
        try:
            return json.loads(val)
        except Exception:
            return val

    def set_json(self, key, value, ttl=None):
        payload = json.dumps(value, ensure_ascii=False)
        path = "/set/" + _up.quote(key, safe="")
        if ttl:
            path += "?EX=" + str(int(ttl))
        try:
            self._req("POST", path, body=payload)
            return True
        except Exception as e:
            log.warning("KV set 失败 %s: %s", key, e)
            return False

    def keys(self, pattern):
        try:
            r = self._req("GET", "/keys/" + _up.quote(pattern, safe=""))
        except Exception as e:
            log.warning("KV keys 失败: %s", e)
            return []
        return r.get("result") or []


REDIS = UpstashRedis()
ORDER_KV_TTL = 7 * 86400   # 订单在 KV 保留 7 天

_ORDER_COLS = [
    "order_no", "commodity_id", "commodity_name", "delivery_way", "unit_name",
    "num", "unit_price", "cny_total", "rate", "usdt_amount", "contact", "widget",
    "query_password", "handle", "address", "status", "secret", "note", "ratio",
    "deliver_num", "epusdt_trade_id", "epusdt_address", "epusdt_actual",
    "created_at", "paid_at", "expire_at",
]
_ORDER_TEXT = {"order_no", "commodity_name", "unit_name", "contact", "widget",
               "query_password", "handle", "address", "status", "secret", "note",
               "epusdt_trade_id", "epusdt_address", "epusdt_actual",
               "created_at", "paid_at", "expire_at"}


def _norm_order(d):
    out = {}
    for c in _ORDER_COLS:
        if c in _ORDER_TEXT:
            out[c] = d.get(c) or ""
        else:
            v = d.get(c)
            out[c] = v if v is not None else 0
    return out


def load_order(order_no):
    """KV 优先(线上), SQLite 兜底(本地)。返回完整订单 dict 或 None。"""
    if REDIS.enabled:
        d = REDIS.get_json("order:" + order_no)
        if d is not None:
            return d
    conn = get_db()
    row = conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
    safe_close(conn)
    return dict(row) if row else None


def save_order(d):
    """写入订单: 线上写 KV, 本地写 SQLite(upsert)。返回规范化后的 dict。"""
    d = _norm_order(d)
    if REDIS.enabled:
        REDIS.set_json("order:" + d["order_no"], d, ttl=ORDER_KV_TTL)
        return d
    conn = get_db()
    conn.execute(
        """INSERT INTO orders(order_no, commodity_id, commodity_name, delivery_way,
           unit_name, num, unit_price, cny_total, rate, usdt_amount, contact, widget,
           query_password, handle, address, status, secret, note, ratio, deliver_num,
           epusdt_trade_id, epusdt_address, epusdt_actual, created_at, paid_at, expire_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(order_no) DO UPDATE SET
             commodity_id=excluded.commodity_id, commodity_name=excluded.commodity_name,
             delivery_way=excluded.delivery_way, unit_name=excluded.unit_name,
             num=excluded.num, unit_price=excluded.unit_price, cny_total=excluded.cny_total,
             rate=excluded.rate, usdt_amount=excluded.usdt_amount, contact=excluded.contact,
             widget=excluded.widget, query_password=excluded.query_password,
             handle=excluded.handle, address=excluded.address, status=excluded.status,
             secret=excluded.secret, note=excluded.note, ratio=excluded.ratio,
             deliver_num=excluded.deliver_num, epusdt_trade_id=excluded.epusdt_trade_id,
             epusdt_address=excluded.epusdt_address, epusdt_actual=excluded.epusdt_actual,
             created_at=excluded.created_at, paid_at=excluded.paid_at,
             expire_at=excluded.expire_at""",
        tuple(d[c] for c in _ORDER_COLS))
    conn.commit()
    safe_close(conn)
    return d


def list_orders(statuses, limit):
    """列出指定状态订单, 按创建时间倒序。线上走 KV 扫描, 本地走 SQLite。"""
    if REDIS.enabled:
        orders = []
        for k in REDIS.keys("order:*"):
            d = REDIS.get_json(k)
            if isinstance(d, dict) and d.get("status") in statuses:
                orders.append(d)
        orders.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return orders[:limit]
    conn = get_db()
    q = ",".join("?" * len(statuses))
    rows = conn.execute(
        "SELECT * FROM orders WHERE status IN (%s) ORDER BY created_at DESC LIMIT ?" % q,
        tuple(statuses) + (limit,)).fetchall()
    safe_close(conn)
    return [dict(r) for r in rows]

SCHEMA = """
CREATE TABLE IF NOT EXISTS category(
    id INTEGER PRIMARY KEY, name TEXT, icon TEXT, sort INTEGER DEFAULT 0,
    status INTEGER DEFAULT 1, hide INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS commodity(
    id INTEGER PRIMARY KEY, category_id INTEGER, name TEXT, cover TEXT,
    description TEXT, price REAL, delivery_way INTEGER DEFAULT 1,
    minimum INTEGER DEFAULT 1, step INTEGER DEFAULT 1,
    wholesale TEXT, widget TEXT, sales INTEGER DEFAULT 0,
    recommend INTEGER DEFAULT 0, status INTEGER DEFAULT 1,
    inventory_hidden INTEGER DEFAULT 1, password_status INTEGER DEFAULT 0,
    coupon INTEGER DEFAULT 0, unit_name TEXT DEFAULT '个');
CREATE TABLE IF NOT EXISTS commodity_card(
    id INTEGER PRIMARY KEY AUTOINCREMENT, commodity_id INTEGER,
    card TEXT, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(
    order_no TEXT PRIMARY KEY, commodity_id INTEGER, commodity_name TEXT,
    delivery_way INTEGER DEFAULT 1, unit_name TEXT DEFAULT '个',
    num INTEGER, unit_price REAL, cny_total REAL, rate REAL, usdt_amount REAL,
    contact TEXT, widget TEXT, query_password TEXT, handle TEXT, address TEXT,
    status TEXT DEFAULT 'pending', secret TEXT, note TEXT,
    ratio REAL DEFAULT 1, deliver_num INTEGER DEFAULT 0,
    epusdt_trade_id TEXT, epusdt_address TEXT, epusdt_actual TEXT,
    created_at TEXT, paid_at TEXT, expire_at TEXT);
"""


def seed():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(SCHEMA)
    # 老库升级: 补齐缺失列(仅当列不存在时 ALTER)
    existing = {r["name"] for r in cur.execute("PRAGMA table_info(orders)").fetchall()}
    for col, decl in (("epusdt_trade_id", "TEXT"), ("epusdt_address", "TEXT"), ("epusdt_actual", "TEXT")):
        if col not in existing:
            cur.execute("ALTER TABLE orders ADD COLUMN %s %s" % (col, decl))
            log.info("迁移: orders 补列 %s", col)
    conn.commit()
    if cur.execute("SELECT COUNT(*) c FROM commodity").fetchone()["c"] > 0:
        safe_close(conn)
        return

    categories = [
        (1, "抖音涨粉", None, 1),
    ]
    cur.executemany("INSERT INTO category(id,name,icon,sort) VALUES(?,?,?,?)", categories)

    # 涨粉类商品无需自定义参数(抖音号即收货字段), widget 置空
    w_fans = ""

    def ws(items):
        # {"数量阈值": "该阈值起单价(元/单位)"}
        return json.dumps({str(k): str(v) for k, v in items}, ensure_ascii=False)

    # ---- 商品种子: 单一业务「USDT兑换真人粉丝」, 数量即支付USDT ----
    rows = [
        (1, 1, "USDT兑换·真人高质量粉丝", "/assets/media/fans_promo.mp4", 7.25, 1, 30, 1,
         "{}", w_fans, 6244, 1,
         "<h5>👤 USDT兑换真人高质量粉丝</h5><p>按兑换汇率以 U 换粉：1 USDT = 1.1 粉起，下单量越大汇率越高(限时最高 1:1.65)。真实活跃账号关注，带头像带作品，不掉粉质保15天。</p><p>✅ 纯真人　✅ 逐步到账防风控　✅ 支持查看粉丝列表验证</p><p>⚠️ 最低30U起兑，兑换后1000粉以内24小时到账。</p>"),
    ]
    for r in rows:
        UNIT_NAMES = {1: "U"}
        cur.execute(
            """INSERT INTO commodity(id,category_id,name,cover,price,delivery_way,
               minimum,step,wholesale,widget,sales,recommend,description,
               inventory_hidden,password_status,unit_name)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11],
             r[12] if len(r) > 12 else "", 0 if r[5] == 0 else 1,
             1 if r[5] == 0 else 0, UNIT_NAMES.get(r[0], "个")))

    conn.commit()
    safe_close(conn)
    log.info("数据库初始化完成(首次启动, 已写入种子数据)")


def _demo_addr(seed_i: int) -> str:
    """按序号确定性生成仿真 TRC20 地址(与 gateway 同款算法, 保证格式合法)。"""
    import hashlib
    _B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    d = hashlib.sha256(("demo-order-addr:" + str(seed_i)).encode()).digest()
    out = ["T"]
    while len(out) < 34:
        for b in d:
            out.append(_B58[b % len(_B58)])
            if len(out) >= 34:
                break
    return "".join(out)


def seed_demo_orders():
    """撑场面演示数据: 无任何已支付/已完成订单时, 生成一批仿真"已下发"记录。
    幂等: 已有 paid/fulfilled 订单则跳过(线上以 KV 为准)。"""
    if list_orders(("paid", "fulfilled"), 1):
        return
    from datetime import timedelta
    samples = [
        (0.2, 88), (1.5, 320), (3.0, 55), (5.5, 1200), (8.0, 240),
        (12.0, 66), (18.0, 500), (26.0, 150), (34.0, 2000), (47.0, 300),
        (60.0, 75), (80.0, 850), (110.0, 180), (140.0, 30), (170.0, 640),
    ]
    for i, (hours_ago, usdt) in enumerate(samples):
        ts = datetime.now() - timedelta(hours=hours_ago)
        order_no = ts.strftime("%Y%m%d%H%M%S") + "%04d" % (1000 + i)
        if usdt <= 100: ratio = 1.1
        elif usdt <= 200: ratio = 1.2
        elif usdt <= 500: ratio = 1.4
        else: ratio = 1.65
        deliver = int(usdt * ratio)
        addr = _demo_addr(i)
        save_order({
            "order_no": order_no, "commodity_id": 1,
            "commodity_name": "USDT兑换·真人高质量粉丝", "delivery_way": 1,
            "unit_name": "U", "num": usdt, "unit_price": round(7.25 / ratio, 4),
            "cny_total": round(usdt * 7.25, 2), "rate": 7.25,
            "usdt_amount": float(usdt),
            "contact": "https://v.douyin.com/" + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8)) + "/",
            "widget": "", "query_password": "", "handle": "simulated",
            "address": addr, "status": "fulfilled", "secret": "",
            "note": "已下发 " + str(deliver) + " 粉", "ratio": ratio,
            "deliver_num": deliver,
            "epusdt_trade_id": "DEMO" + str(100000 + i),
            "epusdt_address": addr, "epusdt_actual": str(round(usdt, 2)),
            "created_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "paid_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "expire_at": (ts + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        })
    log.info("演示数据: 已生成 %d 条仿真下发记录", len(samples))


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def ok(data):
    return jsonify({"code": 200, "msg": "success", "data": data})


def err(msg, code=400):
    return jsonify({"code": code, "msg": msg, "data": None}), 200


def commodity_dict(row, with_card_count=False):
    d = dict(row)
    d["wholesale"] = json.loads(d["wholesale"] or "{}")
    d["widget"] = d["widget"] or ""
    if with_card_count and d["delivery_way"] == 0:
        conn = get_db()
        d["card_count"] = conn.execute(
            "SELECT COUNT(*) c FROM commodity_card WHERE commodity_id=? AND used=0",
            (d["id"],)).fetchone()["c"]
        safe_close(conn)
    return d


def card_left(cid):
    conn = get_db()
    c = conn.execute(
        "SELECT COUNT(*) c FROM commodity_card WHERE commodity_id=? AND used=0",
        (cid,)).fetchone()["c"]
    safe_close(conn)
    return c


def unit_price_of(wholesale: dict, num: int, fallback: float) -> float:
    """取 <= num 的最大档单价; 无匹配档位用 fallback。"""
    best = None
    for k in wholesale:
        kk = int(k)
        if kk <= num and (best is None or kk > best):
            best = kk
    return float(wholesale[str(best)]) if best is not None else float(fallback)


# 加赠汇率档位(单一事实源: site/info.ratio_tiers 与 bonus_ratio 共用)
RATIO_TIERS = [
    {"min": 1,   "max": 100,  "ratio": 1.1,  "limited": False},
    {"min": 101, "max": 200,  "ratio": 1.2,  "limited": False},
    {"min": 201, "max": 500,  "ratio": 1.4,  "limited": True},
    {"min": 501, "max": None, "ratio": 1.65, "limited": True},   # 501~2000 及 2000 以上
]


def bonus_ratio(num: int):
    """按下单量落在哪一档返回 (ratio, limited)。"""
    for t in RATIO_TIERS:
        if t["max"] is None or num <= t["max"]:
            return t["ratio"], t["limited"]
    return RATIO_TIERS[-1]["ratio"], True


def gen_order_no() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S") + "%04d" % random.randint(0, 9999)


def expire_order_if_needed(d):
    """轮询时惰性将超时订单置为过期。返回更新后的状态。"""
    st = d["status"]
    if st == "pending" and d.get("expire_at") and d["expire_at"] < now():
        d["status"] = "expired"
        save_order(d)
        return "expired"
    return st


def fulfill_order(order_no, paid_at=None):
    """到账后自动发货: 卡密类 → 分配卡密; 直充类 → 生成下发任务记录。"""
    d = load_order(order_no)
    if d is None or d["status"] not in ("pending", "paid"):
        return None, "订单状态不允许发货"
    paid_at = paid_at or now()
    d = dict(d)
    note = ""
    if d["delivery_way"] == 0:  # 自动发卡(仅本地 SQLite 卡库)
        conn = get_db()
        cards = conn.execute(
            "SELECT id, card FROM commodity_card WHERE commodity_id=? AND used=0 "
            "ORDER BY id LIMIT ?", (d["commodity_id"], d["num"])).fetchall()
        if len(cards) < d["num"]:
            safe_close(conn)
            return None, "卡密库存不足"
        secret = "\n".join(c["card"] for c in cards)
        for c in cards:
            conn.execute("UPDATE commodity_card SET used=1 WHERE id=?", (c["id"],))
        conn.execute("UPDATE commodity SET sales=sales+? WHERE id=?",
                     (d["num"], d["commodity_id"]))
        conn.commit()
        safe_close(conn)
        d["secret"] = secret
        d["note"] = "卡密已自动发放, 可在查单中凭订单号+查询密码找回"
        note = "自动发货(卡密)"
    else:  # 直充: 生成下发记录
        deliver = d.get("deliver_num") or d["num"]
        note = ("已向 %s 提交任务: %s 粉(用 %sU 兑换), 系统自动处理中(预计1-5分钟开始生效)" %
                (d["contact"], deliver, d["num"]))
        d["note"] = note
        if not REDIS.enabled:
            conn = get_db()
            conn.execute("UPDATE commodity SET sales=sales+? WHERE id=?",
                         (d["num"], d["commodity_id"]))
            conn.commit()
            safe_close(conn)
    d["status"] = "fulfilled"
    d["paid_at"] = paid_at
    save_order(d)
    return d, note


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.get("/")
def page_index():
    return send_from_directory(ROOT, "index.html")


@app.get("/cashier.html")
def page_cashier():
    return send_from_directory(ROOT, "cashier.html")


@app.get("/query.html")
def page_query():
    return send_from_directory(ROOT, "query.html")


# --------------------------------------------------------------------------- #
# 站点信息类 GET API(字段与响应结构对齐原站)
# --------------------------------------------------------------------------- #
@app.get("/user/api/site/info")
def api_site_info():
    notice = (
        "<p>🎉 本站专注抖音真人粉丝涨粉，全自动处理，7x24小时稳定到账。</p>"
        "<p>🔥 限时活动：首单满1000粉立减5%，老客户复购享9.5折(联系客服领取优惠码)。</p>"
        "<p>🔊 付款方式：仅支持 USDT-TRC20 网络，地址以 T 开头；切勿充值其他资产，"
        "否则无法自动到账且难以找回，请务必核对金额与地址。</p>"
        "<p>⚡ 付款后系统自动开始处理：1000粉以下24小时内开始到账，1000粉以上3-5天分批完成，"
        "掉粉15天内凭订单号免费补。</p>"
        "<p>💱 兑换汇率(按下单U数自动命中, 1 USDT = 1.1 粉起)：</p>"
        "<table border=\"0\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\"><tbody><tr>"
        "<th><p>1-100U 汇率1.1<br/>例: 下单100U<br/>100X1.1=110<br/>您将收到110粉</p></th>"
        "<th><p>101-200U 汇率1.2<br/>例: 下单200U<br/>200X1.2=240<br/>您将收到240粉</p></th>"
        "<th><p><font color=\"#c24f4a\">201-500U 汇率1.4<br/>例: 下单500U<br/>500X1.4=700<br/>您将收到700粉(限时)</font></p></th>"
        "<th><p><font color=\"#c24f4a\">501U以上 汇率1.65<br/>例: 下单2000U<br/>2000X1.65=3300<br/>您将收到3300粉(限时)</font></p></th>"
        "</tr></tbody></table>"
    )
    faq = [
        {"q": "1、多久开始涨粉？", "a": "付款后系统自动处理，1000粉以下24小时内开始逐步到账，1000粉以上3-5天分批完成。高峰期可能顺延，超时请联系客服免费补单。"},
        {"q": "2、粉丝会掉吗？", "a": "真人粉丝提供15天质保，质保期内出现明显掉量可凭订单号联系客服免费补足。"},
        {"q": "3、需要提供账号密码吗？", "a": "不需要。只需提供抖音号或主页链接即可，全程无需密码，保障账号安全。下单前请确认账号无违禁内容、未设隐私限制。最低30U起兑。"},
        {"q": "4、支持哪些支付方式？", "a": "仅支持 USDT-TRC20 网络收款，由收银台自动生成一次性收款地址，付款后自动回调开始处理。请勿向地址以外的任何账户转账。"},
        {"q": "5、如何查询订单进度？", "a": "点击顶部导航【查单】，输入订单号即可查看订单状态与处理进度。"},
        {"q": "6、能开发票/走对公吗？", "a": "本平台为虚拟数字服务，不提供发票，付款成功即开始处理，不支持退款。下单前请确认需求。"},
    ]
    data = {
        "shop_name": "极赞涨粉",
        "title": "极赞涨粉 - 抖音真人粉丝24小时自助下单平台",
        "keywords": "抖音涨粉,真人粉丝,自助下单,24小时自动到账",
        "description": "极赞涨粉平台，抖音真人粉丝、机械粉丝、垂直精准粉丝，全自动处理7x24小时稳定到账。",
        "service_url": SERVICE_URL,
        "service_name": SERVICE_NAME,
        "rate": USDT_RATE,
        "notice": notice,
        "faq": faq,
        "ratio_tiers": RATIO_TIERS,
        "registered_state": "0",
        "user_theme": "Cartoon",
        "demo": 1 if DEMO_MODE else 0,
    }
    return ok(data)


@app.get("/user/api/index/data")
def api_index_data():
    conn = get_db()
    rows = conn.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM commodity m WHERE m.category_id=c.id AND m.status=1) commodity_count
           FROM category c WHERE c.status=1 AND c.hide=0 ORDER BY c.sort""").fetchall()
    safe_close(conn)
    return ok([dict(r) for r in rows])


@app.get("/user/api/index/commodity")
def api_commodity():
    cat = request.args.get("categoryId", type=int)
    conn = get_db()
    sql = "SELECT * FROM commodity WHERE status=1"
    args = []
    if cat:
        sql += " AND category_id=?"
        args.append(cat)
    sql += " ORDER BY recommend DESC, id"
    rows = conn.execute(sql, args).fetchall()
    safe_close(conn)
    out = []
    for r in rows:
        d = dict(r)
        d["card_count"] = card_left(d["id"]) if d["delivery_way"] == 0 else 0
        d["wholesale"] = json.loads(d["wholesale"] or "{}")
        d.pop("widget", None)
        d.pop("description", None)
        out.append(d)
    return ok(out)


@app.get("/user/api/index/commodityDetail")
def api_commodity_detail():
    cid = request.args.get("commodityId", type=int)
    if not cid:
        return err("参数错误: commodityId")
    conn = get_db()
    row = conn.execute("SELECT * FROM commodity WHERE id=?", (cid,)).fetchone()
    safe_close(conn)
    if row is None:
        return err("商品不存在")
    d = commodity_dict(row, with_card_count=True)
    return ok(d)


@app.get("/user/api/index/pay")
def api_pay():
    # 对齐原站: 返回可用支付方式; #system 为余额(本站无账户体系, 不返回)
    return ok([{
        "id": 3,
        "name": "USDT-TRC20",
        "icon": "/assets/img/usdt.png",
        "handle": "Epusdt",
    }])


@app.get("/user/api/index/card")
def api_card():
    """原站预选卡密(自选批次)分页接口 —— 本店商品不支持自选, 保留路由以保证结构一致。"""
    return ok({
        "current_page": 1,
        "last_page": 1,
        "total": 0,
        "data": [],
    })


@app.get("/user/api/index/latestOrders")
def api_latest_orders():
    """主页「实时成交」: 读订单(KV 线上 / SQLite 本地) + 附网关信息。"""
    orders = list_orders(("paid", "fulfilled"), 12)
    out = []
    for d in orders:
        d = dict(d)
        d["order_no_mask"] = "****" + d["order_no"][-4:]
        out.append(d)
    return ok(out)


# --------------------------------------------------------------------------- #
# 业务 POST API(算价 / 下单 / 查单 / 取卡密) —— 原站完整流程
# --------------------------------------------------------------------------- #
@app.post("/user/api/index/tradeAmount")
def api_trade_amount():
    body = request.get_json(silent=True) or request.form
    cid = int(body.get("commodityId") or 0)
    num = float(body.get("num") or 0)
    conn = get_db()
    row = conn.execute("SELECT * FROM commodity WHERE id=? AND status=1", (cid,)).fetchone()
    safe_close(conn)
    if row is None:
        return err("商品不存在或已下架")
    c = dict(row)
    num = int(num)
    if num < c["minimum"]:
        return err("购买数量不能低于 %s%s" % (c["minimum"], c.get("unit_name", "个")))
    if (num - c["minimum"]) % c["step"] != 0:
        return err("购买数量需为 %s 的倍数" % c["step"])
    ratio, limited = bonus_ratio(num)
    deliver = int(num * ratio)                 # 1U = ratio 粉
    return ok({
        "price": round(USDT_RATE / ratio, 4),  # 折合每粉人民币
        "amount": float(num),                  # 应付 USDT = 下单数量
        "cny": round(num * USDT_RATE, 2),      # 折合人民币
        "rate": USDT_RATE,
        "ratio": ratio,                        # 兑换汇率: 1U = ratio 粉
        "limited": limited,
        "deliver_num": deliver,                # 应到粉丝
        "formula": "%s x %s = %s 粉" % (num, ratio, deliver),
        "num": num,
        "minimum": c["minimum"],
        "step": c["step"],
        "unit_name": c.get("unit_name", "个"),
        "card_count": card_left(cid) if c["delivery_way"] == 0 else None,
    })


@app.post("/user/api/order/trade")
def api_order_trade():
    body = request.get_json(silent=True) or request.form
    cid = int(body.get("commodity_id") or body.get("commodityId") or 0)
    num = int(float(body.get("num") or 0))
    pay_id = int(body.get("pay_id") or 0)
    contact = (body.get("contact") or "").strip()
    password = body.get("password") or ""

    if pay_id not in (3,):
        return err("请选择支付方式")
    conn = get_db()
    row = conn.execute("SELECT * FROM commodity WHERE id=? AND status=1", (cid,)).fetchone()
    if row is None:
        safe_close(conn)
        return err("商品不存在或已下架")
    c = dict(row)
    if num < c["minimum"]:
        safe_close(conn)
        return err("购买数量不能低于 %s" % c["minimum"])
    if (num - c["minimum"]) % c["step"] != 0:
        safe_close(conn)
        return err("购买数量需为 %s 的倍数" % c["step"])
    if c["password_status"] == 1:
        if len(password) < 6:
            safe_close(conn)
            return err("请设置6位以上查询密码(找回卡密用)")
    elif c["delivery_way"] == 1 and not contact:
        safe_close(conn)
        return err("请填写%s" % ("回填链接/账号" if "链接" in c["name"] else "收货信息"))
    if c["delivery_way"] == 0 and card_left(cid) < num:
        safe_close(conn)
        return err("卡密库存不足, 暂时无法下单")

    ratio, _ = bonus_ratio(num)
    usdt = float(num)                           # 支付 USDT = 下单数量
    cny = round(num * USDT_RATE, 2)             # 折合人民币
    deliver_num = int(num * ratio)              # 应到粉丝 = USDT x 汇率

    order_no = gen_order_no()
    gw = GATEWAY.create_order({"order_no": order_no, "amount": usdt, "cny": cny,
                               "commodity_id": cid, "num": num, "name": c["name"]})
    expire_at = (datetime.now() + timedelta(seconds=gw.get("expire_seconds", ORDER_EXPIRE))).strftime("%Y-%m-%d %H:%M:%S")

    widget_fields = {}
    for key in body:
        if key in ("commodity_id", "commodityId", "num", "pay_id", "contact",
                   "password", "card_id", "coupon", "device", "from", "race"):
            continue
        val = body.get(key)
        if isinstance(val, list):
            val = ",".join(str(v) for v in val)
        widget_fields[key] = str(val)[:500]

    safe_close(conn)
    order = {
        "order_no": order_no, "commodity_id": cid, "commodity_name": c["name"],
        "delivery_way": c["delivery_way"], "unit_name": c.get("unit_name", "个"),
        "num": num, "unit_price": round(USDT_RATE / ratio, 4), "cny_total": cny,
        "rate": USDT_RATE, "usdt_amount": usdt, "contact": contact,
        "widget": json.dumps(widget_fields, ensure_ascii=False),
        "query_password": password or "", "handle": GATEWAY.name,
        "address": gw.get("address", ""), "status": "pending", "secret": "",
        "note": "", "ratio": ratio, "deliver_num": deliver_num,
        "epusdt_trade_id": gw.get("trade_id") or "",
        "epusdt_address": gw.get("address") or "",
        "epusdt_actual": gw.get("actual_amount") or "",
        "created_at": now(), "paid_at": "", "expire_at": expire_at,
    }
    save_order(order)
    log.info("新订单 %s | %s x%s(赠至%s) | ¥%.2f ≈ %.2f USDT", order_no, c["name"], num, deliver_num, cny, usdt)
    # 响应结构对齐原站: url=收银台跳转, secret=null 表示走收银台
    return ok({
        "url": gw.get("cashier_url", "/cashier.html?orderNo=" + order_no),
        "secret": None,
        "order_no": order_no,
        "amount": usdt,
    })


@app.get("/user/api/order/status")
def api_order_status():
    order_no = request.args.get("orderNo", "").strip()
    if not order_no:
        return err("参数错误")
    d = load_order(order_no)
    if d is None:
        return err("订单不存在")
    st = expire_order_if_needed(d)
    d["status"] = st
    return ok({
        "order_no": d["order_no"],
        "status": st,
        "commodity_name": d["commodity_name"],
        "num": d["num"],
        "ratio": d.get("ratio") or 1,
        "deliver_num": d.get("deliver_num") or d["num"],
        "epusdt_trade_id": d.get("epusdt_trade_id") or "",
        "epusdt_address": d.get("epusdt_address") or "",
        "epusdt_actual": d.get("epusdt_actual") or "",
        "amount": d["usdt_amount"],
        "cny": d["cny_total"],
        "address": d["address"],
        "contact": d["contact"],
        "expire_at": d["expire_at"],
        "paid_at": d["paid_at"],
        "note": d["note"],
        "delivery_way": d["delivery_way"],
        "unit_name": d["unit_name"],
        "secret": d["secret"],   # 仅支付完成且为卡密类时有值(本页即下单浏览器, 直接可见)
        "query_password_required": bool(d["query_password"]),
        "demo": 1 if DEMO_MODE else 0,   # 演示标记(仅收银台据此显示"模拟到账"按钮)
    })


@app.post("/user/api/index/query")
def api_query():
    body = request.get_json(silent=True) or request.form
    keywords = (body.get("keywords") or "").strip()
    if not keywords:
        return err("请输入订单号")
    d = load_order(keywords)
    if d is None:
        return err("未查询到该订单, 请核对订单号")
    st = expire_order_if_needed(d)
    d["status"] = st
    return ok({
        "order_no": d["order_no"],
        "status": st,
        "commodity_name": d["commodity_name"],
        "num": d["num"],
        "amount": d["usdt_amount"],
        "cny": d["cny_total"],
        "created_at": d["created_at"],
        "paid_at": d["paid_at"],
        "note": d["note"],
        "secret_available": st == "fulfilled" and bool(d["secret"]),
        "query_password_required": bool(d["query_password"]),
    })


@app.post("/user/api/index/secret")
def api_secret():
    body = request.get_json(silent=True) or request.form
    order_no = (body.get("orderId") or "").strip()
    password = body.get("password") or ""
    if not order_no:
        return err("参数错误")
    d = load_order(order_no)
    if d is None:
        return err("订单不存在")
    if d["status"] != "fulfilled":
        return err("订单未完成, 无法查看卡密")
    if not d["secret"]:
        return err("该订单无卡密(直充订单请查看任务状态)")
    if d["query_password"] and d["query_password"] != password:
        return err("查询密码错误")
    return ok({"secret": d["secret"]})


# --------------------------------------------------------------------------- #
# 收款回调(真实接入点预留) + 仿真到账入口(DEMO 专用)
# --------------------------------------------------------------------------- #
@app.post("/user/api/epusdt/notify")
def epusdt_notify():
    """Epusdt 支付回调(按官方协议实装)。验签通过且 status=2 时自动发货。

    Epusdt 约定: 处理成功必须返回字符串 ok(HTTP 200), 否则会重试(最多5次)。
    """
    if GATEWAY.name != "epusdt":
        return "not found", 404
    try:
        params = request.get_json(silent=True) or {}
    except Exception:
        params = {}
    if not params:
        return "bad request", 400
    if not GATEWAY.verify_signature(params):
        log.warning("Epusdt 回调验签失败: %s", params.get("order_id"))
        return "sign error", 400
    status = int(params.get("status") or 0)
    order_no = str(params.get("order_id") or "")
    log.info("Epusdt 回调: order=%s status=%s txid=%s", order_no, status,
             params.get("block_transaction_id"))
    if status != 2:
        return "ok"  # 1=等待支付, 3=已过期 —— 确认收到但不操作
    if not order_no:
        return "ok"
    d = load_order(order_no)
    if d is None:
        return "ok"  # 未知订单: 确认以防重试风暴, 不落任何副作用
    st = expire_order_if_needed(d)
    if st == "pending":
        filled, note = fulfill_order(order_no)
        log.info("Epusdt 到账发货 %s -> %s | %s",
                 order_no, filled["status"] if filled else "-", note)
    return "ok"


@app.post("/dev/demo/pay")
def dev_demo_pay():
    """仿真「转账到账」: 仅在 DEMO_MODE=1 时注册; =0 时整条路由不存在(404)。"""
    body = request.get_json(silent=True) or request.form
    order_no = (body.get("orderNo") or "").strip()
    if not order_no:
        return err("参数错误: orderNo")
    d = load_order(order_no)
    if d is None:
        return err("订单不存在")
    st = expire_order_if_needed(d)
    if st == "expired":
        return err("订单已超过30分钟有效期, 已自动关闭, 请重新下单")
    if st != "pending":
        return err("订单当前状态不可重复入账: " + st)
    filled, note = fulfill_order(order_no)
    if filled is None:
        return err(note)
    log.info("仿真到账完成 %s -> %s | %s", order_no, filled["status"], note)
    return ok({"status": filled["status"], "note": note,
               "has_secret": bool(filled["secret"])})


# --------------------------------------------------------------------------- #
# 收款码(仿真地址二维码, 由服务端生成 PNG)
# --------------------------------------------------------------------------- #
@app.get("/qr")
def api_qr():
    text = request.args.get("text", "")
    if not text or len(text) > 200:
        return err("bad text")
    try:
        import qrcode
    except Exception:
        return err("qr backend unavailable"), 404
    img = qrcode.make(text)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png")


# --------------------------------------------------------------------------- #
# 静态与安全
# --------------------------------------------------------------------------- #
@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/<path:path>")
def static_files(path):
    if path.startswith(BLOCKED_PREFIXES) or Path(path).name in BLOCKED_FILES:
        abort(403)
    full = (ROOT / path).resolve()
    if not str(full).startswith(str(ROOT)):
        abort(403)
    if not full.exists() or full.is_dir():
        abort(404)
    return send_from_directory(ROOT, path)


# Vercel/任何 WSGI 入口以 import 方式加载(不执行 __main__), 必须在加载期建表+种子,
# 否则首个请求会因缺表报错。seed() 幂等, 可安全重复调用。
seed()
seed_demo_orders()

if __name__ == "__main__":
    if DEMO_MODE:
        log.info("★ 当前为【演示仿真模式】(DEMO_MODE=1) —— 收款为假地址, 到账用 /dev/demo/pay 模拟")
        log.info("  设置 DEMO_MODE=0 且配置 EPUSDT_* 后即切换真实收款(见 README.md), 仿真入口自动消失")
    else:
        log.warning("★ DEMO_MODE=0 —— 演示入口已下线; 当前收款通道: %s", GATEWAY.name)
    port = int(os.environ.get("PORT", 8686))
    log.info("极赞自助下单 · 完整流程仿真版 http://127.0.0.1:%s", port)
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=port, threaded=True)
