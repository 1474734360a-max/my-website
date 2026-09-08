# -*- coding: utf-8 -*-
"""E2E 全流程测试(直充 + 卡密)。python dev/test_e2e.py 运行, 无第三方依赖。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8686"
passed = failed = 0


def call(method, path, payload=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(payload).encode() if payload is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


print("== A. 涨粉订单: 2000U 兑换(1:1.65 -> 3300粉) ==")
st, body = call("POST", "/user/api/order/trade", {
    "commodity_id": 1, "num": 2000, "pay_id": 3,
    "contact": "https://v.douyin.com/abc123/", "url": "https://v.douyin.com/abc123/"})
check("下单返回 code200", st == 200 and body["code"] == 200, str(body))
order1 = body["data"]["order_no"]
check("订单号生成", len(order1) >= 16)
check("cashier url 指向本站", body["data"]["url"].startswith("/cashier.html?orderNo="))

st, body = call("GET", f"/user/api/order/status?orderNo={order1}")
d = body["data"]
check("状态 pending", d["status"] == "pending")
check("伪 TRC20 地址格式", d["address"].startswith("T") and len(d["address"]) == 34, d["address"])
# 承兑口径: 下单2000U -> 支付2000U(≈14500元), 兑换 2000x1.65=3300 粉
check("金额正确(2000U -> cny 14500)", abs(d["amount"] - 2000) < 0.01 and abs(d["cny"] - 14500.0) < 0.01, str(d["amount"]))
check("汇率1.65, 应到3300粉", d["ratio"] == 1.65 and d["deliver_num"] == 3300, str(d.get("ratio")))

st, body = call("POST", "/dev/demo/pay", {"orderNo": order1})
check("模拟到账成功", st == 200 and body["code"] == 200, str(body))
check("直充无卡密", body["data"]["has_secret"] is False)

st, body = call("GET", f"/user/api/order/status?orderNo={order1}")
d = body["data"]
check("状态 fulfilled", d["status"] == "fulfilled")
check("下发记录 note 含任务信息", "已向" in (d["note"] or "") and "2000" in d["note"], d["note"])

st, body = call("POST", "/user/api/index/query", {"keywords": order1})
check("查单可见完成状态", body["data"]["status"] == "fulfilled" and body["data"]["secret_available"] is False)

st, body = call("POST", "/user/api/index/secret", {"orderId": order1, "password": ""})
check("直充订单取卡密应拒绝", body["code"] != 200, str(body))

print("== B. 加赠汇率阶梯 ==")
# (下单U数, 汇率, 应到粉 = n*ratio, 应付元 = n*7.25)
for n, r, fans, cny in ((30, 1.1, 33, 217.5), (100, 1.1, 110, 725.0), (200, 1.2, 240, 1450.0), (300, 1.4, 420, 2175.0), (2000, 1.65, 3300, 14500.0), (5000, 1.65, 8250, 36250.0)):
    st, body = call("POST", "/user/api/index/tradeAmount", {"commodityId": 1, "num": n, "coupon": "", "cardId": 0, "race": ""})
    dd = body["data"]
    check("num=%sU -> 1:%s, 应付cny=%s, 到账%s粉" % (n, r, cny, fans),
          abs(dd["ratio"] - r) < 1e-9 and abs(dd["cny"] - cny) < 0.01 and abs(dd["amount"] - n) < 0.01 and dd["deliver_num"] == fans, str(dd))

print("== C. 边界与防护 ==")
st, body = call("POST", "/user/api/order/trade", {"commodity_id": 1, "num": 10, "pay_id": 3, "contact": "x"})
check("低于最低30U被拒", body["code"] != 200, str(body))
st, body = call("POST", "/user/api/order/trade", {"commodity_id": 1, "num": 1000, "pay_id": 3, "contact": ""})
check("直充缺收货信息被拒", body["code"] != 200, str(body))
st, body = call("POST", "/dev/demo/pay", {"orderNo": order1})
check("重复到账被拒(幂等)", body["code"] != 200, str(body))

st, body = call("GET", "/user/api/index/latestOrders")
names = [o["commodity_name"] for o in body["data"]]
check("实时成交含新订单", any("真人高质量" in n for n in names), str(names))

st, body = call("GET", f"/user/api/order/status?orderNo=NOTEXIST00000000")
check("不存在的订单返回错误", body["code"] != 200)

print(f"\n结果: {passed} passed, {failed} failed")
exit(1 if failed else 0)
