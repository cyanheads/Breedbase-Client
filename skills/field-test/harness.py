"""Field-test harness for Breedbase-Client MCP servers.

Drives both the main server (HTTP) and the PSA server (STDIO) through
the FastMCP client. One CLI, two transports.

Usage:
  uv run python skills/field-test/harness.py --target <t> <subcommand> [args]

Targets:
  http           Connect to running HTTP server at $MCP_URL (default http://127.0.0.1:8000/mcp/)
  stdio-psa      Spawn PSA server as subprocess (uses $BRAPI_BASE_URL, defaults to test server)
  stdio-main     Spawn main server as subprocess in STDIO mode (uses $BASE_URL)

Subcommands:
  catalog                    Print tools/resources/prompts summary
  tools                      List tools (names + descriptions)
  describe <tool>            Print a tool's input schema
  call <tool> [json-args]    Invoke a tool; json-args defaults to '{}'
  resource <uri>             Read a resource
  prompt <name> [json-args]  Render a prompt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = 'https://test-server.brapi.org/brapi/v2'
DEFAULT_HTTP_URL = 'http://127.0.0.1:8000/mcp/'


def build_transport(target: str):
  if target == 'http':
    url = os.environ.get('MCP_URL', DEFAULT_HTTP_URL)
    return StreamableHttpTransport(url=url)

  base_url = os.environ.get('BRAPI_BASE_URL') or os.environ.get('BASE_URL') or DEFAULT_BASE_URL
  env = {**os.environ, 'PYTHONPATH': str(REPO_ROOT / 'src')}

  if target == 'stdio-psa':
    env['BRAPI_BASE_URL'] = base_url
    return StdioTransport(
      command='uv',
      args=['run', 'python', '-m', 'psa.main'],
      cwd=str(REPO_ROOT),
      env=env,
    )

  if target == 'stdio-main':
    env['BASE_URL'] = base_url
    env['MODE'] = 'stdio'
    return StdioTransport(
      command='uv',
      args=['run', 'src/main.py'],
      cwd=str(REPO_ROOT),
      env=env,
    )

  raise SystemExit(f'unknown target: {target}')


def _dump(obj: Any) -> str:
  return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


def _tool_to_dict(t) -> dict:
  return {
    'name': t.name,
    'description': (t.description or '').strip().splitlines()[0] if t.description else '',
    'input_schema': getattr(t, 'inputSchema', None) or getattr(t, 'input_schema', None),
  }


async def cmd_catalog(client: Client) -> None:
  tools = await client.list_tools()
  print(f'### tools ({len(tools)})')
  for t in tools:
    desc = (t.description or '').strip().splitlines()[0] if t.description else '(no description)'
    print(f'- **{t.name}** — {desc}')

  try:
    resources = await client.list_resources()
    print(f'\n### resources ({len(resources)})')
    for r in resources:
      print(f'- {r.uri} — {r.name or ""}')
  except Exception as e:
    print(f'\n### resources — unavailable ({type(e).__name__})')

  try:
    prompts = await client.list_prompts()
    print(f'\n### prompts ({len(prompts)})')
    for p in prompts:
      print(f'- {p.name} — {(p.description or "").strip()}')
  except Exception as e:
    print(f'\n### prompts — unavailable ({type(e).__name__})')


async def cmd_tools(client: Client) -> None:
  tools = await client.list_tools()
  print(_dump([_tool_to_dict(t) for t in tools]))


async def cmd_describe(client: Client, name: str) -> None:
  tools = await client.list_tools()
  match = next((t for t in tools if t.name == name), None)
  if match is None:
    raise SystemExit(f'no tool named {name!r}')
  print(_dump(_tool_to_dict(match)))


async def cmd_call(client: Client, name: str, args_json: str) -> None:
  try:
    args = json.loads(args_json) if args_json else {}
  except json.JSONDecodeError as e:
    raise SystemExit(f'invalid JSON args: {e}')

  try:
    result = await client.call_tool(name, args)
  except Exception as e:
    print(_dump({
      'raised': True,
      'error_type': type(e).__name__,
      'error': str(e),
    }))
    return

  print(_dump({
    'raised': False,
    'is_error': bool(getattr(result, 'is_error', False)),
    'content': [c.model_dump() if hasattr(c, 'model_dump') else c for c in (result.content or [])],
    'structured_content': getattr(result, 'structured_content', None),
  }))


async def cmd_resource(client: Client, uri: str) -> None:
  result = await client.read_resource(uri)
  print(_dump([c.model_dump() if hasattr(c, 'model_dump') else c for c in result]))


async def cmd_prompt(client: Client, name: str, args_json: str) -> None:
  args = json.loads(args_json) if args_json else {}
  result = await client.get_prompt(name, args)
  print(_dump(result.model_dump() if hasattr(result, 'model_dump') else result))


async def run(args: argparse.Namespace) -> None:
  transport = build_transport(args.target)
  async with Client(transport) as client:
    if args.cmd == 'catalog':
      await cmd_catalog(client)
    elif args.cmd == 'tools':
      await cmd_tools(client)
    elif args.cmd == 'describe':
      await cmd_describe(client, args.name)
    elif args.cmd == 'call':
      await cmd_call(client, args.name, args.args or '{}')
    elif args.cmd == 'resource':
      await cmd_resource(client, args.uri)
    elif args.cmd == 'prompt':
      await cmd_prompt(client, args.name, args.args or '{}')


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument('--target', choices=['http', 'stdio-psa', 'stdio-main'], required=True)
  sub = parser.add_subparsers(dest='cmd', required=True)
  sub.add_parser('catalog')
  sub.add_parser('tools')

  p_describe = sub.add_parser('describe')
  p_describe.add_argument('name')

  p_call = sub.add_parser('call')
  p_call.add_argument('name')
  p_call.add_argument('args', nargs='?', default='{}')

  p_res = sub.add_parser('resource')
  p_res.add_argument('uri')

  p_prompt = sub.add_parser('prompt')
  p_prompt.add_argument('name')
  p_prompt.add_argument('args', nargs='?', default='{}')

  args = parser.parse_args()
  asyncio.run(run(args))


if __name__ == '__main__':
  main()
