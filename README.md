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
| `GLADOS_BASE_URL` | `https://glados.cloud` | 主域名 |
| `GLADOS_DOMAIN_FALLBACK` | `1` | 设 `0` 关闭 `rocks` / `network` 域名回退 |
| `PUSHPLUS_TOKEN` | 无（可选） | 签到失败时推送微信提醒，见下节 |

## 失败时的微信提醒（可选）

签到失败时除了 GitHub 的邮件通知，还可以直接推到微信。配置一次即可：

1. 打开 https://www.pushplus.plus ，用微信扫码登录
2. 复制页面上显示的**一对一推送 token**
3. 仓库 → `Settings` → `Secrets and variables` → `Actions` → `New repository secret`
4. 名称填 `PUSHPLUS_TOKEN`，值填刚复制的 token

未配置时这一步会静默跳过，不影响签到本身。

**为什么不依赖 GitHub 自带的邮件通知**：它对 `schedule` 触发有额外限制（只通知 workflow
文件的最后修改者），而且很容易被邮箱网关当成垃圾邮件拦掉。微信推送更直接。

**注意**：PushPlus 可能要求实名认证，不想认证的话删掉 `PUSHPLUS_TOKEN` 即可。那一步本质
上就是 `curl` 发一个 JSON POST，换成任何接受 JSON 的 webhook 服务都能用。

## 排查指南

看 Actions 日志里最后那段 `ERROR:`，它直接给出结论和修复方式。

| 日志关键字 | 含义 | 处理 |
| --- | --- | --- |
| `签到 token 已失效` | token 不对，签到未生效 | 删除 `GLADOS_CHECKIN_TOKEN` 变量 |
| `Cookie 已失效` | 会话过期 | 重新登录复制 Cookie，更新 `GLADOS_COOKIE` |
| `签到响应无法识别` | API 可能又变了，或返回了未适配的新文案 | 对照控制台积分确认，把日志里的响应内容提 Issue |
| `读取 status 失败` / `读取 points 失败` | 只影响信息展示，签到结论仍然有效 | 可忽略，除非持续出现 |
| `可重试的 HTTP 状态` | 服务端 5xx 或限流 | 无需处理，workflow 会自动重试一次 |

> **Cookie 失效时的响应是 `HTTP 200` + `{"code":-2,"message":"没有权限"}`。**
> 它既不是 401/403，也不是 Cloudflare 拦截，所以极容易被误读成"站点加了反脚本检测"。
> 看到它只需要重新登录复制 Cookie，不必改任何代码。
>
> 另一种同样容易误读的情况是 token 过期：也是 `HTTP 200`，只是 message 变成
> `please checkin via https://glados.cloud`。两者的共同点是**接口返回 200，
> 但签到实际没有生效** —— 这正是为什么脚本必须校验响应内容，而不能只看状态码。

判断签到是否真的生效，看 `Current points` 那一行有没有增加，**不要看 `leftDays`**。

## Cookie 获取

登录 `https://glados.cloud` 后：

1. 打开开发者工具，进入 `Application` → `Cookies`
2. 复制 `koa:sess` 和 `koa:sess.sig` 两个值
3. 拼成 `koa:sess=长串; koa:sess.sig=短串`（分号后有且仅有一个空格）
4. 整段写入 `GLADOS_COOKIE`
