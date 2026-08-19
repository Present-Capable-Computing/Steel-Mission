.PHONY: install-dev doctor test run local claude codex glimmer-start glimmer-status glimmer-stop release-check

PYTHON ?= python3
HOST ?= 127.0.0.1
PORT ?= 8765
PROFILE ?= dc13.local

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

release-check:
	git diff --check
	$(PYTHON) -m py_compile steel-mission-chat/server.py bin/present-worker bin/present-control-plane bin/present-evidence-signer
	$(PYTHON) -m pytest
