.PHONY: help install run seed test lint fmt verify clean

help:
	@echo "make install   - 安装依赖 (uv sync)"
	@echo "make run       - 启动网关 :8080"
	@echo "make seed      - 灌入示例数据并打印 demo key"
	@echo "make test      - 跑测试"
	@echo "make lint      - ruff 静态检查"
	@echo "make verify    - 一键验收: lint + test"

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

verify: lint test

clean:
	rm -f aigate.db && rm -rf .pytest_cache .ruff_cache