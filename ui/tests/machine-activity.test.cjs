const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const out = fs.mkdtempSync(path.join(os.tmpdir(), 'swarm-ui-test-'));
after(() => fs.rmSync(out, { recursive: true, force: true }));
execFileSync(path.join(root, 'node_modules/.bin/tsc'), [
  '--target', 'ES2020', '--module', 'commonjs', '--jsx', 'react-jsx',
  '--skipLibCheck', '--strict', '--outDir', out,
  'src/types.ts', 'src/machine-state.ts', 'src/MachineActivity.tsx', 'src/deployment.ts',
], { cwd: root, stdio: 'pipe' });
fs.symlinkSync(path.join(root, 'node_modules'), path.join(out, 'node_modules'));
const state = require(path.join(out, 'machine-state.js'));
const { MachineModels, ReplicaOverview } = require(path.join(out, 'MachineActivity.js'));
const React = require('react');
const { deploymentUrls } = require(path.join(out, 'deployment.js'));
const { renderToStaticMarkup } = require('react-dom/server');
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));
const machine = { key: 'a', agent_id: 'a', hostname: 'primary', alive: true, model: 'qwen3:14b', runtime: { ollama_ready: true, ollama_models: [] } };
const cluster = { cluster_id: 'replica-one', model: 'qwen3:30b-a3b', primary_id: 'a', worker_ids: ['b'], status: 'forming', primary_ready: false, transport: 'quic', ctx_size: 32768 };

test('join and API URLs follow the current deployment origin and port', () => {
  for (const origin of ['https://llm.example.com:8443', 'http://127.0.0.1:8081']) {
    const urls = deploymentUrls(origin);
    assert.equal(urls.api, `${origin}/swarm/api`);
    assert.equal(urls.openai, `${origin}/swarm/api/v1`);
    assert.equal(urls.agent, `${origin}/swarm/agent/agent.py`);
  }
});

test('download percentages require a live download and valid bounded telemetry', () => {
  const m = { ...machine, phase: 'DOWNLOADING', detail: '21.5% 120 MB/s - qwen3:30b-a3b' };
  assert.equal(state.downloadPercent(m), 21.5);
  for (const change of [{ phase: 'RUNNING' }, { alive: false }, { detail: '121%' }, { detail: 'starting pull' }, { detail: '-1%' }]) {
    assert.equal(state.downloadPercent({ ...m, ...change }), null);
  }
  assert.equal(state.downloadPercent({ ...m, detail: '0%' }), 0);
  assert.equal(state.downloadPercent({ ...m, detail: '100%' }), 100);
});

test('replica membership follows identity and reservation, not reused hostname', () => {
  assert.equal(state.machineCluster({ ...machine, in_rpc: true }, [cluster]), cluster);
  assert.equal(state.machineCluster({ ...machine, in_rpc: false }, [cluster]), undefined);
  assert.equal(state.machineCluster({ ...machine, in_rpc: true, agent_id: 'replacement' }, [cluster]), undefined);
});

test('worker readiness does not imply loaded weights while replica is forming', () => {
  const m = { ...machine, in_rpc: true, phase: 'RPC_WORKER', agent_id: 'b' };
  assert.equal(state.machineState(m, cluster), 'Loading model');
  const html = render(MachineModels, { machine: m, cluster });
  assert.match(html, /Target · loading/);
  assert.match(html, /Worker/);
  assert.doesNotMatch(html, />Loaded/);
});

test('actual RPC model replaces configured Ollama model on ready replica', () => {
  const ready = { ...cluster, status: 'active', primary_ready: true };
  const html = render(MachineModels, { machine: { ...machine, in_rpc: true }, cluster: ready });
  assert.match(html, /qwen3:30b-a3b/);
  assert.match(html, /Loaded/);
  assert.doesNotMatch(html, /qwen3:14b/);
  assert.equal(state.machineState({ ...machine, busy: true }, ready), 'Serving');
});

test('configured model is never presented as loaded without resident telemetry', () => {
  const html = render(MachineModels, { machine });
  assert.match(html, /No model loaded/);
  assert.match(html, /Configured: qwen3:14b/);
  const unknown = render(MachineModels, { machine: { ...machine, runtime: null } });
  assert.match(unknown, /state unavailable/);
});

test('Ollama shows all reported resident models and offline state as historical', () => {
  const m = { ...machine, alive: false, runtime: { ollama_models: [{ name: 'one' }, { name: 'two' }] } };
  const html = render(MachineModels, { machine: m });
  assert.match(html, /Last reported loaded/);
  assert.match(html, />one</);
  assert.match(html, />two</);
  assert.equal(state.machineState(m), 'Offline');
});

test('transfer bytes are directional and never become a fabricated percentage', () => {
  const c = { ...cluster, quic_stats: { a: { sent_bytes: 2000000, received_bytes: 10 }, b: { sent_bytes: 10, received_bytes: 2000000 } } };
  assert.equal(state.transferBytes(machine, c), 2000000);
  assert.equal(state.transferBytes({ ...machine, agent_id: 'b' }, c), 2000000);
  const html = render(MachineModels, { machine, cluster: c });
  assert.match(html, /RPC data sent: 1.9 MiB/);
  assert.doesNotMatch(html, /%|<progress/);
});

test('download progress is accessible and absent when progress was not reported', () => {
  const m = { ...machine, phase: 'DOWNLOADING', detail: '42% - qwen3:30b-a3b' };
  assert.match(render(MachineModels, { machine: m }), /aria-label="Model download on primary" value="42"/);
  const unknown = render(MachineModels, { machine: { ...m, detail: 'starting pull' } });
  assert.match(unknown, /progress not reported/);
  assert.doesNotMatch(unknown, /<progress/);
});

test('replica overview reflects failed teardown and unavailable members', () => {
  const html = render(ReplicaOverview, { clusters: [{ ...cluster, status: 'dissolving', dissolve_reason: 'primary_aborted' }], machines: [machine] });
  assert.match(html, /Releasing/);
  assert.match(html, /primary aborted/);
  assert.match(html, /unavailable/);
  const missing = render(ReplicaOverview, { clusters: [{ ...cluster, status: 'active', primary_ready: true }], machines: [machine] });
  assert.match(missing, /Member unavailable/);
  assert.doesNotMatch(missing, />Ready</);
});

test('relay labels do not infer a geographic location from a relay URL', () => {
  assert.equal(state.transportLabel(cluster), 'Direct LAN · QUIC');
  assert.equal(state.transportLabel({ ...cluster, transport: 'relay', relay_url: 'https://relay.example' }), 'Relay');
  assert.equal(state.transportLabel({ ...cluster, transport: 'relay' }), 'Coordinator relay');
});

test('verified route advances from checking to loading even before the next phase report', () => {
  const m = { ...machine, in_rpc: true, phase: 'RPC_CHECKING' };
  assert.equal(state.machineState(m, { ...cluster, preflight: { a: 'checking' } }), 'Checking route');
  assert.equal(state.machineState(m, { ...cluster, preflight: { a: 'verified' } }), 'Loading model');
  assert.equal(state.machineState(m, { ...cluster, status: 'dissolving' }), 'Releasing');
});
