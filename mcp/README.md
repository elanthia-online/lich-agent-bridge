# LAB MCP adapter

This package is a loopback-only Streamable HTTP MCP adapter over Python `SessionHub`. It contains no game logic and cannot access Lich, files, terminals, raw sockets, or the network from agent-supplied code. All state, capability admission, policy, and verified outcomes remain owned by `SessionHub`.

## Configuration

- `LAB_SESSION_HUB_URL` — loopback HTTP URL, default `http://127.0.0.1:18765`
- `LAB_SESSION_HUB_TOKEN` — required bearer token used only for adapter-to-hub requests
- `LAB_MCP_PORT` — MCP listener port, default `18766`; listener host is fixed to `127.0.0.1`
- `LAB_MCP_EXECUTOR_CHILD` — optional on-disk executor child override for packaging

The centralized SessionHub route mapping is in `src/routes.ts`. Direct
`lab.perform` starts an asynchronous SessionHub operation and immediately
returns its stable ticket. Use `lab.operation_watch` with the returned
`operation_id` and cursor until the operation becomes terminal. Each watch is
independently bounded, so long game operations do not consume one MCP request.
`lab.stop` targets an exact operation ID and session generation; an accepted stop
request is not proof that local script cleanup has completed. Inspect the terminal
operation evidence. Registered script suites require `expected_generation` on
perform as well; discover approved arguments through `lab.capabilities`.

`lab.perform` accepts optional `timeout_seconds` for the total execution budget,
including equipment recovery and return. It defaults to 30 seconds; only
configured refuge outings may request more, up to 300 seconds. SessionHub
enforces the capability-specific limit. For example, an authorized refuge test
may pass `timeout_seconds: 120` alongside its character, capability, arguments,
and current `expected_generation`. This does not extend the separate ten-second
`lab.execute_code` limit. Start an outing with direct `lab.perform`, then use
direct `lab.operation_watch` for bounded progress reads.

## Development

```sh
npm install
npm run generate:sdk-types
npm test
npm run typecheck
npm run build
```

The generated declarations come from the same Zod objects registered as direct MCP tool inputs. `npm run check:sdk-types` is the non-mutating drift gate.

`lab.execute_code` accepts a TypeScript function body and returns one compact JSON-serializable result. It is limited to 20 KiB source, 10 seconds, 8 MiB isolate heap, 20 inner calls, two concurrent executions per local connection, one mutation (`lab.perform` or `lab.stop`), and one-second watches. Use the direct watch tools for blocking watches.
