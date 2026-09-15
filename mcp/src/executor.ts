import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
import { createBridge, type BridgeState, type ExecutionStep } from './sdk-bridge.js';
import { SDK_TYPE_DECLARATIONS } from './sdk-types.generated.js';
import type { SessionHubCaller } from './session-hub-client.js';

export const EXECUTOR_LIMITS = Object.freeze({
  maxCodeBytes: 20 * 1024,
  timeoutMs: 10_000,
  maxTimeoutMs: 30_000,
  memoryMb: 8,
  maxCalls: 20,
  maxConcurrent: 2,
  maxResultBytes: 256 * 1024,
  bootTimeoutMs: 15_000,
});

const activeByConnection = new Map<string, number>();

export interface ExecutionResult {
  success: boolean;
  operation_id: string;
  result?: unknown;
  logs: string[];
  lab_calls: number;
  steps: ExecutionStep[];
  execution_time_ms: number;
  startup_time_ms: number | null;
  error?: string;
}

export function transpileTypeScript(code: string): { code: string } | { error: string } {
  const source = `${SDK_TYPE_DECLARATIONS}\nasync function __labUserCode(): Promise<unknown> {\n${code}\n}`;
  const output = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ES2022,
      removeComments: true,
    },
    reportDiagnostics: true,
  });
  const errors = output.diagnostics?.filter((diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error) ?? [];
  if (errors.length > 0) {
    return { error: ts.flattenDiagnosticMessageText(errors[0].messageText, ' ').slice(0, 300) };
  }
  return { code: output.outputText };
}

function childPath(): string {
  return process.env.LAB_MCP_EXECUTOR_CHILD?.trim() || fileURLToPath(new URL('./executor-child.cjs', import.meta.url));
}

function classifyError(message: string, timeoutMs: number): string {
  if (/timed out/i.test(message)) return `Execution timed out after ${timeoutMs}ms`;
  if (/memory|heap|allocation failed|disposed during execution/i.test(message)) {
    return `Execution exceeded ${EXECUTOR_LIMITS.memoryMb}MB memory limit`;
  }
  return message;
}

export async function executeCode(
  code: string,
  connectionId: string,
  client: SessionHubCaller,
  options: { timeout_ms?: number } = {},
): Promise<ExecutionResult> {
  const started = performance.now();
  const operationId = `op-${randomUUID()}`;
  const logs: string[] = [];
  const state: BridgeState = { operationId, performCalls: 0, steps: [] };
  let callCount = 0;
  let startupTimeMs: number | null = null;
  type Completion = Omit<
    ExecutionResult,
    'operation_id' | 'logs' | 'lab_calls' | 'steps' | 'execution_time_ms' | 'startup_time_ms'
  >;
  const finish = (partial: Completion): ExecutionResult => ({
    ...partial,
    operation_id: operationId,
    logs,
    lab_calls: callCount,
    steps: state.steps.map((step) => ({ ...step })),
    execution_time_ms: performance.now() - started,
    startup_time_ms: startupTimeMs,
  });

  const bytes = Buffer.byteLength(code, 'utf8');
  if (bytes > EXECUTOR_LIMITS.maxCodeBytes) {
    return finish({ success: false, error: `Code exceeds maximum size of ${EXECUTOR_LIMITS.maxCodeBytes} bytes (got ${bytes})` });
  }
  const transpiled = transpileTypeScript(code);
  if ('error' in transpiled) return finish({ success: false, error: `typescript_compile_error: ${transpiled.error}` });

  const active = activeByConnection.get(connectionId) ?? 0;
  if (active >= EXECUTOR_LIMITS.maxConcurrent) {
    return finish({ success: false, error: `Maximum ${EXECUTOR_LIMITS.maxConcurrent} concurrent executions per connection` });
  }
  activeByConnection.set(connectionId, active + 1);

  const timeoutMs = Math.max(1, Math.min(options.timeout_ms ?? EXECUTOR_LIMITS.timeoutMs, EXECUTOR_LIMITS.maxTimeoutMs));
  const bridge = createBridge(client, state);
  let child: ChildProcessWithoutNullStreams | undefined;
  try {
    return await new Promise<ExecutionResult>((resolve) => {
      child = spawn(process.execPath, [childPath()], { stdio: ['pipe', 'pipe', 'pipe'], env: {} });
      const spawnedAt = performance.now();
      const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
      let settled = false;
      let inFlightCalls = 0;
      let pendingResult: { success: boolean; result?: unknown; error?: string } | undefined;
      let executionTimer: NodeJS.Timeout | undefined;
      let executionDeadline: number | undefined;
      const write = (message: object) => {
        if (!settled && child?.stdin.writable) child.stdin.write(`${JSON.stringify(message)}\n`);
      };
      const done = (result: { success: boolean; result?: unknown; error?: string }) => {
        if (settled) return;
        state.closed = true;
        settled = true;
        clearTimeout(bootTimer);
        if (executionTimer) clearTimeout(executionTimer);
        lines.close();
        try { child?.stdin.write(`${JSON.stringify({ type: 'shutdown' })}\n`); } catch {}
        try { child?.kill(); } catch {}
        resolve(finish(result.success && state.mutationFailure
          ? { ...result, success: false, error: state.mutationFailure }
          : result));
      };
      const completePendingResult = () => {
        if (pendingResult && inFlightCalls === 0) done(pendingResult);
      };
      const fail = (error: string) => {
        state.closed = true;
        pendingResult = { success: false, error };
        completePendingResult();
      };
      const bootTimer = setTimeout(() => done({ success: false, error: 'Executor child failed to boot' }), EXECUTOR_LIMITS.bootTimeoutMs);

      child.stderr.on('data', () => { /* child diagnostics intentionally stay out of MCP output */ });
      child.on('error', (error) => fail(`Executor child failed: ${error.message}`));
      child.on('close', (code) => {
        if (!settled && !pendingResult) fail(`Executor child exited before a result (code ${code ?? 'unknown'})`);
      });
      lines.on('line', (line) => {
        if (settled || state.closed) return;
        let message: Record<string, unknown>;
        try { message = JSON.parse(line) as Record<string, unknown>; }
        catch { return; }
        if (message.type === 'ready') {
          startupTimeMs = performance.now() - spawnedAt;
          clearTimeout(bootTimer);
          executionDeadline = performance.now() + timeoutMs;
          executionTimer = setTimeout(
            () => done({ success: false, error: `Execution timed out after ${timeoutMs}ms` }),
            timeoutMs,
          );
          write({
            type: 'execute',
            exec_id: operationId,
            code: transpiled.code,
            timeout_ms: timeoutMs,
            memory_mb: EXECUTOR_LIMITS.memoryMb,
            max_result_bytes: EXECUTOR_LIMITS.maxResultBytes,
          });
          return;
        }
        if (message.exec_id !== operationId) return;
        if (message.type === 'log' && typeof message.line === 'string') {
          logs.push(message.line);
          return;
        }
        if (message.type === 'lab_call') {
          if (executionDeadline === undefined || performance.now() >= executionDeadline) {
            done({ success: false, error: `Execution timed out after ${timeoutMs}ms` });
            return;
          }
          callCount += 1;
          const callId = String(message.call_id);
          const method = String(message.method);
          if (callCount > EXECUTOR_LIMITS.maxCalls) {
            const error = `Maximum ${EXECUTOR_LIMITS.maxCalls} lab.* calls per execution exceeded`;
            state.steps.push({ step_id: state.steps.length + 1, tool: `lab.${method}`, success: false, error });
            write({ type: 'lab_result', call_id: callId, result_json: JSON.stringify({ __lab_thrown: true, message: error }) });
            fail(error);
            return;
          }
          inFlightCalls += 1;
          void (async () => {
            try {
              const params = typeof message.params_json === 'string' ? JSON.parse(message.params_json) : {};
              const result = await bridge.call(method, params);
              write({ type: 'lab_result', call_id: callId, result_json: JSON.stringify(result) });
            } catch (error) {
              const candidate = error as Error & { code?: string; payload?: unknown };
              write({
                type: 'lab_result',
                call_id: callId,
                result_json: JSON.stringify({
                  __lab_thrown: true,
                  message: error instanceof Error ? error.message : String(error),
                  code: candidate.code ?? null,
                  payload: candidate.payload ?? null,
                }),
              });
            } finally {
              inFlightCalls -= 1;
              completePendingResult();
            }
          })();
          return;
        }
        if (message.type === 'result' && typeof message.result_json === 'string') {
          state.closed = true;
          try { pendingResult = { success: true, result: JSON.parse(message.result_json) }; }
          catch { pendingResult = { success: false, error: 'Executor returned invalid JSON' }; }
          if (message.unawaited_mutations === true || state.steps.some((step) => step.dispatch_status === 'unconfirmed' && !step.error)) {
            pendingResult = { ...pendingResult, success: false, error: 'Execution ended with an unawaited mutation; inspect operation receipts before continuing' };
          }
          completePendingResult();
          return;
        }
        if (message.type === 'error') {
          fail(classifyError(String(message.message), timeoutMs));
        }
      });
    });
  } finally {
    activeByConnection.set(connectionId, Math.max(0, (activeByConnection.get(connectionId) ?? 1) - 1));
    try { child?.kill(); } catch {}
  }
}
