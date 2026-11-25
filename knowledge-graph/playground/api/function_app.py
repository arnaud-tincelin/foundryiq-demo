import json
import logging
import os

import azure.functions as func
from azure.identity import ManagedIdentityCredential

from records import DemoError
from services import Http
from trace_api import TraceTarget, handle_trace, is_invited

app = func.FunctionApp()
LOGGER = logging.getLogger(__name__)


def reply(status: int, body: dict) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/json",
                             headers={"Cache-Control": "no-store"})


@app.route(route="trace", trigger_arg_name="request", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def trace(request: func.HttpRequest) -> func.HttpResponse:
    # The linked SWA App Service authentication provider protects the Function origin.
    if os.environ.get("PLAYGROUND_LINK_READY") != "true":
        return reply(503, {"error": "The Azure playground is not enabled yet."})
    if not is_invited(request.headers.get("x-ms-client-principal", "")):
        return reply(403, {"error": "An invited Microsoft Entra account is required."})
    if request.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
        return reply(415, {"error": "Content-Type must be application/json."})
    try:
        target = TraceTarget.from_prefix(os.environ["PLAYGROUND_SEARCH_ENDPOINT"], os.environ["PLAYGROUND_INDEX_PREFIX"])
        client_id = os.environ["PLAYGROUND_IDENTITY_CLIENT_ID"]
    except (DemoError, KeyError):
        LOGGER.error("Playground settings are missing or invalid.")
        return reply(503, {"error": "The Azure playground is not configured."})
    with ManagedIdentityCredential(client_id=client_id) as credential:
        return reply(*handle_trace(request.get_body(), target, Http(credential)))
