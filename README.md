# GLaDOS 自动签到

基于 Python `requests` 的 `glados.cloud` 自动签到脚本，通过 GitHub Actions 每天定时执行。

## 2026 API 变更：为什么旧脚本会失败

GLaDOS 在 2026 年初更新了签到接口，**签到 token 从 `glados.one` 改为 `glados.cloud`**。

这个变更的表现很容易被误判：

| 现象 | 真相 |
| --- | --- |
| 接口返回 HTTP **200**（不是 403/429） | 不是被拦截，服务端正常接受了请求 |
| message 是 `please checkin via https://glados.cloud` | token 不匹配，签到没有生效 |
| 手动点签到按钮正常，脚本不行 | 浏览器发的是新 token，脚本发的是旧 token |
| 日志显示"完成"，但积分没涨 | 旧脚本从不校验签到结果 |

> **这不是反脚本检测。** 本项目实测确认，下面这些"绕过检测"的方向对这个问题**全部无效**：
> 补 `sec-ch-ua` / `sec-fetch-*` 请求头、换 User-Agent、用 `curl_cffi` 伪造浏览器 TLS 指纹、挂代理。
> 真正有效的修复只有一个字符改动：token 值。

如果以后又出现"疑似被检测"，**先怀疑 API 变更**，不要往对抗检测的方向走——那条路在这个项目上已经被证明是死路。

## 本次修复内容

1. **token 修正**：`DEFAULT_CHECKIN_TOKEN` 从 `glados.one` 改为 `glados.cloud`。
   若仓库变量 `GLADOS_CHECKIN_TOKEN` 仍残留旧值，脚本会自动纠正并打印警告。
2. **不再静默失败**（最重要）：旧脚本发完签到请求后只检查认证错误，从不判断签到本身是否成功，
   所以 token 失效时它照样打印 `leftDays` 并**以退出码 0 正常结束**——表面成功、实际没签到。
   新脚本显式分类签到响应，无法确认时宁可报失败，也不谎报成功。
3. **修正 `leftDays` 的误用**：它是会员剩余天数，和签到是否生效无关（签到发的是积分）。
   新增读取 `/api/user/points`，输出当前积分与最近一次变化，作为签到生效的对照依据。
4. **退出码语义化**，便于诊断失败原因：
   - `0` 成功
   - `1` 不可重试失败（token / cookie / 响应结构变更）
   - `2` 可重试失败（429 / 5xx / 连接异常）
5. **失败后等待 5 分钟重试一次**：workflow 使用步骤的 `outcome` 判断原始执行结果，
   不依赖 Bash 失败退出后无法写出的退出码。重试成功时任务成功；两次都失败时任务失败，
   由 GitHub Actions 原生通知报告。Cookie 失效仍需要重新登录并更新 `GLADOS_COOKIE`。
6. **收紧认证判定**：旧脚本用 `"login"`、`"cookie"` 这类宽泛关键词匹配，容易把正常响应误判为认证失败。
7. **空环境变量不再覆盖默认值**：GitHub 上定义了但留空的变量会被当作"未设置"。

## 2026-11 签到只给 1 分：缺了挑战流程

2026-11-05 起，只提交 `{"token": ...}` 的旧签到流程仍然返回成功，
但服务端只给 **1 分**（手动签到正常给 9～18 分）。

官方控制台实际发的是三步：

```text
GET  /api/user/checkin/challenge   -> {"code":0, "nonce":"...", "ttl":300000}
proof = sha256(nonce + ".glados-observe-v1")
等待 >= 2.5 秒（本脚本用 2.7 秒留余量）
POST /api/user/checkin  {"token": ..., "nonce": ..., "proof": ...}
```

要点：

- 挑战**绑定签发它的域名**，必须回原域名提交，不能换域名
- 超过 `ttl` 就不要提交，宁可失败重领
- 领不到挑战时**不提交**签到——提交了只会拿 1 分，还会占掉当天名额

本脚本已按上述流程实现。`checkin.py` 里没有这一步时，看积分流水会发现
每天只有 `change=1`。

> 服务端决定最终入账积分。拿到 `Checkin! Got 1 Points` 且流水 +1，
> 说明签到成功了、只是这次奖励低，**不要当成网络失败反复补签**。

### 设备校验：`device-mismatch` / `Automated check-in detected`

新版签到还会校验会话绑定的浏览器。用脚本默认 UA 可能被拒，此时把生成
cookie 的那个浏览器的 UA 配进来：

1. 浏览器打开 `chrome://version`，复制「用户代理」整行
2. 仓库 `Settings → Secrets and variables → Actions → Variables`
   → `New repository variable`，名称 `GLADOS_USER_AGENT`，值是刚才那行
3. 重新登录一次 glados.cloud，同步更新 `GLADOS_COOKIE`

脚本日志里只显示「User-Agent 来源: GLADOS_USER_AGENT / 默认值」，不打印 UA 本身。

## 目录结构

```text
.
|-- .github
|   `-- workflows
|       `-- checkin.yml
|-- checkin.py
|-- test_checkin.py
|-- requirements.txt
`-- README.md
```

## GitHub 上的配置

1. 把本目录文件推送到仓库（覆盖旧的 `checkin.py` 和 workflow）。
2. `Settings` → `Secrets and variables` → `Actions` → `New repository secret`：
   - 名称 `GLADOS_COOKIE`，值为浏览器里复制的完整 Cookie，至少包含 `koa:sess` 和 `koa:sess.sig`。
3. **如果之前建过 `GLADOS_CHECKIN_TOKEN` 变量，请删除它**（或者把它设为 `glados.cloud`）。
   留着旧值虽然会被脚本自动纠正，但会持续打印警告。
4. 到 `Actions` 页面手动跑一次 `GLaDOS Checkin`，确认日志正常。

## 本地运行

```bash
pip install -r requirements.txt
export GLADOS_COOKIE='koa:sess=xxx; koa:sess.sig=yyy'
python checkin.py
echo "exit=$?"
```

Windows PowerShell:

```powershell
$env:GLADOS_COOKIE='koa:sess=xxx; koa:sess.sig=yyy'
python checkin.py
```

跑自测（不联网，全部走 mock）：

```bash
python test_checkin.py
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GLADOS_COOKIE` | 无（必需） | 完整 Cookie 字符串 |
| `GLADOS_CHECKIN_TOKEN` | `glados.cloud` | 签到 token，一般不需要改 |
| `GLADOS_USER_AGENT` | 内置 Chrome UA | 生成该 cookie 的浏览器 UA，用于设备校验 |
| `GLADOS_BASE_URL` | `https://glados.cloud` | 主域名 |
| `GLADOS_DOMAIN_FALLBACK` | `1` | 设 `0` 关闭 `rocks` / `network` 域名回退 |

## 每月更新 cookie

cookie 的有效期是 **30 天**（`koa:sess` 的 `_maxAge` 硬限制），到期后签到返回
`没有权限`、退出码为 `1`。服务端不会在调用时刷新 cookie（实测 `status` / `points`
都没有 `Set-Cookie`），而登录走邮箱验证码——所以**没有任何自动续期的办法，必须周期更新**。

> 拖到第 31 天才换会断签（`STREAK` 会清零）。建议提前几天，比如设个 25 天后的提醒。

### 手动方式（约 30 秒）

1. 登录 https://glados.cloud → `F12` → **Application** → **Cookies** → 选 `https://glados.cloud`
2. 复制四个值：`koa:sess`、`koa:sess.sig`、`gld:sess`、`gld:sess.sig`
3. 拼成一行（分号后一个空格）：
   ```
   koa:sess=…; koa:sess.sig=…; gld:sess=…; gld:sess.sig=…
   ```
4. 更新仓库 Secret `GLADOS_COOKIE`

### 一键方式（约 5 秒）

需要一次性配置 fine-grained PAT：

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens**
2. **Repository access** 只勾 `pyx13638516490/glados_checkin`
3. **Permissions → Repository permissions → Secrets: Read and write**
4. 生成后立刻复制（只显示一次）

之后每次更新：

```powershell
pip install pynacl            # 只需第一次
$env:GH_PAT = 'github_pat_xxx'
python tools/update_secret.py
```

脚本从**剪贴板**读取刚复制的 cookie，用仓库公钥加密后写入 Secret。cookie 不落盘、
不打印，也不会留在 shell 历史里。

> `tools/update_secret.py` 也支持改目标：`$env:REPO`、`$env:SECRET_NAME`。

## 排查指南

看 Actions 日志里最后那段 `ERROR:`，它直接给出结论和修复方式。失败时运行页面上还会有一份
`Summary`（由 `$GITHUB_STEP_SUMMARY` 写成），不用展开日志就能看到退出码和对应处理方式。

**关于失败通知**：GitHub 会发邮件 —— 手动触发的发给触发者，`schedule` 触发的发给 workflow
文件的最后修改者。**没收到邮件先查垃圾邮件箱**：163 等邮箱经常把 `notifications@github.com`
判成垃圾邮件，建议把它和 `noreply@github.com` 一起加进白名单。

| 日志关键字 | 含义 | 处理 |
| --- | --- | --- |
| `签到 token 已失效` | token 不对，签到未生效 | 删除 `GLADOS_CHECKIN_TOKEN` 变量 |
| `Cookie 已失效` | 会话过期 | 重新登录复制 Cookie，更新 `GLADOS_COOKIE` |
| `签到响应无法识别` | API 可能又变了，或返回了未适配的新文案 | 对照控制台积分确认，把日志里的响应内容提 Issue |
| `读取 status 失败` / `读取 points 失败` | 只影响信息展示，签到结论仍然有效 | 可忽略，除非持续出现 |
| `可重试的 HTTP 状态` | 服务端 5xx 或限流 | 无需处理，workflow 会自动重试一次 |
| `带 cookie 请求超时` / Cloudflare `502` | **源站认证链路故障**，不是 cookie 失效 | 做「无 cookie 对照」（见下），两边表现一致就等站点恢复 |

> **Cookie 失效时的响应是 `HTTP 200` + `{"code":-2,"message":"没有权限"}`。**
> 它既不是 401/403，也不是 Cloudflare 拦截，所以极容易被误读成"站点加了反脚本检测"。
> 看到它只需要重新登录复制 Cookie，不必改任何代码。
>
> 另一种同样容易误读的情况是 token 过期：也是 `HTTP 200`，只是 message 变成
> `please checkin via https://glados.cloud`。两者的共同点是**接口返回 200，
> 但签到实际没有生效** —— 这正是为什么脚本必须校验响应内容，而不能只看状态码。

判断签到是否真的生效，看 `Current points` 那一行有没有增加，**不要看 `leftDays`**。

### 区分「服务端故障」与「cookie 失效」

服务端出故障时，表现和 cookie 失效**几乎一样**：页面转圈、`Days Left: NaN`、积分显示 0、
签到返回"没有权限"。区别靠一个对照实验，只要 30 秒：

```python
# 同一个接口，唯一变量是带不带 cookie
GET /api/user/points                      # 不带 cookie
GET /api/user/points  (Cookie: koa:sess=…)  # 带 cookie
```

| 现象 | 结论 |
| --- | --- |
| 不带 cookie 秒回 `{"code":-2,…}`，带 cookie 也秒回 `{"code":-2,…}` | **cookie 真的失效了** → 重新登录取新 cookie |
| 不带 cookie 秒回，带 cookie **超时 / 502 / 500** | **源站认证链路故障** → cookie 大概率是好的，等恢复 |

2026-11-01 实测过一次这个故障：HTML 和 `service-status` 正常，
但 `status` / `points` / `checkin` 一旦带 cookie 就 20 秒后 502 或超时；
连**伪造的乱码 cookie** 也超时——源码在进账号校验之前就卡住了，所以与具体账号无关。

> 这个故障期间 GitHub Actions 会以 `exit 1` 结束（服务端错误地返回"没有权限"），
> 很容易被误读成 token/cookie 过期。**先做对照实验，再决定要不要换 cookie。**

## Cookie 获取

登录 `https://glados.cloud` 后：

1. 打开开发者工具，进入 `Application` → `Cookies`
2. 复制 `koa:sess` 和 `koa:sess.sig` 两个值
3. 拼成 `koa:sess=长串; koa:sess.sig=短串`（分号后有且仅有一个空格）
4. 整段写入 `GLADOS_COOKIE`
