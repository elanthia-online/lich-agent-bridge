import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { EXECUTOR_LIMITS, executeCode, transpileTypeScript } from '../src/executor.js';
import { EXECUTE_CODE_INPUT } from '../src/tool-registry.js';
import { SessionHubError, type BridgeMetadata, type SessionHubCaller } from '../src/session-hub-client.js';
import type { SessionHubRoute } from '../src/routes.js';

class ExecutorHub implements SessionHubCaller {
  calls: Array<{ route: SessionHubRoute; payload: Record<string, unknown>; metadata?: BridgeMetadata }> = [];
  async call(route: SessionHubRoute, payload: Record<string, unknown>, metadata?: BridgeMetadata): Promise<unknown> {
    this.calls.push({ route, payload, metadata });
    if (payload.query === 'throw') throw new SessionHubError('bridge said no', 400, 'bad_query', { query: 'throw' });
    if (route === 'snapshot') return { character: payload.character, room: { id: '42' }, noisy: 'x'.repeat(10_000) };
    if (route === 'inventoryFind') return { items: [{ id: 'a', name: 'black crystal' }, { id: 'b', name: 'black crystal' }] };
    if (route === 'wikiSearch') return { items: [{ title: 'Ensorcell' }, { title: 'Necromancy' }] };
    if (route === 'watch') return { items: [] };
    if (route === 'perform') return { operation_id: `ticket-${this.calls.length}`, character: payload.character, status: 'succeeded', capability: payload.capability };
    return {};
  }
}

test('TypeScript transpilation accepts annotations and rejects malformed syntax', () => {
  assert.ok('code' in transpileTypeScript("const x: string = 'ok'; return x;"));
  const invalid = transpileTypeScript('const x: : = 1; return x;');
  assert.ok('error' in invalid);
});

test('execute_code chains and batches reads but returns only compact aggregation', async () => {
  const hub = new ExecutorHub();
  const result = await executeCode(`
    const state: CharacterSnapshot = await lab.snapshot({ character: 'Testmage' });
    const [items, knowledge] = await Promise.all([
      lab.inventoryFind({ character: 'Testmage', query: 'black crystal' }),
      lab.wikiSearch({ query: 'ensorcell', character: 'Testmage', limit: 2 }),
    ]);
    return { room: (state.room as { id: string }).id, item_count: items.items.length, topics: knowledge.items.map((x: any) => x.title) };
  `, 'compact', hub);
  assert.equal(result.success, true, result.error);
  assert.deepEqual(result.result, { room: '42', item_count: 2, topics: ['Ensorcell', 'Necromancy'] });
  assert.equal(result.lab_calls, 3);
  assert.equal(typeof result.startup_time_ms, 'number');
  assert.ok((result.startup_time_ms ?? -1) >= 0);
  assert.match(result.operation_id, /^op-/);
  assert.deepEqual(result.steps.map((step) => step.step_id), [1, 2, 3]);
  assert.ok(JSON.stringify(result.result).length < 100);
  assert.equal(new Set(hub.calls.map((call) => call.metadata?.operationId)).size, 1);
});

test('bridge errors remain catchable inside the isolate with code and payload', async () => {
  const result = await executeCode(`
    try { await lab.wikiSearch({ query: 'throw' }); return { caught: false }; }
    catch (error: any) { return { caught: true, message: error.message, code: error.code, query: error.payload.query }; }
  `, 'errors', new ExecutorHub());
  assert.equal(result.success, true, result.error);
  assert.deepEqual(result.result, { caught: true, message: 'bridge said no', code: 'bad_query', query: 'throw' });
  assert.equal(result.steps[0]?.success, false);
});

test('observation watches overlap across characters and preserve independent cursors', async () => {
  const waiting: Array<() => void> = [];
  const calls: Record<string, unknown>[] = [];
  const hub: SessionHubCaller = { async call(route, payload) {
    assert.equal(route, 'watch');
    calls.push(payload);
    await new Promise<void>(resolve => {
      waiting.push(resolve);
      if (waiting.length === 3) waiting.forEach(release => release());
    });
    return { items: [], total: 0, cursor: String(Number(payload.cursor) + 1), timed_out: true, truncated: false };
  } };
  const result = await executeCode(`
    return await Promise.all(['Testlead', 'Testone', 'Testtwo'].map(async (character, index) => {
      const page = await lab.watch({ character, cursor: String(index * 10), timeout_ms: 1000 });
      return { character, cursor: page.cursor, timed_out: page.timed_out };
    }));
  `, 'parallel-watch', hub, { timeout_ms: 2000 });
  assert.equal(result.success, true, result.error);
  assert.equal(result.lab_calls, 3);
  assert.deepEqual(result.result, [
    { character: 'Testlead', cursor: '1', timed_out: true },
    { character: 'Testone', cursor: '11', timed_out: true },
    { character: 'Testtwo', cursor: '21', timed_out: true },
  ]);
  assert.ok(calls.every(payload => payload.timeout_ms === 1000));
});

test('wall-clock jumps cannot prematurely expire executor dispatch or corrupt elapsed metrics', async (context) => {
  let wallClock = 0;
  context.mock.method(Date, 'now', () => { wallClock += 60_000; return wallClock; });
  const hub = new ExecutorHub();
  const result = await executeCode(`
    await lab.snapshot({ character: 'Testmage' });
    return await lab.perform({ character: 'Testmage', capability: 'hunt.prepare' });
  `, 'monotonic-deadline', hub);
  assert.equal(result.success, true, result.error);
  assert.equal(hub.calls.length, 2);
  assert.ok(result.execution_time_ms >= 0 && result.execution_time_ms < 10_000);
});

test('three sequential mutations and two parallel characters retain every operation receipt', async () => {
  const hub = new ExecutorHub();
  const result = await executeCode(`
    for (let i = 0; i < 3; i++) await lab.perform({ character: 'Testmage', capability: 'hunt.prepare' });
    await Promise.all(['Testmage', 'Testwarrior'].map(character => lab.perform({ character, capability: 'hunt.prepare' })));
    return 'complete';
  `, 'batch-perform', hub);
  assert.equal(result.success, true, result.error);
  assert.equal(hub.calls.filter((call) => call.route === 'perform').length, 5);
  assert.deepEqual(result.steps.map(step => step.operation_id), ['ticket-1', 'ticket-2', 'ticket-3', 'ticket-4', 'ticket-5']);
  assert.equal(result.steps[4].character, 'Testwarrior');
});

test('operation watches release synthetic character ownership before dependent performs; independent characters overlap', async () => {
  const active = new Map<string, string>();
  let maximumActive = 0;
  let sequence = 0;
  const hub: SessionHubCaller = { async call(route, payload) {
    if (route === 'perform') {
      const character = String(payload.character);
      if ([...active.values()].includes(character)) throw new SessionHubError('character busy', 409, 'operation_busy', null);
      const operation_id = `ticket-${++sequence}`;
      active.set(operation_id, character);
      maximumActive = Math.max(maximumActive, active.size);
      return { operation_id, character, status: 'running' };
    }
    const operation_id = String(payload.operation_id);
    const character = active.get(operation_id);
    active.delete(operation_id);
    return { operation: { operation_id, character, status: 'succeeded' } };
  } };
  const result = await executeCode(`
    for (let i = 0; i < 3; i++) {
      const ticket = await lab.perform({ character: 'Testmage', capability: 'hunt.prepare' });
      await lab.operationWatch({ operation_id: ticket.operation_id });
    }
    return await Promise.all(['Testmage', 'Testwarrior'].map(character => lab.perform({ character, capability: 'hunt.prepare' })));
  `, 'owned-batch', hub);
  assert.equal(result.success, true, result.error);
  assert.equal(sequence, 5);
  assert.equal(maximumActive, 2);
  assert.equal(result.steps[0].operation_status, 'succeeded');
  assert.equal(result.steps[6].operation_status, 'running');
});

test('terminal operation failure remains failure when user code catches or ignores it', async () => {
  for (const status of ['failed', 'timed_out', 'interrupted']) {
    const hub = new ExecutorHub();
    const original = hub.call.bind(hub);
    hub.call = async (route, payload, metadata) => route === 'operationWatch'
      ? { operation: { operation_id: payload.operation_id, character: 'Testmage', status } }
      : original(route, payload, metadata);
    const result = await executeCode(`
      const ticket = await lab.perform({ character: 'Testmage', capability: 'hunt.prepare' });
      await lab.operationWatch({ operation_id: ticket.operation_id });
      try { await lab.perform({ character: 'Testmage', capability: 'room.loot' }); } catch {}
      return 'ignored';
    `, `watched-${status}`, hub);
    assert.equal(result.success, false);
    assert.match(result.error ?? '', new RegExp(status));
    assert.equal(hub.calls.length, 1);
    assert.equal(result.steps[0].operation_status, status);
  }
});

test('timeout metadata keeps 10s default and allows opt-in 30s; queued calls after terminal result never dispatch', async () => {
  assert.equal(EXECUTE_CODE_INPUT.safeParse({ code: 'return 1', timeout_ms: 30_000 }).success, true);
  assert.equal(EXECUTE_CODE_INPUT.safeParse({ code: 'return 1', timeout_ms: 30_001 }).success, false);
  const prior = process.env.LAB_MCP_EXECUTOR_CHILD;
  process.env.LAB_MCP_EXECUTOR_CHILD = fileURLToPath(new URL('./fixtures/executor-protocol.cjs', import.meta.url));
  try {
    const hub = new ExecutorHub();
    for (const timeout_ms of [undefined, 30_000, 40_000]) {
      const result = await executeCode('return 1;', 'protocol', hub, { timeout_ms });
      assert.equal(result.success, true, result.error);
      assert.equal(result.result, timeout_ms === undefined ? 10_000 : 30_000);
      assert.equal(result.lab_calls, 0);
      assert.deepEqual(result.steps, []);
    }
    assert.deepEqual(hub.calls, []);
  } finally {
    if (prior === undefined) delete process.env.LAB_MCP_EXECUTOR_CHILD;
    else process.env.LAB_MCP_EXECUTOR_CHILD = prior;
  }
});

test('caught ninth mutation denial still reports partial failure and eight receipts', async () => {
  const hub = new ExecutorHub();
  const result = await executeCode(`
    for (let i = 0; i < 9; i++) {
      try { await lab.perform({ character: 'Testmage', capability: 'hunt.prepare' }); } catch {}
    }
    return 'caught';
  `, 'batch-limit', hub);
  assert.equal(result.success, false);
  assert.match(result.error ?? '', /At most 8 mutation attempts/);
  assert.equal(hub.calls.length, 8);
  assert.equal(result.steps.filter(step => step.operation_id).length, 8);
});

test('Promise.all failure drains an already-forwarded sibling receipt and blocks chained admissions', async () => {
  class PartialHub extends ExecutorHub {
    override async call(route: SessionHubRoute, payload: Record<string, unknown>, metadata?: BridgeMetadata) {
      if (payload.character === 'Testmage') {
        await new Promise(resolve => setTimeout(resolve, 10));
        throw new SessionHubError('access denied', 403, 'full_access_required', null);
      }
      await new Promise(resolve => setTimeout(resolve, 50));
      return super.call(route, payload, metadata);
    }
  }
  const hub = new PartialHub();
  const result = await executeCode(`
    await Promise.all([
      lab.perform({ character: 'Testmage', capability: 'hunt.prepare' }),
      lab.perform({ character: 'Testwarrior', capability: 'hunt.prepare' }).then(() => lab.perform({ character: 'Testwarrior', capability: 'room.loot' })),
    ]);
  `, 'partial', hub);
  assert.equal(result.success, false);
  assert.equal(result.steps[1].operation_id, 'ticket-1');
  assert.equal(result.steps[1].dispatch_status, 'acknowledged');
  assert.equal(hub.calls.length, 1);
});

test('timeout preserves unconfirmed admission and immutable returned steps after a late receipt', async () => {
  let forwarded = 0;
  let release!: (value: unknown) => void;
  const hub: SessionHubCaller = { call: async () => {
    forwarded++;
    return new Promise(resolve => { release = resolve; });
  } };
  const result = await executeCode(`
    await lab.perform({ character: 'Testmage', capability: 'hunt.prepare' });
    await lab.perform({ character: 'Testmage', capability: 'room.loot' });
  `, 'late-receipt', hub, { timeout_ms: 50 });
  assert.equal(result.success, false);
  assert.match(result.error ?? '', /timed out/);
  assert.equal(result.steps[0].dispatch_status, 'unconfirmed');
  const snapshot = JSON.stringify(result);
  release({ operation_id: 'ticket-late', status: 'succeeded' });
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(forwarded, 1);
  assert.equal(JSON.stringify(result), snapshot);
});

test('fire-and-forget mutation cannot report full success or run a dependent mutation', async () => {
  const hub = new ExecutorHub();
  const result = await executeCode(`
    lab.perform({ character: 'Testmage', capability: 'hunt.prepare' })
      .then(() => lab.perform({ character: 'Testmage', capability: 'room.loot' }));
    return 'selected';
  `, 'unawaited-mutation', hub);
  assert.equal(result.success, false);
  assert.match(result.error ?? '', /unawaited mutation/i);
  assert.ok(hub.calls.length <= 1);
});

test('an already-started unawaited call settles before the operation result is returned', async () => {
  class SlowHub extends ExecutorHub {
    settled = false;
    override async call(route: SessionHubRoute, payload: Record<string, unknown>, metadata?: BridgeMetadata): Promise<unknown> {
      await new Promise((resolve) => setTimeout(resolve, 30));
      const result = await super.call(route, payload, metadata);
      this.settled = true;
      return result;
    }
  }
  const hub = new SlowHub();
  const result = await executeCode(`lab.snapshot({ character: 'Testmage' }); return 'selected';`, 'unawaited', hub);
  assert.equal(result.success, true, result.error);
  assert.equal(result.result, 'selected');
  assert.equal(hub.settled, true);
  assert.equal(result.steps[0]?.success, true);
});

test('recursive execute_code and long isolate watch are rejected and catchable', async () => {
  const result = await executeCode(`
    const errors: string[] = [];
    try { await (lab as any).executeCode({ code: 'return 1' }); } catch (error: any) { errors.push(error.message); }
    try { await lab.watch({ character: 'Testmage', timeout_ms: 5000 }); } catch (error: any) { errors.push(error.message); }
    return errors;
  `, 'exclusions', new ExecutorHub());
  assert.equal(result.success, true, result.error);
  assert.match((result.result as string[])[0], /Recursive/);
  assert.match((result.result as string[])[1], /direct lab.watch/);
});

test('isolate has no filesystem, network, process, raw socket, terminal, or module authority', async () => {
  const result = await executeCode(`
    return {
      process: typeof (globalThis as any).process,
      require: typeof (globalThis as any).require,
      fetch: typeof (globalThis as any).fetch,
      WebSocket: typeof (globalThis as any).WebSocket,
      Deno: typeof (globalThis as any).Deno,
      Bun: typeof (globalThis as any).Bun,
    };
  `, 'ambient', new ExecutorHub());
  assert.equal(result.success, true, result.error);
  assert.deepEqual(result.result, {
    process: 'undefined', require: 'undefined', fetch: 'undefined', WebSocket: 'undefined', Deno: 'undefined', Bun: 'undefined',
  });
});

test('code, timeout, call count, and per-connection concurrency limits fail closed', async () => {
  const hub = new ExecutorHub();
  const oversize = await executeCode('x'.repeat(EXECUTOR_LIMITS.maxCodeBytes + 1), 'oversize', hub);
  assert.match(oversize.error ?? '', /Code exceeds maximum size/);

  const timeout = await executeCode('while (true) {}', 'timeout', hub, { timeout_ms: 100 });
  assert.match(timeout.error ?? '', /timed out after 100ms/i);

  const calls = await executeCode(`for (let i = 0; i < 21; i++) await lab.snapshot({ character: 'Testmage' }); return true;`, 'calls', hub);
  assert.match(calls.error ?? '', /Maximum 20 lab\.\* calls/);

  const jobs = [1, 2, 3].map(() => executeCode('while (true) {}', 'same-connection', hub, { timeout_ms: 250 }));
  const concurrency = await Promise.all(jobs);
  assert.equal(concurrency.filter((result) => /Maximum 2 concurrent/.test(result.error ?? '')).length, 1);
});

test('8MB isolate memory limit is configured and memory exhaustion is classified', async () => {
  assert.equal(EXECUTOR_LIMITS.memoryMb, 8);
  const result = await executeCode(`
    const values: string[] = [];
    while (true) values.push('x'.repeat(100000));
  `, 'memory', new ExecutorHub(), { timeout_ms: 3000 });
  assert.equal(result.success, false);
  assert.match(result.error ?? '', /8MB memory limit|timed out/i);
});
