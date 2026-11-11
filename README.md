# GLaDOS 自动签到

基于 GitHub Actions 的 `glados.cloud` 自动签到，每天定时执行一次（本仓库配置了两次，
原因见「关于定时」）。

> **实现来源**：`checkin.py` 与 `tests/` 移植自
> [lankerr/2026-glados-checkin](https://github.com/lankerr/2026-glados-checkin)（MIT）。
> 本项目保留其挑战式签到、设备校验、自动兑换与推送能力，并补充了本仓库的排障经验。

## 2026-11 签到只给 1 分：缺了挑战流程

2026-11-05 起，只提交 `{"token": ...}` 的旧签到流程仍然返回成功，
但服务端只给 **1 分**（手动签到正常给 9～18 分）。

官方控制台实际发的是三步：

```text
GET  /api/user/checkin/challenge   -> {"code":0, "nonce":"...", "ttl":300000}
proof = sha256(nonce + ".glados-observe-v1")
等待 >= 2.5 秒（脚本用 2.7 秒留余量）
POST /api/user/checkin  {"token": ..., "nonce": ..., "proof": ...}
```

- 挑战**绑定签发它的域名**，必须回原域名提交
- 超过 `ttl` 不提交，宁可失败重领
- 领不到挑战时**不提交**签到——提交只会拿 1 分，还占掉当天名额

> 服务端决定最终入账积分。拿到 `Checkin! Got 1 Points` 且流水 +1，
> 说明签到成功、只是这次奖励低，**不要当成网络失败反复补签**。

## 区分「服务端故障」与「cookie 失效」

服务端出故障时，表现和 cookie 失效**几乎一样**：页面转圈、`Days Left: NaN`、
积分显示 0、签到返回“没有权限”。区别靠一个对照实验，30 秒：

```text
GET /api/user/points                       # 不带 cookie
GET /api/user/points  (Cookie: koa:sess=…) # 带 cookie
```

| 现象 | 结论 |
| --- | --- |
| 两边都秒回 `{"code":-2,…}` | **cookie 真的失效** → 重新登录取新 cookie |
| 不带 cookie 秒回，带 cookie **超时 / 502 / 500** | **源站认证链路故障** → cookie 大概率好的，等恢复 |

2026-11-01 实测过一次：HTML 和 `service-status` 正常，但 `status` / `points` /
`checkin` 一旦带 cookie 就 20 秒后 502；连**伪造的乱码 cookie** 也超时——
请求在进账号校验之前就卡住了，与具体账号无关。

> 那次故障期间 Actions 以 `exit 1` 结束，看起来和 cookie 过期一模一样。

## 配置

### 1. Secret

`Settings → Secrets and variables → Actions → New repository secret`

| Name | 值 |
| --- | --- |
| `GLADOS_COOKIE` | 完整单行 Cookie，必须含 `koa:sess` 与 `koa:sess.sig`（新版还会看 `gld:sess`） |

取法：登录 `glados.cloud` → `F12` → **Network** → 刷新 → 点一次成功的
`/api/user/status` → **Headers** → 请求头里的 `Cookie:` → 右键 Copy value。

### 2. Variable（设备校验，强烈建议配）

新版签到会校验会话绑定的浏览器，UA 不一致会返回 `device-mismatch` 或
`Automated check-in detected`：

1. 浏览器打开 `chrome://version`，复制「用户代理」整行
2. `Settings → Secrets and variables → Actions → Variables`
   → `New repository variable`，名称 `GLADOS_USER_AGENT`

### 3. 可选变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EXCHANGE_PLAN` | `plan500` | 积分达标自动兑换：`plan500`(500→100天) / `plan200` / `plan100` / `off` |
| `PUSH_LEVEL` | `fail_only` | `all` 表示每次签到都推送 |
| `PUSHPLUS_TOKEN` | — | 微信推送（Secret） |
| `SERVERCHAN_SENDKEY` | — | Server酱 SendKey（Secret） |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Telegram 推送（Secret） |

## 手动运行与诊断

`Actions → GLaDOS 2026 Checkin → Run workflow`：

- **`diagnose_only = true`**：只验登录态，不签到、不兑换、不推送。
  配置改完先跑这个，确认 cookie 没问题
- **`diagnose_only = false`**：真实签到

> 只读诊断通过不代表签到的设备校验也通过；UA 不匹配仍会被拒。

## 关于定时（重要）

配置了两个 cron：`17 1 * * *` 与 `47 7 * * *`（北京时间 09:17 / 15:47）。

**不是写错了。** 实测这个仓库的 `schedule` 有约 6 小时延迟，偶尔直接丢弃：

| cron 目标 (UTC) | 实际运行 (UTC) | 延迟 |
| --- | --- | --- |
| 01:17 | 06:50 / 07:03 / 07:04 / 06:58 / 07:30 | 5.5–6.2 h |
| 07:47 | 13:37 / 14:32 / 14:39 / 13:54 / 14:31 | 5.8–6.7 h |

所以实际签到时间大约在 **14:00** 和 **21:00**。连续两次是提高当天成功率的冗余；
签到幂等，重复运行不会多扣分。

> GitHub 官方说明：高负载时计划任务可能被延迟甚至丢弃。公开仓库连续 60 天
> 无活动会自动停用 schedule，本仓库的 `keepalive.yml` 每月提交一次保活。

## 判断签到是否真的生效

看积分流水里当天那行的 `change`，**不要看 `leftDays`**（那是会员剩余天数）。
脚本每次都会读 `/api/user/points` 并打印当前积分与最近一次变化。

## 目录

```text
checkin.py                    签到主程序
tests/                        单元测试（unittest，92 个）
tools/wait_and_checkin.py     服务端故障期间守候，恢复后自动补签
.github/workflows/checkin.yml Actions 配置
.github/workflows/keepalive.yml 每月保活，防止 schedule 被停用
```

跑测试：

```bash
python -m unittest discover -s tests -v
```

## 排查

| 日志关键字 | 含义 | 处理 |
| --- | --- | --- |
| `没有权限` | cookie 失效 | 重新登录，更新 `GLADOS_COOKIE` |
| `device-mismatch` / `Automated check-in detected` | 设备校验被拒 | 配 `GLADOS_USER_AGENT`，并重新登录同步 cookie |
| `未获取有效签到验证` | 挑战接口异常 | 稍后重试；持续出现说明接口又变了 |
| `please checkin via https://glados.cloud` | token 值不对 | 删除 `GLADOS_CHECKIN_TOKEN` 变量 |
| 带 cookie 超时 / 502 | 源站故障 | 见上文对照实验，等恢复 |

## License

`checkin.py` 与 `tests/` 来自 [lankerr/2026-glados-checkin](https://github.com/lankerr/2026-glados-checkin)，
MIT License。本项目其余改动同样以 MIT 发布。
