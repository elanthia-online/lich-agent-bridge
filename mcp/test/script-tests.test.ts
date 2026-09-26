import assert from 'node:assert/strict';
import test from 'node:test';
import { invokeDirectTool } from '../src/direct-tools.js';
import { createBridge } from '../src/sdk-bridge.js';
import type { SessionHubCaller } from '../src/session-hub-client.js';

test('test perform and exact stop preserve session fences', async () => {
  const calls: unknown[] = [];
  const hub: SessionHubCaller = { async call(route, payload) { calls.push({ route, payload }); return {}; } };
  await invokeDirectTool(hub, 'lab.perform', {
    character: 'Testmage', capability: 'controller.test-probe', expected_generation: 'generation-test',
  });
  await invokeDirectTool(hub, 'lab.stop', {
    character: 'Testmage', operation_id: 'op-test', expected_generation: 'generation-test',
  });
  assert.deepEqual(calls, [
    { route: 'perform', payload: { character: 'Testmage', capability: 'controller.test-probe', expected_generation: 'generation-test' } },
    { route: 'operationStop', payload: { character: 'Testmage', operation_id: 'op-test', expected_generation: 'generation-test' } },
  ]);
  await assert.rejects(invokeDirectTool(hub, 'lab.stop', { character: 'Testmage' }), /Invalid params/);
  assert.equal(calls.length, 2);
});

test('isolated executor shares eight mutation attempts across perform and exact stop', async () => {
  let calls = 0;
  const hub: SessionHubCaller = { async call(route, payload) {
    calls++;
    return route === 'perform'
      ? { operation_id: `ticket-${calls}`, status: 'succeeded' }
      : { character: payload.character, operation_id: payload.operation_id, stopped: false };
  } };
  for (const methods of [['perform', 'stop'], ['stop', 'perform'], ['stop', 'stop']]) {
    const bridge = createBridge(hub, { operationId: 'op-test', performCalls: 0, steps: [] });
    const params = (method: string) => method === 'perform'
      ? { character: 'Testmage', capability: 'controller.test-probe' }
      : { character: 'Testmage', operation_id: 'op-test', expected_generation: 'generation-test' };
    for (let i = 0; i < 8; i++) await bridge.call(methods[i % 2], params(methods[i % 2]));
    await assert.rejects(bridge.call(methods[1], params(methods[1])), /At most 8 mutation attempts/);
  }
  assert.equal(calls, 24);
});
