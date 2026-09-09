# 极赞涨粉 —— 抖音涨粉业务「完整流程仿真版」

按原站 heiu.org 的页面结构与 API 路由族 1:1 复刻的**抖音涨粉自助下单站**，
补齐了旧教学版缺失的完整业务闭环：

```
填抖音号/主页 → 选数量 → tradeAmount 服务端算价
   → order/trade 下单 → 收银台(一次性 TRC20 地址 + 30分钟倒计时 + 轮询)
   → 到账 → 自动发货(生成下发任务记录)
   → query 查单(secret 路由保留)
```

## 与原站结构对照

| 原站接口 | 方法 | 本版 |
|---|---|---|
| /user/api/site/info | GET | ✅ |
| /user/api/index/data | GET | ✅ (分类) |
| /user/api/index/commodity?categoryId= | GET | ✅ |
| /user/api/index/commodityDetail?commodityId= | GET | ✅ |
| /user/api/index/pay | GET | ✅ (USDT-TRC20/Epusdt) |
| /user/api/index/card?commodityId=&page=&race= | GET | ✅ (保留, 本店无自选卡密) |
| /user/api/index/tradeAmount | POST | ✅ 服务端算价(档位批发) |
| /user/api/order/trade | POST | ✅ 下单, 生成一次性地址+订单号 |
| /user/api/index/query | POST | ✅ 查单(订单号) |
| /user/api/index/secret | POST | ✅ 凭查询密码取卡密 |
| Epusdt 收银台外链 | - | ✅ 内置收银台页(结构等价: 地址/QR/倒计时/轮询) |
| 链上回调 → 自动发货 | - | ✅ 仿真回调 /dev/demo/pay; 真实回调路由已预留 |

数据库为 SQLite(`data/dylikes.db`, 首次启动自动建表+种子数据):
单一业务: 抖音涨粉·真人高质量(¥0.6/粉起, 批发5档)。卡密/查单取密路由保留但无在售卡密商品。

## 运行

```bash
pip install -r requirements.txt   # 或 docker build -t dylikes .
python app.py                     # http://127.0.0.1:8686
```

Docker: `docker build -t dylikes . && docker run -p 8686:8686 dylikes`
(注意: Docker 内 SQLite 写 data/ 卷, 重启保留数据需挂载 `-v $PWD/data:/app/data`)

## 演示模式与「仿真到账」

- `DEMO_MODE=1`(默认): 收款地址为由订单号确定性生成的**伪 TRC20 地址**(格式合法但纯仿真, 不触网不收真钱)。
  收银台底部出现「模拟转账到账」按钮 → 调用 `/dev/demo/pay` → 走一遍与真实回调完全相同的
  到账→自动发货→查单取卡密流程。**页面本体无任何"演示/反诈"水印**, 外观纯净。
- 演示标记仅存在于后端: 服务启动日志横幅 + `site/info` 返回的 `demo` 字段(仅收银台据此显示模拟按钮, 无其他用途)。

## 真实收款(Epusdt) —— 已按官方协议实装

`gateway.py` 的 `EpusdtAdapter` 与 `app.py` 的 `/user/api/epusdt/notify` 已按 epusdt 开源版
官方协议完整实现(签名 = 非空参数按 key 字典序拼接后直接连 token 做 md5; 创建交易传人民币金额;
回调验签通过且 status=2 自动发货, 返回 `ok`)。签名实现已与官方文档示例逐字节对拍验证。

启用真实收款:
```bash
cp .env.example .env   # 填 DEMO_MODE=0 + EPUSDT_API_URL / EPUSDT_MERCHANT_ID / EPUSDT_TOKEN / SITE_URL
python app.py
```
- `SITE_URL` 必须是 epusdt 能访问到的公网/局域网地址(本机联调用 cloudflared/ngrok 隧道);
- 下单后前端直接跳转 epusdt 收银台外链, 支付成功回调自动发货, redirect_url 带回本站收银台显示完成页。

## 测试网联调(不碰真钱, 推荐先做)

目的: 真实协议、真实回调、真实发货链路, 但全程测试币。步骤:

1. 部署 epusdt(开源版镜像 `assimon/epusdt` 或按其仓库文档), 并把它的 **TRON 节点指向 Shasta 测试网**:
   - 若版本支持 `TRON_API_URL`/节点配置 → 填 `https://api.shasta.trongrid.io`;
   - 否则在源码 TRXService 中把 TronGrid 地址改为测试网地址(一处常量)后重新构建;
2. 在 epusdt 后台创建收款钱包(测试网 TRC20 地址), 通过 Shasta 水龙头获取测试 USDT/TRX
   (官方渠道: Telegram @TronTestFaucetBot 等, 请求 `!shasta_usdt <地址>`);
3. 本站 `.env`: `DEMO_MODE=0` + `EPUSDT_*` + `SITE_URL`(指向 epusdt 可回调的地址);
4. 下单 → 跳转 epusdt 收银台 → 用测试 USDT 转账到显示的收款地址 → 等链上确认;
5. 观察本站日志出现 `Epusdt 回调: order=... status=2` → `Epusdt 到账发货` → 订单变已完成;
6. 全程无真钱流动。确认链路无误后, 才需要考虑是否接主网与真实发货能力。

> 再次提醒: 接主网=收真钱, 必须有真实可交付的发货能力与合规主体; 本复刻站仅用于教学演示。

> 注意: 上述步骤涉及真实资金与真实发货, 由部署方自行负责。默认配置下站点绝不产生真实收款。

## 测试

```bash
.venv/Scripts/python dev/test_e2e.py    # 26 项端到端断言: 直充/卡密/边界/安全
```

## 页面

- `/` 主页: 公告 / 分类胶囊 / 商品网格 / 详情+下单表单(动态 widget 参数、批发档位、实时算价) / 实时成交 / FAQ
- `/cashier.html?orderNo=` 收银台: 一次性地址、收款码、30分钟倒计时、状态轮询、到账自动跳成功页(卡密/任务记录)
- `/query.html` 查单: 订单号(+查询密码)查状态、取卡密

## 结构

```
app.py              Flask 主程序(全部 API + 订单状态机 + SQLite + 种子数据 + 安全加固)
gateway.py          收款通道抽象: SimulatedGateway(默认) / EpusdtAdapter(预留)
index.html          主页(结构与原站一致: 分类→商品→详情→下单)
cashier.html        收银台(仿真收款+轮询+倒计时)
query.html          查单/取卡密
assets/static/      原站同款前端库(bootstrap4/layer/pay.js 等, pay.js 为原站原版)
assets/img/         商品封面与图标
data/dylikes.db     SQLite(首次启动自动生成)
```

# KV 持久化(Upstash Redis)已于部署环境启用, 解决 serverless 订单跨实例丢失
