export const SESSION_HUB_ROUTES = {
  capabilities: { method: 'POST', path: '/v1/session/capabilities' },
  snapshot: { method: 'POST', path: '/v1/session/snapshot' },
  watch: { method: 'POST', path: '/v1/session/watch' },
  inventoryFind: { method: 'POST', path: '/v1/session/inventory/find' },
  combatReport: { method: 'POST', path: '/v1/session/combat/report' },
  wikiSearch: { method: 'POST', path: '/v1/session/wiki/search' },
  perform: { method: 'POST', path: '/v1/session/perform' },
  operationWatch: { method: 'POST', path: '/v1/session/operation/watch' },
  operationStop: { method: 'POST', path: '/v1/session/operation/stop' },
} as const;

export type SessionHubRoute = keyof typeof SESSION_HUB_ROUTES;
