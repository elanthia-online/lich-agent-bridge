import { z } from 'zod';
import type { SessionHubRoute } from './routes.js';

const character = z.string().trim().min(1).max(64).describe('Exact character name.');

export const DIRECT_TOOL_REGISTRY = {
  capabilities: {
    toolName: 'lab.capabilities',
    sdkMethod: 'capabilities',
    route: 'capabilities',
    description: 'List registered SessionHub capabilities, argument schemas, and optional per-character availability.',
    input: z.strictObject({ character: character.optional() }),
    returnType: 'CapabilityPage',
  },
  snapshot: {
    toolName: 'lab.snapshot',
    sdkMethod: 'snapshot',
    route: 'snapshot',
    description: 'Return the current structured state for one character without issuing a game command.',
    input: z.strictObject({ character }),
    returnType: 'CharacterSnapshot',
  },
  watch: {
    toolName: 'lab.watch',
    sdkMethod: 'watch',
    route: 'watch',
    description: 'Return meaningful events after a cursor. Prefer this direct tool for blocking watches.',
    input: z.strictObject({
      character,
      cursor: z.string().max(256).optional().describe('Opaque event cursor from snapshot or a prior watch.'),
      timeout_ms: z.number().int().min(0).max(30_000).optional().describe('Long-poll timeout in milliseconds, up to 30000.'),
    }),
    returnType: 'EventPage',
  },
  inventoryFind: {
    toolName: 'lab.inventory_find',
    sdkMethod: 'inventoryFind',
    route: 'inventoryFind',
    description: 'Find durable inventory facts for one character without touching the game.',
    input: z.strictObject({
      character,
      query: z.string().trim().min(1).max(256).describe('Item name or inventory search terms.'),
    }),
    returnType: 'ItemPage',
  },
  combatReport: {
    toolName: 'lab.combat_report',
    sdkMethod: 'combatReport',
    route: 'combatReport',
    description: 'Read retained recorded evidence for a LAB controller trial, after verified safe return. Historical association, not causal proof. No game commands. Omit operation_id for the latest controller operation.',
    input: z.strictObject({
      character,
      operation_id: z.string().regex(/^[0-9a-f]{16}$/).optional(),
    }),
    returnType: 'CombatReport',
  },
  wikiSearch: {
    toolName: 'lab.wiki_search',
    sdkMethod: 'wikiSearch',
    route: 'wikiSearch',
    description: 'Search curated LAB and GSWiki knowledge with optional character bias.',
    input: z.strictObject({
      query: z.string().trim().min(1).max(512).describe('Knowledge search terms.'),
      character: character.optional(),
      limit: z.number().int().min(1).max(20).optional().describe('Maximum excerpts to return.'),
    }),
    returnType: 'KnowledgePage',
  },
  perform: {
    toolName: 'lab.perform',
    sdkMethod: 'perform',
    route: 'perform',
    description: 'Start one outcome-oriented SessionHub capability and return its operation ticket immediately. Poll it with lab.operation_watch.',
    input: z.strictObject({
      character,
      capability: z.string().trim().regex(/^[a-z][a-z0-9_.-]{0,63}$/).describe('Registered SessionHub capability name.'),
      args: z.record(z.string(), z.unknown()).optional().describe('Capability-specific JSON arguments.'),
      expected_generation: z.string().trim().min(1).max(128).optional().describe('Observed session generation; required for trusted script tests.'),
      timeout_seconds: z.number().positive().max(300).optional().describe('Total operation budget in seconds, including recovery and return; default 30. Configured refuge outings may use up to 300 seconds; direct travel may use up to 120.'),
    }),
    returnType: 'OperationResult',
  },
  operationWatch: {
    toolName: 'lab.operation_watch',
    sdkMethod: 'operationWatch',
    route: 'operationWatch',
    description: 'Read progress after a cursor for one operation ticket. Repeat until the returned operation is terminal.',
    input: z.strictObject({
      operation_id: z.string().trim().min(1).max(64),
      cursor: z.string().regex(/^\d+$/).max(32).optional().describe('Progress cursor from a prior operation watch; default 0.'),
      timeout_ms: z.number().int().min(0).max(30_000).optional().describe('Long-poll timeout in milliseconds, up to 30000.'),
    }),
    returnType: 'OperationPage',
  },
  stop: {
    toolName: 'lab.stop',
    sdkMethod: 'stop',
    route: 'operationStop',
    description: 'Request cancellation of one exact operation and generation. Acknowledgment is not proof of script cleanup.',
    input: z.strictObject({
      character,
      operation_id: z.string().trim().min(1).max(64),
      expected_generation: z.string().trim().min(1).max(128),
    }),
    returnType: 'OperationStopResult',
  },
} as const satisfies Record<string, {
  toolName: `lab.${string}`;
  sdkMethod: string;
  route: SessionHubRoute;
  description: string;
  input: z.ZodObject;
  returnType: string;
}>;

export type DirectToolEntry = (typeof DIRECT_TOOL_REGISTRY)[keyof typeof DIRECT_TOOL_REGISTRY];
export type DirectToolName = DirectToolEntry['toolName'];
export type SdkMethod = DirectToolEntry['sdkMethod'];

export const DIRECT_TOOLS = Object.values(DIRECT_TOOL_REGISTRY);

export const EXECUTE_CODE_INPUT = z.strictObject({
  code: z.string().min(1).describe('TypeScript function-body code. Use await lab.* and return a JSON-serializable result.'),
  timeout_ms: z.number().int().min(1).max(10_000).optional().describe('Execution timeout in milliseconds; default and maximum are 10000.'),
});

export function entryByToolName(name: string): DirectToolEntry | undefined {
  return DIRECT_TOOLS.find((entry) => entry.toolName === name);
}

export function entryBySdkMethod(method: string): DirectToolEntry | undefined {
  return DIRECT_TOOLS.find((entry) => entry.sdkMethod === method);
}
