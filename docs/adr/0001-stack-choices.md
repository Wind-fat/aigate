# ADR-0001 技术选型

- 状态：已采纳
- 日期：2026-11

## 决策

| 层 | 选型 | 被拒方案 | 理由 |
|---|---|---|---|
| 后端 | FastAPI + SQLAlchemy 2.0 async | Django / Flask | 网关是 IO 密集（等待上游），async 是刚需；Django 的 ORM 同步模型会逼你上线程池 |
| 数据库 | SQLite（dev/CI）→ PostgreSQL（prod） | 只支持 PG | 让 `git clone && make test` 零依赖可跑，评审者不会卡在装数据库上；同一套 ORM 代码，只换 URL |
| 向量库 | pgvector（Stage 3） | Chroma / FAISS / Milvus / Qdrant | 语义缓存只有万级向量，且要按 `tenant + model + prompt_version` 过滤——这正是 PG 的强项。少一个组件就少一次数据不一致 |
| 前端 | Next.js（Stage 3） | Streamlit | Streamlit 三天能出图，但作品集截图会被读成"脚本"。控制台要展示的是产品判断，值得多花一周 |
| 部署 | Docker Compose | K8s | 单机规模下引入编排，会让运维成本超过收益；多副本的第一道坎是配额与缓存一致性，不是编排 |

## 后果

- SQLite 无 `pgvector`：向量检索通过 `VectorStore` 接口抽象，SQLite 走暴力搜索，PG 走索引。测试覆盖接口而不覆盖实现细节。
- 无 Alembic 起步：`create_all` 保证 MVP 能跑；表结构稳定后引入迁移（Stage 2），此时 `AIGATE_AUTO_CREATE_SCHEMA=false`。
- Next.js 延后到 Stage 3：前两周所有验证靠 curl + pytest，保证"能跑"优先于"好看"。