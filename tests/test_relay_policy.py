"""Route coverage, cloud disablement, preflight ordering and fenced recovery."""
import asyncio
import json
import socket
import threading
import unittest
from unittest.mock import Mock, patch
from scheduling_support import SchedulingFixture
from test_agent_reports import ROOT, load

policy = load('relay_policy_under_test', ROOT / 'coordinator/relay_policy.py')


class RelayPolicyTests(unittest.TestCase):
    def test_multiple_networks_require_a_candidate_covering_every_member(self):
        routes = [dict(id='a', url='https://a.example', control_key='secret', agent_ids=['a1','a2']),
                  dict(id='b', url='https://b.example', control_key='secret', agent_ids=['b1','b2'])]
        p = policy.RelayPolicy(json.dumps(routes))
        self.assertEqual([r.identity for r in p.candidates(['a1','a2'])], ['lan-quic','a','coordinator'])
        self.assertEqual([r.identity for r in p.candidates(['b1','b2'])], ['lan-quic','b','coordinator'])
        self.assertEqual([r.identity for r in p.candidates(['a1','b1'])], ['lan-quic','coordinator'])

    def test_cloud_disabled_and_failure_cooldown_is_pair_specific(self):
        clock = Mock(return_value=1000)
        p = policy.RelayPolicy(json.dumps([dict(id='cloud',scope='cloud',url='https://cloud.example',control_key='secret')]), clock=clock)
        p.reject('lan-quic',['one','two'])
        self.assertEqual([r.identity for r in p.candidates(['two','one'],managed=True)],['coordinator'])
        self.assertEqual(p.candidates(['three','four'])[0].identity,'lan-quic')
        clock.return_value=1301
        self.assertEqual(p.candidates(['one','two'])[0].identity,'lan-quic')
        self.assertNotIn('secret',json.dumps(p.status()))

    def test_untrusted_candidate_configuration_is_rejected(self):
        for item in [dict(id='bad',url='http://relay.example',control_key='secret'),
                     dict(id='coordinator',url='https://relay.example',control_key='secret'),
                     dict(id='bad',url='https://user:password@relay.example',control_key='secret')]:
            with self.assertRaises(ValueError):policy.RelayPolicy(json.dumps([item]))


class RecoveryTests(SchedulingFixture):
    def pair(self, prefix):
        for name in [prefix+'1',prefix+'2']:
            m=self.machine(name);m.runtime={'rpc_quic':True,'rpc_preflight':True,'rpc_relay_external':True}
        return self.c._form_rpc_cluster('qwen3:30b-a3b',19,32768,'quic',agent_ids=[prefix+'1',prefix+'2'])

    async def test_preflight_must_match_assignment_and_precede_model_readiness(self):
        c=self.pair('a')
        async def stage(aid,role,value):
            return await self.client.post('/agent/rpc/preflight',headers=self.headers,
                json=dict(agent_id=aid,cluster_id=c.cluster_id,role=role,stage=value))
        self.assertEqual((await stage('a2','worker','native_ready')).status_code,409)
        self.assertEqual((await stage('a1','worker','checking')).status_code,409)
        ready=dict(agent_id='a1',cluster_id=c.cluster_id,role='primary')
        self.assertEqual((await self.client.post('/agent/rpc/ready',headers=self.headers,json=ready)).status_code,409)
        for aid,role,value in [('a1','primary','checking'),('a2','worker','checking'),('a2','worker','verified'),('a1','primary','verified'),('a2','worker','native_ready')]:
            self.assertEqual((await stage(aid,role,value)).status_code,200)
        self.assertEqual((await stage('a2','worker','native_ready')).status_code,200)
        self.assertEqual((await self.client.post('/agent/rpc/ready',headers=self.headers,json=ready)).status_code,200)

    async def test_unavailable_local_relay_selects_next_candidate_before_releasing_assignments(self):
        self.pair('a')
        self.c.rpc_clusters.clear();self.c.agent_rpc_assignments.clear()
        for m in self.c.machines.values():
            m.in_rpc=False;m.rpc_cluster_id=None;m.runtime['rpc_quic']=False
        self.c.relay_policy=policy.RelayPolicy(json.dumps([
            dict(id='first',url='https://first.example',control_key='first-secret'),
            dict(id='second',url='https://second.example',control_key='second-secret')]))
        with patch.object(self.c,'_regional_relay_call',side_effect=[OSError('relay offline'),{'status':'ok'}]) as control:
            cluster=await self.c._form_automatic_rpc_cluster('qwen3:30b-a3b',19,32768)
        self.assertEqual(cluster.route_id,'second');self.assertEqual(control.call_count,2)
        self.assertEqual(len(self.c.rpc_clusters),1)
        self.assertTrue(all(a['cluster_id']==cluster.cluster_id for a in self.c.agent_rpc_assignments.values()))
        self.assertNotIn('second-secret',json.dumps(cluster.to_dict()))

    async def test_concurrent_first_use_waits_for_a_validated_route_and_shares_one_cluster(self):
        self.pair('a');self.c.rpc_clusters.clear();self.c.agent_rpc_assignments.clear()
        for m in self.c.machines.values():
            m.in_rpc=False;m.rpc_cluster_id=None;m.runtime['rpc_quic']=False
        self.c.relay_policy=policy.RelayPolicy(json.dumps([
            dict(id='local',url='https://local.example',control_key='secret')]))
        entered=threading.Event();finish=threading.Event()
        def control(*args):
            entered.set();finish.wait(3);return {'status':'ok'}
        with patch.object(self.c,'_regional_relay_call',side_effect=control) as call:
            first=asyncio.create_task(self.c._form_automatic_rpc_cluster('qwen3:30b-a3b',19,32768))
            self.assertTrue(await asyncio.to_thread(entered.wait,2))
            self.assertIsNone(self.c._find_rpc_cluster_for_model('qwen3:30b-a3b'))
            second=asyncio.create_task(self.c._form_automatic_rpc_cluster('qwen3:30b-a3b',19,32768))
            finish.set();a,b=await asyncio.gather(first,second)
        self.assertIs(a,b);self.assertEqual(call.call_count,1)

    async def test_cancelled_route_setup_releases_unpublished_gpu_reservations(self):
        self.pair('a');self.c.rpc_clusters.clear();self.c.agent_rpc_assignments.clear()
        for m in self.c.machines.values():
            m.in_rpc=False;m.rpc_cluster_id=None;m.runtime['rpc_quic']=False
        self.c.relay_policy=policy.RelayPolicy(json.dumps([
            dict(id='local',url='https://local.example',control_key='secret')]))
        entered=threading.Event();finish=threading.Event()
        def control(*args):
            entered.set();finish.wait(3);return {'status':'ok'}
        with patch.object(self.c,'_regional_relay_call',side_effect=control):
            task=asyncio.create_task(self.c._form_automatic_rpc_cluster('qwen3:30b-a3b',19,32768))
            self.assertTrue(await asyncio.to_thread(entered.wait,2))
            task.cancel();await asyncio.gather(task,return_exceptions=True);finish.set()
        self.assertFalse(self.c.rpc_clusters);self.assertFalse(self.c.agent_rpc_assignments)
        self.assertTrue(all(not m.in_rpc for m in self.c.machines.values()))

    async def test_route_failure_retains_request_until_teardown_then_uses_next_route(self):
        c=await self.c._form_automatic_rpc_cluster('qwen3:30b-a3b',19,32768)
        self.assertIsNone(c)
        self.pair('a')
        # Release the fixture role and form a real automatic assignment.
        self.c.rpc_clusters.clear();self.c.agent_rpc_assignments.clear()
        for m in self.c.machines.values():m.in_rpc=False;m.rpc_cluster_id=None
        c=await self.c._form_automatic_rpc_cluster('qwen3:30b-a3b',19,32768)
        job=self.enqueue(c.model,32768);job.rpc_cluster_id=c.cluster_id
        self.c._dissolve_cluster(c,'preflight_failed')
        self.assertTrue(job.rpc_recovering);self.assertFalse(job.cancelled)
        await self.c._recover_rpc_jobs_once();self.assertEqual(job.rpc_cluster_id,c.cluster_id)
        for aid,role in [(c.primary_id,'primary'),(c.worker_ids[0],'worker')]:
            await self.client.post('/agent/rpc/done',headers=self.headers,json=dict(agent_id=aid,cluster_id=c.cluster_id,role=role))
        await self.c._recover_rpc_jobs_once()
        new=self.c.rpc_clusters[job.rpc_cluster_id]
        self.assertEqual(new.route_id,'coordinator');self.assertEqual(new.ctx_size,32768)
        self.assertFalse(job.rpc_recovering)
        self.assertEqual(sum(j.id==job.id for j in self.c.job_queues[job.model]._queue),1)

    async def test_failed_relay_moves_request_to_other_ready_replica_and_fences_old_result(self):
        first=self.pair('a');second=self.pair('b')
        for c in [first,second]:c.status='active';c.primary_ready=True
        first.automatic=True;first.route_id='local-a'
        job=self.enqueue(first.model,32768);job.rpc_cluster_id=first.cluster_id
        claim=(await self.claim(first.primary_id,job.model)).json()
        self.c._dissolve_cluster(first,'relay_offline')
        stale=await self.submit(job,claim['claim_token'])
        self.assertEqual(stale.status_code,409)
        for aid in [first.primary_id,*first.worker_ids]:self.c.agent_rpc_assignments.pop(aid)
        await self.c._recover_rpc_jobs_once()
        self.assertEqual(job.rpc_cluster_id,second.cluster_id)
        self.assertEqual((await self.claim(second.primary_id,job.model)).status_code,200)


class AgentPreflightTests(unittest.TestCase):
    def setUp(self):
        self.a=load('preflight_agent_under_test',ROOT/'agent/agent.py')

    def test_primary_failed_route_never_downloads_or_launches_model(self):
        a=self.a;assignment=dict(cluster_id='pair',model='qwen3:30b-a3b',role='primary',transport='quic',preflight_required=True)
        with patch.object(a,'_start_rpc_quic_tunnel',side_effect=OSError('unreachable')), \
             patch.object(a,'pull_model') as pull,patch.object(a,'ensure_llamacpp') as native,patch.object(a,'_launch_rpc_process') as launch,patch.object(a,'write_status'):
            with self.assertRaises(a.RpcPreflightError):a.run_rpc_primary(assignment,'https://coordinator','key')
        pull.assert_not_called();native.assert_not_called();launch.assert_not_called()

    def test_primary_validates_actual_end_to_end_challenge(self):
        a=self.a;token='a'*64;listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
        self.addCleanup(listener.close)
        def echo():
            client,_=listener.accept()
            with client:client.sendall(client.recv(128))
        t=threading.Thread(target=echo,daemon=True);t.start()
        assignment=dict(cluster_id='pair',role='primary',preflight_token=token)
        with patch.object(a,'_rpc_preflight_status',return_value={'workers_ready':True}):
            a._rpc_preflight_primary(assignment,['127.0.0.1:'+str(listener.getsockname()[1])],'https://coordinator','key')
        t.join(2);self.assertFalse(t.is_alive())

    def test_worker_failed_route_never_starts_native_runtime(self):
        a=self.a;assignment=dict(cluster_id='pair',model='qwen3:30b-a3b',role='worker',transport='relay',preflight_required=True)
        with patch.object(a,'_rpc_preflight_worker',side_effect=a.RpcPreflightError('failed')), \
             patch.object(a,'ensure_llamacpp') as native,patch.object(a,'_launch_rpc_process') as launch,patch.object(a,'write_status'):
            with self.assertRaises(a.RpcPreflightError):a.run_rpc_worker(assignment,'https://coordinator','key')
        native.assert_not_called();launch.assert_not_called()

    def test_verified_worker_switches_bridge_to_fresh_native_port(self):
        a=self.a
        assignment=dict(cluster_id='pair',model='qwen3:30b-a3b',role='worker',transport='quic',preflight_required=True)
        tunnel=Mock(assignment={'cluster_id':'pair'},native_port=45000)
        a._rpc_quic_tunnels.append(tunnel)
        with patch.object(a,'_available_loopback_port',side_effect=[45000,45001]), \
             patch.object(a,'_rpc_preflight_worker'),patch.object(a,'_rpc_preflight_status'), \
             patch.object(a,'ensure_llamacpp',return_value={'llama_rpc_server':'native'}), \
             patch.object(a,'_stop_ollama_for_rpc'),patch.object(a,'_wait_tcp',return_value=True), \
             patch.object(a,'write_status'),patch.object(a,'_rpc_poll_assignment',return_value={'dissolve':True}), \
             patch.object(a,'_launch_rpc_process',return_value=Mock(pid=42)) as launch:
            a.run_rpc_worker(assignment,'https://coordinator','key')
        self.assertEqual(tunnel.native_port,45001)
        command=launch.call_args.args[0]
        self.assertEqual(command[command.index('--port')+1],'45001')
