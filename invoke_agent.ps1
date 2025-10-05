# invoke-agent.ps1
param (
    [string]$Session = "cli-test-1"
)

$ErrorActionPreference = "Stop"

aws bedrock-agent-runtime create-invocation `
    --region us-east-1 `
    --cli-input-json file://invoke_input.json
