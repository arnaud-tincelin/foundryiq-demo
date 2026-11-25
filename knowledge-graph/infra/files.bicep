param name string
param location string
param tags object
param containers array
param operatorId string
param searchIdentity string = ''

var blobContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var blobReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

resource storage 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = {
  parent: storage
  name: 'default'
  properties: {
    isVersioningEnabled: false
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource sourceContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = [for container in containers: {
  parent: blobs
  name: container
  properties: {
    publicAccess: 'None'
  }
}]

resource seedAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (container, i) in containers: {
  scope: sourceContainers[i]
  name: guid(storage.id, container, operatorId, blobContributor)
  properties: {
    principalId: operatorId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributor)
  }
}]

resource indexerAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(searchIdentity)) {
  scope: storage
  name: guid(storage.id, 'foundry-iq-indexer', blobReader)
  properties: {
    principalId: searchIdentity
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobReader)
  }
}

output storageId string = storage.id
output blobEndpoint string = storage.properties.primaryEndpoints.blob
