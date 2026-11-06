# 安全

## 报告问题

- 一般问题：开一个 GitHub Issue，标题前缀 `[security]`
- 敏感问题：**不要开 issue**，用仓库的 Security → "Report a vulnerability"（私密通道），
  或直接邮件联系仓库维护者（见 commit 作者邮箱）

这是个人项目，没有 SLA。承诺只有一条：**48 小时内回应，修好之前不公开细节**。

## 设计上的安全边界

| 项 | 做法 | 边界 |
|---|---|---|
| API Key | `sk-ag-` + 32 字节随机；库里只存 SHA-256；明文只在创建响应里出现一次 | 明文丢了只能重签，没有任何找回接口（有意） |
| 控制台口令 | stdlib `scrypt(n=16384, r=8, p=1)`，参数写在哈希串里，便于将来调高 | 没有 2FA，没有口令复杂度强制 |
| 会话 | HMAC-SHA256 签名 cookie（`uid:exp.sig`），HttpOnly + SameSite=Lax | 无状态；吊销靠每请求查 `status`。轮换 secret = 全体下线 |
| 角色 | `viewer < admin < owner`，越级创建 owner 被拒 | 没有资源级 ACL |
| PII | 出网前掩码（身份证→手机→银行卡→邮箱，顺序敏感），命中数进 Trace | 只覆盖国内四类正则，不是通用 DLP |
| 审计 | 写操作只追加，无删除/修改接口，含 actor / ip / request_id | **只记 HTTP 层**；直接改库不留痕 |
| Prompt | 默认脱敏后入库（`prompt_preview`），完整 prompt 不落库 | 需要原文排查时要自己开开关，代价自负 |
| 上游凭证 | 只在网关进程环境变量里；控制台从不接触 | 容器内被读等于主机被读 |
| 生产校验 | `AIGATE_ENV=prod` 时拒绝占位 token / 短 secret / 非 HTTPS cookie | 只防呆，不防有心人 |

## 已知的、明确不修的

1. **没有 SSO/OIDC/2FA**：本地账号 + 单机部署是当前的定位（ADR-0009）。
2. **没有速率限制之外的账号保护**：登录复用业务令牌桶（同 IP 10 次/5 分钟），
   没有验证码、没有锁定策略。
3. **审计可被有数据库权限的人清空**：真要防篡改，得把审计行异步投到 WORM 存储或
   外部日志系统。当前只保证"正常路径下不可删"。
4. **控制台没有 CSRF token**：依赖 `SameSite=Lax` + 只接受 JSON 的接口。跨站简单请求
   带不上 cookie，但不是纵深防御。
5. **多副本不共享限流与熔断状态**：`memory` 后端下 N 个副本的实际配额是 `N × rpm`。
   这是当前不支持多副本的根本原因，不是可以调参忽略的细节。

## 生产环境检查清单

```bash
# 1) 凭证不是占位值（不然服务直接起不来，这是设计）
grep -E "AIGATE_(ADMIN_TOKEN|SESSION_SECRET)" .env.production

# 2) 只用 HTTPS 暴露
#    AIGATE_COOKIE_SECURE=true 且反向代理有证书

# 3) 网关端口不要直接暴露到公网
#    只让反向代理访问 8080/3000（compose 里改 ports 为 127.0.0.1:8080:8080）

# 4) 数据库不要暴露宿主端口
#    compose 里 db 服务没有 ports 段（当前就是这样）

# 5) 上游 key 只在网关环境里，不要传给控制台
grep -r "API_KEY" console/ || echo "clean"
```

## 依赖

没有运行时安全扫描（Dependabot 需要仓库设置里手动开）。升级依赖时手动跑一次
`uv run pytest -q && bash scripts/smoke.sh`——这是当前唯一的"升级验证"。