# ADR-0009：控制台账号体系

- 状态：已采纳
- 日期：2026-11-05
- 相关：ADR-0006（控制台取舍）

## 背景

控制台此前用 `X-Admin-Token` 调 `/admin/v1/*`，token 通过 `NEXT_PUBLIC_AIGATE_ADMIN_TOKEN`
进了浏览器包。对一个要交给团队内部用的工具，这件事有三个后果：

1. **不可追责**。所有人共用一个 token，审计日志只能写「管理员」，写不出是谁。
2. **不可吊销**。想收回某个人的权限，只能是换 token，等于把所有人踢下线。
3. **不可分权**。财务只想看成本，实习生不该能签发 Key，但这个模型里只有「能」和「不能」。

## 决策

### 1. 三个角色，不建权限矩阵

`viewer` / `admin` / `owner`，用 `ROLE_ORDER = {viewer: 1, admin: 2, owner: 3}` 比较。

- 读：全员；写：`admin` 及以上；创建/停用 `owner`：仅 `owner`。
- 拒绝往上越级：`admin` 不能创建 `owner`，否则提权只是一个 POST。

没有做资源级 ACL（某个人只能看某个项目）。团队内部工具就这么点人，展示 ACL 只会
增加一张表和一堆边界用例；真有这需求，租户维度已经在 `Project` 上，扩成
`membership(team_id, user_id, role)` 是加法不是重写。

### 2. 密码用 stdlib scrypt，不引 argon2/bcrypt

`hashlib.scrypt(password, salt, n=16384, r=8, p=1, dklen=32)`，存
`scrypt$n$r$p$salt_b64$digest_b64`。

理由只有一个：Windows 上 argon2-cffi / bcrypt 需要编译或预编译轮子，团队里有人
`git clone` 完装不上就没人用。scrypt 够用（内存硬，16MB/次），且零依赖。
参数写进哈希串，将来调高 N 不需要迁移脚本——老哈希按自己的参数验证。

代价：单次哈希 ~40ms，测试夹具建三个用户就多花 0.1s。这是正确性换来的，接受。

### 3. 会话是无状态签名 cookie，不是 session 表

`aigate_session = base64(uid:exp) + "." + HMAC-SHA256(secret)`，HttpOnly + SameSite=Lax。

- 没有 session 表 → 没有 GC 任务、不需要 Redis、服务重启不掉线。
- **吊销靠每请求查 `status`**：签名有效但账号已停用，立即 401。这一点让无状态会话
  不至于变成「发出去的 token 永远有效」。
- 轮换 `AIGATE_SESSION_SECRET` 会让所有人下线——这是有意的紧急开关。

### 4. 双入口统一成 `Actor`

`get_actor` 接受 cookie **或** `X-Admin-Token`，两者都解析成同一个 `Actor(kind, name, role)`。
机器 token 视为 `owner`，并在审计里记 `token:admin`——脚本干的事也要能查。

这样做的关键收益是：**审计不需要知道请求是哪种凭证来的**，它只需要一个能署名的人。

### 5. 审计在中间件里写，actor 从 `request.state` 取

中间件跑在依赖图外面，最初版本只能自己解析 cookie，于是审计行写成了
`user:c34e2d00bb10…`——一串 id，没有任何人能在值班时拿它去问话。

修法：鉴权依赖把它解析出的 `Actor` 放进 `request.state.actor`，中间件在
`call_next` 返回后优先读它，读不到（401/422，依赖没跑完）才回退到匿名。
**一行代码的改动，决定了这份日志有没有用。**

只记写操作（POST/PUT/PATCH/DELETE）且只记 `/admin/v1` 与 `/console/v1`，并跳过
`/console/v1/me`。读操作不记：概览页十秒一次轮询，一天就是八千行噪音。

### 6. 生产环境拒绝启动

`assert_production_ready()` 在 lifespan 最前面跑，`AIGATE_ENV=prod` 时检查：

- admin token 不是占位值（`change-me-*` / `dev-*`）且 ≥24 字符
- session secret ≥32 字符
- `AIGATE_COOKIE_SECURE=true`

任一不过 → `RuntimeError`，进程起不来。带默认口令上线的项目，都是因为没人拦它。

## 后果

- 控制台不再需要 `NEXT_PUBLIC_AIGATE_ADMIN_TOKEN`；token 只活在 `console/.env.local`
  和网关环境里。`NEXT_PUBLIC_*` 是编译期常量，会进 bundle——这条边界必须守住。
- 前端多了一个 middleware 做路由守卫，只查 cookie 是否存在（签名由每个 API 调用验证），
  所以 edge runtime 里不需要签名密钥。
- 密码策略是「≥10 位」，不强制大小写符号。团队内部工具，强制复杂度只会让人写
  `Password1!` 然后贴在群里。

## 未做（明确排除）

- SSO / OIDC：等有企业 IdP 再说，本地账号足够跑一个团队。
- 2FA：同上。
- 密码重置邮件：没有邮件服务，owner 直接在「成员」里重设。
- 权限矩阵 / 资源级 ACL：见上。