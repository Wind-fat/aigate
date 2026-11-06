# ADR 0011 · 供应商密钥可以录入，但只以密文落库

## 背景

ADR-0003（供应商配置）当初做了一个决定：**密钥不入库**，表里只记录
「密钥在哪个环境变量里」（`api_key_env`）。理由是"数据库文件同时放着账单和厂商 key，
等于每次备份都是一次凭据泄露"。

在一个公开仓库的演示里这没问题。在真实团队里它直接卡住了上线：

> 运维刚在阿里云申请到 API Key，打开控制台，看到的是
> 「未配置密钥 · 测试连通失败 0ms · AIGATE_DASHSCOPE_API_KEY 为空」。
> 他要么去改 `.env` 再重启进程，要么把 key 发给别人让代填。
> 两种都不该出现在一个"内部可用"的系统里。

## 决定

**允许在控制台录入密钥，但只存密文。**

- 加密：Fernet（AES-128-CBC + HMAC-SHA256），每条密文带随机 IV。
- 主密钥：`AIGATE_SECRET_KEY`；未设置时退化为从 `session_secret` 派生，并在启动日志里告警
  （换 session secret 会让已保存的密钥失效，这是必须提前说的事）。
- 存储：`provider_configs.api_key_cipher`（密文）+ `api_key_hint`（末四位）
  + `api_key_updated_at/by`（谁在什么时候录的）。
- 解析优先级：**库里的密文 → `api_key_env` 环境变量 → 空**。两条路都保留，
  因为很多团队本来就用 K8s Secret 注入环境变量。
- 读接口只返回 `key_source`（db/env/none）与 `key_hint`（`…a1b2`）。明文只出现在两个地方：
  写入那次请求体，以及发往上游的那个 `Authorization` 头。
- 主密钥换了之后解不开：接口返回"re-enter the key"，而不是静默当成空密钥继续跑
  （静默失败会表现为"昨天还好好的，今天全部 401"，排查成本远高于一句提示）。

## 这不是 KMS

不做的部分，写清楚比藏着强：

- **没有 HSM / KMS 托管**：主密钥在进程环境里（compose 的 env、K8s Secret）。
- **没有自动轮换**：轮换是"重新录一次"，没有定时任务。
- **没有按租户分密钥**：全公司一份主密钥，够内部用，不够多租户 SaaS。
- **没有审计到"值"**：审计记录 `provider.secret.set/clear` 与操作人，不记录密钥内容。

它能解决的具体问题是两个：**运维能自己把 key 填进去**，以及**盘上的副本不足以直接使用**。

## 连带修正

同一轮里发现并修掉的三个问题：

1. **控制台代理可以借用机器令牌**。`/api/admin/*` 在没有 cookie 时会自动附上
   `X-Admin-Token` —— 任何能访问 3000 端口的人都能拿到整个控制面。
   现在改为显式开关 `CONSOLE_ALLOW_TOKEN_FALLBACK=1`（仅本地/冒烟脚本开启，
   compose 里显式设 0）。
2. **换身份登录后侧栏还是上一个人**。`useSession` 的模块级缓存在同一次 SPA 生命周期里
   从不失效，于是"用 owner 登录 → 退出 → 用开发者登录"，侧栏仍写 owner、按钮仍是 owner 的，
   而服务端早已按开发者判权。新增 `clearSession()`，登录与退出各调一次。
3. **模型只能靠种子脚本登记**。接一个新供应商（OpenAI / Azure / 自建端点）必然带来一批新模型，
   没有 `POST /admin/v1/models` 时，"接 OpenAI"在界面上走不通——路由页会以
   "候选不在 `model_prices` 里"为由拒绝。

## 后果

- 好处：接一家新供应商 = 录入密钥 → 登记模型 → 路由加候选 → 给项目签 key，全部在控制台完成。
- 代价：多一个必须保管好的环境变量（`AIGATE_SECRET_KEY`），以及一个"换主密钥要重录"的运维规则；
  已写进 runbook 与 `.env.example`。
- **生产要求**：`AIGATE_SECRET_KEY` 必须显式设置且与数据库同级保密；`config` 在 `ENV=prod` 时
  应当拒绝用它派生（TODO：已有 `check_deploy.py` 的 prod 检查位，下一轮加）。

## 相关

- `src/aigate/secrets.py`、`src/aigate/provider_config.py`、`src/aigate/api/admin_providers.py`
- `tests/test_provider_secrets.py`（9 例）、`tests/test_model_catalog.py`（5 例）
- 替代 ADR-0003 中"密钥不入库"那一段；其余（运行时读库、5 秒 TTL、真打测试）不变。