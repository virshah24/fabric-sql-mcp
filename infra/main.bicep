targetScope = 'resourceGroup'

@description('The Azure region for all resources.')
param location string = resourceGroup().location

@description('The azd environment name.')
param environmentName string

@description('Initial container image. azd deploy replaces this with the built ACR image.')
param containerImageName string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Microsoft Entra tenant ID used to acquire Fabric SQL tokens.')
param fabricTenantId string = ''

@description('Default Fabric SQL endpoint server host.')
param fabricSqlServer string = ''

@description('Default Fabric SQL database/catalog.')
param fabricSqlDatabase string = ''

@description('Optional default schema profile for NLP translation.')
param fabricSqlSchemaProfile string = ''

@description('Optional bearer/API key required for remote MCP calls. Leave empty only for trusted POC environments.')
@secure()
param mcpAuthToken string = ''

@description('Comma-separated list of allowed browser origins for CORS.')
param mcpAllowedOrigins string = ''

@description('Per-client request limit per minute.')
param mcpRateLimitPerMinute string = '60'

@description('Maximum rows returned by non-export SQL query tools.')
param fabricSqlMaxQueryRows string = '5000'

@description('Maximum rows written by CSV export tools.')
param fabricSqlMaxExportRows string = '100000'

@description('Maximum rows fetched per CSV export page.')
param fabricSqlMaxExportPageSize string = '5000'

var normalizedEnvironmentName = toLower(replace(environmentName, '-', ''))
var resourceToken = take(uniqueString(resourceGroup().id, environmentName), 6)
var namePrefix = 'fabric-sql-mcp'
var acrName = take('cr${normalizedEnvironmentName}${resourceToken}', 50)
var serviceName = 'fabric-sql-mcp'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-log'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-app'
  location: location
  tags: {
    'azd-service-name': serviceName
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
      containers: [
        {
          name: serviceName
          image: containerImageName
          env: [
            {
              name: 'PORT'
              value: '8080'
            }
            {
              name: 'HOST'
              value: '0.0.0.0'
            }
            {
              name: 'MCP_TRANSPORT'
              value: 'streamable-http'
            }
            {
              name: 'MCP_DNS_REBINDING_PROTECTION'
              value: 'false'
            }
            {
              name: 'FABRIC_USE_AZURE_IDENTITY'
              value: 'true'
            }
            {
              name: 'FABRIC_TENANT_ID'
              value: fabricTenantId
            }
            {
              name: 'FABRIC_SQL_SERVER'
              value: fabricSqlServer
            }
            {
              name: 'FABRIC_SQL_DATABASE'
              value: fabricSqlDatabase
            }
            {
              name: 'FABRIC_SQL_SCHEMA_PROFILE'
              value: fabricSqlSchemaProfile
            }
            {
              name: 'MCP_AUTH_TOKEN'
              value: mcpAuthToken
            }
            {
              name: 'MCP_ALLOWED_ORIGINS'
              value: mcpAllowedOrigins
            }
            {
              name: 'MCP_RATE_LIMIT_PER_MINUTE'
              value: mcpRateLimitPerMinute
            }
            {
              name: 'FABRIC_SQL_MAX_QUERY_ROWS'
              value: fabricSqlMaxQueryRows
            }
            {
              name: 'FABRIC_SQL_MAX_EXPORT_ROWS'
              value: fabricSqlMaxExportRows
            }
            {
              name: 'FABRIC_SQL_MAX_EXPORT_PAGE_SIZE'
              value: fabricSqlMaxExportPageSize
            }
            {
              name: 'FABRIC_SQL_EXPORT_DIR'
              value: '/tmp/fabric-sql-mcp-exports'
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
}

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, containerApp.name, 'acrpull')
  scope: containerRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistry.properties.loginServer
output AZURE_CONTAINER_APP_NAME string = containerApp.name
output AZURE_CONTAINER_APP_ENDPOINT string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output MCP_ENDPOINT string = 'https://${containerApp.properties.configuration.ingress.fqdn}/mcp'
output MANAGED_IDENTITY_PRINCIPAL_ID string = containerApp.identity.principalId
