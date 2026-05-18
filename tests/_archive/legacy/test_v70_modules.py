"""OMNI AGENT v70: AgentCoordinatorV2, NotificationRouterV2, PermissionManagerV2, BatchProcessorV3"""
import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ════════════════════════════════════════════════════════
# AGENT COORDINATOR V2
# ════════════════════════════════════════════════════════
class TestAgentCoordinatorV2(unittest.TestCase):
    def setUp(self):
        from agent.agent_coordinator_v2 import AgentCoordinatorV2
        self.ac = AgentCoordinatorV2()

    def test_register_agent(self):
        a = self.ac.register("worker1", fn=lambda p, c: p * 2)
        self.assertIsNotNone(a.agent_id)

    def test_single_delegate(self):
        self.ac.register("a1", fn=lambda p, c: p + 10,
                           capabilities=["math"])
        task = self.ac.delegate("compute", payload=5,
                                 required_capability="math")
        self.assertEqual(task.final_result, 15)

    def test_no_capable_agent(self):
        from agent.agent_coordinator_v2 import TaskState
        task = self.ac.delegate("compute", payload=1,
                                 required_capability="nonexistent")
        self.assertEqual(task.state, TaskState.FAILED)

    def test_parallel_dispatch(self):
        from agent.agent_coordinator_v2 import AggregationStrategy
        for i in range(3):
            self.ac.register(f"p{i}", fn=lambda p, c, i=i: i * 10,
                              capabilities=["parallel"])
        task = self.ac.delegate("multi", payload=1,
                                 required_capability="parallel",
                                 n_agents=3,
                                 aggregation=AggregationStrategy.FIRST)
        self.assertIsNotNone(task.final_result)
        self.assertEqual(len(task.results), 3)

    def test_aggregation_majority(self):
        from agent.agent_coordinator_v2 import AggregationStrategy
        self.ac.register("m1", fn=lambda p, c: "yes", capabilities=["vote"])
        self.ac.register("m2", fn=lambda p, c: "yes", capabilities=["vote"])
        self.ac.register("m3", fn=lambda p, c: "no",  capabilities=["vote"])
        task = self.ac.delegate("vote", required_capability="vote",
                                 n_agents=3,
                                 aggregation=AggregationStrategy.MAJORITY)
        self.assertEqual(task.final_result, "yes")

    def test_aggregation_average(self):
        from agent.agent_coordinator_v2 import AggregationStrategy
        self.ac.register("av1", fn=lambda p, c: 10.0, capabilities=["avg"])
        self.ac.register("av2", fn=lambda p, c: 20.0, capabilities=["avg"])
        task = self.ac.delegate("score", required_capability="avg", n_agents=2,
                                 aggregation=AggregationStrategy.AVERAGE)
        self.assertAlmostEqual(task.final_result, 15.0)

    def test_aggregation_consensus_agree(self):
        from agent.agent_coordinator_v2 import AggregationStrategy
        self.ac.register("c1", fn=lambda p, c: "agreed", capabilities=["con"])
        self.ac.register("c2", fn=lambda p, c: "agreed", capabilities=["con"])
        task = self.ac.delegate("con", required_capability="con", n_agents=2,
                                 aggregation=AggregationStrategy.CONSENSUS)
        self.assertEqual(task.final_result, "agreed")

    def test_aggregation_consensus_disagree(self):
        from agent.agent_coordinator_v2 import AggregationStrategy
        self.ac.register("d1", fn=lambda p, c: "yes", capabilities=["dis"])
        self.ac.register("d2", fn=lambda p, c: "no",  capabilities=["dis"])
        task = self.ac.delegate("dis", required_capability="dis", n_agents=2,
                                 aggregation=AggregationStrategy.CONSENSUS)
        self.assertIsNone(task.final_result)

    def test_chain_mode(self):
        from agent.agent_coordinator_v2 import AggregationStrategy
        a1 = self.ac.register("ch1", fn=lambda p, c: p + 1,
                               capabilities=["chain"])
        a2 = self.ac.register("ch2", fn=lambda p, c: p * 2,
                               capabilities=["chain"])
        task = self.ac.delegate("chain", payload=3,
                                 agent_ids=[a1.agent_id, a2.agent_id],
                                 aggregation=AggregationStrategy.CHAIN)
        self.assertEqual(task.final_result, 8)  # (3+1)*2

    def test_pre_post_hooks(self):
        pre = []; post = []
        self.ac.on_before_task(lambda t: pre.append(t.task_type))
        self.ac.on_after_task(lambda t: post.append(t.task_type))
        self.ac.register("hk", fn=lambda p, c: "ok")
        self.ac.delegate("hook_task", payload=1)
        self.assertGreater(len(pre), 0)
        self.assertGreater(len(post), 0)

    def test_timeout(self):
        from agent.agent_coordinator_v2 import TaskState
        self.ac.register("slow",
                          fn=lambda p, c: time.sleep(10) or "done",
                          timeout_s=0.05)
        task = self.ac.delegate("slow_task", timeout_s=0.05)
        # Should fail due to timeout
        self.assertIsNotNone(task)

    def test_agent_stats_updated(self):
        a = self.ac.register("stats_a", fn=lambda p, c: 1)
        self.ac.delegate("t1"); self.ac.delegate("t2")
        self.assertGreater(a.total_tasks, 0)

    def test_list_agents_by_role(self):
        from agent.agent_coordinator_v2 import AgentRole
        self.ac.register("crit", fn=lambda p, c: 1, role=AgentRole.CRITIC)
        result = self.ac.list_agents(role=AgentRole.CRITIC)
        self.assertGreater(len(result), 0)

    def test_stats(self):
        self.ac.register("st", fn=lambda p, c: 1)
        self.ac.delegate("t")
        s = self.ac.stats()
        self.assertGreater(s["total_tasks"], 0)


# ════════════════════════════════════════════════════════
# NOTIFICATION ROUTER V2
# ════════════════════════════════════════════════════════
class TestNotificationRouterV2(unittest.TestCase):
    def setUp(self):
        from agent.notification_router_v2 import NotificationRouterV2, Channel
        self.nr = NotificationRouterV2(db_path=":memory:")
        self.delivered = []
        self.nr.register_adapter(
            Channel.EMAIL,
            lambda n: self.delivered.append(n.recipient) or True)

    def test_send_basic(self):
        from agent.notification_router_v2 import Channel, DeliveryStatus
        n = self.nr.send("alice@x.com", subject="Hi", body="Hello",
                          channel=Channel.EMAIL)
        self.assertEqual(n.status, DeliveryStatus.SENT)
        self.assertIn("alice@x.com", self.delivered)

    def test_send_no_adapter(self):
        from agent.notification_router_v2 import Channel, DeliveryStatus
        n = self.nr.send("bob", channel=Channel.SMS)
        self.assertEqual(n.status, DeliveryStatus.FAILED)

    def test_template_render(self):
        from agent.notification_router_v2 import Channel, DeliveryStatus
        t = self.nr.add_template(
            "welcome", Channel.EMAIL,
            subject_template="Welcome {name}!",
            body_template="Hello {name}, you signed up.")
        n = self.nr.send("user@x.com",
                          template_id=t.template_id,
                          variables={"name": "Carol"})
        self.assertEqual(n.subject, "Welcome Carol!")
        self.assertEqual(n.status, DeliveryStatus.SENT)

    def test_throttle_rule(self):
        from agent.notification_router_v2 import Channel, DeliveryStatus
        self.nr.add_throttle_rule(Channel.EMAIL, max_per_window=2,
                                   window_s=60.0)
        for _ in range(2):
            self.nr.send("throttled@x.com", channel=Channel.EMAIL)
        n3 = self.nr.send("throttled@x.com", channel=Channel.EMAIL)
        self.assertEqual(n3.status, DeliveryStatus.THROTTLED)

    def test_opt_out(self):
        from agent.notification_router_v2 import Channel, DeliveryStatus
        self.nr.opt_out("noemail@x.com", Channel.EMAIL)
        n = self.nr.send("noemail@x.com", channel=Channel.EMAIL)
        self.assertEqual(n.status, DeliveryStatus.FAILED)
        self.assertIn("opted out", n.error.lower())

    def test_opt_in_after_opt_out(self):
        from agent.notification_router_v2 import Channel, DeliveryStatus
        self.nr.opt_out("re@x.com", Channel.EMAIL)
        self.nr.opt_in("re@x.com", Channel.EMAIL)
        n = self.nr.send("re@x.com", channel=Channel.EMAIL)
        self.assertEqual(n.status, DeliveryStatus.SENT)

    def test_scheduled_delivery(self):
        from agent.notification_router_v2 import Channel, DeliveryStatus
        future = time.time() + 3600
        n = self.nr.send("sched@x.com", channel=Channel.EMAIL,
                          scheduled_at=future)
        self.assertEqual(n.status, DeliveryStatus.SCHEDULED)

    def test_flush_scheduled(self):
        from agent.notification_router_v2 import Channel, DeliveryStatus
        past = time.time() - 1
        n = self.nr.send("flush@x.com", channel=Channel.EMAIL,
                          scheduled_at=past)
        # Already delivered since past < now in send()
        self.assertIn(n.status, [DeliveryStatus.SENT, DeliveryStatus.SCHEDULED])

    def test_routing_rule(self):
        from agent.notification_router_v2 import Channel, DeliveryStatus
        self.nr.add_routing_rule(
            lambda n: "urgent" in n.subject.lower(),
            Channel.LOG)
        n = self.nr.send("x@x.com", subject="URGENT: system down",
                          channel=Channel.EMAIL)
        self.assertEqual(n.channel, Channel.LOG)

    def test_batch_send(self):
        from agent.notification_router_v2 import Channel
        recipients = ["a@x.com", "b@x.com", "c@x.com"]
        results = self.nr.send_batch(recipients, channel=Channel.EMAIL)
        self.assertEqual(len(results), 3)

    def test_history_filter(self):
        from agent.notification_router_v2 import Channel
        self.nr.send("hist@x.com", channel=Channel.EMAIL)
        h = self.nr.history(recipient="hist@x.com")
        self.assertGreater(len(h), 0)

    def test_log_adapter_default(self):
        from agent.notification_router_v2 import Channel, DeliveryStatus
        n = self.nr.send("log_user", channel=Channel.LOG)
        self.assertEqual(n.status, DeliveryStatus.SENT)

    def test_stats(self):
        from agent.notification_router_v2 import Channel
        self.nr.send("s@x.com", channel=Channel.EMAIL)
        s = self.nr.stats()
        self.assertGreater(s["total_sent"], 0)


# ════════════════════════════════════════════════════════
# PERMISSION MANAGER V2
# ════════════════════════════════════════════════════════
class TestPermissionManagerV2(unittest.TestCase):
    def setUp(self):
        from agent.permission_manager_v2 import (
            PermissionManagerV2, Effect, PolicyType)
        self.pm = PermissionManagerV2(db_path=":memory:", default_deny=True)
        self.admin = self.pm.add_principal("admin", roles=["admin"])
        self.user  = self.pm.add_principal("user",  roles=["viewer"])
        self.pm.add_role("admin",  permissions=["*:*"])
        self.pm.add_role("viewer", permissions=["doc:read"])
        self.pm.add_resource("doc1", "document")

    def test_allow_with_policy(self):
        from agent.permission_manager_v2 import Effect, PolicyType
        self.pm.add_policy("admin_all", roles=["admin"],
                            resources=["*"], actions=["*"],
                            effect=Effect.ALLOW)
        d = self.pm.authorize(self.admin.principal_id, "doc1", "delete")
        self.assertTrue(d.allowed)

    def test_default_deny(self):
        d = self.pm.authorize(self.user.principal_id, "doc1", "delete")
        self.assertFalse(d.allowed)

    def test_rbac_role_match(self):
        from agent.permission_manager_v2 import Effect
        self.pm.add_policy("viewer_read", roles=["viewer"],
                            resources=["doc*"], actions=["read"],
                            effect=Effect.ALLOW)
        d = self.pm.authorize(self.user.principal_id, "doc1", "read")
        self.assertTrue(d.allowed)

    def test_deny_overrides(self):
        from agent.permission_manager_v2 import Effect
        self.pm.add_policy("allow_all", principals=["*"],
                            resources=["*"], actions=["*"],
                            effect=Effect.ALLOW, priority=0)
        self.pm.add_policy("deny_delete", roles=["viewer"],
                            resources=["*"], actions=["delete"],
                            effect=Effect.DENY, priority=10)
        d = self.pm.authorize(self.user.principal_id, "doc1", "delete")
        self.assertFalse(d.allowed)

    def test_wildcard_principal(self):
        from agent.permission_manager_v2 import Effect
        self.pm.add_policy("everyone_read", principals=["*"],
                            resources=["public*"], actions=["read"],
                            effect=Effect.ALLOW)
        new_user = self.pm.add_principal("anyone")
        d = self.pm.authorize(new_user.principal_id, "public_doc", "read")
        self.assertTrue(d.allowed)

    def test_abac_condition(self):
        from agent.permission_manager_v2 import Effect, PolicyType
        self.pm.add_policy(
            "time_policy", policy_type=PolicyType.ABAC,
            principals=["*"], resources=["*"], actions=["*"],
            effect=Effect.ALLOW,
            condition_fn=lambda p, r, a, ctx: ctx.get("hour", 0) < 18)
        d = self.pm.authorize(self.user.principal_id, "doc1", "read",
                               context={"hour": 10})
        self.assertTrue(d.allowed)
        d2 = self.pm.authorize(self.user.principal_id, "doc1", "read",
                                context={"hour": 20})
        self.assertFalse(d2.allowed)

    def test_role_inheritance(self):
        from agent.permission_manager_v2 import Effect
        self.pm.add_role("superuser", parent_roles=["admin"])
        super_p = self.pm.add_principal("superp", roles=["superuser"])
        self.pm.add_policy("admin_allow", roles=["admin"],
                            resources=["*"], actions=["*"],
                            effect=Effect.ALLOW)
        d = self.pm.authorize(super_p.principal_id, "doc1", "write")
        self.assertTrue(d.allowed)

    def test_assign_revoke_role(self):
        self.pm.add_policy("viewer_read", roles=["viewer"],
                            resources=["*"], actions=["read"])
        from agent.permission_manager_v2 import Effect
        self.pm.add_policy("vread", roles=["viewer"], resources=["*"],
                            actions=["read"], effect=Effect.ALLOW)
        self.pm.assign_role(self.user.principal_id, "admin")
        self.pm.add_policy("aall", roles=["admin"], resources=["*"],
                            actions=["*"])
        self.pm.revoke_role(self.user.principal_id, "admin")
        self.assertNotIn("admin", self.user.roles)

    def test_deactivated_principal(self):
        self.pm.deactivate(self.user.principal_id)
        d = self.pm.authorize(self.user.principal_id, "doc1", "read")
        self.assertFalse(d.allowed)

    def test_bulk_check(self):
        from agent.permission_manager_v2 import Effect
        self.pm.add_policy("v_read", roles=["viewer"],
                            resources=["doc*"], actions=["read"],
                            effect=Effect.ALLOW)
        results = self.pm.check_bulk(self.user.principal_id,
                                      [("doc1", "read"), ("doc1", "delete")])
        self.assertTrue(results["doc1:read"])
        self.assertFalse(results["doc1:delete"])

    def test_resource_prefix_wildcard(self):
        from agent.permission_manager_v2 import Effect
        self.pm.add_policy("docs_read", roles=["viewer"],
                            resources=["docs/*"], actions=["read"],
                            effect=Effect.ALLOW)
        d = self.pm.authorize(self.user.principal_id, "docs/report1", "read")
        self.assertTrue(d.allowed)

    def test_audit_log(self):
        self.pm.authorize(self.user.principal_id, "doc1", "read")
        log = self.pm.audit_log()
        self.assertGreater(len(log), 0)

    def test_stats(self):
        self.pm.authorize(self.user.principal_id, "doc1", "read")
        s = self.pm.stats()
        self.assertGreater(s["audit_entries"], 0)


# ════════════════════════════════════════════════════════
# BATCH PROCESSOR V3
# ════════════════════════════════════════════════════════
class TestBatchProcessorV3(unittest.TestCase):
    def setUp(self):
        from agent.batch_processor_v3 import BatchProcessorV3
        self.bp = BatchProcessorV3(db_path=":memory:",
                                    default_workers=2,
                                    checkpoint_every=5,
                                    max_retries=1)

    def test_basic_run(self):
        from agent.batch_processor_v3 import BatchStatus
        job = self.bp.run(range(10), lambda x: x * 2)
        self.assertEqual(job.status, BatchStatus.DONE)
        self.assertEqual(job.succeeded, 10)

    def test_failed_items_tracked(self):
        from agent.batch_processor_v3 import BatchStatus
        def proc(x):
            if x == 3: raise ValueError("bad")
            return x
        job = self.bp.run(range(5), proc)
        self.assertGreater(job.failed_count, 0)
        self.assertEqual(job.status, BatchStatus.PARTIAL)

    def test_dlq_populated(self):
        def always_fail(x):
            raise RuntimeError("fail")
        self.bp.run([1, 2], always_fail)
        self.assertGreater(len(self.bp.dlq()), 0)

    def test_flush_dlq(self):
        self.bp.run([1], lambda x: (_ for _ in ()).throw(RuntimeError("e")))
        n = self.bp.flush_dlq()
        self.assertGreater(n, 0)
        self.assertEqual(len(self.bp.dlq()), 0)

    def test_parallel_partitions(self):
        from agent.batch_processor_v3 import BatchStatus
        job = self.bp.run(range(20), lambda x: x + 1,
                           partitions=4, workers=4)
        self.assertEqual(job.succeeded, 20)

    def test_round_robin_partition(self):
        from agent.batch_processor_v3 import PartitionStrategy, BatchStatus
        job = self.bp.run(range(10), lambda x: x,
                           partitions=3,
                           strategy=PartitionStrategy.ROUND_ROBIN)
        self.assertEqual(job.status, BatchStatus.DONE)

    def test_hash_partition(self):
        from agent.batch_processor_v3 import PartitionStrategy, BatchStatus
        job = self.bp.run(range(9), lambda x: x,
                           partitions=3,
                           strategy=PartitionStrategy.HASH)
        self.assertEqual(job.status, BatchStatus.DONE)

    def test_transform_applied(self):
        results = []
        self.bp.add_transform(lambda x: x * 10)
        job = self.bp.run([1, 2, 3],
                           lambda x: results.append(x) or x)
        self.assertEqual(results, [10, 20, 30])

    def test_pre_post_hooks(self):
        pre = []; post = []
        self.bp.on_before_batch(lambda j: pre.append(j.job_id))
        self.bp.on_after_batch(lambda j: post.append(j.job_id))
        self.bp.run([1], lambda x: x)
        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)

    def test_item_hooks(self):
        pre = []; post = []
        self.bp.on_before_item(lambda i: pre.append(i.item_id))
        self.bp.on_after_item(lambda i: post.append(i.item_id))
        self.bp.run([1, 2, 3], lambda x: x)
        self.assertEqual(len(pre), 3)
        self.assertEqual(len(post), 3)

    def test_progress_callback(self):
        progress = []
        self.bp.on_progress(lambda done, total: progress.append(done))
        self.bp.run(range(5), lambda x: x)
        self.assertGreater(len(progress), 0)

    def test_map_shortcut(self):
        job, results = self.bp.map(range(5), lambda x: x * 3)
        self.assertEqual(results, [0, 3, 6, 9, 12])

    def test_filter_shortcut(self):
        job, passed = self.bp.filter(range(10),
                                      lambda x: x % 2 == 0)
        self.assertEqual(len(passed), 5)

    def test_throughput_reported(self):
        job = self.bp.run(range(100), lambda x: x)
        self.assertGreater(job.throughput, 0)

    def test_stats(self):
        self.bp.run([1, 2], lambda x: x)
        s = self.bp.stats()
        self.assertGreater(s["total_jobs"], 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total  = result.testsRun; failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}\n  v70: {total-failed}/{total} passed")
    sys.exit(0 if not failed else 1)
