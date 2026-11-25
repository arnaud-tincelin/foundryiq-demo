"""Use the native Foundry IQ entity-graph preview, not a hand-built graph."""

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from records import DemoError, QUALITY_FILES, Record
from services import (SEARCH_SCOPE, SOURCE_FILE_LIMIT, STORAGE_SCOPE, Http, HttpError, container_url,
                      setting, storage_headers, uuid_setting, write_blob)

API_VERSION = "2026-11-01-preview"
CHUNK_FIELDS = "uid,snippet,snippet_parent_id,blob_url"
EVIDENCE_LIMIT = 10
RESOURCE_PREFIX = r"/subscriptions/[0-9a-f-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/providers/"
RETRIEVAL_INSTRUCTIONS = (
    "Use graph_search for questions about lot genealogy, quality holds, and confirmed deliveries. "
    "Traverse QualityHold appliesTo SensorLot consumedIn AssemblyLot usedToBuild FinishedLot "
    "loadedOn Shipment deliveredTo CustomerSite. Use the GQL node .name property for both filters "
    "and returned business identifiers (QH-, SL-, AL-, FL-, SH-, CS-). Never project .id, which is an "
    "opaque graph identifier. Return the named business identifiers of all nodes together in each "
    "matched path without grouping or aggregation. Use document search for supporting citations "
    "and status-only lookups. A dispatch is not evidence of a delivery."
)


def model(endpoint: str, deployment: str, name: str) -> dict:
    return {"kind": "azureOpenAI", "azureOpenAIParameters": {
        "resourceUri": endpoint, "deploymentId": deployment, "modelName": name,
    }}


def document_target(prefix: str) -> tuple[str, str]:
    return prefix + "-documents-kb", prefix + "-documents-source"


def retrieve(http: Http, endpoint: str, base: str, source: str, prompt: str, *,
             graph: bool, max_runtime: int = 60, synthesize: bool = False) -> dict:
    url = f"{endpoint}/knowledgebases('{base}')/retrieve?api-version={API_VERSION}"
    source_params = {"kind": "azureBlob" if graph else "searchIndex", "knowledgeSourceName": source,
                     "includeReferences": True, "includeReferenceSourceData": True, "failOnError": True}
    if graph:
        source_params["graphParameters"] = {"includeQuery": True, "provenance": {
            "kind": "hydrate", "hydrateDocuments": {"select": CHUNK_FIELDS, "top": EVIDENCE_LIMIT},
        }}
    return http.json(url, f"Query Foundry IQ knowledge base {base}", SEARCH_SCOPE, method="POST", timeout=max_runtime + 10,
                     body={
                         "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                         "includeActivity": True,
                         "maxRuntimeInSeconds": max_runtime,
                         "outputMode": "answerSynthesis" if synthesize else "extractiveData",
                         "knowledgeSourceParams": [source_params],
                     })


class KnowledgeBase:
    def __init__(self, http: Http, config: dict):
        self.http = http
        self.endpoint = setting(config, "searchEndpoint", r"https://[a-z0-9-]+\.search\.windows\.net/?").rstrip("/")
        self.prefix = setting(config, "indexPrefix", r"lot-trace-demo-[a-z0-9]+(?:-[a-z0-9]+)*")
        if len(self.prefix) > 40:
            raise DemoError("indexPrefix must be at most 40 characters.")
        self.input_folder = self.prefix + "-blob"
        self.source = self.input_folder + "-source"
        self.name = self.prefix + "-kb"
        self.documents_base, self.documents_source = document_target(self.prefix)
        self.source_path = f"/knowledgesources('{self.source}')"
        self.base_path = f"/knowledgebases('{self.name}')"
        self.mcp_endpoint = f"{self.endpoint}/knowledgebases/{self.name}/mcp?api-version={API_VERSION}"
        self.model_endpoint = setting(config, "openaiEndpoint", r"https://[a-z0-9-]+\.openai\.azure\.com/?").rstrip("/")
        self.chat = setting(config, "chatDeployment", r"[A-Za-z0-9_.-]{1,64}")
        self.planner = setting(config, "plannerDeployment", r"[A-Za-z0-9_.-]{1,64}")
        self.embedding = setting(config, "embeddingDeployment", r"[A-Za-z0-9_.-]{1,64}")

    def request(self, path: str, operation: str, **kwargs) -> dict:
        return self.http.json(f"{self.endpoint}{path}?api-version={API_VERSION}", operation, SEARCH_SCOPE, **kwargs)

    def assert_new(self) -> None:
        self.request("/servicestats", "Check Foundry IQ preview API")
        for path, name in [(self.source_path, self.source), (self.base_path, self.name)]:
            try:
                self.request(path, f"Check whether {name} already exists")
            except HttpError as exc:
                if exc.status != 404:
                    raise
            else:
                raise DemoError(f"{name} already exists. Use a fresh indexPrefix.")

    def source_definition(self, config: dict, ontology: dict) -> dict:
        staging = urlsplit(container_url(config, "graphContainer"))
        production = urlsplit(container_url(config, "productionContainer"))
        warehouse = urlsplit(container_url(config, "warehouseContainer"))
        resource = setting(config, "graphStorageResourceId", RESOURCE_PREFIX +
                           r"Microsoft\.Storage/storageAccounts/[a-z0-9]{3,24}")
        storage_name = resource.rsplit("/", 1)[1]
        if (resource.split("/")[2].lower() != uuid_setting(config, "subscriptionId")
                or staging.hostname != f"{storage_name}.blob.core.windows.net"
                or staging.hostname == warehouse.hostname or staging == production):
            raise DemoError("Use a dedicated graph-input container matching its Storage ARM ID, not an originals container.")
        return {
            "name": self.source,
            "kind": "azureBlob",
            "description": "Normalized manufacturing facts with links to the original Blob files.",
            "resultsProcessing": "none",
            "azureBlobParameters": {
                "connectionString": f"ResourceId={resource}",
                "containerName": staging.path.lstrip("/"),
                "folderPath": self.input_folder + "/",
                "ingestionParameters": {
                    "contentExtractionMode": "minimal",
                    "disableImageVerbalization": True,
                    "chatCompletionModel": model(self.model_endpoint, self.chat, "gpt-5.6-luna"),
                    "embeddingModel": model(self.model_endpoint, self.embedding, "text-embedding-3-small"),
                },
                "graphExtraction": {
                    "graphName": self.input_folder,
                    "pageDegreeOfParallelism": 2,
                    "ontology": {"kind": "inline", "value": json.dumps(ontology, separators=(",", ":"))},
                },
            },
        }

    def build(self, config: dict, ontology: dict, records: list[Record]) -> dict:
        self.assert_new()
        indexes = self.publish_source(config, ontology, records)
        self.request(self.base_path, f"Create Foundry IQ knowledge base {self.name}", method="PUT",
                     headers={"If-None-Match": "*", "Prefer": "return=representation"}, body={
                         "name": self.name,
                         "description": "Trace manufacturing quality holds to confirmed customer deliveries.",
                         "knowledgeSources": [{"name": self.source}],
                         "models": [model(self.model_endpoint, self.planner, "gpt-5-mini")],
                         "outputMode": "extractiveData",
                         "retrievalReasoningEffort": {"kind": "low"},
                         "retrievalInstructions": RETRIEVAL_INSTRUCTIONS,
                     })
        return {"knowledgeBase": self.name, "knowledgeSource": self.source, "indexes": indexes}

    def publish_source(self, config: dict, ontology: dict, records: list[Record]) -> dict:
        definition = self.source_definition(config, ontology)
        folder = f"{container_url(config, 'graphContainer')}/{self.input_folder}"
        for record in records:
            write_blob(self.http, folder, record.filename, record.document(), "text/plain; charset=utf-8")
        # The indexer and both indexes are created and owned by the knowledge source.
        self.request(self.source_path, f"Create graph-enabled source {self.source}", method="PUT", body=definition,
                     headers={"If-None-Match": "*", "Prefer": "return=representation"})
        return self.wait_for_ingestion(len(records))

    def indexes(self) -> dict:
        document = self.request(self.source_path, f"Read graph source {self.source}")
        return document.get("azureBlobParameters", {}).get("createdResources") or {}

    def chunk_index(self) -> str:
        index = self.indexes().get("index")
        if not isinstance(index, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", index):
            raise DemoError("The managed source did not report its chunk index.")
        return index

    def refresh_quality(self, config: dict, records: list[Record]) -> dict:
        quality_ids = {record_id for record_id, _ in QUALITY_FILES.values()}
        if (len(records) != len(quality_ids)
                or {(r.source, r.record_id) for r in records} != {("quality-blob", r) for r in quality_ids}):
            raise DemoError("Refresh requires all four original quality Blob records.")
        base = self.request(self.base_path, "Verify the existing graph knowledge base")
        if [ref["name"] for ref in base.get("knowledgeSources", [])] != [self.source]:
            raise DemoError("The knowledge base does not reference only this demo's graph source.")

        container = container_url(config, "graphContainer")
        folder = f"{container}/{self.input_folder}"
        indexes = self.indexes()
        indexer, chunks = indexes.get("indexer"), indexes.get("index")
        if indexer != self.source + "-indexer" or chunks != self.source + "-index" or not indexes.get("graphIndex"):
            raise DemoError("The existing graph source did not report the expected managed resources.")
        index_path = f"/indexes('{chunks}')/docs/search.post.search"
        expected = {f"{folder}/{record.filename}": " ".join(record.document().decode().split()) for record in records}

        def read_chunks() -> dict:
            result = self.request(index_path, "Verify the indexed quality evidence", method="POST",
                                  body={"search": "*", "top": 64, "select": "snippet,blob_url"})
            rows = [(row["blob_url"], " ".join(row["snippet"].split())) for row in result["value"]
                    if row["blob_url"] in expected]
            if len(dict(rows)) != len(rows):
                raise DemoError("The index contains duplicate chunks for a demo quality input.")
            return dict(rows)

        indexed = read_chunks()
        if indexed.keys() != expected.keys():
            raise DemoError("The configured graph input folder does not match the existing indexed evidence.")

        updates = []
        for record in records:
            content, headers = self.http.read_with_headers(
                f"{folder}/{record.filename}", f"Read staged input {record.record_id}", STORAGE_SCOPE,
                headers=storage_headers(), limit=SOURCE_FILE_LIMIT,
            )
            existing = known_record(content.decode("utf-8"))
            if not existing or existing[1] != record.record_id:
                raise DemoError(f"Unexpected input at the existing {record.record_id} graph slot.")
            if content != record.document():
                etag = headers.get("ETag")
                if not etag:
                    raise DemoError(f"No storage ETag was returned for {record.record_id}.")
                updates.append((record, etag))

        if updates or indexed != expected:
            status = self.request(self.source_path + "/status", "Check the graph synchronization state")
            if status.get("currentSynchronizationState"):
                raise DemoError("The source is synchronizing. Wait for it to finish before refreshing.")
            previous = (status.get("lastSynchronizationState") or {}).get("startTime")
            for record, etag in updates:
                write_blob(self.http, folder, record.filename, record.document(), "text/plain; charset=utf-8", etag=etag)
            self.http.read(f"{self.endpoint}/indexers('{indexer}')/search.run?api-version={API_VERSION}",
                           "Refresh the existing graph source", SEARCH_SCOPE, method="POST")
            self.wait_for_ingestion(previous_start=previous)
            if read_chunks() != expected:
                raise DemoError("The completed synchronization did not index all four quality Blob originals.")
        return {"knowledgeBase": self.name, "knowledgeSource": self.source, "graphIndex": indexes["graphIndex"],
                "qualityRecords": sorted(quality_ids), "updatedStagedFiles": len(updates)}

    def wait_for_ingestion(self, expected: int | None = None, *, previous_start: str | None = None) -> dict:
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            status = self.request(self.source_path + "/status", "Read Foundry IQ synchronization status")
            state = status.get("lastSynchronizationState") or {}
            if (state.get("endTime") and (previous_start is None or state.get("startTime") != previous_start)
                    and not status.get("currentSynchronizationState")):
                indexes = self.indexes()
                if state.get("itemsUpdatesFailed", 0) or state.get("itemsSkipped", 0):
                    raise DemoError(f"Graph ingestion failed or skipped files. Inspect indexer {indexes.get('indexer')}.")
                if ((expected is None or state.get("itemsUpdatesProcessed") == expected)
                        and indexes.get("graphIndex") and indexes.get("index")):
                    return indexes
            time.sleep(10)
        target = f"importing all {expected} files" if expected is not None else "refreshing the quality facts"
        raise DemoError(f"Graph source {self.source} did not finish {target}.")

    def retrieve(self, prompt: str) -> dict:
        return retrieve(self.http, self.endpoint, self.name, self.source, prompt, graph=True)

    def retrieve_documents(self, prompt: str) -> dict:
        return retrieve(self.http, self.endpoint, self.documents_base, self.documents_source, prompt, graph=False)

    def ensure_documents(self) -> dict:
        graph = self.request(self.base_path, "Verify the graph knowledge base for comparison")
        planner = model(self.model_endpoint, self.planner, "gpt-5-mini")
        effort = {"kind": "low"}
        models = graph.get("models") or []
        if ([ref.get("name") for ref in graph.get("knowledgeSources", [])] != [self.source]
                or len(models) != 1 or not matches_model(models[0], planner)
                or graph.get("retrievalReasoningEffort") != effort or graph.get("outputMode") != "extractiveData"):
            raise DemoError("The graph KB does not use the configured source and planner for this comparison.")
        chunks = self.chunk_index()
        schema = self.request(f"/indexes('{chunks}')", "Read the common document chunk index")
        semantic = (schema.get("semantic") or {}).get("defaultConfiguration")
        if not semantic:
            raise DemoError("The shared chunk index has no default semantic configuration.")
        source = self.ensure("knowledgesources", {
            "name": self.documents_source, "kind": "searchIndex",
            "description": "Document-only baseline over the same Blob evidence chunks as the lot trace graph.",
            "searchIndexParameters": {
                "searchIndexName": chunks, "semanticConfigurationName": semantic,
                "searchFields": [{"name": "snippet"}],
                "sourceDataFields": [{"name": name} for name in ("uid", "snippet", "blob_url")],
            },
        })
        params = source.get("searchIndexParameters") or {}
        if source.get("kind") != "searchIndex" or params.get("searchIndexName") != chunks:
            raise DemoError("The document-only knowledge source does not wrap the shared chunk index.")
        base = self.ensure("knowledgebases", {
            "name": self.documents_base,
            "description": "Compare document retrieval with graph traversal on the same manufacturing records.",
            "knowledgeSources": [{"name": self.documents_source}],
            "models": [planner], "retrievalReasoningEffort": effort, "outputMode": "extractiveData",
            "retrieveDefaults": {"maxOutputDocuments": EVIDENCE_LIMIT},
        })
        models = base.get("models") or []
        if ([ref.get("name") for ref in base.get("knowledgeSources", [])] != [self.documents_source]
                or len(models) != 1 or not matches_model(models[0], planner)
                or base.get("retrievalReasoningEffort") != effort or base.get("outputMode") != "extractiveData"
                or (base.get("retrieveDefaults") or {}).get("maxOutputDocuments") != EVIDENCE_LIMIT):
            raise DemoError("The document-only knowledge base does not match the intended comparison settings.")
        return {"knowledgeBase": self.documents_base, "knowledgeSource": self.documents_source, "chunkIndex": chunks}

    def ensure(self, collection: str, definition: dict) -> dict:
        path = f"/{collection}('{definition['name']}')"
        try:
            return self.request(path, f"Verify {definition['name']}")
        except HttpError as exc:
            if exc.status != 404:
                raise
        return self.request(path, f"Create {definition['name']}", method="PUT", body=definition,
                            headers={"If-None-Match": "*", "Prefer": "return=representation"})


def matches_model(actual: dict, expected: dict) -> bool:
    params = actual.get("azureOpenAIParameters") or {}
    return (actual.get("kind") == expected["kind"]
            and all(params.get(key) == value for key, value in expected["azureOpenAIParameters"].items()))


def known_record(text: str) -> tuple[str, str] | None:
    source = re.search(r"(?m)^Origin:\s+([a-z-]+)\s*$", text)
    record = re.search(r"(?m)^Original record ID:\s+([A-Z0-9-]+)\s*$", text)
    return (source[1], record[1]) if source and record else None


def coverage(question: dict, snippets: list[str]) -> dict:
    required = {(ref["source"], ref["recordId"]) for path in question["expectedPaths"]
                for ref in path["evidence"].values()}
    found = {known_record(text) for text in snippets}
    return {"found": len(required & found), "required": len(required),
            "missing": [{"source": source, "recordId": record} for source, record in sorted(required - found)]}


def graph_evidence(result: dict) -> tuple[list[str], list[dict], list[str]]:
    calls = {
        activity["id"]: activity["graphSearchArguments"]["gql"]
        for activity in result.get("activity", [])
        if activity.get("type") == "graphSearch"
        and activity.get("graphSearchArguments", {}).get("outcome") == "Success"
        and activity["graphSearchArguments"].get("gql")
    }
    rows, snippets = [], []
    for ref in result.get("references", []):
        if ref.get("type") != "graphSearch" or ref.get("activitySource") not in calls:
            continue
        data = ref.get("sourceData") or {}
        rows.extend(data.get("rows") or [])
        if data.get("snippet"):
            snippets.append(data["snippet"])
    return list(calls.values()), rows, snippets


def document_evidence(result: dict) -> list[str]:
    return [source["snippet"] for ref in result["references"] if ref.get("type") != "graphSearch"
            and isinstance(source := ref.get("sourceData"), dict) and isinstance(source.get("snippet"), str)]
