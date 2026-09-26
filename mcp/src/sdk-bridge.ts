import { entryBySdkMethod } from './tool-registry.js';
import { formatZodError } from './direct-tools.js';
import type { SessionHubCaller } from './session-hub-client.js';

export const MAX_ISOLATE_WATCH_MS = 1_000;
export const MAX_MUTATION_ATTEMPTS = 8;

const operationStatuses = new Set(['requested', 'admitted', 'running', 'succeeded', 'failed', 'timed_out', 'interrupted']);
const failedStatuses = new Set(['failed', 'timed_out', 'interrupted']);
const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' ? value as Record<string, unknown> : {};

export interface ExecutionStep {
  step_id: number;
  tool: string;
  success: boolean;
  error?: string;
  character?: string;
  operation_id?: string;
  operation_status?: string;
  dispatch_status?: 'unconfirmed' | 'acknowledged';
  stop_requested?: boolean;
  stopped?: boolean;
}

export interface BridgeState {
  operationId: string;
  performCalls: number;
  steps: ExecutionStep[];
  closed?: boolean;
  mutationFailure?: string;
}

export function createBridge(client: SessionHubCaller, state: BridgeState) {
  return {
    async call(method: string, params: unknown): Promise<unknown> {
      const stepId = state.steps.length + 1;
      const step: ExecutionStep = { step_id: stepId, tool: `lab.${method}`, success: false };
      state.steps.push(step);
      let mutation = false;
      try {
        if (state.closed) throw new Error('Execution dispatch is closed; no further LAB calls are allowed');
        if (method === 'executeCode' || method === 'execute_code') {
          throw new Error('Recursive lab.execute_code is forbidden');
        }
        const entry = entryBySdkMethod(method);
        if (!entry) throw new Error(`Unknown LAB SDK method '${method}'`);
        step.tool = entry.toolName;
        if (entry.route === 'perform' || entry.route === 'operationStop') {
          mutation = true;
          state.performCalls += 1;
          if (state.performCalls > MAX_MUTATION_ATTEMPTS) throw new Error(`At most ${MAX_MUTATION_ATTEMPTS} mutation attempts (lab.perform or lab.stop) are allowed per execute_code operation`);
          if (entry.route === 'perform' && state.mutationFailure) {
            throw new Error(`Further lab.perform admissions are blocked after a failed or ambiguous mutation: ${state.mutationFailure}`);
          }
        }
        const parsed = entry.input.safeParse(params ?? {});
        if (!parsed.success) {
          throw new Error(`Invalid params for SDK method '${method}' (${entry.toolName}): ${formatZodError(parsed.error)}`);
        }
        if (entry.route === 'watch' || entry.route === 'operationWatch') {
          const timeout = (parsed.data as { timeout_ms?: number }).timeout_ms ?? 0;
          if (timeout > MAX_ISOLATE_WATCH_MS) {
            const directTool = entry.route === 'watch' ? 'lab.watch' : 'lab.operation_watch';
            throw new Error(`Isolate watch timeout is capped at ${MAX_ISOLATE_WATCH_MS}ms; use direct ${directTool} for blocking watches`);
          }
        }
        if (mutation) {
          step.character = (parsed.data as { character: string }).character;
          if (entry.route === 'operationStop') step.operation_id = (parsed.data as { operation_id: string }).operation_id;
          // A forwarded request cannot be unsent. Until its reply arrives, admission is unknown.
          step.dispatch_status = 'unconfirmed';
        }
        const result = await client.call(entry.route, parsed.data, { operationId: state.operationId, stepId });
        step.success = true;
        if (entry.route === 'perform' || entry.route === 'operationWatch') {
          const operation = record(entry.route === 'perform' ? result : record(result).operation);
          const id = typeof operation.operation_id === 'string' && operation.operation_id.length > 0 && operation.operation_id.length <= 64 ? operation.operation_id : undefined;
          const status = typeof operation.status === 'string' && operationStatuses.has(operation.status) ? operation.status : undefined;
          if (id) step.operation_id = id;
          if (status) step.operation_status = status;
          if (entry.route === 'perform') {
            if (!id || !status) throw new Error('Mutation reply lacks a valid operation receipt; admission is unconfirmed');
            step.dispatch_status = 'acknowledged';
          }
          if (entry.route === 'operationWatch' && id && status) {
            for (const prior of state.steps) {
              if (prior.tool === 'lab.perform' && prior.operation_id === id) prior.operation_status = status;
            }
          }
          if (status && failedStatuses.has(status)) {
            state.mutationFailure ??= `Operation ${id ?? 'unknown'} ${status}`;
          }
        } else if (entry.route === 'operationStop') {
          const stop = record(result);
          if (typeof stop.character !== 'string' || stop.character.toLowerCase() !== step.character?.toLowerCase() ||
              stop.operation_id !== step.operation_id || typeof stop.stopped !== 'boolean') {
            throw new Error('Stop reply lacks a matching operation receipt; stop outcome is unconfirmed');
          }
          step.dispatch_status = 'acknowledged';
          if (typeof stop.stop_requested === 'boolean') step.stop_requested = stop.stop_requested;
          if (typeof stop.stopped === 'boolean') step.stopped = stop.stopped;
        }
        return result;
      } catch (error) {
        step.success = false;
        step.error = error instanceof Error ? error.message : String(error);
        if (mutation) state.mutationFailure ??= step.error;
        throw error;
      }
    },
  };
}
