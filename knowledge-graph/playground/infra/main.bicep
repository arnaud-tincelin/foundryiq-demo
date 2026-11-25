targetScope = 'resourceGroup'

@description('Existing dedicated Foundry IQ Search service in this resource group.')
param searchServiceName string
@description('Existing graph demo prefix shared by the graph and document-only knowledge bases.')
param indexPrefix string = 'lot-trace-demo-portal'
param staticLocation string = 'westeurope'
param apiLocation string = 'swedencentral'
@description('Enable only after linking the Function to Static Web Apps and verifying direct anonymous API requests are blocked.')
param linkedApiReady bool = false

var suffix = uniqueString(resourceGroup().id, 'trace-playground')
var workloadTags = {
  workload: 'manufacturing-lot-trace'
  environment: 'demo'
  component: 'playground'
}

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchServiceName
}

resource frontend 'Microsoft.Web/staticSites@2025-05-01' = {
  name: 'lot-trace-web-${suffix}'
  location: staticLocation
  tags: workloadTags
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    deploymentAuthPolicy: 'DeploymentToken'
    allowConfigFileUpdates: true
    stagingEnvironmentPolicy: 'Disabled'
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'lot-trace-web-id-${suffix}'
  location: apiLocation
  tags: workloadTags
}

resource storage 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: 'lotweb${suffix}'
  location: apiLocation
  kind: 'StorageV2'
  tags: workloadTags
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = {
  name: 'default'
  parent: storage
}

resource packages 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  name: 'function-packages'
  parent: blobs
  properties: {
    publicAccess: 'None'
  }
}

resource storageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, 'Storage Blob Data Owner')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
  }
}

resource searchRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, identity.id, 'Search Index Data Reader')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '1407120a-92aa-4202-b7e9-c0e197c71c8f')
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-lot-trace-web-${suffix}'
  location: apiLocation
  tags: workloadTags
  kind: 'functionapp'
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  properties: {
    reserved: true
  }
}

resource backend 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-lot-trace-web-${suffix}'
  location: apiLocation
  tags: workloadTags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    siteConfig: {
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'AzureWebJobsStorage__clientId', value: identity.properties.clientId }
        { name: 'PLAYGROUND_IDENTITY_CLIENT_ID', value: identity.properties.clientId }
        { name: 'PLAYGROUND_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
        { name: 'PLAYGROUND_INDEX_PREFIX', value: indexPrefix }
        { name: 'PLAYGROUND_LINK_READY', value: linkedApiReady ? 'true' : 'false' }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${packages.name}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.id
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
  }
  dependsOn: [
    storageRole
    searchRole
  ]
}

output staticWebAppName string = frontend.name
output staticWebAppHost string = frontend.properties.defaultHostname
output playgroundUrl string = 'https://${frontend.properties.defaultHostname}'
output backendName string = backend.name
output backendId string = backend.id
output backendUrl string = 'https://${backend.properties.defaultHostName}'
output backendRegion string = apiLocation
