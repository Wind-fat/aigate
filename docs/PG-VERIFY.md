# 真 PostgreSQL 验证

本机没有 docker、没有 psql、没有 redis-server。这份记录说明：用 pip 的 `pgserver`
（自带 PostgreSQL 二进制）在本机起了一个**真的 PG 16.2**，在它上面跑 alembic 迁移、
跑 DB 相关测试子集，并把过程中暴露的两个真缺陷钉住。

全部命令都在 `D:\code\aigate` 下、git-bash 里执行；`$` 后面是可复制的命令。

## 1. 起库（pgserver）

```
$ uv venv .pgvenv -p 3.13
$ uv pip install --python .pgvenv/Scripts/python.exe pgserver
  × No solution found when resolving dependencies:
  ╰─▶ Because all versions of pgserver have no wheels with a matching Python
      ABI tag (e.g., `cp313`) and you require pgserver, we can conclude that
      your requirements are unsatisfiable.
      hint: You require CPython 3.13 (`cp313`), but we only found wheels for
      `pgserver` (v0.1.4) with the following Python ABI tags: `cp39`, `cp310`,
      `cp311`, `cp312`
```

`pgserver==0.1.4` 只发到 cp312，本机主 venv 是 3.14 / 上一版是 3.13，所以**单独开一个 3.12
venv 当「数据库进程宿主」**，不进项目运行时依赖、也不进主 venv：

```
$ uv venv .pgvenv -p 3.12 && uv pip install --python .pgvenv/Scripts/python.exe pgserver
Using CPython 3.12.13
Installed 4 packages in 36.26s
 + fasteners==0.20
 + pgserver==0.1.4
 + platformdirs==4.14.5
 + psutil==7.2.2
```

启动（pgserver 自己挑端口，所以脚本把 URL 打出来）：

```
$ .pgvenv/Scripts/python.exe scripts/pg_local.py --pgdata .pgdata2
PGDATA=D:\code\aigate\.pgdata2
PID=21288
URI=postgresql://postgres:@127.0.0.1:55299/postgres
AIGATE_DATABASE_URL=postgresql+asyncpg://postgres:@127.0.0.1:55299/postgres
ready; Ctrl-C to stop
```

核对版本（asyncpg 直连）：

```
$ .venv/Scripts/python.exe -c "import asyncio,asyncpg; ..."
version: PostgreSQL 16.2 on x86_64-pc-mingw64, compiled by gcc.exe (x86_64-posix-seh-rev2,
         Built by MinGW-W64 project) 12.2.0, 64-bit
server_version: 16.2
```

## 2. 建库 + alembic upgrade head

```
$ .venv/Scripts/python.exe -c "... asyncpg ... CREATE DATABASE aigate_pgtest ..."
created aigate_pgtest
```

```
$ AIGATE_DATABASE_URL="postgresql+asyncpg://postgres:@127.0.0.1:55299/aigate_pgtest" \
  .venv/Scripts/python.exe -m alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 47f391b490c9, initial schema
INFO  [alembic.runtime.migration] Running upgrade 47f391b490c9 -> 8c1f2d4a9b31, console accounts and audit log
INFO  [alembic.runtime.migration] Running upgrade 8c1f2d4a9b31 -> c4a7e5b90f22, provider configs and per-user project scope
INFO  [alembic.runtime.migration] Running upgrade c4a7e5b90f22 -> d9b3c1f47e08, quotas per level, daily budgets, alerts, prompt versions, shadow traffic
INFO  [alembic.runtime.migration] Running upgrade d9b3c1f47e08 -> e1a2b3c4d5e6, key ownership and lifecycle
INFO  [alembic.runtime.migration] Running upgrade e1a2b3c4d5e6 -> f1c2d3e4a5b6, provider secrets: ciphertext at rest, hint for humans
INFO  [alembic.runtime.migration] Running upgrade f1c2d3e4a5b6 -> a7d4e5f60b12, prompt version library
```

7 条迁移全部落到真 PG 上（`transactional DDL`，不是 SQLite 的 batch 改写）。落库结果：

```
$ ... select tablename from pg_tables where schemaname='public' order by 1
15 tables: alembic_version, alerts, api_keys, audit_logs, console_users, model_prices,
projects, prompt_templates, provider_configs, request_traces, route_policies,
semantic_cache_entries, shadow_runs, teams, usage_records
```

## 3. 迁移暴露的第一个缺陷：PRAGMA 发给了 PostgreSQL

第一次把测试指向 PG 时，`init_db()` 直接炸：

```
E   sqlalchemy.exc.ProgrammingError: (sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError)
E   syntax error at or near "PRAGMA"
E   [SQL: PRAGMA journal_mode=WAL]
```

根因：`init_db()` 用 `settings.is_sqlite` 判断方言，而 `set_engine()` 允许显式传 URL
（测试、脚本、切库）。此时 `settings.database_url` 还停在默认 SQLite 上，于是「SQLite 专属」
的 PRAGMA 被发给了 PG。改成 `engine.dialect.name == "sqlite"` 后，同一套 schema 创建代码
在两种库上都能跑（`src/aigate/db.py`）。

## 4. 跑测试子集

`tests/conftest.py` 的 `client` fixture 默认用临时 SQLite 文件；新增的
`AIGATE_TEST_DB_URL` 让同一套用例直接打在真库上，每个用例前 `drop_all` + `create_all`，
避免行数据跨用例泄漏。

```
$ AIGATE_TEST_DB_URL="postgresql+asyncpg://postgres:@127.0.0.1:55299/aigate_pgtest" \
  .venv/Scripts/python.exe -m pytest \
    tests/test_qa_alert_concurrency.py \
    tests/test_qa_alert_rollback_greenlet.py \
    tests/test_monthly_alert_timezone.py \
    tests/test_metering_writer.py \
    tests/test_quota.py \
    tests/test_sqlite_pragmas.py -q
..................s                                                      [100%]
18 passed, 1 skipped in 44.27s
```

选择了这几个文件，是因为它们覆盖「DB 相关 + 一次性并发写」这条主线：

| 文件 | 覆盖 |
|---|---|
| `test_qa_alert_concurrency.py` | N 个并发请求同时写告警/用量（唯一索引去重、计费不丢） |
| `test_qa_alert_rollback_greenlet.py` | warn 带内的 SAVEPOINT 回滚不污染请求会话（MissingGreenlet） |
| `test_monthly_alert_timezone.py` | 账期键按 `AIGATE_TIMEZONE` 切（跨月边界） |
| `test_metering_writer.py` | 批量写、flush、`last_used_at` 每批一次 |
| `test_quota.py` | 限流/预算 429 |
| `test_sqlite_pragmas.py` | 只在 SQLite 有意义，PG 下 `skip`（1 skipped 就是它） |

## 5. 并发写用例暴露的第二个问题：N=16 必然有一个请求池外等待

第一次在 PG 上跑时 `test_concurrent_requests_in_warn_band[16]` 失败：

```
E   AssertionError: 状态码分布：[200, 200, ..., 503, 503]
E   assert 14 == 16
WARNING aigate:main.py:200 db pool exhausted path=/v1/chat/completions:
        QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 1.00
```

池容量 15（`pool_size 5 + max_overflow 10`），而用例发 16 个并发。第 16 个请求必须排队，
排队超过 `AIGATE_DB_POOL_TIMEOUT=1s` 就是**设计好的** 503 + `Retry-After`
（`tests/test_qa_pool_fast_fail.py::test_sixteen_concurrent_never_hangs` 早就允许
「200 或快速 503」）。原用例把「全部 200」写死，在 SQLite 上只是恰好够快才绿。

改法是让断言对齐契约而不是对齐某台机器：

- 只允许 `200` 或 `503`（且 503 必须带 `Retry-After`、`error.code == gateway.pool_timeout`）；
- 用量行数 = 基线 + **200 的条数**（成功的请求一条都不许丢，被拒绝的本来就没调上游）；
- 告警行恒为 1（并发去重仍然成立）。

## 6. 结论边界

- 验证了：真 PG 上 7 条迁移可执行、15 张表落地、18 条 DB 相关用例通过（1 条 SQLite 专属跳过）。
- **没有**验证：PG 上的长期稳定性、PG 版压测的绝对数字（见 `docs/LOAD-TEST.md` 末尾：
  PG 下单请求多出约 90 ms 的原因已在 `docs/PERF-PG.md` 定位到「每次新建连接的 ~119 ms
  + 请求路径每请求多要一条连接」，但那是空库单机结果，不是生产结论）。
- 环境是 `pgserver` 的便携版 PG 16.2（Windows、非生产配置），不是生产同款；
  结论只到「schema 与 DB 语义在 PG 上成立」这一层。

## 复现

```
uv venv .pgvenv -p 3.12 && uv pip install --python .pgvenv/Scripts/python.exe pgserver
.pgvenv/Scripts/python.exe scripts/pg_local.py --pgdata .pgdata2          # 前台阻塞，记下 URL
# 另开一个 shell：
AIGATE_DATABASE_URL="postgresql+asyncpg://postgres:@127.0.0.1:<port>/aigate_pgtest" \
  .venv/Scripts/python.exe -m alembic upgrade head
AIGATE_TEST_DB_URL="postgresql+asyncpg://postgres:@127.0.0.1:<port>/aigate_pgtest" \
  .venv/Scripts/python.exe -m pytest tests/test_qa_alert_concurrency.py tests/test_quota.py -q
```