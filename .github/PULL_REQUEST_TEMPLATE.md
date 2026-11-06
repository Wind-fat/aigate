## 改了什么

<!-- 一句话。 -->

## 为什么

<!-- 不写的话评审只能猜。 -->

## 怎么验证的

```
# 贴真实输出，不贴"应该能过"
uv run pytest -q          # x passed
bash scripts/smoke.sh     # 23 passed, 0 failed
```

## 检查

- [ ] 新行为有测试；修 bug 的测试在修之前是红的
- [ ] 改了账务口径 → 同步 `docs/runbook.md`
- [ ] 改了架构取舍 → 新增 `docs/adr/000N-*.md`
- [ ] 前端改动跑过 `npx tsc --noEmit && npm run build`
- [ ] 部署文件改动跑过 `scripts/check_deploy.py`
- [ ] 没有提交 `.env*` / `*.db` / `console/.env.local`
