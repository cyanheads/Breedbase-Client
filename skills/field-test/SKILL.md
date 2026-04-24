---
name: field-test
description: >
  Exercise the Breedbase-Client MCP servers (main HTTP + PSA STDIO) through the
  FastMCP client. Surfaces the tool catalog, runs real and adversarial inputs
  against a live BrAPI server, and produces a tight report with concrete findings
  and numbered follow-up options. Use after adding or modifying tools, or when
  the user asks to test, try out, or verify the MCP surface.
metadata:
  author: cyanheads
  version: "1.0"
  audience: internal
  type: debug
---

## Context

Unit tests (`tests/mcp.py`) verify handler logic. Field testing exercises the real transport against a live BrAPI server: spins up the server, calls `list_tools`, invokes each tool, and checks what a client actually sees. It catches what unit tests miss — awkward input shapes, unhelpful errors, `structuredContent`↔`content[]` drift, pagination surprises, upstream failure handling.

**Actively call the tools. Don't read code and guess.**

**Two servers, two transports:**

| Target | Server | Transport | Start step needed? |
|:---|:---|:---|:---|
| `--target http` | Main (`src/main.py`) | HTTP @ `127.0.0.1:8000/mcp/` | Yes — daemon |
| `--target stdio-main` | Main (`src/main.py`) | STDIO, spawned per call | No |
| `--target stdio-psa` | PSA (`src/psa/main.py`) | STDIO, spawned per call | No |

All three default to `BASE_URL=https://test-server.brapi.org/brapi/v2` (the no-auth BrAPI test server). Pass `BRAPI_BASE_URL` / `BASE_URL` in the env to override.

---

## Steps

### 1. Start the HTTP server (only for `--target http`)

The main server takes ~5–10s to bind. Poll `/health` until it returns 200. Write a bash helper once and source it in every subsequent Bash call — state lives in `/tmp/brapi-ft.env` so PID/URL survive across tool invocations.

```bash
cat > /tmp/brapi-ft.sh <<'HELPER_EOF'
#!/bin/bash
# Breedbase-Client field-test helper: manage the HTTP server lifecycle.
STATE_FILE="/tmp/brapi-ft.env"
[ -f "$STATE_FILE" ] && . "$STATE_FILE"

ft_start_http() {
  local repo="${1:-$PWD}"
  local port="${PORT:-8000}"
  local base_url="${BASE_URL:-https://test-server.brapi.org/brapi/v2}"
  local name="${NAME:-ft_run}"
  echo "starting main server (HTTP, port=$port, base_url=$base_url) ..."
  (cd "$repo" && MODE=http PORT=$port BASE_URL="$base_url" NAME="$name" uv run src/main.py) \
    >/tmp/brapi-ft-server.log 2>&1 &
  local pid=$!
  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      cat > "$STATE_FILE" <<EOF
export FT_PID=$pid
export FT_PORT=$port
export FT_REPO=$repo
export MCP_URL=http://127.0.0.1:$port/mcp/
EOF
      . "$STATE_FILE"
      echo "ready pid=$pid mcp=$MCP_URL"
      return 0
    fi
    sleep 0.5
  done
  echo "server failed to become ready — see /tmp/brapi-ft-server.log"
  kill "$pid" 2>/dev/null
  return 1
}

ft_stop_http() {
  [ -n "$FT_PID" ] && kill "$FT_PID" 2>/dev/null
  rm -f "$STATE_FILE"
  echo "stopped"
}
HELPER_EOF

. /tmp/brapi-ft.sh
ft_start_http /Users/casey/Developer/github/Breedbase-Client
```

**Notes**

- If port 8000 is already in use (`lsof -i :8000`), confirm with the user before killing it — it may be their own dev session. Set `PORT=8001` etc. to pick a different port.
- If startup logs show upstream warnings (e.g. SSL, websockets deprecation), those are noise — look for `Uvicorn running on http://...`.
- Skip this step entirely for STDIO targets; the harness spawns the server per call.

### 2. Surface the catalog

```bash
uv run python skills/field-test/harness.py --target http catalog
# or: --target stdio-psa, --target stdio-main
```

Prints tools, resources, and prompts as a markdown list. Present a compact catalog to the user: each tool's name + 1-line description. Flag vague or missing descriptions as you go — those feed into the report.

### 3. Plan the test pass

**Budget.** Don't run every category against every tool — the cross-product is infeasible. Apply the **universal battery** to everything; apply **situational categories** only when triggered.

**Universal battery — run on every tool**

| Category | What to verify |
|:---------|:---------------|
| Happy path | One realistic input. Output shape matches schema. `content[]` text reads clearly. |
| `structuredContent` ↔ `content[]` parity | Every field in `structuredContent` is surfaced in the text. Parity gap = client-specific blindness. |
| Input error | One invalid input (wrong type or missing required). Error text says *what*, *why*, *how to fix*. |

**Situational — add only when triggered**

| Trigger | Add category |
|:--------|:-------------|
| `page` / `pageSize` / `offset` / `limit` / cursor params | Pagination: second page, end-of-list |
| Array return with `query` / `filter` / search params | Empty result: does response explain *why* (echo criteria, suggest broadening)? |
| Chained tools (e.g. `brapi_search` → `brapi_get` → `load_result` → `quick_download_link`) | Run one representative chain end-to-end; does each step return the IDs/cursors the next needs? |
| Tool writes to local filesystem (downloads, cache) | Confirm file lands where expected; check cache cleanup doesn't remove in-progress work |
| Tool requires auth (`BRAPI_USERNAME`/`PASSWORD` set) | Note `skipped — requires SGN credentials` if running against test server |
| `describe_server_capabilities` drives other tool choices | Call it first, then pick a real `service` name from its output for `brapi_get` tests |

**BrAPI-specific reality: every tool is an external-API call.** The `.env` points at `test-server.brapi.org` (public, no auth) by default. Upstream rate-limits or transient 5xx from BrAPI are a real signal — note them, don't retry-loop.

**Auth.** If a tool requires SGN credentials and they're not set, note `skipped — requires $BRAPI_USERNAME/$BRAPI_PASSWORD` and move on. Don't fabricate inputs.

### 4. Execute

Use `TaskCreate` — one task per tool. Mark complete as you go. Don't batch.

```bash
# Happy path
uv run python skills/field-test/harness.py --target http call brapi_get '{"service":"studies","page_size":2}'

# Input error (missing required)
uv run python skills/field-test/harness.py --target http call brapi_get '{}'

# Describe a tool's schema
uv run python skills/field-test/harness.py --target http describe brapi_get
```

**Interpreting responses**

- `{"raised": false, "is_error": false, ...}` — clean success.
- `{"raised": false, "is_error": true, ...}` — tool-domain error (e.g. upstream 404, validation rejected by handler). Inspect `content[]` text for quality.
- `{"raised": true, ...}` — protocol/schema error (e.g. missing required arg caught by pydantic before the handler runs). `error_type` + `error` describe it.

For each call, capture: input sent, whether it raised or errored, and anything surprising (slow response, parity drift, unhelpful text, crash). Trim huge payloads — just the first item plus the total count is usually enough.

### 5. Tear down

```bash
. /tmp/brapi-ft.sh
ft_stop_http
```

Kills the background HTTP server, clears state. STDIO targets have nothing to clean up. Do this *before* writing the report so nothing leaks into the next session.

### 6. Report

Three sections. Tight. The user should be able to skim the summary, read details only for what matters, and act on numbered options.

#### Summary (1 paragraph)

One paragraph. How many tools exercised across which targets, how many passed clean, how many have issues, and the single most important finding. No tables, no lists.

#### Findings

Only include tools with issues. Group by severity. Each finding is 2–4 lines unless it genuinely needs more.

| Severity | Meaning |
|:---------|:--------|
| **bug** | Broken: crash, wrong output, `is_error: true` on valid input, data loss, schema violation |
| **ux** | Works but degrades the user/LLM experience: vague description, unhelpful error text, parity drift, description-vs-behavior mismatch |
| **nit** | Polish: phrasing, inconsistent tone, minor doc gaps |

Format:

```
**<tool_name> — <bug|ux|nit>**
Input: `<short input>` → <what happened>
Expected: <what should happen>
Fix: <one sentence>
```

#### Options

Numbered, actionable, cherry-pickable. Each item maps to a concrete change.

```
1. Fix empty-result message in `brapi_search` — echo criteria (finding #2)
2. Tighten `service` description in `brapi_get` — list valid values from capabilities (finding #5)
3. Surface upstream 5xx cleanly in `download_images` — currently dumps traceback (finding #8)
```

End with:

> Pick by number (e.g. "do 1, 3, 5" or "expand on 2").

---

## Checklist

- [ ] HTTP server started (if testing `--target http`); `/health` returned 200
- [ ] Catalog surfaced and presented (tools, resources, prompts)
- [ ] Universal battery run on every tool
- [ ] Situational categories applied only when triggered
- [ ] Auth-gated tools explicitly handled (run, skip, or confirm)
- [ ] HTTP server stopped; `/tmp/brapi-ft.env` removed
- [ ] Report: summary paragraph → grouped findings → numbered options
