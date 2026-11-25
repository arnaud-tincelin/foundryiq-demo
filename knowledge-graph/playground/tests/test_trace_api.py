import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

PLAYGROUND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLAYGROUND), str(PLAYGROUND.parent)]

from api.trace_api import TraceTarget, handle_trace, is_invited, shape_result
from services import HttpError


TARGET = TraceTarget.from_prefix("https://example.search.windows.net", "lot-trace-demo-example")
ROW = {"quality_hold": "QH-014", "customer_site": "CS-021"}
ORIGINAL = "https://examplestorage.blob.core.windows.net/warehouse/quality/delivery.json"
SNIPPET = ("Shipment SH-901 was delivered to CS-021 with a signed receipt.\n\n"
           "Original record ID: POD-901\nOrigin: quality-blob\nOriginal record URL: " + ORIGINAL)


def response(*snippets):
    return {
        "activity": [{"type": "graphSearch", "id": 1, "graphSearchArguments": {"outcome": "Success", "gql": "MATCH ..."}}],
        "references": [
            {"type": "graphSearch", "activitySource": 1, "sourceData": {"rows": [ROW]}},
            *[{"type": "graphSearch", "activitySource": 1, "sourceData": {"snippet": snippet}} for snippet in snippets],
        ],
    }


def principal(provider, roles):
    return base64.b64encode(json.dumps({
        "identityProvider": provider, "userId": "example-user", "userRoles": roles,
    }).encode()).decode()


class TraceApiTests(unittest.TestCase):
    def test_one_retrieve_call_returns_graph_and_hydrated_blob_citation(self):
        http = Mock()
        native = response(SNIPPET, SNIPPET)
        native["activity"].append({"type": "modelAnswerSynthesis"})
        native["references"][0]["id"] = "0"
        native["references"][1]["id"] = "1"
        native["response"] = [{"content": [
            {"type": "text", "text": "CS-021 appears on the delivery path [ref_id:0]. Receipt [ref_id:1]."},
        ]}]
        http.json.return_value = native
        status, result = handle_trace(b'{"question":"  Trace QH-014.  "}', TARGET, http)
        self.assertEqual(200, status)
        http.json.assert_called_once()
        self.assertIn("/knowledgebases('lot-trace-demo-example-kb')/retrieve?", http.json.call_args.args[0])
        request = http.json.call_args.kwargs["body"]
        self.assertEqual("answerSynthesis", request["outputMode"])
        source, = request["knowledgeSourceParams"]
        self.assertEqual("lot-trace-demo-example-blob-source", source["knowledgeSourceName"])
        self.assertEqual("hydrate", source["graphParameters"]["provenance"]["kind"])
        self.assertEqual("Trace QH-014.", request["messages"][0]["content"][0]["text"])
        self.assertEqual([ROW], result["paths"])
        self.assertEqual(["MATCH ..."], result["queries"])
        self.assertEqual("CS-021 appears on the delivery path [graph paths]. Receipt [POD-901].", result["answer"])
        self.assertEqual([{"recordId": "POD-901", "source": "quality-blob", "url": ORIGINAL,
                           "excerpt": "Shipment SH-901 was delivered to CS-021 with a signed receipt."}],
                         result["evidence"])

    def test_invalid_question_and_upstream_failure_are_not_empty_successes(self):
        http = Mock()
        self.assertEqual(400, handle_trace(b'{"question":" "}', TARGET, http)[0])
        self.assertEqual(413, handle_trace(b"x" * 4097, TARGET, http)[0])
        self.assertEqual(400, handle_trace(b'{"question":"Trace QH-014","mode":"unknown"}', TARGET, http)[0])
        http.json.assert_not_called()
        http.json.side_effect = HttpError("Search", 403, "Sensitive upstream details")
        status, result = handle_trace(b'{"question":"Trace QH-014"}', TARGET, http)
        self.assertEqual(502, status)
        self.assertNotIn("Sensitive", result["error"])

    def test_open_original_link_requires_an_uncredentialed_azure_blob_url(self):
        result = shape_result(response(
            SNIPPET.replace(ORIGINAL, "javascript:alert(1)"),
            SNIPPET.replace(ORIGINAL, ORIGINAL + "?sig=secret"),
            SNIPPET.replace(ORIGINAL, "https://example.net/document"),
        ))
        self.assertEqual([ROW], result["paths"])
        self.assertEqual([], result["evidence"])

    def test_document_mode_uses_the_document_kb_and_never_requests_a_graph(self):
        http = Mock()
        http.json.return_value = {
            "activity": [{"type": "searchIndex"}, {"type": "modelAnswerSynthesis"}],
            "references": [{"id": "3", "type": "searchIndex", "sourceData": {"snippet": SNIPPET}}],
            "response": [{"content": [
                {"type": "text", "text": "The retrieved receipt shows CS-021 [ref_id:3]."},
            ]}],
        }
        status, result = handle_trace(b'{"question":"Trace QH-014","mode":"documents"}', TARGET, http)
        self.assertEqual(200, status)
        http.json.assert_called_once()
        self.assertIn("/knowledgebases('lot-trace-demo-example-documents-kb')/retrieve?", http.json.call_args.args[0])
        request = http.json.call_args.kwargs["body"]
        self.assertEqual("answerSynthesis", request["outputMode"])
        source, = request["knowledgeSourceParams"]
        self.assertEqual(("searchIndex", TARGET.documents_source), (source["kind"], source["knowledgeSourceName"]))
        self.assertNotIn("graphParameters", source)
        self.assertEqual("documents", result["mode"])
        self.assertEqual([], result["paths"])
        self.assertEqual([], result["queries"])
        self.assertEqual("The retrieved receipt shows CS-021 [POD-901].", result["answer"])
        self.assertEqual([ORIGINAL], [entry["url"] for entry in result["evidence"]])

    def test_document_mode_does_not_render_misconfigured_graph_results(self):
        http = Mock()
        http.json.return_value = response(SNIPPET)
        status, result = handle_trace(b'{"question":"Trace QH-014","mode":"documents"}', TARGET, http)
        self.assertEqual(502, status)
        self.assertNotIn("evidence", result)

    def test_empty_graph_can_still_show_plain_evidence_from_same_source(self):
        result = shape_result({
            "activity": [], "references": [{"sourceData": {"snippet": SNIPPET}}],
            "response": [{"content": [{"type": "text", "text": SNIPPET}]}],
        })
        self.assertEqual([], result["paths"])
        self.assertIsNone(result["answer"])
        self.assertEqual(ORIGINAL, result["evidence"][0]["url"])

    def test_only_invited_entra_principal_passes_function_role_check(self):
        self.assertTrue(is_invited(principal("aad", ["authenticated", "trace_reader"])))
        self.assertFalse(is_invited(principal("aad", ["authenticated"])))
        self.assertFalse(is_invited(principal("github", ["trace_reader"])))
        self.assertFalse(is_invited("not base64"))


if __name__ == "__main__":
    unittest.main()
