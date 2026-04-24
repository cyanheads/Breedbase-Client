# Field Test Findings

Ran the field-test skill against `test-server.brapi.org/brapi/v2` — 22 tools across both servers (10 main via HTTP, 12 PSA via STDIO). Nits, noise, and nice-to-haves dropped; only the concrete issues worth fixing below.

## Bugs

### PSA `structured_content` is stringified, not structured

Every PSA tool is annotated `-> str` and returns `json.dumps(data, indent=2)` (e.g. `src/psa/tools/studies.py:25,71`; `src/psa/tools/discovery.py:19,42,76,112`; `src/psa/tools/germplasm.py:22,61,86`; `src/psa/tools/observations.py:64,111,154`). FastMCP sees a `str` return, auto-wraps it as `{"result": "<string>"}` in `structured_content`, and any client reading `structuredContent` per the MCP spec has to `json.loads` a nested string to use the data.

**Fix:** change the return type annotations to `list` / `dict` and return the raw objects — FastMCP serializes them correctly. Drop the `json.dumps(...)` wrapping.

### PSA upstream 404 raises unhandled `ToolError`

`src/psa/client.py:79` calls `resp.raise_for_status()` and only catches `InvalidTokenError`; every other HTTP error propagates. No PSA tool wraps the call. Result: a bad id (`get_study_details {"study_db_id":"bogus"}`, `get_germplasm_by_id`, `download_study`) surfaces to the client as `raised: true` `ToolError` with a Rich traceback on stderr. The main server handles the same case gracefully (`{"error": "404 …", "service": "...", "endpoint": "..."}` with `is_error: false`).

**Fix:** catch `requests.HTTPError` at the tool boundary (or in `src/psa/client.py:79`) and return `{"error": "...", "endpoint": "..."}` — matching the main server's shape.

### `download_images` overwrites files and strips extensions

Input `{"search_params":{},"max_images":2}` reports `images_downloaded: 2` but only one file lands on disk. `src/client/helpers.py:188-215` uses `record.get('imageName')` as the filename with no de-duplication across the batch, so two images with the same `imageName` ("Example Image") collide and the second overwrites the first. Extension handling at :196-199 only runs when `imageName` is absent — if the server returns an imageName without an extension (as the test server does), the downloaded file has none either.

**Fix:** always sanitize + extension-check the filename (append `image_id` or `mimeType`-derived extension when missing); de-dup collisions within a batch (append `idx` or `image_id` suffix).

### Download-URL tools hard-code `http://localhost:...`

`src/mcp_server/tools/file_handling/result_cache.py:181` (`get_download_instructions`) and `:223` (`quick_download_link`) both build `base_url = f'http://localhost:{port}'` as a literal. Port comes from config; host does not. Breaks in Docker, remote deploy, or any host not on `localhost`.

**Fix:** derive the host from the incoming request (MCP `Context` / FastAPI request) or from a `PUBLIC_URL`/`HOST` config value. Apply the fix to both call sites.

### `load_result` returns nested values as Python repr

Round-trip through the session cache goes DataFrame → CSV (`ResultCache.save_result` at `src/mcp_server/session/result_cache.py:70`, `df.to_csv(...)`) → CSV read (`load_result` at `:126`, `pd.read_csv(...)`) → `to_dict`. Pandas stringifies nested list/dict cells via `str()`, producing Python repr (`"[{'contactDbId': 'study_contact_1', 'email': '…'}]"` with single quotes), and reading back doesn't reverse that. JSON-parseable values come back as invalid-JSON strings.

**Fix:** save as JSON / parquet instead of CSV (`save_result` already accepts `format='json'|'parquet'`, but callers in `src/mcp_server/tools/generic/tools.py:211,339` hard-code `'csv'`). Alternatively, re-`json.loads` each cell on the way back out when `format == 'csv'`.

### `/data` vs `.gitignore` `/Data` case mismatch

`src/psa/config.py:43` defaults `data_dir = "./data"` (lowercase); `src/psa/tools/observations.py:61` uses the same fallback. `.gitignore` lists `/Data` (capital). macOS's default case-insensitive APFS hides the mismatch; on a case-sensitive filesystem (Linux, case-sensitive macOS volumes) user data is not ignored.

**Fix:** lowercase the `.gitignore` entry to `/data`, or consolidate PSA downloads under `cache/{name}/downloads/` like the main server's `config.downloads_dir` pattern.

## UX

### `get_search_parameters` / `get_image_search_parameters` double-encode JSON

`src/mcp_server/tools/generic/tools.py:81` and `src/mcp_server/tools/file_handling/images.py:33` return `parameters` / `valid_parameters` as whatever `capabilities` stored, which is the raw `dictionary_loc` CSV cell from `src/client/capabilities/capability_builder.py:52` — a JSON *string*, not an object. The response comes out with escaped inner quotes; every caller has to `json.loads` it.

**Fix:** parse once on the way out (`json.loads(search_params)`) — or store `dictionary_loc` as parsed JSON in `CapabilityBuilder.from_server`.

### `brapi_get` / `brapi_search` descriptions reference tools that don't exist

`src/mcp_server/tools/generic/tools.py:106` and `:255` open with `**GENERIC FALLBACK** - Use specific tools first if available!` — but no per-service specific tools are registered on the main server (only these two generics, the two search-parameter helpers, `describe_server_capabilities`, the result-chain tools, and `download_images`). An LLM reading "use specific tools first" looks for tools that aren't there.

**Fix:** rewrite the opening to say what the tool does and point to `describe_server_capabilities` for valid service names. Drop the `GENERIC FALLBACK` framing unless per-service tools are actually added.

### `search_studies.study_name` docstring contradicts behavior

`src/psa/tools/studies.py:42` docstring says `study_name: Filter by study name (partial match)`. The implementation at `:60-61` passes it straight through as `params["studyName"]` to `GET /studies`, where the test server does exact match: `search_studies({"study_name":"yield"})` returns `[]` even though a study named "Paw paw 2013 yield trial" exists.

**Fix:** either match the docstring (filter substrings client-side after fetching, or use `POST /search/studies` with `studyNames` + fetch-then-filter) or correct the docstring to `"Filter by study name (exact match, case-sensitive)"`.

## Options

1. Fix PSA `structured_content` double-serialization
2. Catch upstream HTTP errors in PSA tools and return structured errors
3. Fix `download_images` filename collision and missing extension
4. Replace hard-coded `localhost` in `quick_download_link` + `get_download_instructions`
5. Preserve JSON in `load_result` nested cells
6. Reconcile `/data` vs `/Data` (or consolidate under `cache/`)
7. Un-stringify `parameters` in the two search-parameter tools
8. Rewrite `brapi_get` / `brapi_search` descriptions
9. Clarify `search_studies.study_name` semantics
