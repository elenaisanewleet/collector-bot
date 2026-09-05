# collector-bot — developer commands.
#
# Everything runs against the local virtualenv in .venv. `make install` creates
# it; the remaining targets assume it exists.

PYTHON ?= python3.12
VENV   := .venv
BIN    := $(VENV)/bin
PY     := $(BIN)/python

.DEFAULT_GOAL := help
.PHONY: help install venv run demo seed onec-doctor migrate migration test lint format typecheck check clean docker-build docker-up docker-down

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

$(VENV):
	$(PYTHON) -m venv $(VENV)

venv: $(VENV)  ## Create the virtualenv

install: venv  ## Install the project and its dev dependencies
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e '.[dev]'
	@test -f .env || cp .env.example .env
	@echo "Ready. Edit .env, then: make demo"

run:  ## Start the Telegram bot (needs TELEGRAM_BOT_TOKEN and ALLOWED_TELEGRAM_USER_IDS)
	$(PY) -m app.main

demo:  ## Run the full pipeline in the terminal — no credentials needed
	APP_MODE=demo $(PY) -m app.main demo

seed:  ## Load data/demo_debtors.csv into the local database
	$(PY) scripts/seed_demo.py

onec-doctor:  ## Inventory the customer's published 1С OData and check ONEC_FIELD_MAP
	$(PY) scripts/onec_doctor.py

migrate:  ## Apply database migrations
	$(BIN)/alembic upgrade head

migration:  ## Create a migration: make migration m="add x"
	$(BIN)/alembic revision --autogenerate -m "$(m)"

test:  ## Run the test suite
	$(BIN)/pytest

lint:  ## Check style and formatting
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:  ## Apply autofixes and formatting
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:  ## Run mypy in strict mode
	$(BIN)/mypy

check: lint typecheck test  ## Everything CI runs

clean:  ## Remove caches and the local database
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -f collector_bot.db

docker-build:  ## Build the container image
	docker compose build

docker-up:  ## Start the bot in Docker
	docker compose up -d

docker-down:  ## Stop the containers
	docker compose down
