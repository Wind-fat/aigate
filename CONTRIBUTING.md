# 参与开发

这是一个求职作品集项目，但按真实工程的方式维护：**能跑、能测、能交接**。
下面是你需要知道的全部规则，都可以在五分钟内验证。

## 环境

```bash
git clone https://github.com/Wind-fat/aigate && cd aigate
uv sync --all-groups          # python 3.11+，uv 管理依赖
cd console && npm ci          # node 20+
```

不需要数据库、不需要 key、不需要联网：测试全部走 `mock` / `mock-alt` 两个离线 provider。

## 三条命令

```bash
uv run pytest -q              # 93 项，约 90s
uv run ruff check src tests scripts evals
bash scripts/smoke.sh         # 真起进程的 23 项端到端断言（独立 smoke.db）
```

或者 `make verify` 一次跑完前两条。

改动部署文件（compose / Dockerfile）时，额外跑：

```bash
uv run python scripts/check_deploy.py --env-file .env.production
```

## 提交前自检

- [ ] `uv run pytest -q` 全绿（不许 `-k` 只跑自己改的那部分）
- [ ] 新增/修改的行为有测试；修 bug 的测试在修之前应该是红的
- [ ] 改了账务口径（`metering.py`、`usage_records` 语义）→ 同步 `docs/runbook.md`
- [ ] 改了架构取舍 → 新增一个 `docs/adr/000N-*.md`，而不是改旧的（ADR 是记录，不是文档）
- [ ] 改了前端 → `cd console && npx tsc --noEmit && npm run build`
- [ ] 提交信息能单独看懂，正文写「为什么」，不写「改了哪些行」

## 提交信息

```
<type>(<scope>): <imperative summary, lowercase, < 72 chars>

为什么这么改。可以不写，但写了就写人话，不要复述 diff。
```

type：`feat` `fix` `docs` `test` `refactor` `chore`。
scope 举例：`gateway` `console` `metering` `cache` `routing` `deploy`。

示例（真实提交）：

```
feat(metering): crash-safe spool — a kill -9 must not lose charges
fix(console): let viewers be read-only instead of 403-on-click
```

## 代码约定

- Python：`ruff`，行宽 100，`select = ["E","F","I","UP","B"]`。类型标注能写就写。
- 注释写**为什么**，不写**是什么**。一个注释如果只是把代码翻译成中文，删掉。
- 错误一律 `{"error": {"code": "gateway.xxx", "message": "..."}}`，code 是机器契约，
  新增 code 要同步到控制台的错误展示。
- 金额一律 `Decimal` 并 `quantize(1e-6)`；日志里不打明文 key、不打完整 prompt。
- 前端：不要引入图表库/组件库来解决一个页面问题（ADR-0006 记录了这条门槛）。
  服务端组件优先；`"use client"` 只加在真的需要交互的叶子。

## 加一个控制台页面

1. `console/app/(shell)/<name>/page.tsx` —— 放在 `(shell)` 里才会带侧边栏和登录守卫
2. 后端加只读接口到 `api/admin.py`，写接口到 `api/admin_ops.py`（分开是为了"谁都能读、
   少数人能写"一眼可辨）
3. 写操作会由中间件自动进审计；给 `audit.py` 的 `_ACTION_TEMPLATES` 加一条，
   让它有可读的动作名（否则审计里是一行 raw path）
4. 导航加一项 `console/app/(shell)/nav.tsx`

## 加一个 provider

1. `src/aigate/providers/__init__.py`：`known_providers()` 与 `build_provider()` 各加一行
2. `scripts/seed.py` 或迁移里给 `model_prices` 插单价行（没有单价行 = 成本按 0 记）
3. 加一个「路由指向它」的测试；离线测试里用 `mock` 家族，不要真连网

## 不要做的事

- 不要提交 `.env` / `.env.production` / `*.db` / `console/.env.local`（`.gitignore` 已覆盖）
- 不要为了过测试放宽断言；先确认是实现错了还是断言错了，把结论写进提交信息
- 不要在 README 里写没有跑过的数字。测不出来的指标就写"未实测"
- 不要把 `patch` 用在有缩进的函数体上（会重排缩进）；改函数体用整文件重写

## 许可证

Apache-2.0。提交即表示同意以该协议发布。