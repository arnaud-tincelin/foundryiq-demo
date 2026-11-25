"""Query either of the two configured KBs from the linked Function or local server."""

import base64
import json
import logging
import re
from dataclasses import dataclass
from time import perf_counter
from urllib.parse import urlsplit

from azure.core.exceptions import AzureError

from records import DemoError
from search import document_evidence, document_target, graph_evidence, known_record, retrieve
from services import Http, HttpError, setting

MAX_BODY_BYTES = 4096
MAX_QUESTION = 400
READER_ROLE = "trace_reader"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraceTarget:
    endpoint: str
    base: str
    source: str
    documents_base: str
    documents_source: str

    @classmethod
    def from_prefix(cls, endpoint: str, prefix: str):
        prefix = setting({"prefix": prefix}, "prefix", r"lot-trace-demo-[a-z0-9]+(?:-[a-z0-9]+)*")
        endpoint = setting({"endpoint": endpoint}, "endpoint", r"https://[a-z0-9-]+\.search\.windows\.net/?")
        if len(prefix) > 40:
            raise DemoError("The demo index prefix must be at most 40 characters.")
        return cls(endpoint.rstrip("/"), prefix + "-kb", prefix + "-blob-source", *document_target(prefix))


def is_invited(principal_header: str) -> bool:
    if not principal_header or len(principal_header) > 4096:
        return False
    try:
        principal = json.loads(base64.b64decode(principal_header, validate=True))
    except ValueError:
        return False
    if not isinstance(principal, dict):
        return False
    roles = principal.get("userRoles")
    return (principal.get("identityProvider") == "aad" and isinstance(principal.get("userId"), str)
            and bool(principal["userId"]) and isinstance(roles, list) and READER_ROLE in roles)


def original_excerpt(snippet: str) -> dict | None:
    record = known_record(snippet)
    link = re.search(r"(?m)^Original record URL:[ \t]+(\S+)[ \t]*$", snippet)
    excerpt = re.split(r"(?m)^Original record ID:", snippet, maxsplit=1)[0].strip()
    if not record or not link or not excerpt:
        return None
    source, record_id = record
    try:
        parsed = urlsplit(link[1])
    except ValueError:
        return None
    if (source not in {"production-blob", "warehouse-blob", "quality-blob"} or parsed.scheme != "https"
            or not re.fullmatch(r"[a-z0-9]{3,24}\.blob\.core\.windows\.net", parsed.netloc)
            or not parsed.path.startswith("/") or parsed.query or parsed.fragment):
        return None
    return {"source": source, "recordId": record_id, "url": link[1], "excerpt": excerpt}


def answer_text(result: dict, evidence: dict[tuple[str, str], dict], *, has_paths: bool) -> str | None:
    if not any(item.get("type") == "modelAnswerSynthesis" for item in result["activity"]):
        return None
    messages = result.get("response")
    if not isinstance(messages, list):
        return None
    texts = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        texts.extend(part["text"].strip() for part in content
                     if isinstance(part, dict) and part.get("type") == "text"
                     and isinstance(part.get("text"), str) and part["text"].strip())
    if not texts:
        return None

    labels = {}
    for ref in result["references"]:
        data = ref.get("sourceData")
        if not isinstance(data, dict):
            continue
        snippet = data.get("snippet")
        key = known_record(snippet) if isinstance(snippet, str) else None
        if key is not None and key in evidence:
            labels[str(ref.get("id"))] = evidence[key]["recordId"]
        elif has_paths and ref.get("type") == "graphSearch" and data.get("rows"):
            labels[str(ref.get("id"))] = "graph paths"
    return re.sub(r"\[ref_id:\s*(\d+)\]", lambda match: f"[{labels.get(match[1], 'source not shown')}]",
                  "\n\n".join(texts))


def shape_result(result: dict, *, graph: bool = True) -> dict:
    if any(not isinstance(result.get(key), list) or not all(isinstance(item, dict) for item in result[key])
           for key in ("activity", "references")):
        raise DemoError("The knowledge base returned an unexpected retrieval response.")
    if graph:
        queries, rows, snippets = graph_evidence(result)
    else:
        if any(item.get("type") == "graphSearch" for key in ("activity", "references") for item in result[key]):
            raise DemoError("The document-only KB unexpectedly returned graph results.")
        queries, rows, snippets = [], [], []
    if not rows:
        snippets.extend(document_evidence(result))
    evidence = {}
    for snippet in snippets:
        if isinstance(snippet, str) and (record := original_excerpt(snippet)):
            evidence.setdefault((record["source"], record["recordId"]), record)
    return {"answer": answer_text(result, evidence, has_paths=bool(rows)),
            "paths": rows, "queries": queries, "evidence": list(evidence.values())}


def handle_trace(body: bytes, target: TraceTarget, http: Http) -> tuple[int, dict]:
    if len(body) > MAX_BODY_BYTES:
        return 413, {"error": "The request is too large."}
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeError):
        return 400, {"error": "Send a JSON object with a question."}
    question = value.get("question") if isinstance(value, dict) else None
    if not isinstance(question, str) or not question.strip() or len(question.strip()) > MAX_QUESTION:
        return 400, {"error": f"Enter a question between 1 and {MAX_QUESTION} characters."}
    mode = value.get("mode", "graph")
    if mode not in ("graph", "documents"):
        return 400, {"error": "Choose graph or documents as the retrieval mode."}
    graph = mode == "graph"
    base, source = (target.base, target.source) if graph else (target.documents_base, target.documents_source)
    started = perf_counter()
    try:
        result = retrieve(http, target.endpoint, base, source, question.strip(), graph=graph, max_runtime=28,
                          synthesize=True)
        answer = shape_result(result, graph=graph)
    except HttpError as exc:
        LOGGER.warning("Foundry IQ retrieval rejected the request: HTTP %s", exc.status)
        if exc.status in (429, 503):
            return 503, {"error": "Foundry IQ is busy. Try again shortly."}
        if exc.status in (401, 403):
            return 502, {"error": "The playground identity is not authorized to query the knowledge base."}
        return 502, {"error": "Foundry IQ could not complete the request."}
    except AzureError as exc:
        LOGGER.warning("Search credential failed: %s", type(exc).__name__)
        return 503, {"error": "The playground could not sign in to Azure Search."}
    except (DemoError, AttributeError, KeyError, TypeError) as exc:
        LOGGER.warning("Foundry IQ response unavailable: %s", type(exc).__name__)
        return 502, {"error": "No complete retrieval response was received from Foundry IQ."}
    return 200, {**answer, "mode": mode, "knowledgeBase": base, "elapsedMs": round((perf_counter() - started) * 1000)}
