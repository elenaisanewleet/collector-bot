# collector-bot — developer commands.
#
# Everything runs against the local virtualenv in .venv. `make install` creates
# it; the remaining targets assume it exists.

PYTHON ?= python3.12
VENV   := .venv
BIN    := $(VENV)/bin
PY     := $(BIN)/python

.DEFAULT_GOAL := help
# Боевое развёртывание идёт по СВОЕМУ файлу compose: в нём есть Caddy с TLS, а
# веб-порт наружу не публикуется. Забыть `-f` значит поднять локальную сборку
# рядом с боевой — без прокси и с портом в мир.
COMPOSE_PROD := docker compose -f docker-compose.prod.yml

.PHONY: help install venv run demo seed migrate migration test lint format typecheck check clean docker-build docker-up docker-down deploy prod-ps prod-logs prod-down

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

# ---------------------------------------------------------------- прод
#
# Зачем эти цели вообще. Код живёт В ОБРАЗЕ, а не в рабочем каталоге: `git pull`
# без пересборки не меняет на проде ничего, и выкладка выглядит успешной, не
# будучи ею. Проверено дорогой ценой — владелец обновил сервер, а бот продолжал
# отвечать старым кодом.
#
# `git pull` здесь намеренно НЕ делается. На сервере правятся карты полей в
# `config/field_maps/`, они под контролем версий, и pull может упереться в
# конфликт — его надо увидеть, а не проглотить внутри цели.

deploy:  ## Пересобрать образ и поднять прод (git pull делается ДО этого, руками)
	GIT_COMMIT=$$(git rev-parse --short HEAD) $(COMPOSE_PROD) build
	$(COMPOSE_PROD) up -d
	@$(MAKE) --no-print-directory prod-ps

# Вывод СРАВНИВАЕТ версии сам, а не просит сравнить глазами. Строка
# «Расходятся — значит…» печаталась безусловно, и владелец прочитал её дважды
# при совпадающих версиях: проверка выкладки, которая предупреждает об ошибке,
# когда ошибки нет, обесценивает собственное предупреждение.
prod-ps:  ## Что запущено на проде: версия ОБРАЗА против версии каталога
	@$(COMPOSE_PROD) ps
	@echo
	@tree=$$(git rev-parse --short HEAD); \
	image=$$($(COMPOSE_PROD) exec -T bot printenv APP_REVISION 2>/dev/null | tr -d '\r\n'); \
	echo "в каталоге:   $$tree $$(git log -1 --format=%s)"; \
	echo "в контейнере: $${image:-(не отвечает)}"; \
	echo; \
	if [ -z "$$image" ]; then \
		echo "Контейнер не ответил: он не запущен или собран до появления метки версии."; \
	elif [ "$$image" = "$$tree" ]; then \
		echo "Совпадают: запущен тот код, что в каталоге."; \
	else \
		echo "РАСХОЖДЕНИЕ: git pull прошёл, а пересборка нет. Нужен make deploy."; \
	fi

prod-logs:  ## Последние строки лога бота: make prod-logs n=100
	$(COMPOSE_PROD) logs --tail=$(or $(n),40) bot

prod-down:  ## Остановить прод целиком
	$(COMPOSE_PROD) down
