#!/usr/bin/env node
'use strict';

const ivm = require('isolated-vm');
const readline = require('node:readline');
const crypto = require('node:crypto');

const send = (message) => process.stdout.write(`${JSON.stringify(message)}\n`);
const pendingCalls = new Map();
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

input.on('line', async (line) => {
  let message;
  try { message = JSON.parse(line); }
  catch { return; }
  if (message.type === 'shutdown') process.exit(0);
  if (message.type === 'lab_result') {
    const pending = pendingCalls.get(message.call_id);
    if (pending) {
      pendingCalls.delete(message.call_id);
      pending(message.result_json);
    }
    return;
  }
  if (message.type === 'execute') await execute(message);
});

async function execute(message) {
  const isolate = new ivm.Isolate({ memoryLimit: message.memory_mb });
  let dispatchOpen = true;
  try {
    const context = await isolate.createContext();
    const jail = context.global;
    await jail.set('__labLog', new ivm.Callback((...args) => {
      const line = args.map((value) => typeof value === 'string' ? value : JSON.stringify(value)).join(' ');
      send({ type: 'log', exec_id: message.exec_id, line });
    }));
    const callRef = new ivm.Reference((method, paramsJson) => new Promise((resolve) => {
      if (!dispatchOpen) {
        resolve(JSON.stringify({ __lab_thrown: true, message: 'Execution dispatch is closed' }));
        return;
      }
      const callId = crypto.randomUUID();
      pendingCalls.set(callId, resolve);
      send({ type: 'lab_call', exec_id: message.exec_id, call_id: callId, method, params_json: paramsJson || '{}' });
    }));
    await jail.set('__labCallRef', callRef);

    const wrapper = `
      let __labDispatchOpen = true;
      let __labPendingMutations = 0;
      const console = {
        log: (...args) => __labLog(...args),
        warn: (...args) => __labLog('WARN:', ...args),
        error: (...args) => __labLog('ERROR:', ...args),
      };
      const lab = new Proxy(Object.create(null), {
        get(_, method) {
          if (typeof method !== 'string') return undefined;
          return async (params) => {
            if (!__labDispatchOpen) throw new Error('Execution dispatch is closed');
            const mutation = method === 'perform' || method === 'stop';
            if (mutation) __labPendingMutations++;
            try {
              const wire = await __labCallRef.apply(
                undefined,
                [method, params === undefined ? '{}' : JSON.stringify(params)],
                { arguments: { copy: true }, result: { promise: true, copy: true } }
              );
              const parsed = JSON.parse(wire);
              if (parsed && typeof parsed === 'object' && parsed.__lab_thrown === true) {
                const error = new Error(parsed.message || 'LAB SDK call failed');
                if (parsed.code) error.code = parsed.code;
                if (parsed.payload !== undefined) error.payload = parsed.payload;
                throw error;
              }
              return parsed;
            } finally {
              if (mutation) __labPendingMutations--;
            }
          };
        }
      });
      ${message.code}
      (async () => {
        try {
          const result = await __labUserCode();
          __labDispatchOpen = false;
          return JSON.stringify({ result_json: JSON.stringify(result) ?? 'null', unawaited_mutations: __labPendingMutations > 0 });
        } finally {
          __labDispatchOpen = false;
        }
      })();
    `;
    const script = await isolate.compileScript(wrapper);
    const result = await script.run(context, { timeout: message.timeout_ms, promise: true });
    dispatchOpen = false;
    const completion = JSON.parse(String(result));
    const json = completion.result_json;
    if (Buffer.byteLength(json, 'utf8') > message.max_result_bytes) {
      throw new Error(`Result exceeds maximum size of ${message.max_result_bytes} bytes`);
    }
    send({ type: 'result', exec_id: message.exec_id, result_json: json, unawaited_mutations: completion.unawaited_mutations });
  } catch (error) {
    dispatchOpen = false;
    send({ type: 'error', exec_id: message.exec_id, message: error && error.message ? error.message : String(error) });
  } finally {
    dispatchOpen = false;
    pendingCalls.clear();
    try { isolate.dispose(); } catch {}
  }
}

send({ type: 'ready' });
