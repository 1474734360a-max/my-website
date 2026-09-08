# -*- coding: utf-8 -*-
"""
PaymentGateway 抽象层 —— 收款通道可插拔设计。

结构对齐原站 heiu.org 的 Epusdt(USDT-TRC20) 收款方式：

    SimulatedGateway   : 全仿真。为每个订单生成"一次性"伪 TRC20 地址(确定性、可校验格式 T 开头)、
                         无任何链上能力；配合 app 的 /dev/demo/pay 完成状态流转。DEMO 模式默认使用。
    EpusdtAdapter      : 预留的真实接入点(骨架)。配置好 EPUSDT_* 环境变量后启用：
                         创建订单时调用 Epusdt API(v1/order/create)生成一次性地址与收银台链接，
                         真实回调走 /user/api/epusdt/notify (验签见 verify_signature)。

切换方式见 README.md「切换真实收款」一节。默认不启用任何真实收款能力。
"""
import os
import json
import time
import hashlib
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("gateway")

# TRC20 地址字符集(Base58, 去掉易混淆字符), 仅用于仿真地址生成
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _fake_trc20(seed_text: str) -> str:
    """由订单号确定性生成一个格式合法的伪 TRC20 地址(T 开头, 34 位)。"""
    digest = hashlib.sha256(("dylikes-fake-addr:" + seed_text).encode()).digest()
    out = ["T"]
    while len(out) < 34:
        for b in digest:
            out.append(_B58[b % len(_B58)])
            if len(out) >= 34:
                break
    return "".join(out)


class SimulatedGateway:
    """全仿真通道：只生成假地址，不触网、不收真钱。"""

    name = "simulated"
    deliver_on_pay = True  # 到账即自动发货(仿真)

    def create_order(self, order: dict) -> dict:
        address = _fake_trc20(order["order_no"])
        return {
            "address": address,
            "expire_seconds": int(os.environ.get("ORDER_EXPIRE_SECONDS", 1800)),
            "cashier_url": "/cashier.html?orderNo=" + urllib.parse.quote(order["order_no"]),
        }


class EpusdtAdapter:
    """
    Epusdt(开源 USDT-TRC20 收款网关)真实接入点 —— 按官方协议实装(见 wiki/API.md)。

    协议要点(2026 复核自 epusdt 开源版文档):
      - 签名: 所有非空参数(除 signature)按 key ASCII 字典序拼成 k1=v1&k2=v2...,
        末尾【直接拼接】api token(无 &), 整体 MD5 取小写。
      - 创建交易: POST {api}/api/v1/order/create-transaction, JSON body:
          order_id(商户订单号) / amount(【人民币】,2位小数) / notify_url / redirect_url(可选) / signature
        返回 data: { trade_id, order_id, amount(CNY), actual_amount(USDT),
                     token(一次性TRC20地址), expiration_time(时间戳), payment_url(收银台外链) }
      - 回调: 支付成功 epusdt POST 到 notify_url, JSON body 含 trade_id/order_id/amount/
        actual_amount/token/block_transaction_id/signature/status(2=支付成功);
        商户处理成功必须返回字符串 ok, 否则 epusdt 会重试(最多5次)。

    启用条件: DEMO_MODE=0 且设置 EPUSDT_API_URL / EPUSDT_MERCHANT_ID / EPUSDT_TOKEN / SITE_URL。
    SITE_URL 必须是 epusdt 实例能访问到的公网/局域网地址(本机 127.0.0.1 回调不可达)。
    """

    name = "epusdt"
    deliver_on_pay = True

    def __init__(self):
        self.api_url = os.environ.get("EPUSDT_API_URL", "").rstrip("/")
        self.merchant_id = os.environ.get("EPUSDT_MERCHANT_ID", "")
        self.token = os.environ.get("EPUSDT_TOKEN", "")

    @property
    def ready(self) -> bool:
        return bool(self.api_url and self.merchant_id and self.token)

    def _sign(self, params: dict) -> str:
        # 非空参数按 key 字典序, 值跳过空串; 参数名区分大小写
        items = {k: str(v) for k, v in params.items()
                 if k != "signature" and str(v) != ""}
        raw = "&".join(f"{k}={items[k]}" for k in sorted(items)) + self.token
        return hashlib.md5(raw.encode()).hexdigest()

    def create_order(self, order: dict) -> dict:
        cny = order.get("cny")
        if not cny:
            raise ValueError("Epusdt 按人民币计价, 需传 cny")
        site = os.environ.get("SITE_URL", "").rstrip("/")
        if not site:
            raise RuntimeError("启用真实收款必须设置 SITE_URL(epusdt 回调/跳转可达的公网或局域网地址)")

        params = {
            "order_id": order["order_no"],
            "amount": "%.2f" % float(cny),
            "notify_url": site + "/user/api/epusdt/notify",
            "redirect_url": site + "/cashier.html?orderNo=" + order["order_no"],
        }
        params["signature"] = self._sign(params)
        req = urllib.request.Request(
            self.api_url + "/api/v1/order/create-transaction",
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode("utf-8"))
        if resp.get("status_code") != 200:
            raise RuntimeError("Epusdt 创建订单失败: %s %s" %
                               (resp.get("status_code"), resp.get("message")))
        data = resp["data"]
        return {
            "cashier_url": data["payment_url"],          # Epusdt 收银台外链(前端直接跳转)
            "address": data["token"],                     # 一次性 TRC20 收款地址
            "expire_seconds": max(1, int(data["expiration_time"]) - int(time.time())),
            "trade_id": data.get("trade_id"),
            "actual_amount": data.get("actual_amount"),   # 实际需付 USDT
        }

    def verify_signature(self, params: dict) -> bool:
        sign = params.get("signature") or params.get("sign") or ""
        return bool(sign) and self._sign(params) == sign.lower()


def build_gateway():
    """按环境变量选择当前收款通道。默认全仿真, 绝不误触真实收款。"""
    if os.environ.get("DEMO_MODE", "1") == "0":
        adapter = EpusdtAdapter()
        if adapter.ready:
            log.warning("Epusdt 真实收款通道已启用 —— 订单将生成真实收款地址")
            return adapter
    return SimulatedGateway()
