import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from demo import PATH_COLUMNS, compare, validate_agent_answer
from foundry import FoundryAgent
from records import DemoError, PRODUCTION_FILES, QUALITY_FILES, WAREHOUSE_FILES
from records import complete_snapshot, production_record, quality_record
from search import KnowledgeBase, model
from services import SEARCH_SCOPE, STORAGE_SCOPE, HttpError, read_blob_records

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config.example.json").read_bytes())
QUESTIONS = json.loads((ROOT / "benchmarks" / "ground-truth.json").read_bytes())["questions"]
HERO = next(q for q in QUESTIONS if q["id"] == "delivered-impact")
ONTOLOGY = json.loads((ROOT / "ontology.json").read_bytes())


def blob_fixtures(directory: str, names: dict) -> list[dict]:
    return [json.loads((ROOT / "data" / directory / name).read_bytes()) for name in names]


def blob_reader() -> Mock:
    blobs = {
        f"{CONFIG[container]}/{name}": data
        for container, directory, names in (
            ("productionContainer", "production-blob", PRODUCTION_FILES),
            ("warehouseContainer", "warehouse-blob", WAREHOUSE_FILES | QUALITY_FILES),
        )
        for name, data in zip(names, blob_fixtures(directory, names))
    }
    http = Mock()
    http.json.side_effect = lambda url, *_args, **_kwargs: blobs[url]
    return http


def snapshot() -> list:
    return complete_snapshot(read_blob_records(blob_reader(), CONFIG))


class FileSourceTests(unittest.TestCase):
    def test_complete_snapshot_keeps_files_and_provenance_separate(self):
        records = snapshot()
        self.assertEqual(16, len(records))
        by_key = {(record.source, record.record_id): record for record in records}
        for path in HERO["expectedPaths"]:
            for ref in path["evidence"].values():
                record = by_key[(ref["source"], ref["recordId"])]
                self.assertIn(record.locator, record.document().decode())
        self.assertNotIn("SL-2408", by_key["quality-blob", "QH-014"].statement)
        self.assertNotIn("delivered", by_key["warehouse-blob", "SH-905"].statement)
        with self.assertRaisesRegex(DemoError, "missing or repeats"):
            complete_snapshot(records[:-1] + records[:1])

    def test_only_confirmed_actions_become_relationship_statements(self):
        draft = blob_fixtures("production-blob", PRODUCTION_FILES)[0]
        with self.assertRaisesRegex(DemoError, "posted material"):
            production_record(next(iter(PRODUCTION_FILES)), {**draft, "status": "planned"}, "unused")
        name = "quality/delivery-pod-901.json"
        receipt = blob_fixtures("warehouse-blob", {name: None})[0]
        with self.assertRaisesRegex(DemoError, "signed receipt"):
            quality_record(name, {**receipt, "status": "pending"}, "unused")

    def test_blob_reader_rejects_a_single_combined_account(self):
        http = Mock()
        with self.assertRaisesRegex(DemoError, "separate production and warehouse"):
            read_blob_records(http, {**CONFIG, "warehouseContainer": CONFIG["productionContainer"] + "-warehouse"})
        http.json.assert_not_called()

    def test_quality_records_are_read_from_the_warehouse_container(self):
        http = blob_reader()
        records = read_blob_records(http, CONFIG)
        self.assertEqual(16, http.json.call_count)
        self.assertTrue(all(call.args[2] == STORAGE_SCOPE for call in http.json.call_args_list))
        self.assertEqual(
            {f"{CONFIG['warehouseContainer']}/{name}" for name in QUALITY_FILES},
            {record.locator for record in records if record.source == "quality-blob"},
        )


class NativeFoundryTests(unittest.TestCase):
    def test_publish_uses_managed_blob_extraction_not_an_uploaded_edge_index(self):
        http = Mock()
        http.json.side_effect = [
            {}, HttpError("source", 404), HttpError("base", 404), {},
            {"lastSynchronizationState": {"endTime": "completed", "itemsUpdatesProcessed": 16}},
            {"azureBlobParameters": {"createdResources": {"graphIndex": "managed-graph", "index": "managed-chunks"}}},
            {},
        ]
        kb = KnowledgeBase(http, CONFIG)
        self.assertEqual("managed-graph", kb.build(CONFIG, ONTOLOGY, snapshot())["indexes"]["graphIndex"])
        self.assertEqual(16, http.read.call_count)
        for call in http.read.call_args_list:
            self.assertTrue(call.args[0].startswith(CONFIG["graphContainer"] + "/" + kb.input_folder + "/"))
            self.assertEqual("*", call.kwargs["headers"]["If-None-Match"])
            self.assertIn(b"Original record URL: https://", call.kwargs["body"])
        writes = [call.kwargs["body"] for call in http.json.call_args_list if call.kwargs.get("method") == "PUT"]
        self.assertEqual("azureBlob", writes[0]["kind"])
        self.assertEqual(ONTOLOGY, json.loads(writes[0]["azureBlobParameters"]["graphExtraction"]["ontology"]["value"]))
        self.assertEqual([{"name": kb.source}], writes[1]["knowledgeSources"])
        self.assertEqual(CONFIG["plannerDeployment"], writes[1]["models"][0]["azureOpenAIParameters"]["deploymentId"])

    def test_refresh_replaces_only_existing_quality_slots_and_indexes_the_new_provenance(self):
        http = Mock()
        kb = KnowledgeBase(http, CONFIG)
        records = [record for record in snapshot() if record.source == "quality-blob"]
        folder = f"{CONFIG['graphContainer']}/{kb.input_folder}"
        indexes = {"index": kb.source + "-index", "indexer": kb.source + "-indexer",
                   "graphIndex": kb.source + "-graph-index"}
        source = {"azureBlobParameters": {"createdResources": indexes}}
        old = [r.document().replace(r.locator.encode(), b"https://example.test/previous-record") for r in records]
        http.read_with_headers.side_effect = [(data, {"ETag": f'"old-{i}"'}) for i, data in enumerate(old)]

        def chunks(docs):
            return {"value": [{"blob_url": f"{folder}/{r.filename}", "snippet": doc.decode()}
                              for r, doc in zip(records, docs)]}

        http.json.side_effect = [
            {"knowledgeSources": [{"name": kb.source}]}, source, chunks(old),
            {"lastSynchronizationState": {"startTime": "before"}},
            {"lastSynchronizationState": {"startTime": "after", "endTime": "complete"}},
            source, chunks([r.document() for r in records]),
        ]
        result = kb.refresh_quality(CONFIG, records)
        self.assertEqual(4, result["updatedStagedFiles"])
        self.assertEqual([f"{folder}/{r.filename}" for r in records],
                         [call.args[0] for call in http.read.call_args_list[:-1]])
        self.assertEqual([f'"old-{i}"' for i in range(4)],
                         [call.kwargs["headers"]["If-Match"] for call in http.read.call_args_list[:-1]])
        run = http.read.call_args
        self.assertIn(f"/indexers('{indexes['indexer']}')/search.run?", run.args[0])
        self.assertEqual(SEARCH_SCOPE, run.args[2])
        self.assertEqual("POST", run.kwargs["method"])

    def test_document_kb_uses_shared_chunks_and_same_planner_without_mutating_the_graph(self):
        http = Mock()
        kb = KnowledgeBase(http, CONFIG)
        base = {"models": [model(kb.model_endpoint, kb.planner, "gpt-5-mini")],
                "retrievalReasoningEffort": {"kind": "low"}, "outputMode": "extractiveData"}
        http.json.side_effect = [
            {**base, "knowledgeSources": [{"name": kb.source}]},
            {"azureBlobParameters": {"createdResources": {"index": "managed-chunks", "graphIndex": "managed-graph"}}},
            {"semantic": {"defaultConfiguration": "shared-semantic"}},
            HttpError("document source", 404),
            {"kind": "searchIndex", "searchIndexParameters": {"searchIndexName": "managed-chunks"}},
            HttpError("document KB", 404),
            {**base, "knowledgeSources": [{"name": kb.documents_source}], "retrieveDefaults": {"maxOutputDocuments": 10}},
        ]
        self.assertEqual(kb.documents_base, kb.ensure_documents()["knowledgeBase"])
        source, documents = [call.kwargs["body"] for call in http.json.call_args_list
                             if call.kwargs.get("method") == "PUT"]
        self.assertEqual((kb.documents_source, "searchIndex"), (source["name"], source["kind"]))
        self.assertEqual("managed-chunks", source["searchIndexParameters"]["searchIndexName"])
        self.assertNotIn("azureBlobParameters", source)
        self.assertEqual(kb.documents_base, documents["name"])
        self.assertEqual([{"name": kb.documents_source}], documents["knowledgeSources"])
        self.assertEqual(base["models"], documents["models"])
        http.read.assert_not_called()

    def test_native_compare_rejects_missing_graph_provenance_and_extra_sites(self):
        kb = Mock(spec=KnowledgeBase)
        snippets = [record.document().decode() for record in snapshot()]
        kb.retrieve_documents.return_value = {"activity": [], "references": [{"sourceData": {"snippet": snippets[0]}}]}
        graph_rows = [{key: value for key, value in path.items() if key != "evidence"} for path in HERO["expectedPaths"]]
        activity = {"type": "graphSearch", "id": 1, "graphSearchArguments": {"outcome": "Success", "gql": "MATCH ..."}}
        row_ref = {"type": "graphSearch", "activitySource": 1, "sourceData": {"rows": graph_rows}}
        fact_refs = [{"type": "graphSearch", "activitySource": 1, "sourceData": {"snippet": snippet}}
                     for snippet in snippets]
        kb.retrieve.return_value = {"activity": [activity], "references": [row_ref, *fact_refs]}
        result = compare(kb, HERO)
        self.assertTrue(result["graph"]["matchesExpected"])
        self.assertEqual([snippets[0]], result["documents"]["originalRecordExcerpts"])
        kb.retrieve.assert_called_once_with(HERO["prompt"])
        kb.retrieve_documents.assert_called_once_with(HERO["prompt"])
        kb.retrieve.return_value["references"] = [row_ref, *[{**ref, "activitySource": 2} for ref in fact_refs]]
        self.assertFalse(compare(kb, HERO)["graph"]["matchesExpected"])
        graph_rows.append({"customerSite": "CS-063"})
        kb.retrieve.return_value["references"] = [row_ref, *fact_refs]
        self.assertFalse(compare(kb, HERO)["graph"]["matchesExpected"])

    def test_project_connection_calls_the_first_party_kb_with_managed_identity(self):
        http = Mock()
        kb = KnowledgeBase(http, CONFIG)
        agent = FoundryAgent(http, CONFIG, kb)
        props = {"target": kb.mcp_endpoint, "authType": "ProjectManagedIdentity", "category": "RemoteTool"}
        http.json.side_effect = [{}, HttpError("agent", 404), HttpError("connection", 404),
                                 {"properties": props}, {"name": agent.name}]
        self.assertEqual(agent.name, agent.connect()["name"])
        writes = [call for call in http.json.call_args_list if call.kwargs.get("method") in {"PUT", "POST"}]
        self.assertEqual(2, len(writes))
        connection = writes[0].kwargs["body"]["properties"]
        self.assertEqual(props, {key: connection[key] for key in props})
        self.assertEqual(kb.name, connection["metadata"]["knowledgeBaseName"])
        body = http.json.call_args.kwargs["body"]["definition"]
        self.assertEqual("prompt", body["kind"])
        self.assertEqual(CONFIG["plannerDeployment"], body["model"])
        tool, = body["tools"]
        self.assertEqual(("knowledge-base", kb.mcp_endpoint), (tool["server_label"], tool["server_url"]))
        self.assertEqual(["knowledge_base_retrieve"], tool["allowed_tools"])

    def test_revising_the_existing_agent_detaches_the_extra_tool(self):
        http = Mock()
        kb = KnowledgeBase(http, CONFIG)
        agent = FoundryAgent(http, CONFIG, kb)
        existing = {"versions": {"latest": {"definition": {"tools": [
            {"server_url": kb.mcp_endpoint}, {"server_url": kb.endpoint + "/retired-tool"},
        ]}}}}
        props = {"target": kb.mcp_endpoint, "authType": "ProjectManagedIdentity", "category": "RemoteTool"}
        http.json.side_effect = [{}, existing, {"properties": props}, {"version": "4"}]
        self.assertEqual("4", agent.connect(new_version=True)["version"])
        writes = [call for call in http.json.call_args_list if call.kwargs.get("method") in {"PUT", "POST"}]
        update, = writes
        self.assertTrue(update.args[0].endswith(f"/agents/{agent.name}/versions?api-version=v1"))
        self.assertEqual([kb.mcp_endpoint],
                         [tool["server_url"] for tool in update.kwargs["body"]["definition"]["tools"]])

    def test_answer_without_a_real_kb_tool_call_is_not_verified(self):
        http = Mock()
        agent = FoundryAgent(http, CONFIG, KnowledgeBase(http, CONFIG))
        text = "\n".join(" ".join(path[col] for col in PATH_COLUMNS) for path in HERO["expectedPaths"])
        answer = {"type": "message", "content": [{"type": "output_text", "text": text}]}
        http.json.side_effect = [{"id": "conversation"}, {"status": "completed", "output": [answer]}]
        with self.assertRaisesRegex(DemoError, "No successful native Foundry IQ"):
            agent.ask(HERO["prompt"])
        tool = {"type": "mcp_call", "name": "knowledge_base_retrieve", "status": "completed", "output": "retrieved"}
        http.json.side_effect = [{"id": "conversation"}, {"status": "completed", "output": [tool, answer]}]
        self.assertTrue(validate_agent_answer(HERO, agent.ask(HERO["prompt"])["answer"]))
        self.assertFalse(validate_agent_answer(HERO, text + " CS-063"))
        self.assertFalse(validate_agent_answer(HERO, text.replace("FL-801", "another lot")))


if __name__ == "__main__":
    unittest.main()
