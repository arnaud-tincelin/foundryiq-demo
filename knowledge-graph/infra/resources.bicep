param location string
param operatorId string
param tags object = {
  workload: 'manufacturing-lot-trace'
  environment: 'demo'
}
param reuseWarehouse bool

var suffix = uniqueString(resourceGroup().id)
var warehouseName = 'lottrace${suffix}'
var searchEndpoint = 'https://${search.name}.search.windows.net'
var openaiUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var cognitiveUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var searchReader = '1407120a-92aa-4202-b7e9-c0e197c71c8f'
var searchOperatorRoles = [
  '7ca78c08-252a-4471-8644-bb5ff32d4ba0' // Search Service Contributor
  '8ebe5a00-799e-43f5-93ac-243d3dce84a7' // Search Index Data Contributor
]
var foundryOperatorRoles = [
  'eadc314b-1a2d-4efa-be10-5d325db5065e' // Foundry Project Manager
  openaiUser
]

resource search 'Microsoft.Search/searchServices@2026-11-01' = {
  name: 'lot-trace-iq-${suffix}'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'serverless'
  }
  properties: {
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  name: 'lot-trace-foundry-${suffix}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'lot-trace-foundry-${suffix}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-10-01-preview' = {
  parent: foundry
  name: 'manufacturing-lot-trace'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Manufacturing Lot Traceability'
    description: 'Foundry IQ entity-graph retrieval from synthetic manufacturing file records.'
  }
  // Project creation and model deployment both acquire the parent account's write lock.
  dependsOn: [
    planner
  ]
}

resource searchConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-10-01-preview' = {
  parent: project
  name: 'foundry-iq-search'
  properties: {
    category: 'CognitiveSearch'
    authType: 'AAD'
    target: searchEndpoint
    isSharedToAll: false
    metadata: {
      ApiType: 'Azure'
      ResourceId: search.id
    }
  }
}

resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = {
  parent: foundry
  name: 'text-embedding-3-small'
  sku: {
    name: 'DataZoneStandard'
    capacity: 5
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-small'
      version: '1'
    }
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = {
  parent: foundry
  name: 'gpt-5.6-luna'
  sku: {
    name: 'DataZoneStandard'
    capacity: 10
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.6-luna'
      version: '2026-07-09'
    }
  }
  dependsOn: [
    embedding
  ]
}

resource planner 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = {
  parent: foundry
  name: 'gpt-5-mini'
  sku: {
    name: 'DataZoneStandard'
    capacity: 160
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5-mini'
      version: '2025-08-07'
    }
  }
  dependsOn: [
    chat
  ]
}

module production 'files.bicep' = {
  name: 'production-file-stores'
  params: {
    name: 'lotproduction${take(suffix, 11)}'
    location: location
    tags: tags
    containers: [
      'production'
      'graph-input'
    ]
    operatorId: operatorId
    searchIdentity: search.identity.principalId
  }
}

module newWarehouse 'files.bicep' = if (!reuseWarehouse) {
  name: 'warehouse-file-store'
  params: {
    name: warehouseName
    location: location
    tags: tags
    containers: [
      'warehouse'
    ]
    operatorId: operatorId
  }
}

resource searchOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in searchOperatorRoles: {
  name: guid(search.id, operatorId, role)
  scope: search
  properties: {
    principalId: operatorId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
  }
}]

resource foundryOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in foundryOperatorRoles: {
  name: guid(foundry.id, operatorId, role)
  scope: foundry
  properties: {
    principalId: operatorId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
  }
}]

resource searchModelAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in [openaiUser, cognitiveUser]: {
  name: guid(foundry.id, search.id, role)
  scope: foundry
  properties: {
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
  }
}]

resource projectSearchAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, project.id, searchReader)
  scope: search
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchReader)
  }
}

output searchEndpoint string = searchEndpoint
output projectEndpoint string = project.properties.endpoints['AI Foundry API']
output projectId string = project.id
output openaiEndpoint string = 'https://${foundry.name}.openai.azure.com'
output embeddingDeployment string = embedding.name
output chatDeployment string = chat.name
output plannerDeployment string = planner.name
output productionContainer string = '${production.outputs.blobEndpoint}production'
output graphContainer string = '${production.outputs.blobEndpoint}graph-input'
output graphStorageResourceId string = production.outputs.storageId
output warehouseContainer string = 'https://${warehouseName}.blob.core.windows.net/warehouse'
