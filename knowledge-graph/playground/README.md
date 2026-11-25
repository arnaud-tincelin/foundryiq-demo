# Lot Trace Playground

A browser UI that compares two Foundry IQ knowledge bases over the same 16 Azure Blob records. One click sends the same question to both in parallel. Each KB uses native `answerSynthesis` to write its own answer from its retrieved evidence. The graph side also shows generated GQL, paths and hydrated citations; the document-only side shows ranked excerpts without graph traversal. Timings include answer synthesis. The backend does not call the Foundry agent or change the KBs' default output mode.

## Run locally

From `knowledge-graph`, use the deployed demo's `config.foundry.local.json`. Both KBs must exist; run `python demo.py documents` to add the document baseline to a published graph. Sign into the Azure tenant with an account that can query both. The Python preview binds to loopback only.

```powershell
python -m pip install -r requirements.txt
az login --tenant <tenant-id>
python playground\serve.py
```

Open [localhost:8765](http://127.0.0.1:8765/) and run the prefilled quality-hold question. Read the answer at the top of each column. The graph should find `CS-021` and `CS-044` and cite nine original Blob records; the document answer depends on the excerpts it retrieved and may not confirm the deliveries. The evidence names private originals; opening them requires an authorized Azure Storage client.

Run the API tests from the same directory with `python -m unittest discover -s playground\tests`.

## Host on Azure Static Web Apps

[The Bicep template](infra/main.bicep) targets the resource group with the existing graph service. It creates **Static Web Apps Standard**, an Azure Functions Flex Consumption backend to link, a dedicated deployment storage account, and a managed identity. Standard is required because [SWA-managed Functions cannot use managed identity](https://learn.microsoft.com/en-us/azure/static-web-apps/apis-functions). Static hosting has a recurring charge; the API, storage, and existing Search service are usage-billed. Defaults are West Europe for SWA and Sweden Central for Functions. No knowledge bases or demo originals are modified.

Validate and review the scope from `knowledge-graph`, then run the create step only when the target and cost are approved:

```powershell
$rg = '<resource-group-containing-the-graph-search-service>'
$subscription = '<subscription-id>'
$search = '<existing-search-service-name>'
$deployment = @(
    '--subscription', $subscription, '--resource-group', $rg,
    '--name', 'manufacturing-trace-playground',
    '--template-file', 'playground\infra\main.bicep',
    '--parameters', "searchServiceName=$search"
)
az deployment group validate @deployment
az deployment group what-if @deployment
```

After reviewing the preview and approving these new resources:

```powershell
az deployment group create @deployment
$out = az deployment group show --subscription $subscription --resource-group $rg `
    --name manufacturing-trace-playground --query properties.outputs -o json | ConvertFrom-Json
```

Link the Function to the SWA production environment. This must create the **Azure Static Web Apps (Linked)** authentication provider on the Function. The API is disabled by default even if deployed before that protection is in place.

```powershell
az staticwebapp backends link --subscription $subscription --resource-group $rg `
    --name $out.staticWebAppName.value --backend-resource-id $out.backendId.value `
    --backend-region $out.backendRegion.value

$package = Join-Path $env:TEMP "lot-trace-api-$([guid]::NewGuid()).zip"
python playground\package_api.py $package
az functionapp deployment source config-zip --subscription $subscription --resource-group $rg `
    --name $out.backendName.value --src $package --build-remote true
```

In the Function app's **Authentication** settings, keep **Require authentication** enabled and set unauthenticated requests to **HTTP 401 Unauthorized**. The linked provider does not support interactive login at the Function origin. After Azure registers the `trace` trigger, verify the Function rejects anonymous requests **directly**, not merely when accessed through SWA. Run this check in PowerShell 7:

```powershell
$probe = Invoke-WebRequest -Uri "$($out.backendUrl.value)/api/trace" -Method Post `
    -ContentType application/json -Body '{"question":"Trace QH-014"}' `
    -MaximumRedirection 0 -SkipHttpErrorCheck
if ($probe.StatusCode -notin 401, 403) { throw 'The direct Function endpoint is not protected.' }
az functionapp config appsettings set --subscription $subscription --resource-group $rg `
    --name $out.backendName.value --settings 'PLAYGROUND_LINK_READY=true' --output none
```

Install the [Static Web Apps CLI](https://learn.microsoft.com/en-us/azure/static-web-apps/static-web-apps-cli-install) and deploy only the static `web` folder. Keep the deployment token in the process environment and never put it in the repository or page:

```powershell
$env:SWA_CLI_DEPLOYMENT_TOKEN = az staticwebapp secrets list --subscription $subscription `
    --resource-group $rg --name $out.staticWebAppName.value --query properties.apiKey -o tsv
try { swa deploy playground\web --env production }
finally { Remove-Item Env:SWA_CLI_DEPLOYMENT_TOKEN -ErrorAction SilentlyContinue }
```

Invite the intended Microsoft Entra users with the custom `trace_reader` role under **Role Management** in the Static Web App. The static site and `/api/trace` both require that role; the linked Function checks it as well. Use the `playgroundUrl` output after accepting the invitation. Verify anonymous access is blocked and that the signed-in page shows both KBs side by side. The two API requests differ only in their `mode` (`graph` or `documents`); neither accepts a caller-supplied resource name.
