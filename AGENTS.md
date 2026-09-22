<!-- loom:v2 -->
## LOOM

This project uses LOOM as Agent-only continuity infrastructure. Run `loom context` when entering the
project, after a context reset, before substantial work, and when the human gives feedback or a direction
change. When a Task is active, the command restores its execution protocol, exact state, and declared
context; rerun it before editing after any interruption. Keep project truth, design documents, professional
capability dossiers, and Tasks current through the CLI, and never ask the human to operate LOOM. Do not
rerun LOOM before every tool call: persist and restore at meaningful work boundaries. The installed
`loom` command is the stable project-local invocation. If it is not on PATH in a source checkout, use the
absolute path to that checkout's `cli/bin/loom.js`; do not guess a relative path from this project.

## Devin Notification Rule (STRICT)

Every Devin task completion MUST send a Feishu notification. This is not optional.

Required steps:
1. Write completion JSON to `/home/haa/.skillhome/notifications/latest.json`
2. Execute: `export SKILLHOME_SOURCE={assistant|majordomo} && skillhome-notify <task_type> <status> "<summary>" <duration_seconds>`
3. If skillhome-notify fails, fallback to: `python3 ~/.skillhome/bin/skillhome.py --source <source> --no-interactive notify --task-type <task_type> --status <status> --summary "<summary>" --duration-seconds <duration>`

Do not skip step 2. Writing JSON alone is not notification completion.
