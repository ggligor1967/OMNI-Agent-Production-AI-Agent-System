"""
OMNI AGENT v6 — Test Suite
Covers: Evaluation, Persona, KnowledgeGraph, ConfigManager
Run: python3 tests/test_v6_modules.py
"""
import asyncio, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ══════════════════════════════════════════════════════════════════════════════
# EVALUATION
# ══════════════════════════════════════════════════════════════════════════════

class TestScoringMethods(unittest.TestCase):
    def setUp(self):
        from agent.evaluation import ResponseScorer
        self.scorer = ResponseScorer()

    def _score(self, response, criteria):
        return asyncio.get_event_loop().run_until_complete(
            self.scorer._score_one(response, criteria))

    def test_exact_match_pass(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("yes", ScoringCriteria(ScoringMethod.EXACT_MATCH, expected="yes"))
        self.assertEqual(r.score, 1.0); self.assertTrue(r.passed)

    def test_exact_match_fail(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("no", ScoringCriteria(ScoringMethod.EXACT_MATCH, expected="yes"))
        self.assertEqual(r.score, 0.0)

    def test_exact_match_case_insensitive(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("YES", ScoringCriteria(ScoringMethod.EXACT_MATCH, expected="yes"))
        self.assertEqual(r.score, 1.0)

    def test_substring_found(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("Capital is Paris.", ScoringCriteria(ScoringMethod.SUBSTRING, expected="Paris"))
        self.assertEqual(r.score, 1.0)

    def test_substring_not_found(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("Capital is London.", ScoringCriteria(ScoringMethod.SUBSTRING, expected="Paris"))
        self.assertEqual(r.score, 0.0)

    def test_regex_match(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("def add(a, b):", ScoringCriteria(ScoringMethod.REGEX, expected=r"def\s+\w+\("))
        self.assertEqual(r.score, 1.0)

    def test_regex_no_match(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("no function", ScoringCriteria(ScoringMethod.REGEX, expected=r"def\s+\w+\("))
        self.assertEqual(r.score, 0.0)

    def test_regex_invalid(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("text", ScoringCriteria(ScoringMethod.REGEX, expected=r"[invalid"))
        self.assertEqual(r.score, 0.0)

    def test_keyword_all_found(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("red blue yellow", ScoringCriteria(ScoringMethod.KEYWORD,
                        keywords=["red","blue","yellow"]))
        self.assertEqual(r.score, 1.0)

    def test_keyword_partial(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("only red", ScoringCriteria(ScoringMethod.KEYWORD,
                        keywords=["red","blue","yellow"]))
        self.assertAlmostEqual(r.score, 1/3, places=2)

    def test_keyword_empty(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("anything", ScoringCriteria(ScoringMethod.KEYWORD, keywords=[]))
        self.assertEqual(r.score, 1.0)

    def test_length_in_range(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("Hello world", ScoringCriteria(ScoringMethod.LENGTH, min_length=5, max_length=50))
        self.assertEqual(r.score, 1.0)

    def test_length_too_short(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("Hi", ScoringCriteria(ScoringMethod.LENGTH, min_length=10, max_length=50))
        self.assertLess(r.score, 1.0)

    def test_length_too_long(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("x"*100, ScoringCriteria(ScoringMethod.LENGTH, min_length=1, max_length=5))
        self.assertLess(r.score, 1.0)

    def test_custom_fn(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        r = self._score("x", ScoringCriteria(ScoringMethod.CUSTOM, custom_fn=lambda r,e: 0.75))
        self.assertAlmostEqual(r.score, 0.75)

    def test_weighted_avg(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        criteria = [
            ScoringCriteria(ScoringMethod.SUBSTRING, expected="255", weight=2.0),
            ScoringCriteria(ScoringMethod.LENGTH, min_length=1, max_length=100, weight=1.0),
        ]
        total, results = asyncio.get_event_loop().run_until_complete(
            self.scorer.score_all("answer is 255", criteria))
        self.assertGreater(total, 0.8); self.assertEqual(len(results), 2)

    def test_heavy_weight_dominates(self):
        from agent.evaluation import ScoringCriteria, ScoringMethod
        criteria = [
            ScoringCriteria(ScoringMethod.SUBSTRING, expected="999", weight=10.0),
            ScoringCriteria(ScoringMethod.LENGTH, min_length=1, max_length=100, weight=1.0),
        ]
        total, _ = asyncio.get_event_loop().run_until_complete(
            self.scorer.score_all("answer is 255", criteria))
        self.assertLess(total, 0.2)


class TestEvalSuite(unittest.TestCase):
    def test_add_and_chain(self):
        from agent.evaluation import EvalSuite, EvalCase, ScoringCriteria, ScoringMethod
        suite = (EvalSuite("s")
                 .add(EvalCase("c1","p1",criteria=[ScoringCriteria(ScoringMethod.SUBSTRING,expected="x")]))
                 .add(EvalCase("c2","p2",criteria=[ScoringCriteria(ScoringMethod.SUBSTRING,expected="y")])))
        self.assertEqual(len(suite.cases), 2)

    def test_filter_by_category(self):
        from agent.evaluation import EvalSuite, EvalCase, ScoringCriteria, ScoringMethod
        suite = EvalSuite("s")
        for i,cat in enumerate(["math","math","lang"]):
            suite.add(EvalCase(f"c{i}","p",category=cat,
                               criteria=[ScoringCriteria(ScoringMethod.SUBSTRING,expected="x")]))
        self.assertEqual(len(suite.filter_by_category("math").cases), 2)
        self.assertEqual(len(suite.filter_by_category("lang").cases), 1)


class TestSuiteResult(unittest.TestCase):
    def _make(self, scores):
        from agent.evaluation import SuiteResult, CaseResult, CriterionResult
        cases = [CaseResult(f"c{i}","m","r",[CriterionResult("sub",s,s>=0.5)],s,s>=0.5,100.0)
                 for i,s in enumerate(scores)]
        return SuiteResult("suite","model","run1",cases,time.time()-5,time.time())

    def test_pass_rate_all(self):
        self.assertEqual(self._make([1.0,1.0]).pass_rate, 1.0)

    def test_pass_rate_half(self):
        self.assertAlmostEqual(self._make([1.0,0.0]).pass_rate, 0.5)

    def test_avg_score(self):
        self.assertAlmostEqual(self._make([0.6,1.0]).avg_score, 0.8)

    def test_to_dict(self):
        d = self._make([1.0]).to_dict()
        self.assertIn("pass_rate",d); self.assertIn("avg_score",d)

    def test_to_markdown(self):
        md = self._make([1.0,0.0]).to_markdown()
        self.assertIn("# Eval Report", md)
        self.assertIn("✓", md); self.assertIn("✗", md)

    def test_empty(self):
        from agent.evaluation import SuiteResult
        r = SuiteResult("s","m","r",[],time.time(),time.time())
        self.assertEqual(r.pass_rate, 0.0); self.assertEqual(r.avg_score, 0.0)


class TestEvalResultStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent.evaluation import EvalResultStore
        self.store = EvalResultStore(os.path.join(self.tmpdir,"eval.db"))

    def _make(self, run_id="r1", model="m-x", score=1.0):
        from agent.evaluation import SuiteResult, CaseResult, CriterionResult
        return SuiteResult("suite",model,run_id,
            [CaseResult("c1",model,"resp",[CriterionResult("sub",score,score>=0.5)],score,score>=0.5,100.0)],
            time.time()-5,time.time())

    def test_save_retrieve(self):
        self.store.save(self._make())
        h = self.store.get_history("suite","m-x")
        self.assertEqual(len(h),1); self.assertEqual(h[0]["model_id"],"m-x")

    def test_multiple_runs(self):
        for i in range(3):
            self.store.save(self._make(run_id=f"r{i}",score=i/2))
        self.assertEqual(len(self.store.get_history("suite","m-x")),3)

    def test_model_comparison(self):
        self.store.save(self._make(model="a",score=0.9))
        self.store.save(self._make(model="b",score=0.6,run_id="r2"))
        comp = self.store.model_comparison("suite")
        self.assertEqual(comp[0]["model_id"],"a")

    def test_history_limit(self):
        for i in range(5): self.store.save(self._make(run_id=f"r{i}"))
        self.assertEqual(len(self.store.get_history("suite",limit=2)),2)


class TestEvaluatorBuiltins(unittest.TestCase):
    def setUp(self):
        from agent.evaluation import Evaluator
        self.ev = Evaluator(llm=None)

    def test_builtin_suites(self):
        names = {s["name"] for s in self.ev.list_suites()}
        self.assertIn("basic_capabilities",names)
        self.assertIn("code_generation",names)
        self.assertIn("instruction_following",names)

    def test_get_suite(self):
        s = self.ev.get_suite("basic_capabilities")
        self.assertIsNotNone(s); self.assertGreater(len(s.cases),0)

    def test_register_custom(self):
        from agent.evaluation import EvalSuite, EvalCase, ScoringCriteria, ScoringMethod
        suite = EvalSuite("my_suite")
        suite.add(EvalCase("q1","hi?",criteria=[ScoringCriteria(ScoringMethod.SUBSTRING,expected="hello")]))
        self.ev.register_suite(suite)
        self.assertIsNotNone(self.ev.get_suite("my_suite"))

    def test_ab_report(self):
        from agent.evaluation import SuiteResult, CaseResult, CriterionResult
        r = SuiteResult("s","model-a","r1",
            [CaseResult("c1","model-a","resp",[CriterionResult("sub",1.0,True)],1.0,True,100.0)],
            time.time()-5,time.time())
        report = self.ev.ab_report({"model-a":r})
        self.assertIn("| Model |",report); self.assertIn("model-a",report)


# ══════════════════════════════════════════════════════════════════════════════
# PERSONA
# ══════════════════════════════════════════════════════════════════════════════

class TestPersonaRegistry(unittest.TestCase):
    def setUp(self):
        from agent.persona import PersonaRegistry
        self.reg = PersonaRegistry()

    def test_builtins_loaded(self):
        names = {p["name"] for p in self.reg.list_personas()}
        for n in ["assistant","tutor","coach","analyst","creative","engineer","concise"]:
            self.assertIn(n, names)

    def test_get_existing(self):
        p = self.reg.get("engineer")
        self.assertIsNotNone(p); self.assertEqual(p.name,"engineer")

    def test_get_nonexistent(self): self.assertIsNone(self.reg.get("xyz"))

    def test_register_custom(self):
        from agent.persona import Persona
        self.reg.register(Persona(name="pirate",display_name="Pirate",system_inject="Arr!"))
        self.assertIsNotNone(self.reg.get("pirate"))

    def test_list_by_tag(self):
        results = self.reg.list_personas(tag="education")
        self.assertTrue(all("education" in p["tags"] for p in results))

    def test_search(self):
        results = self.reg.search("education")
        self.assertGreater(len(results),0)


class TestPersonaSystemPrompt(unittest.TestCase):
    def setUp(self):
        from agent.persona import PersonaRegistry
        self.reg = PersonaRegistry()

    def test_base_included(self):
        sp = self.reg.get("assistant").build_system_prompt("You must be honest.")
        self.assertIn("honest",sp)

    def test_system_inject_present(self):
        sp = self.reg.get("engineer").build_system_prompt()
        self.assertIn("pragmatic",sp)

    def test_tone_words_present(self):
        sp = self.reg.get("tutor").build_system_prompt()
        self.assertTrue(any(w in sp.lower() for w in ["patient","encouraging"]))

    def test_few_shot_examples(self):
        msgs = self.reg.get("tutor").few_shot_messages()
        self.assertEqual(len(msgs),2)
        self.assertEqual(msgs[0]["role"],"user")
        self.assertEqual(msgs[1]["role"],"assistant")

    def test_no_examples_for_analyst(self):
        self.assertEqual(len(self.reg.get("analyst").few_shot_messages()),0)


class TestPersonaBlend(unittest.TestCase):
    def setUp(self):
        from agent.persona import PersonaRegistry
        self.reg = PersonaRegistry()

    def test_formality_blend(self):
        b = self.reg.blend("engineer","creative",weight_a=0.7)
        self.assertAlmostEqual(b.formality, 0.6*0.7+0.2*0.3, places=2)

    def test_blend_tag(self):
        self.assertIn("blend",self.reg.blend("engineer","creative").tags)

    def test_missing_persona_returns_none(self):
        self.assertIsNone(self.reg.blend("engineer","nonexistent"))

    def test_combines_parent_tags(self):
        b = self.reg.blend("tutor","coach")
        self.assertIn("education", set(b.tags)); self.assertIn("goals", set(b.tags))


class TestPersonaAutoDetect(unittest.TestCase):
    def setUp(self):
        from agent.persona import PersonaRegistry
        self.reg = PersonaRegistry()

    def test_detect_engineer(self): self.assertEqual(self.reg.detect_best("write python code"),"engineer")
    def test_detect_tutor(self):    self.assertEqual(self.reg.detect_best("explain recursion"),"tutor")
    def test_detect_analyst(self):  self.assertEqual(self.reg.detect_best("analyze data metrics"),"analyst")
    def test_detect_creative(self): self.assertEqual(self.reg.detect_best("write me a poem"),"creative")
    def test_detect_concise(self):  self.assertEqual(self.reg.detect_best("quick tldr summary"),"concise")
    def test_detect_default(self):  self.assertEqual(self.reg.detect_best("hello there"),"assistant")


class TestPersonaManager(unittest.TestCase):
    def setUp(self):
        from agent.persona import PersonaRegistry, PersonaManager
        self.pm = PersonaManager(PersonaRegistry())

    def test_default_is_assistant(self): self.assertEqual(self.pm.get_persona_name("s","u"),"assistant")
    def test_set_user(self):
        self.assertTrue(self.pm.set_user_persona("u1","engineer"))
        self.assertEqual(self.pm.get_persona_name("","u1"),"engineer")
    def test_session_overrides_user(self):
        self.pm.set_user_persona("u1","engineer"); self.pm.set_session_persona("s1","tutor")
        self.assertEqual(self.pm.get_persona_name("s1","u1"),"tutor")
    def test_clear_session_reverts(self):
        self.pm.set_user_persona("u1","engineer"); self.pm.set_session_persona("s1","tutor")
        self.pm.clear_session_persona("s1")
        self.assertEqual(self.pm.get_persona_name("s1","u1"),"engineer")
    def test_invalid_persona_fails(self): self.assertFalse(self.pm.set_user_persona("u1","nope"))
    def test_build_system_prompt(self):
        self.pm.set_user_persona("u1","engineer")
        sp = self.pm.build_system_prompt("s1","u1","Base.")
        self.assertIn("Base",sp); self.assertIn("pragmatic",sp)
    def test_get_examples(self):
        self.pm.set_user_persona("u1","tutor")
        self.assertGreater(len(self.pm.get_examples("s1","u1")),0)
    def test_auto_detect(self):
        detected = self.pm.auto_detect_and_set("s2","write python code")
        self.assertEqual(detected,"engineer")
        self.assertEqual(self.pm.get_persona_name("s2"),"engineer")
    def test_auto_detect_no_override_explicit(self):
        self.pm.set_session_persona("s3","tutor")
        self.assertIsNone(self.pm.auto_detect_and_set("s3","write python"))
        self.assertEqual(self.pm.get_persona_name("s3"),"tutor")
    def test_session_info(self):
        self.pm.set_session_persona("s4","analyst")
        info = self.pm.session_info("s4")
        self.assertEqual(info["persona_name"],"analyst")
        self.assertEqual(info["source"],"session")


# ══════════════════════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphStore(unittest.TestCase):
    def setUp(self):
        from agent.knowledge_graph import GraphStore, Entity, EntityType, _make_id
        self.tmpdir = tempfile.mkdtemp()
        self.store = GraphStore(os.path.join(self.tmpdir,"kg.db"))
        self.EntityType = EntityType
        self.e1 = Entity(id=_make_id(),name="Alice",entity_type=EntityType.PERSON)
        self.e2 = Entity(id=_make_id(),name="Bob",  entity_type=EntityType.PERSON)
        self.e3 = Entity(id=_make_id(),name="ACME", entity_type=EntityType.ORG)
        for e in [self.e1,self.e2,self.e3]: self.store.upsert_entity(e)

    def test_get_entity(self):
        got = self.store.get_entity(self.e1.id)
        self.assertEqual(got.name,"Alice")

    def test_get_nonexistent(self): self.assertIsNone(self.store.get_entity("nope"))

    def test_find_by_name(self):
        self.assertIsNotNone(self.store.find_entity("alice"))

    def test_find_by_alias(self):
        from agent.knowledge_graph import Entity, EntityType, _make_id
        e = Entity(id=_make_id(),name="Corp",entity_type=EntityType.ORG,aliases=["The Company"])
        self.store.upsert_entity(e)
        self.assertIsNotNone(self.store.find_entity("The Company",fuzzy=True))

    def test_list_all(self): self.assertEqual(len(self.store.list_entities()),3)

    def test_list_by_type(self):
        self.assertEqual(len(self.store.list_entities(entity_type=self.EntityType.ORG)),1)
        self.assertEqual(len(self.store.list_entities(entity_type=self.EntityType.PERSON)),2)

    def test_delete(self):
        self.store.delete_entity(self.e3.id)
        self.assertIsNone(self.store.get_entity(self.e3.id))

    def test_merge(self):
        from agent.knowledge_graph import Entity, EntityType, _make_id
        e_dup = Entity(id=_make_id(),name="Alice Smith",entity_type=EntityType.PERSON)
        self.store.upsert_entity(e_dup)
        self.store.merge_entities(self.e1.id, e_dup.id)
        merged = self.store.get_entity(self.e1.id)
        self.assertIn("Alice Smith", merged.aliases)
        self.assertIsNone(self.store.get_entity(e_dup.id))


class TestGraphRelationships(unittest.TestCase):
    def setUp(self):
        from agent.knowledge_graph import GraphStore, Entity, Relationship, EntityType, _make_id
        self.tmpdir = tempfile.mkdtemp()
        self.store = GraphStore(os.path.join(self.tmpdir,"kg.db"))
        self.ids = {}
        for name, typ in [("Alice",EntityType.PERSON),("Bob",EntityType.PERSON),("ACME",EntityType.ORG)]:
            e = Entity(id=_make_id(),name=name,entity_type=typ)
            self.store.upsert_entity(e); self.ids[name] = e.id
        for src,tgt,lbl in [("Alice","ACME","WORKS_AT"),("Bob","ACME","WORKS_AT")]:
            self.store.upsert_relationship(
                Relationship(id=_make_id(),source_id=self.ids[src],target_id=self.ids[tgt],label=lbl))

    def test_outgoing(self):
        rels = self.store.get_relationships(self.ids["Alice"],direction="out")
        self.assertEqual(len(rels),1); self.assertEqual(rels[0].label,"WORKS_AT")

    def test_incoming(self):
        self.assertEqual(len(self.store.get_relationships(self.ids["ACME"],direction="in")),2)

    def test_filter_by_label(self):
        self.assertEqual(len(self.store.get_relationships(self.ids["Alice"],label="WORKS_AT")),1)
        self.assertEqual(len(self.store.get_relationships(self.ids["Alice"],label="FOUNDED")),0)

    def test_delete_cascades(self):
        self.store.delete_entity(self.ids["Alice"])
        rels = self.store.get_relationships(self.ids["ACME"],direction="in")
        self.assertEqual(len(rels),1)


class TestGraphPathAndStats(unittest.TestCase):
    def setUp(self):
        from agent.knowledge_graph import GraphStore, Entity, Relationship, EntityType, _make_id
        self.tmpdir = tempfile.mkdtemp()
        self.store = GraphStore(os.path.join(self.tmpdir,"kg.db"))
        self.ids = {}
        for name,typ in [("A",EntityType.PERSON),("B",EntityType.PERSON),
                         ("C",EntityType.ORG),("D",EntityType.ORG)]:
            e = Entity(id=_make_id(),name=name,entity_type=typ)
            self.store.upsert_entity(e); self.ids[name] = e.id
        for src,tgt in [("A","C"),("B","C"),("D","C")]:
            self.store.upsert_relationship(
                Relationship(id=_make_id(),source_id=self.ids[src],target_id=self.ids[tgt],label="REL"))

    def test_path_direct(self):
        p = self.store.shortest_path(self.ids["A"],self.ids["C"])
        self.assertEqual(p.length,1)

    def test_path_indirect(self):
        p = self.store.shortest_path(self.ids["A"],self.ids["B"])
        self.assertEqual(p.length,2)

    def test_path_self(self):
        p = self.store.shortest_path(self.ids["A"],self.ids["A"])
        self.assertEqual(p.length,0)

    def test_no_path(self):
        from agent.knowledge_graph import Entity, EntityType, _make_id
        iso = Entity(id=_make_id(),name="Isolated",entity_type=EntityType.UNKNOWN)
        self.store.upsert_entity(iso)
        self.assertIsNone(self.store.shortest_path(self.ids["A"],iso.id))

    def test_neighbors(self):
        nbrs = self.store.get_neighbors(self.ids["C"],max_hops=1)
        self.assertGreater(len(nbrs["entities"]),0)

    def test_connected_components(self):
        comps = self.store.connected_components()
        self.assertGreater(len(comps[0]),1)

    def test_stats(self):
        s = self.store.stats()
        self.assertGreaterEqual(s["entities"],4); self.assertGreaterEqual(s["relationships"],3)

    def test_export(self):
        d = self.store.export_json()
        self.assertIn("entities",d); self.assertIn("relationships",d)


class TestKnowledgeGraph(unittest.TestCase):
    def setUp(self):
        from agent.knowledge_graph import GraphStore, KnowledgeGraph, EntityType
        self.tmpdir = tempfile.mkdtemp()
        self.kg = KnowledgeGraph(store=GraphStore(os.path.join(self.tmpdir,"kg.db")), llm=None)
        self.ET = EntityType

    def test_add_find(self):
        self.kg.add_entity("Alice",self.ET.PERSON)
        self.assertIsNotNone(self.kg.find("Alice"))

    def test_add_relationship(self):
        self.kg.add_entity("Alice",self.ET.PERSON); self.kg.add_entity("ACME",self.ET.ORG)
        rel = self.kg.add_relationship("Alice","ACME","WORKS_AT")
        self.assertIsNotNone(rel); self.assertEqual(rel.label,"WORKS_AT")

    def test_add_relationship_missing_entity(self):
        self.assertIsNone(self.kg.add_relationship("X","Y","Z"))

    def test_path(self):
        self.kg.add_entity("A",self.ET.PERSON); self.kg.add_entity("B",self.ET.PERSON)
        self.kg.add_entity("C",self.ET.ORG)
        self.kg.add_relationship("A","C","REL"); self.kg.add_relationship("B","C","REL")
        p = self.kg.path("A","B")
        self.assertIsNotNone(p); self.assertEqual(p.length,2)

    def test_neighbors(self):
        self.kg.add_entity("A",self.ET.PERSON); self.kg.add_entity("B",self.ET.ORG)
        self.kg.add_relationship("A","B","REL")
        nbrs = self.kg.neighbors("A",hops=1)
        self.assertGreater(len(nbrs["entities"]),0)

    def test_regex_fallback(self):
        raw = self.kg._regex_extract("Elon Musk founded SpaceX in 2002.")
        self.assertGreater(len(raw["entities"]),0)

    def test_extract_and_add_no_llm(self):
        async def _run():
            entities, rels = await self.kg.extract_and_add("Alan Turing worked at Bletchley Park.")
            self.assertGreater(len(entities),0)
        asyncio.get_event_loop().run_until_complete(_run())


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class TestConfigDefaults(unittest.TestCase):
    def setUp(self):
        from agent.config_manager import ConfigManager
        self.cfg = ConfigManager(env_file="/tmp/_omni_test_nonexistent.env")

    def test_bool_default(self): self.assertEqual(self.cfg.get("MODEL_AUTO_ROUTE"), True)
    def test_int_default(self):  self.assertEqual(self.cfg.get("RATE_LIMIT_RPM"), 60)
    def test_list_default(self): self.assertIsInstance(self.cfg.get("MODEL_EXCLUDE"), list)
    def test_unknown_key(self):  self.assertIsNone(self.cfg.get("NONEXISTENT"))
    def test_fallback_value(self): self.assertEqual(self.cfg.get("NONEXISTENT","fb"),"fb")


class TestConfigRuntimeSet(unittest.TestCase):
    def setUp(self):
        from agent.config_manager import ConfigManager
        self.cfg = ConfigManager(env_file="/tmp/_omni_test_nonexistent.env")

    def test_set_int(self):
        self.cfg.set("RATE_LIMIT_RPM", 30)
        self.assertEqual(self.cfg.get("RATE_LIMIT_RPM"), 30)

    def test_coerce_str_to_int(self):
        self.cfg.set("RATE_LIMIT_RPM","42"); self.assertEqual(self.cfg.get("RATE_LIMIT_RPM"),42)

    def test_coerce_str_to_bool(self):
        self.cfg.set("MODEL_AUTO_ROUTE","false"); self.assertFalse(self.cfg.get("MODEL_AUTO_ROUTE"))

    def test_validate_min(self):
        self.assertGreater(len(self.cfg.set("RATE_LIMIT_RPM",0)),0)

    def test_validate_max(self):
        self.assertGreater(len(self.cfg.set("RATE_LIMIT_RPM",99999)),0)

    def test_validate_choices(self):
        self.assertGreater(len(self.cfg.set("LOG_LEVEL","VERBOSE")),0)

    def test_valid_choice(self):
        self.assertEqual(self.cfg.set("LOG_LEVEL","DEBUG"),[])
        self.assertEqual(self.cfg.get("LOG_LEVEL"),"DEBUG")

    def test_unset_reverts(self):
        self.cfg.set("RATE_LIMIT_RPM",99); self.cfg.unset("RATE_LIMIT_RPM")
        self.assertEqual(self.cfg.get("RATE_LIMIT_RPM"),60)

    def test_reset_all(self):
        self.cfg.set("RATE_LIMIT_RPM",99); self.cfg.set("RAG_CHUNK_SIZE",256)
        self.cfg.reset_all()
        self.assertEqual(self.cfg.get("RATE_LIMIT_RPM"),60)
        self.assertEqual(self.cfg.get("RAG_CHUNK_SIZE"),512)


class TestConfigHooksAndScoped(unittest.TestCase):
    def setUp(self):
        from agent.config_manager import ConfigManager
        self.cfg = ConfigManager(env_file="/tmp/_omni_test_nonexistent.env")

    def test_hook_fires(self):
        changes = []
        self.cfg.on_change("RAG_CHUNK_SIZE", lambda o,n: changes.append((o,n)))
        self.cfg.set("RAG_CHUNK_SIZE",256)
        self.assertEqual(changes, [(512,256)])

    def test_hook_no_fire_if_same(self):
        calls = []
        self.cfg.on_change("RATE_LIMIT_RPM", lambda o,n: calls.append(1))
        self.cfg.set("RATE_LIMIT_RPM",60); self.assertEqual(len(calls),0)

    def test_multiple_hooks(self):
        calls = []
        self.cfg.on_change("MAX_HISTORY", lambda o,n: calls.append("a"))
        self.cfg.on_change("MAX_HISTORY", lambda o,n: calls.append("b"))
        self.cfg.set("MAX_HISTORY",100); self.assertEqual(len(calls),2)

    def test_scoped_override(self):
        self.cfg.set_scoped("MODEL_AUTO_ROUTE","model:gpt4",False)
        self.assertFalse(self.cfg.get_scoped("MODEL_AUTO_ROUTE","model:gpt4"))
        self.assertTrue(self.cfg.get("MODEL_AUTO_ROUTE"))

    def test_scoped_fallback(self):
        self.assertEqual(self.cfg.get_scoped("RATE_LIMIT_RPM","session:xyz"),60)


class TestFeatureFlags(unittest.TestCase):
    def setUp(self):
        from agent.config_manager import ConfigManager
        self.cfg = ConfigManager(env_file="/tmp/_omni_test_nonexistent.env")

    def test_builtins_registered(self):
        names = {f["name"] for f in self.cfg.list_flags()}
        self.assertIn("streaming",names); self.assertIn("tracing",names)

    def test_enabled_flag(self):  self.assertTrue(self.cfg.flag("streaming"))
    def test_disabled_flag(self): self.assertFalse(self.cfg.flag("knowledge_graph"))

    def test_set_flag(self):
        self.cfg.set_flag("knowledge_graph",True); self.assertTrue(self.cfg.flag("knowledge_graph"))

    def test_toggle_off(self):
        self.cfg.set_flag("streaming",False); self.assertFalse(self.cfg.flag("streaming"))

    def test_rollout_deterministic(self):
        self.cfg.set_flag("streaming",True,rollout_pct=50)
        self.assertEqual(self.cfg.flag_for("streaming","user_abc"),
                         self.cfg.flag_for("streaming","user_abc"))

    def test_rollout_100_all_pass(self):
        self.cfg.set_flag("streaming",True,rollout_pct=100)
        for u in ["u1","u2","u3"]: self.assertTrue(self.cfg.flag_for("streaming",u))

    def test_rollout_0_none_pass(self):
        self.cfg.set_flag("streaming",True,rollout_pct=0)
        for u in ["u1","u2","u3"]: self.assertFalse(self.cfg.flag_for("streaming",u))

    def test_blocklist(self):
        from agent.config_manager import FeatureFlag
        self.cfg.register_flag(FeatureFlag("beta",enabled=True,blocked_users=["b_user"]))
        self.assertFalse(self.cfg.flag_for("beta","b_user"))
        self.assertTrue(self.cfg.flag_for("beta","other"))

    def test_allowlist(self):
        from agent.config_manager import FeatureFlag
        self.cfg.register_flag(FeatureFlag("alpha",enabled=True,allowed_users=["vip"]))
        self.assertTrue(self.cfg.flag_for("alpha","vip"))
        self.assertFalse(self.cfg.flag_for("alpha","regular"))


class TestConfigDiagnostics(unittest.TestCase):
    def setUp(self):
        from agent.config_manager import ConfigManager
        self.cfg = ConfigManager(env_file="/tmp/_omni_test_nonexistent.env")

    def test_sensitive_masked(self):
        from agent.config_manager import ConfigSpec, ValueType
        self.cfg.register(ConfigSpec("MY_SECRET",ValueType.STRING,"secretval",sensitive=True))
        self.assertEqual(self.cfg.all(include_sensitive=False)["MY_SECRET"],"***")

    def test_sensitive_visible(self):
        from agent.config_manager import ConfigSpec, ValueType
        self.cfg.register(ConfigSpec("MY_SECRET2",ValueType.STRING,"sv2",sensitive=True))
        self.assertEqual(self.cfg.all(include_sensitive=True)["MY_SECRET2"],"sv2")

    def test_diff_from_defaults(self):
        self.cfg.set("RAG_CHUNK_SIZE",256)
        diff = self.cfg.diff_from_defaults()
        self.assertIn("RAG_CHUNK_SIZE",diff)
        self.assertEqual(diff["RAG_CHUNK_SIZE"]["effective"],256)
        self.assertEqual(diff["RAG_CHUNK_SIZE"]["source"],"runtime")

    def test_diff_excludes_unchanged(self):
        self.assertNotIn("RATE_LIMIT_RPM",self.cfg.diff_from_defaults())

    def test_validate_all_clean(self):
        self.assertEqual(len(self.cfg.validate_all()),0)

    def test_validate_all_detects_invalid(self):
        self.cfg._runtime["RATE_LIMIT_RPM"] = 9999
        self.assertIn("RATE_LIMIT_RPM",self.cfg.validate_all())

    def test_hot_reload_watcher(self):
        async def _run():
            await self.cfg.start_watcher("/tmp/_none.env",interval=0.1)
            await asyncio.sleep(0.05)
            await self.cfg.stop_watcher()
        asyncio.get_event_loop().run_until_complete(_run())


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun
    failed = len(result.failures) + len(result.errors)
    passed = total - failed
    print(f"\n{'='*60}")
    print(f"  v6 Test Results: {passed}/{total} passed")
    if failed:
        for t, tb in result.failures + result.errors:
            print(f"  ✗ {t}")
    else:
        print(f"  ✅ ALL {total} PASSED")
    print(f"{'='*60}")
    sys.exit(0 if not failed else 1)
