# Local Quickstart

Use this path when you want to try Steel Mission without a cloud model dependency.

## Start

```bash
git clone https://github.com/Present-Capable-Computing/Steel-Mission.git
cd Steel-Mission
python3 -m pip install -r requirements-dev.txt
bin/steel-mission doctor
bin/steel-mission serve
```

Open `http://127.0.0.1:8765/`.

The server keeps writable configuration in
`${XDG_STATE_HOME:-$HOME/.local/state}/steel-mission/config`, not in the cloned
repository. Set `STEEL_MISSION_STATE_DIR` to relocate this configuration state,
or set `STEEL_MISSION_CONFIG_DIR` when an installation provides a complete
configuration directory. Delete the runtime `config` directory to reseed the
starter configuration on the next launch.

## Docker Starter Company

With host Ollama running and the Claude/Codex CLIs already authenticated:

```bash
docker compose build steel-mission
docker compose up -d steel-mission
docker compose ps
```

This starts the packaged Steel Mission company and binds Claude to planning
and acceptance assessment, the local `qwen2.5-coder:14b` model to coding, and
Codex to read-only review. Deterministic verification remains the only phase
that can issue PASS.

## Optional Local Model

```bash
ollama pull qwen2.5-coder:14b
bin/present-worker glimmer start
bin/present-worker glimmer status
STEEL_MISSION_RUNTIME_PROFILE=dc13.local bin/steel-mission serve
```

## Hardware

- 8 GB RAM for core-only evaluation;
- 16 GB RAM minimum for local 14B model mode;
- 24-32 GB RAM recommended for smoother local model work.

## What To Try

- open Settings;
- inspect the Steel Mission launch organization and founder-led team;
- review KD01-KD03 knowledge domains;
- review DC01-DC13 Domain Capabilities;
- inspect PRJ-0001 Durable Core and its six epics;
- start a mission in DC13 Delivery Coordinator mode;
- inspect the generated mission evidence.
