# 评测数据集（Stage 4 接入执行器）

三个层级，跑在不同时机。用 YAML 而不是代码，因为评测集是**资产**，要能被人 review。

- `smoke.yaml` — 20 条，每次提交跑，秒级
- `regression.yaml` — 200 条，合并前跑，出对比报告
- `redteam.yaml` — 40 条，注入 / 越权 / 泄漏

用例字段：

```yaml
- id: string          # 稳定 id，报告里用它做 diff
  intent: string      # 这条在测什么能力，失败时一眼看出退化点
  messages: [...]     # 直接喂给 /v1/chat/completions
  assert:
    contains: [...]       # 必须包含
    not_contains: [...]   # 必须不包含
    must_refuse: true     # 无答案场景必须拒答
    json_schema: {...}    # 结构化输出必须通过 schema
  judge: string|None    # 语义断言的 rubric，交给 LLM-as-judge
```

**原则**：能写确定性断言的，绝不交给 judge。judge 只用于"回答是否忠实"这类无法规则化的判断，
且必须报告 judge 与人工标注的一致性（Cohen's kappa）。