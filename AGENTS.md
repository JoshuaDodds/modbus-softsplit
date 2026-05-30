This repository uses the Documentation and Memory Hygiene policy in
[`docs/policies/documentation-and-memory-hygiene.md`](docs/policies/documentation-and-memory-hygiene.md).

When a project has a root `.mcp.json` of `mcp.json` file, treat that policy as active.

Read before meaningful work:

- `AGENTS.md`
- `docs/policies/documentation-and-memory-hygiene.md`
- relevant project docs such as `README.md`, `docs/`, `docs/adr/`, `docs/archive/`, and `runbooks/`

Follow the policy when updating code, docs, configuration, or durable memory.

For repository-specific durable knowledge, write or update the matching
document under `docs/` in the same task and treat it as the canonical record. If an appropriate doc does not yet exist, create it.  This is YOUR memory and you can manage it in the best way you yourself can utilize it in the future. 
Use external memory only as a supplemental reminder, not as the sole source of
truth for this repository. 

Extra tools, skills, personas, roles, and access to third party platforms is available to you via the MCP servers listed in `*mcp.json` file in the project root. If a connector is not configured it is probably exposed to you via these MCP servers. Use them.
