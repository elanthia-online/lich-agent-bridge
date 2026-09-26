// Synthetic subprocess only: verifies parent protocol gates without a game connection.
const readline = require('node:readline');
const input = readline.createInterface({ input: process.stdin });
input.on('line', (line) => {
  const message = JSON.parse(line);
  if (message.type === 'shutdown') process.exit(0);
  if (message.type !== 'execute') return;
  const result = { type: 'result', exec_id: message.exec_id, result_json: String(message.timeout_ms) };
  const lateCall = { type: 'lab_call', exec_id: message.exec_id, call_id: 'late', method: 'perform',
    params_json: JSON.stringify({ character: 'Testmage', capability: 'hunt.prepare' }) };
  // One output chunk deliberately leaves a queued call after a terminal result.
  process.stdout.write(`${JSON.stringify(result)}\n${JSON.stringify(lateCall)}\n`);
});
process.stdout.write(`${JSON.stringify({ type: 'ready' })}\n`);
