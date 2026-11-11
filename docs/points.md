# 邮箱验证码登录与积分

## 登录

- 默认入口是邮箱验证码：输入邮箱 → 收 6 位码 → 登录；邮箱第一次出现就自动建账号。
- 老的用户名 + 密码账号继续可用（登录弹窗的「密码登录」页）；登录后顶部有「绑定邮箱」，绑定后两种方式都能登录。
- 邮箱账号没有密码，不能用密码登录。
- 验证码：10 分钟有效、一次性、每个码最多错 5 次；同邮箱 60 秒冷却、每小时最多 5 次；全站每日上限 `THESIS_EMAIL_DAILY_CAP`（默认 1000）。库里只存哈希。
- 发送接口无论邮箱是否已注册都返回一样的结果，不泄露账号是否存在。
- `+标签` 视为同一个邮箱（`a+1@x.com` 与 `a@x.com` 同一账号），避免一个邮箱刷多个注册赠送。

### 发信配置（SMTP）

`SMTP_HOST / SMTP_PORT / SMTP_SECURITY(ssl|starttls) / SMTP_USER / SMTP_FROM`，授权码放 `SMTP_PASSWORD_FILE`（Docker 里是 `deploy/secrets/smtp_password`）。
没配 SMTP：`THESIS_ENV=production` 时邮箱登录返回 503；非生产只把验证码打到日志（`core/mailer.py`）。

## 积分

- 1 元 = 10 积分，**积分价与人民币价一致，不取整**：文本检测 0.99 元 = 9.9 积分，文件检测 3.99 元 = 39.9 积分，Word 导出 29.99 元 = 299.9 积分。积分最多一位小数（1 分钱 = 0.1 积分）。
- 来源：支付宝充值（档位后台可设）、注册赠送（只给邮箱验证通过的新用户，后台设额度和每日人数上限，默认 0）、管理员手动发放。
- 用途：所有服务（文本/文件检测、改写、排版 Word 导出）都能用积分付款，与支付宝并存；积分不可提现、不退现金。
- 积分支付不叠加优惠券，也不产生推广员佣金（充值那一笔按真钱计佣）。

### 记账原则（`core/points.py`）

- 库里的积分是整数「0.1 积分」（1 分钱 = 1 个单位），没有浮点误差；接口和页面里才换算成 9.9 这样的数。早期按整数积分存的数据会在启动时一次性 ×10 迁移。
- 余额缓存在 `users.points`，流水在 `points_ledger`；`UNIQUE(kind, ref)` 让充值入账、消费扣减、注册赠送都幂等。
- 扣减带 `points >= ?` 条件，并发下不会透支或双花。
- 积分支付最终落成 `payment_orders` 里一笔 `TRADE_SUCCESS`、`payment_product='points'` 的订单，所以 `authorize`、我的记录、24 小时清理、下载校验都沿用同一套「已付款」判断。
- 扣积分和写订单是两步（账号库与订单库可分开配置）。订单号由任务号确定性推出，第二步失败后用户重试或读余额时会补完订单，不会重复扣。
- 积分支付时会关闭同一任务还没付的支付宝订单。极小概率：用户在支付宝已付款的同时又用积分付了同一任务，需要在后台订单列表里发现并用「积分调整」退还。

### 管理后台（`/admin/affiliates`）

- 「积分设置」：开关、注册赠送、每日赠送人数上限、充值档位。
- 「积分调整与流水」：按邮箱加减积分（最多一位小数，失败订单退还也走这里）并查看流水。
- 订单 CSV 新增「支付方式」列（积分 / 支付宝）。

### 接口

`GET /api/points`（余额、档位、积分价、流水）· `POST /api/points/topup` · `GET /api/points/topup/{order}` · `POST /api/points/pay`（`quote_id` 或 `job_id`）。
邮箱：`POST /api/account/email/send|login`，`/email/bind/send|verify`。
管理：`GET/PUT /api/admin/points-settings`、`POST /api/admin/points/adjust`、`GET /api/admin/points/ledger`。
