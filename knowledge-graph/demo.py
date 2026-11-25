"""Run the Blob-only comparison of graph and document retrieval in Foundry IQ."""

import argparse
import json
import re
import sys
from pathlib import Path

from foundry import FoundryAgent
from records import DemoError, complete_snapshot
from search import KnowledgeBase, coverage, document_evidence, graph_evidence
from services import Http, decode_json, read_blob_records, uuid_setting

ROOT = Path(__file__).resolve().parent
HERO_CASE = "delivered-impact"
PATH_COLUMNS = ("sensorLot", "assemblyLot", "finishedLot", "shipment", "customerSite")


def site_ids(text: str) -> set[str]:
    return set(re.findall(r"\bCS-[0-9]+\b", text.upper()))


def compare(kb: KnowledgeBase, question: dict) -> dict:
    prompt = question["prompt"]
    response = kb.retrieve(prompt)
    queries, rows, snippets = graph_evidence(response)
    if not queries:
        raise DemoError("Foundry IQ returned no successful graphSearch activity with generated GQL.")
    documents = kb.retrieve_documents(prompt)
    if any(a.get("type") == "graphSearch" for a in documents["activity"]):
        raise DemoError("The document-only knowledge base unexpectedly ran a graph query.")
    document_snippets = document_evidence(documents)
    expected_sites = {path["customerSite"] for path in question["expectedPaths"]}
    serialized_rows = [json.dumps(row) for row in rows]
    actual_sites = set().union(*(site_ids(row) for row in serialized_rows))
    complete_paths = all(any(all(re.search(r"\b" + re.escape(path[col]) + r"\b", row)
                                 for col in PATH_COLUMNS) for row in serialized_rows)
                         for path in question["expectedPaths"])
    graph_coverage = coverage(question, snippets)
    return {
        "question": prompt,
        "graph": {
            "matchesExpected": actual_sites == expected_sites and complete_paths and not graph_coverage["missing"],
            "coverage": graph_coverage, "queries": queries, "rows": rows, "originalRecordExcerpts": snippets,
        },
        "documents": {"coverage": coverage(question, document_snippets), "originalRecordExcerpts": document_snippets},
    }


def validate_agent_answer(question: dict, answer: str) -> bool:
    expected = {path["customerSite"] for path in question["expectedPaths"]}
    required_ids = {path[col] for path in question["expectedPaths"] for col in PATH_COLUMNS}
    return (site_ids(answer) == expected
            and all(re.search(r"\b" + re.escape(value) + r"\b", answer) for value in required_ids))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.foundry.local.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="Validate the Blob originals and check preview access")
    commands.add_parser("publish", help="Build a managed graph and a Foundry IQ knowledge base")
    commands.add_parser("refresh-quality", help="Refresh the quality Blob records in the existing graph")
    commands.add_parser("documents", help="Create or verify the document-only comparison knowledge base")
    connect = commands.add_parser("connect", help="Connect the graph knowledge base to a Microsoft Foundry prompt agent")
    connect.add_argument("--new-version", action="store_true", help="Revise this existing demo agent")
    commands.add_parser("compare", help="Check paths and source evidence from the two Foundry IQ knowledge bases")
    ask = commands.add_parser("ask", help="Ask the deployed agent a benchmark question")
    ask.add_argument("--case", default=HERO_CASE, help="Benchmark ID")
    args = parser.parse_args(argv)

    try:
        from azure.core.exceptions import AzureError
        from azure.identity import AzureCliCredential
    except ImportError:
        parser.exit(1, "Install the runner dependencies from requirements.txt first.\n")

    ok = True
    try:
        config = decode_json(args.config.read_bytes(), "Read demo configuration")
        if not isinstance(config, dict):
            raise DemoError("Configuration must be a JSON object.")
        tenant = uuid_setting(config, "tenantId")
        with AzureCliCredential(tenant_id=tenant, process_timeout=30) as credential:
            http = Http(credential)
            kb = KnowledgeBase(http, config)
            agent = FoundryAgent(http, config, kb)
            if args.command in {"preflight", "publish"}:
                records = complete_snapshot(read_blob_records(http, config))
                ontology = decode_json((ROOT / "ontology.json").read_bytes(), "Read ontology")
                if args.command == "preflight":
                    kb.source_definition(config, ontology)
                    kb.assert_new()
                    agent.check_project()
                    result = {"sourceRecords": len(records), "projectEndpoint": agent.endpoint,
                              "knowledgeBase": kb.name, "published": False}
                else:
                    result = kb.build(config, ontology, records)
            elif args.command == "refresh-quality":
                records = complete_snapshot(read_blob_records(http, config))
                result = kb.refresh_quality(config, [record for record in records if record.source == "quality-blob"])
            elif args.command == "documents":
                result = kb.ensure_documents()
            elif args.command == "connect":
                result = agent.connect(new_version=args.new_version)
            else:
                questions = decode_json((ROOT / "benchmarks" / "ground-truth.json").read_bytes(), "Read benchmark")
                case = args.case if args.command == "ask" else HERO_CASE
                question = next((q for q in questions["questions"] if q["id"] == case), None)
                if question is None:
                    raise DemoError(f"Unknown benchmark case: {case}.")
                if args.command == "compare":
                    result = compare(kb, question)
                    ok = result["graph"]["matchesExpected"]
                else:
                    result = agent.ask(question["prompt"])
                    if case == HERO_CASE:
                        labels = {call.get("server_label") for call in result["knowledgeBaseCalls"]}
                        result["matchesExpected"] = ok = (
                            labels == {"knowledge-base"}
                            and validate_agent_answer(question, result["answer"])
                        )
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0 if ok else 1
    except (DemoError, OSError, AzureError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
