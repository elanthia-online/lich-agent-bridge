import assert from 'node:assert/strict';
import type { AddressInfo } from 'node:net';
import test from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { createApp } from '../src/server.js';

test('Streamable HTTP MCP endpoint advertises the exact ten-tool surface', async (context) => {
  const app = createApp({
    host: '127.0.0.1',
    port: 1,
    sessionHubUrl: new URL('http://127.0.0.1:18765'),
    sessionHubToken: 'test-token',
  });
  const listener = app.listen(0, '127.0.0.1');
  await new Promise<void>((resolve) => listener.once('listening', resolve));
  context.after(() => new Promise<void>((resolve, reject) => listener.close((error) => error ? reject(error) : resolve())));

  const port = (listener.address() as AddressInfo).port;
  const client = new Client({ name: 'adapter-test', version: '1.0.0' });
  const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`));
  context.after(async () => { await client.close(); });
  await client.connect(transport);
  const listed = await client.listTools();
  assert.deepEqual(listed.tools.map((tool) => tool.name).sort(), [
    'lab.capabilities',
    'lab.combat_report',
    'lab.execute_code',
    'lab.inventory_find',
    'lab.operation_watch',
    'lab.perform',
    'lab.snapshot',
    'lab.stop',
    'lab.watch',
    'lab.wiki_search',
  ]);
});
