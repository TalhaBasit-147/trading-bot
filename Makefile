.PHONY: install dev test lint fmt backtest paper live train db clean

install:
	pip install -r requirements.txt

dev:
	pip install -r requirements-dev.txt

test:
	pytest -v --cov=app --cov-report=term-missing

lint:
	ruff check app tests

fmt:
	ruff check --fix app tests
	ruff format app tests

db:
	python scripts/init_db.py

backtest:
	python scripts/run_backtest.py

paper:
	python scripts/run_paper.py

live:
	python scripts/run_live.py

train:
	python scripts/train_model.py

clean:
	rm -rf .pytest_cache __pycache__ .ruff_cache htmlcov .coverage
	find . -name __pycache__ -type d -exec rm -rf {} +
