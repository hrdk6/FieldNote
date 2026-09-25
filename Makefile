# FieldNote developer tasks. `make demo` works with no API keys.
PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
endif
FN := $(BIN)/fieldnote
WS ?= ev_two_wheelers_india

.PHONY: help install dev demo web web-dev dashboard dashboard-demo run run-offline doctor eval eval-readme test cov lint format typecheck check fixtures docker-build docker-up clean

help:
	@echo "install       create .venv and install FieldNote"
	@echo "dev           install with dev tools and pre-commit hooks"
	@echo "demo          run everything on fixtures with the MockClient (no keys needed)"
	@echo "dashboard     launch the dashboard on http://localhost:8501"
	@echo "web           rebuild the dashboard UI (needs Node 20+)"
	@echo "web-dev       UI dev server with hot reload (API on :8502 via \`fieldnote dashboard --port 8502\`)"
	@echo "run           live run for WS=$(WS) (dry-run delivery unless configured)"
	@echo "run-offline   offline run for WS=$(WS) on fixtures"
	@echo "doctor        validate configs, credentials and sources"
	@echo "eval          run evals and write out/eval_report.md"
	@echo "eval-readme   run evals on the demo data and update the README table"
	@echo "test | cov | lint | format | typecheck | check"

$(VENV):
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip

install: $(VENV)
	$(BIN)/pip install -e .

dev: $(VENV)
	$(BIN)/pip install -e ".[dev]"
	$(BIN)/pre-commit install || true

demo: install
	$(FN) demo

web:
	cd web && npm ci && npm run build

web-dev:
	cd web && npm run dev

dashboard: install
	$(FN) dashboard

dashboard-demo: install
	$(FN) dashboard --demo

run: install
	$(FN) run -w $(WS)

run-offline: install
	$(FN) run -w $(WS) --offline --dry-run --weekly

doctor: install
	$(FN) doctor

eval: install
	$(FN) eval --all

eval-readme: install
	$(FN) demo --update-readme

test:
	$(BIN)/pytest

cov:
	$(BIN)/pytest --cov=fieldnote --cov-report=term --cov-report=xml

lint:
	$(BIN)/ruff check src tests scripts
	$(BIN)/ruff format --check src tests scripts

format:
	$(BIN)/ruff format src tests scripts
	$(BIN)/ruff check --fix src tests scripts

typecheck:
	$(BIN)/mypy

check: lint typecheck test

fixtures:
	$(BIN)/python scripts/build_fixtures.py

docker-build:
	docker build -t fieldnote:latest .

docker-up:
	docker compose up --build

clean:
	rm -rf out data .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov build dist *.egg-info
