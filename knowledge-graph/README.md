# Manufacturing lot traceability in Microsoft Foundry

Show the graph knowledge base and its ontology-backed Blob staging source in the `Manufacturing Lot Traceability` project in [Microsoft Foundry](https://ai.azure.com/). Then ask the [Lot Trace Playground](playground/) a question:

> Which customer sites are confirmed to have received finished lots affected by quality hold QH-014? For each, show the sensor lot, assembly lot, finished lot, and shipment ID, with the source records for each step.

The playground sends the same question to **two** Foundry IQ knowledge bases in parallel and shows each KB's own written answer. The graph KB can return the paths to `CS-021` and `CS-044`, the generated GQL, and nine hydrated Blob citations. The document-only KB answers from ranked text from the same document chunks without running GQL. Compare the answers, original citations, and total response time. The optional Foundry prompt agent uses only the graph KB.

## What the graph connects

```text
QualityHold -> SensorLot -> AssemblyLot -> FinishedLot -> Shipment -> CustomerSite
```

| System of record | Files | Evidence |
| --- | --- | --- |
| [Production Blob account](data/production-blob/) | 7 JSON files | Posted material consumption and completed builds |
| [Warehouse Blob account](data/warehouse-blob/) | 5 JSON files | Dispatch and inventory |
| [Warehouse Blob account, `quality/` prefix](data/warehouse-blob/quality/) | 4 JSON files | The quality hold and signed delivery receipts |

The [native entity graph preview](https://learn.microsoft.com/en-us/azure/search/agentic-knowledge-source-how-to-entity-graph) requires a Blob knowledge source. The importer reads all 16 originals and writes one short fact record per file, with its original Blob URL, to a private `graph-input` container. Foundry IQ uses [the ontology](ontology.json) to extract the graph and generate GQL at query time. It does not receive prebuilt edges, a fixed query, or the [answer key](benchmarks/ground-truth.json). The document-only source wraps the graph pipeline's document chunk index, not its graph index. It does not copy the corpus or supply citations to the graph KB.

## Provision and seed

Use dedicated synthetic-data resources and Microsoft Entra authentication. Keep the Foundry project, graph storage, and Search access restricted to the demo operators. The graph and its two backing indexes are created through the preview API; the portal is the place to present the Foundry project and agent.

[Bicep](infra/main.bicep) creates an isolated Foundry resource and project, Serverless Search, and separate private Blob accounts for production and warehouse files. `gpt-5.6-luna` extracts the entities, `gpt-5-mini` plans both KB searches with `low` reasoning effort, and `text-embedding-3-small` embeds the shared evidence chunks. It enables Blob soft delete and disables versioning for the graph input. The project identity has read access to Search; the Search identity has read access to the graph storage and inference access to the Foundry models. Search, model, and storage usage are billed. Check region support for the graph preview and model deployments before provisioning.

```powershell
az login --tenant <tenant-id>
$target = @(
    '--subscription', '<subscription-id>', '--location', 'swedencentral',
    '--template-file', 'infra\main.bicep', '--parameters', 'operatorId=<operator-object-id>'
)
az deployment sub validate @target
az deployment sub what-if @target
```

After reviewing what-if, deploy with `az deployment sub create @target`. Use a fresh resource group name for a new environment. `reuseWarehouse=true` is for an existing demo group whose dedicated warehouse account and container already exist; it does not update that account.

Run the following from `knowledge-graph`:

1. Upload `data/production-blob` to `production` and `data/warehouse-blob` to `warehouse` in their respective Blob accounts, preserving the `quality/` prefix. Do not upload the answer key or put originals into `graph-input`.
2. In a Python 3.10+ virtual environment, run `python -m pip install -r requirements.txt`. Copy [config.example.json](config.example.json) to `config.foundry.local.json` and fill in your deployment outputs and tenant and subscription IDs. Use a fresh `indexPrefix` for a new graph build. The config contains no tokens or keys.

```powershell
python demo.py preflight
python demo.py publish
python demo.py documents
python demo.py connect
python demo.py compare
python demo.py ask
```

`publish` reads and validates all 16 files, waits for native ingestion and creates the graph KB. `documents` creates or verifies the independent document-only KB over the managed chunks. `connect` attaches only the graph KB to a Foundry prompt agent over a keyless project connection. Use `connect --new-version` to revise the agent. If publishing fails, inspect the source, indexer and staged files before retrying.

For an existing deployment, `python demo.py refresh-quality` refreshes the four quality records from their warehouse blobs without rebuilding the graph. Both KBs then use the updated chunks.

## What to show live

1. **Foundry project and knowledge bases.** Show the model deployments, graph-enabled Blob source, and document-only source. Explain how the ontology turns file evidence into entity relationships. Entity graph configuration is API-only in this preview; don't assume the portal renders a visual graph.
2. **Lot Trace Playground.** Ask the question above. The expected graph paths are `SL-2407 -> AL-301 -> FL-801 -> SH-901 -> CS-021` and `SL-2407 -> AL-302 -> FL-802 -> SH-902 -> CS-044`. Neither `FL-803` (still in inventory), `FL-804` (a released lot), nor `SH-905` (no signed receipt) qualifies. Compare the retrieved evidence and open the generated GQL in the graph column.
3. **Measure evidence coverage.** `compare` sends the same question to both KBs and measures their actual coverage against the nine records supporting the confirmed deliveries. `ask` separately checks that the Foundry agent invoked its graph KB and returned the expected sites and lot identifiers.

See the [Ignite BRK307 session](https://ignite.microsoft.com/en-US/sessions/BRK307) and the official [Foundry IQ agent connection guide](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/foundry-iq-connect). Run the offline contract tests with `python -m unittest discover -s tests`. These tests do not replace a live graph and agent run.
