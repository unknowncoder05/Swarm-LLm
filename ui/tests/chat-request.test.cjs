const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const out = fs.mkdtempSync(path.join(os.tmpdir(), 'swarm-chat-test-'));
after(() => fs.rmSync(out, { recursive: true, force: true }));
execFileSync(path.join(root, 'node_modules/.bin/tsc'), [
  '--target', 'ES2020', '--module', 'commonjs', '--skipLibCheck', '--strict',
  '--outDir', out, 'src/chat-request.ts',
], { cwd: root, stdio: 'pipe' });
const { requestChatCompletion } = require(path.join(out, 'chat-request.js'));
const payload = { model: 'qwen3:30b-a3b', messages: [{ role: 'user', content: 'hello' }] };
const waiting = () => new Response(JSON.stringify({ detail: 'Waiting for GPU capacity' }), {
  status: 503, headers: { 'X-Swarm-Status': 'waiting_for_capacity', 'Retry-After': '0' },
});
const answer = { choices: [{ message: { content: 'Hello' } }] };

test('restart capacity rejections wait and retain one unchanged prompt until setup succeeds', async t => {
  const bodies = []; const phases = [];
  t.mock.method(global, 'fetch', async (url, options) => {
    assert.equal(url, '/swarm/api/v1/chat/completions');
    bodies.push(options.body);
    return bodies.length < 3 ? waiting() : Response.json(answer);
  });
  const result = await requestChatCompletion('consumer', payload, {
    signal: new AbortController().signal, onPhase: phase => phases.push(phase),
  });
  assert.deepEqual(result, answer);
  assert.equal(bodies.length, 3);
  assert.ok(bodies.every(body => body === JSON.stringify(payload)));
  assert.equal(phases.filter(phase => phase === 'waiting_for_capacity').length, 2);
  assert.equal(phases.at(-1), 'preparing');
});

test('an accepted request stays pending during model loading without a second submission', async t => {
  let release; let calls = 0;
  t.mock.method(global, 'fetch', () => {
    calls++;
    return new Promise(resolve => { release = resolve; });
  });
  const request = requestChatCompletion('consumer', payload, {
    signal: new AbortController().signal, onPhase: () => {},
  });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(calls, 1);
  release(Response.json(answer));
  assert.deepEqual(await request, answer);
  assert.equal(calls, 1);
});

for (const status of [401, 429, 502, 503]) {
  test(`HTTP ${status} without the explicit capacity signal preserves the server error`, async t => {
    let calls = 0;
    t.mock.method(global, 'fetch', async () => {
      calls++;
      return Response.json({ detail: 'Actual server failure' }, { status });
    });
    await assert.rejects(requestChatCompletion('consumer', payload, {
      signal: new AbortController().signal, onPhase: () => {},
    }), /Actual server failure/);
    assert.equal(calls, 1);
  });
}

test('cancel during reconnect waiting stops retries promptly', async t => {
  let calls = 0;
  const controller = new AbortController();
  t.mock.method(global, 'fetch', async () => { calls++; return waiting(); });
  await assert.rejects(requestChatCompletion('consumer', payload, {
    signal: controller.signal,
    onPhase: phase => { if (phase === 'waiting_for_capacity') controller.abort(); },
  }), { name: 'AbortError' });
  assert.equal(calls, 1);
});

test('capacity waiting has a deadline and does not submit after expiry', async t => {
  let calls = 0;
  t.mock.method(global, 'fetch', async () => { calls++; return waiting(); });
  await assert.rejects(requestChatCompletion('consumer', payload, {
    signal: new AbortController().signal, onPhase: () => {}, capacityWaitMs: 5,
  }), /Timed out waiting for available GPUs/);
  assert.equal(calls, 1);
});

test('network failure is not automatically replayed because acceptance is unknown', async t => {
  let calls = 0;
  t.mock.method(global, 'fetch', async () => { calls++; throw new TypeError('Connection lost'); });
  await assert.rejects(requestChatCompletion('consumer', payload, {
    signal: new AbortController().signal, onPhase: () => {},
  }), /Connection lost/);
  assert.equal(calls, 1);
});
