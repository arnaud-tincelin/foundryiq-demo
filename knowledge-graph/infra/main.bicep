targetScope = 'subscription'

param location string = 'swedencentral'
param resourceGroupName string = 'rg-manufacturing-lot-trace'
param operatorId string
param reuseWarehouse bool = false

var tags = {
  workload: 'manufacturing-lot-trace'
  environment: 'demo'
}

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module demo 'resources.bicep' = {
  name: 'manufacturing-lot-trace-resources'
  scope: group
  params: {
    location: location
    operatorId: operatorId
    reuseWarehouse: reuseWarehouse
    tags: tags
  }
}

output searchEndpoint string = demo.outputs.searchEndpoint
output projectEndpoint string = demo.outputs.projectEndpoint
output projectId string = demo.outputs.projectId
output openaiEndpoint string = demo.outputs.openaiEndpoint
output embeddingDeployment string = demo.outputs.embeddingDeployment
output chatDeployment string = demo.outputs.chatDeployment
output plannerDeployment string = demo.outputs.plannerDeployment
output productionContainer string = demo.outputs.productionContainer
output graphContainer string = demo.outputs.graphContainer
output graphStorageResourceId string = demo.outputs.graphStorageResourceId
output warehouseContainer string = demo.outputs.warehouseContainer
