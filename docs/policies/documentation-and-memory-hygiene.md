# Documentation and Memory Hygiene

## Scope

This policy applies to any project where this policy document is present.

The policy is active when any file in the project root has a filename containing
the string `mcp.json`.

Examples of activating root filenames include:

- `.mcp.json`
- `mcp.json`
- `local.mcp.json`
- `project-mcp.json`
- `example.mcp.json.backup`

## Compact Gateway Digest

Use this short form by default to keep prompts small:

- Read relevant project documentation before meaningful work.
- Treat current implementation, tests, and active documentation as truth.
- Update documentation in the same task when behavior changes.
- Write a short checkpoint before handoff on long tasks.
- Do not rely only on chat history.

## Rule

Keep code, documentation, and durable project memory aligned with the actual
project state.

## Start Of Task

Before meaningful work:

1. Read relevant project documentation if it has not already been read in the
   current session.
2. Check for project instructions, such as:
   - `README.md`
   - `AGENTS.md`
   - `CLAUDE.md`
   - `codex.md`
   - `CONTRIBUTING.md`
   - `docs/`
   - `adr/`
   - `runbooks/`
3. Check whether persistent memory or memory-capable tools are available.
4. Load relevant project memory if available.

Do not rely only on chat history.

## During Work

When behavior, commands, architecture, configuration, dependencies, workflows,
tools, or assumptions change:

1. Update the relevant documentation in the same task.
2. For repository-specific durable knowledge, prefer a matching document under
   `docs/` and keep it as the canonical project record.
   If an appropriate doc does not yet exist, create it.  This is YOUR memory and you can manage it in the best way you yourself can utilize it in the future. 
3. Edit existing documentation in place.
4. Remove or replace stale information.
5. Avoid appending duplicate or contradictory notes.
6. Update persistent memory only when the knowledge is cross-project or a
   supplemental reminder is genuinely useful; do not use memory as the sole
   durable record for repository-specific behavior.

Documentation should describe the current truth, not the history of how the task
evolved.

## Before Context Compaction Or Handoff

Before the context window becomes too full, write a durable checkpoint to
documentation, memory, or a task log. For project-specific state, prefer a
repo-local checkpoint in `docs/` so future runs can re-read the canonical
project record without depending on chat history or external memory.

Include only:

- current goal
- completed work
- files changed
- decisions made
- commands or tests run
- known issues
- next step

After resuming, re-read the checkpoint, relevant documentation, memory, and
changed files before continuing.

## End Of Task

Before declaring work complete:

1. Check whether documentation changed or became stale.
2. Update documentation to match the final implementation.
3. Update persistent memory if available and relevant, but keep repo docs as
   the canonical record for repository-specific decisions.
4. Report what documentation and memory were updated.

Project-specific durable knowledge should always be reflected in the repo docs
before handoff. External memory may be used as a supplemental reminder, but it
is not the canonical record for this repository.

If no documentation or memory updates were needed, say so.

## Memory Rules

Store durable project knowledge only.

Good memory candidates:

- architecture decisions
- recurring commands
- project conventions
- release workflows
- tool usage
- stable constraints
- organization-specific practices

Do not store:

- secrets
- credentials
- temporary debugging notes
- speculation
- stale details
- personal data unless explicitly required

## Conflict Resolution

When sources disagree, prefer:

1. current implementation
2. tests
3. active documentation
4. configuration
5. persistent memory
6. task logs
7. chat history

Resolve known contradictions before finishing.

## Completion Standard

A task is not complete until implementation, documentation, and durable memory
are consistent.
