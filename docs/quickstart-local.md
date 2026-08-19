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
- inspect the Northstar Forge starter organization;
- review KD01-KD03 knowledge domains;
- review DC01-DC13 Domain Capabilities;
- start a mission in DC13 Delivery Coordinator mode;
- inspect the generated mission evidence.
