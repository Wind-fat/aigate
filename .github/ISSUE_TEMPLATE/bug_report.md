---
name: Bug 报告
about: 有东西和行为不符（先看 docs/runbook.md，一半的问题那里面就有答案）
labels: bug
---

## 发生了什么

<!-- 一句话。不要写"不能用了"，写"401 之后明明带了 key 还是 401"。 -->

## 期望什么

## 复现步骤

```bash
# 最小可复现命令。能贴 curl 就别描述。
```

## 现场证据

```
# 1) 健康检查
curl -s localhost:8080/health

# 2) 相关 trace（如果是请求问题，request_id 是最有用的东西）
curl -s -H "X-Admin-Token: ***" localhost:8080/admin/v1/traces/<request_id>

# 3) 网关日志尾部
docker compose logs --tail 50 gateway   # 或本地开发的终端输出
```

## 环境

- 部署方式：本机 `up.sh` / docker compose / 其他
- 网关版本：`curl -s localhost:8080/health | jq .version`
- 是否能在 `mock` provider 下复现（是的话大概率与上游无关）：
