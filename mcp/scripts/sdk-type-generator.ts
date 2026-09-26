import { z } from 'zod';
import { DIRECT_TOOLS } from '../src/tool-registry.js';

type JsonSchema = Record<string, unknown>;

function renderType(schema: JsonSchema): string {
  if (Array.isArray(schema.anyOf)) return (schema.anyOf as JsonSchema[]).map(renderType).join(' | ');
  if (Array.isArray(schema.enum)) return schema.enum.map((value) => JSON.stringify(value)).join(' | ');
  if (schema.type === 'string') return 'string';
  if (schema.type === 'number' || schema.type === 'integer') return 'number';
  if (schema.type === 'boolean') return 'boolean';
  if (schema.type === 'null') return 'null';
  if (schema.type === 'array') return `${renderType((schema.items ?? {}) as JsonSchema)}[]`;
  if (schema.type === 'object' || schema.properties) {
    const properties = (schema.properties ?? {}) as Record<string, JsonSchema>;
    const required = new Set((schema.required ?? []) as string[]);
    const members = Object.entries(properties).map(([name, child]) => `${name}${required.has(name) ? '' : '?'}: ${renderType(child)}`);
    if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
      if (members.length === 0) return `Record<string, ${renderType(schema.additionalProperties as JsonSchema)}>`;
      members.push(`[key: string]: ${renderType(schema.additionalProperties as JsonSchema)}`);
    }
    return `{ ${members.join('; ')} }`;
  }
  return 'unknown';
}

export function generatedSdkTypes(): string {
  const resultTypes = [
    'export type JsonPrimitive = string | number | boolean | null',
    'export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue }',
    'export interface RoomState { id?: string; title?: string }',
    'export interface VitalPair { current: number; max: number }',
    'export interface VitalsState { health?: VitalPair; mana?: VitalPair; spirit?: VitalPair; stamina?: VitalPair }',
    'export interface HandItem { id: string; name: string }',
    'export interface HandsState { right?: HandItem | null; left?: HandItem | null }',
    'export interface ActiveSpell { id: string; remaining_seconds?: number }',
    'export interface CharacterStat { value?: number; bonus?: number; base_value?: number; base_bonus?: number; enhanced_value?: number; enhanced_bonus?: number }',
    'export interface CharacterInfoValues { level?: number; race?: string; profession?: string; gender?: string; age?: number; experience?: number; stats?: Record<string, CharacterStat> }',
    'export interface CharacterSkillsValues { skills?: Record<string, { ranks: number; bonus?: number }>; spell_circles?: Record<string, number>; training_points?: { physical?: number; mental?: number } }',
    "export interface CharacterInfoData { source: 'info' | 'infomon_cache'; observed_at: string | null; complete: boolean; observed_level?: number | null; values: CharacterInfoValues }",
    "export interface CharacterSkillsData { source: 'skills' | 'infomon_cache'; observed_at: string | null; complete: boolean; observed_level?: number | null; values: CharacterSkillsValues }",
    'export interface CharacterData { info?: CharacterInfoData; skills?: CharacterSkillsData }',
    'export interface NearbyItem { id: string; noun: string; name?: string }',
    'export interface NearbyState { creatures?: NearbyItem[]; corpses?: NearbyItem[] }',
    'export interface OwnershipState { movement?: string | null; combat?: string | null; inventory?: string | null; communication?: string | null }',
    'export interface Freshness { observed_at: string; age_seconds: number; stale: boolean }',
    'export interface CharacterSnapshot { character: string; generation: string; sequence: number; observed_at: string; game: string; cursor: string; freshness: Freshness; session_id?: string; room?: RoomState; vitals?: VitalsState; stance?: string; roundtime?: number; stunned?: boolean; dead?: boolean; mind?: string | number; encumbrance?: string | number; hands?: HandsState; wounds?: Record<string, JsonValue>; active_spells?: ActiveSpell[]; nearby?: NearbyState; scripts?: string[]; owners?: OwnershipState; script_status?: Record<string, string>; character_data?: CharacterData }',
    'export interface MeaningfulEvent { cursor: number; character: string; generation: string; observed_at: string; kind: string; summary: string; data: Record<string, JsonValue> }',
    'export interface EventPage { items: MeaningfulEvent[]; total: number; cursor: string; timed_out: boolean; truncated: boolean }',
    'export interface InventoryIdentity { type: string; noun: string; name: string; full_name: string }',
    'export interface InventoryItem { dossier_id: string; fingerprint: string; identity: InventoryIdentity; last_game_id: string | null; last_seen_at: string | null; last_location: Record<string, JsonValue> | null; facts: Record<string, JsonValue>[] }',
    'export interface ItemPage { items: InventoryItem[]; total: number }',
    "export interface CombatReport { character: string; status: 'observed' | 'partial' | 'unavailable'; reason?: string; operation_id?: string; operation_status?: OperationStatus; historical?: boolean; ended_at?: number | null; generation?: string | null; action_id?: string | null; recovery_complete?: boolean; report?: Record<string, JsonValue> }",
    'export interface KnowledgeExcerpt { authority: string; title: string; text: string; source: string; url: string | null; revision_id: number | null }',
    'export interface KnowledgePage { items: KnowledgeExcerpt[]; total: number }',
    "export type OperationStatus = 'requested' | 'admitted' | 'running' | 'succeeded' | 'failed' | 'timed_out' | 'interrupted'",
    'export interface OwnedLocation { location_id: string; kind: string; owner: string; verified_owned: boolean; safety_rank: number }',
    'export interface BoundItemState { object_id: string; dossier_id: string; fingerprint: string; location: OwnedLocation }',
    'export interface OperationState { character: string; generation: string; room_id: string; items: BoundItemState[] }',
    'export interface ItemBinding { character: string; generation: string; object_id: string; dossier_id: string; fingerprint: string; original_location: OwnedLocation }',
    'export interface EvidenceRecord { method: string; generation: string; object_id: string; action_id: string | null; detail: string; facts: Record<string, JsonValue> }',
    'export interface OperationProgress { cursor: number; status: OperationStatus; detail: string; timestamp: number }',
    'export interface OperationResult { operation_id: string; capability: string; character: string; args: Record<string, JsonValue>; status: OperationStatus; requested_at: number; admitted_at: number | null; started_at: number | null; ended_at: number | null; binding: ItemBinding | null; start_state: OperationState | null; end_state: OperationState | null; evidence: EvidenceRecord[]; progress: OperationProgress[]; alerts: string[]; explanation: string }',
    'export interface OperationPage { operation: OperationResult; items: OperationProgress[]; total: number; cursor: string; timed_out: boolean }',
    'export interface CapabilityDescriptor { name: string; summary: string; arguments: Record<string, JsonValue>; supported_characters: string[]; available: boolean | null }',
    'export interface CapabilityPage { character: string | null; items: CapabilityDescriptor[]; total: number }',
    'export interface OperationStopResult { character: string; stopped: boolean; stop_requested?: boolean; operation_id?: string; reason?: string }',
  ];
  const methods = DIRECT_TOOLS.map((entry) => {
    const schema = z.toJSONSchema(entry.input) as JsonSchema;
    return `  ${entry.sdkMethod}(p: ${renderType(schema)}): Promise<${entry.returnType}>;`;
  });
  const runtimeTypes = resultTypes.map((line) => line.replace(/^export /, '')).join('\n');
  const declaration = `\n${runtimeTypes}\ninterface LabSDK {\n${methods.join('\n')}\n}\ndeclare const lab: LabSDK;\n`;
  return `// GENERATED FILE -- DO NOT EDIT BY HAND.\n// Sources: tool-registry input schemas and SessionHub response contracts.\n// Regenerate with: npm run generate:sdk-types\n\nexport const SDK_TYPE_DECLARATIONS = ${JSON.stringify(declaration)};\n\nexport interface LabSDK {\n${methods.join('\n')}\n}\n\n${resultTypes.join('\n')}\n`;
}
