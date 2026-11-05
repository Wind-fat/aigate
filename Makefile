.PHONY: help up setup install run seed test lint fmt bench smoke eval verify clean eval

help:
	@echo "make up        - 一键起网关+控制台（首次自动 setup）"
	@echo "make setup     - 生成 .env / console/.env.local 并灌示例数据"
	@echo "make install   - 安装依赖 (uv sync)"
	@echo "make run       - 启动网关 :8080"
	@echo "make seed      - 灌入示例数据并打印 demo key"
	@echo "make test      - 跑测试"
	@echo "make lint      - ruff 静态检查"
	@echo "make verify    - 一键验收: lint + test"

up:
	./scripts/up.sh

setup:
	uv run python scripts/setup.py

install:
	uv sync --all-groups

run:
	uv run uvicorn aigate.main:app --reload --port 8080

seed:
	uv run python scripts/seed.py

test:
	uv run pytest -q

lint:
	uv run ruff check src tests scripts

fmt:
	uv run ruff format src tests scripts

bench:
	uv run python scripts/bench_cache.py

smoke:
	bash scripts/smoke.sh

# make eval              -> smoke
# make eval DATASET=regression
eval:
	uv run python evals/run.py $(or $(DATASET),smoke)

verify: lint test

clean:
	rm -f aigate.db && rm -rf .pytest_cache .ruff_cache