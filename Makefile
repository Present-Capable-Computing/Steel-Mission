.PHONY: install-dev doctor test run local claude codex glimmer-start glimmer-status glimmer-stop docker-build docker-up docker-down docker-status private-runner-image private-runner-status ui-build ui-check ui-test style-check compile-entrypoints release-check plan-check plan-sync

PYTHON ?= python3
HOST ?= 127.0.0.1
PORT ?= 8765
PROFILE ?= dc13.local
PYTHON_ENTRYPOINTS := steel-mission-chat/server.py bin/present-worker bin/present-control-plane bin/present-evidence-signer bin/present-private-runner bin/present-lease-broker

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

doctor:
	bin/steel-mission doctor

test:
	$(PYTHON) -m pytest

run:
	STEEL_MISSION_HOST=$(HOST) STEEL_MISSION_PORT=$(PORT) STEEL_MISSION_RUNTIME_PROFILE=$(PROFILE) bin/steel-mission serve

local:
	$(MAKE) run PROFILE=dc13.local

claude:
	$(MAKE) run PROFILE=dc13.claude

codex:
	bin/present-worker status
	codex login status

glimmer-start:
	bin/present-worker glimmer start

glimmer-status:
	bin/present-worker glimmer status

glimmer-stop:
	bin/present-worker glimmer stop

docker-build:
	docker compose build steel-mission

docker-up:
	docker compose up -d steel-mission

docker-down:
	docker compose down

docker-status:
	docker compose ps

private-runner-image:
	docker build -f Dockerfile.private-runner -t steel-mission-private-runner:alpha .

private-runner-status:
	PRESENT_PRIVATE_RUNNER_MODE=docker bin/present-private-runner status

ui-build:
	npm run ui:typecheck
	npm run ui:build

ui-check:
	npm run ui:typecheck
	npm run ui:check

ui-test:
	npm run ui:test

plan-check:
	$(PYTHON) -m pytest tests/test_plan_records.py -q
	$(PYTHON) tooling/gh-plan-sync.py --dry-run
	$(PYTHON) tooling/gh-project-fields.py --dry-run

plan-sync:
	$(PYTHON) tooling/gh-plan-sync.py
	$(PYTHON) tooling/gh-project-fields.py

style-check:
	@command -v vale >/dev/null 2>&1 || { echo "vale is not installed. Install it (macOS: brew install vale) and re-run; the engineering-style CI job runs this same target."; exit 1; }
	vale sync
	vale --minAlertLevel=error .
	@! git grep -n -e '—' -- '*.py' || { echo "em dash in Python source; the ban covers whole files, docstrings included"; exit 1; }

compile-entrypoints:
	$(PYTHON) -m py_compile $(PYTHON_ENTRYPOINTS)

release-check:
	git diff --check
	$(MAKE) style-check
	$(MAKE) compile-entrypoints PYTHON=$(PYTHON)
	$(PYTHON) -m pytest
