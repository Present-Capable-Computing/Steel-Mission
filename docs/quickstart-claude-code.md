# Claude Code Quickstart

Use this path when you want Steel Mission's Delivery Coordinator to bind to Claude Code.

## Requirements

- Steel Mission repository cloned locally;
- Python 3.11 or newer;
- Git;
- Claude Code CLI installed;
- Claude Code authenticated.

## Verify Claude Code

```bash
claude auth status
```

## Start Steel Mission With Claude

```bash
STEEL_MISSION_RUNTIME_PROFILE=dc13.claude bin/steel-mission serve
```

Open `http://127.0.0.1:8765/`.

## Role Boundary

Claude is a model/provider binding. The organizational capability remains DC13 Delivery Coordinator. Switching from Claude to another provider creates a different runtime instance of the same capability; it does not create a new role.

## Notes

- Claude Code access and rate limits are controlled outside Steel Mission.
- If Claude is unavailable, use `dc13.auto` or `dc13.local` when a local model is ready.
- Mission execution still enters through the guarded control-plane boundary.
