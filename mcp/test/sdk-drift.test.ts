import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { generatedSdkTypes } from '../scripts/sdk-type-generator.js';
import { DIRECT_TOOLS } from '../src/tool-registry.js';
import { SDK_TYPE_DECLARATIONS } from '../src/sdk-types.generated.js';

test('generated SDK declarations are byte-for-byte current with the Zod registry', async () => {
  const path = fileURLToPath(new URL('../src/sdk-types.generated.ts', import.meta.url));
  assert.equal(await readFile(path, 'utf8'), generatedSdkTypes());
});

test('every direct tool appears exactly once in the generated isolate SDK surface', () => {
  for (const entry of DIRECT_TOOLS) {
    assert.match(SDK_TYPE_DECLARATIONS, new RegExp(`\\b${entry.sdkMethod}\\s*\\(`));
  }
  assert.doesNotMatch(SDK_TYPE_DECLARATIONS, /executeCode|execute_code/);
});

test('generated SDK results are concrete response contracts, not open unknown bags', () => {
  assert.match(
    SDK_TYPE_DECLARATIONS,
    /interface CharacterSnapshot \{ character: string; generation: string; sequence: number;/,
  );
  assert.match(SDK_TYPE_DECLARATIONS, /interface OperationResult \{ operation_id: string;/);
  assert.match(SDK_TYPE_DECLARATIONS, /interface OperationPage \{ operation: OperationResult;/);
  assert.match(SDK_TYPE_DECLARATIONS, /interface CapabilityPage \{/);
  assert.doesNotMatch(SDK_TYPE_DECLARATIONS, /\[key: string\]: unknown/);
});
