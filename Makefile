# Cyanobacteria Detection System Digital Twin: developer commands.
# POSIX shell + make. Windows users: run under WSL.
SHELL := /bin/sh

ENV_FILE := $(wildcard .env)
RUN := uv run $(if $(ENV_FILE),--env-file .env,)

API_PORT ?= 8000
DASH_PORT ?= 8501
API_URL ?= http://localhost:$(API_PORT)
DEMO_DB := sqlite:///data/demo.db
DEMO_DAYS ?= 60

.PHONY: setup setup-ml test test-e2e lint fmt api sim dash demo stop reset-db reproduce help

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

setup: ## install core + dev dependencies into .venv (uv); create .env from .env.example if missing
	uv sync
	@test -f .env || { cp .env.example .env; echo "created .env from .env.example (edit API_KEY before exposing the API)"; }

setup-ml: ## also install the optional ML extra (scikit-learn, LightGBM, SHAP, joblib, openpyxl)
	uv sync --extra ml

test: ## unit tests (excludes the e2e marker)
	$(RUN) pytest -q -m "not e2e"

test-e2e: ## end-to-end tests (start a real API + simulator)
	$(RUN) pytest -q -m e2e tests/e2e

lint: ## ruff check + ruff format --check
	$(RUN) ruff check .
	$(RUN) ruff format --check .

fmt: ## ruff format + autofix
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

api: ## run the FastAPI server (foreground) on API_PORT
	$(RUN) uvicorn api.app:create_app --factory --host 127.0.0.1 --port $(API_PORT)

sim: ## run the simulator: make sim ARGS="--scenario baseline --days 30"
	$(RUN) python -m sim.run $(ARGS)

dash: ## run the Streamlit dashboard (foreground) on DASH_PORT
	$(RUN) streamlit run dashboard/app.py --server.port $(DASH_PORT) --server.headless true

reset-db: ## recreate empty tables in the database behind DB_URL (default sqlite:///data/twin.db)
	$(RUN) python -m db.reset

reproduce: ## rebuild ML reports (Phase 4+)
	@echo "not built yet"

demo: ## one-command demo: fresh demo DB, API in background, summer_bloom backfill, dashboard
	@$(MAKE) stop
	@if curl -sf $(API_URL)/health > /dev/null 2>&1; then \
	  echo "something already answers on $(API_URL); stop it or run with API_PORT=<other>"; exit 1; fi
	@mkdir -p .run data
	@DB_URL=$(DEMO_DB) $(RUN) python -m db.reset
	@echo "starting API on port $(API_PORT) (log: .run/api.log) ..."
	@DB_URL=$(DEMO_DB) nohup $(RUN) uvicorn api.app:create_app --factory --host 127.0.0.1 --port $(API_PORT) > .run/api.log 2>&1 & echo $$! > .run/api.pid
	@i=0; until curl -sf $(API_URL)/health > /dev/null 2>&1; do \
	  i=$$((i+1)); if [ $$i -gt 60 ]; then echo "API did not start; see .run/api.log"; $(MAKE) stop; exit 1; fi; sleep 0.5; done
	@echo "API healthy. Backfilling summer_bloom ($(DEMO_DAYS) days, seed 1, dense labels) ..."
	@$(RUN) python -m sim.run --scenario summer_bloom --days $(DEMO_DAYS) --seed 1 --dense-labels --api-url $(API_URL)
	@echo "starting dashboard on port $(DASH_PORT) (log: .run/dash.log) ..."
	@API_URL=$(API_URL) nohup $(RUN) streamlit run dashboard/app.py --server.port $(DASH_PORT) --server.headless true > .run/dash.log 2>&1 & echo $$! > .run/dash.pid
	@echo ""
	@echo "API:       $(API_URL)/docs"
	@echo "Dashboard: http://localhost:$(DASH_PORT)"
	@echo "Stop with: make stop"

stop: ## stop the demo API and dashboard (safe when nothing is running)
	@for name in api dash; do \
	  if [ -f .run/$$name.pid ]; then \
	    pid=$$(cat .run/$$name.pid); \
	    if kill -0 $$pid 2>/dev/null; then \
	      kids=$$(pgrep -P $$pid); grandkids=""; for k in $$kids; do grandkids="$$grandkids $$(pgrep -P $$k)"; done; \
	      kill $$pid $$kids $$grandkids 2>/dev/null; \
	      for n in 1 2 3 4 5 6 7 8 9 10; do kill -0 $$pid 2>/dev/null || break; sleep 0.5; done; \
	      kill -9 $$pid $$kids $$grandkids 2>/dev/null; \
	      echo "stopped $$name (pid $$pid)"; \
	    fi; \
	    rm -f .run/$$name.pid; \
	  fi; \
	done
