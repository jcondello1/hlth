Param(
  [string]$RootFile = "lambda_function.py",
  [string]$DeployDir = "deploy",
  [string]$ZipFile = "lambda.zip",
  [string]$ReqFile = "requirements.txt",
  [string]$FunctionName = "sheets-updater",
  [string]$Region = "us-east-1",
  [string]$Profile = "root"
)

if (-not (Test-Path $ReqFile)) {
  Set-Content -NoNewline $ReqFile "google-api-python-client`ngoogle-auth`ngoogle-auth-httplib2"
}

if (Test-Path $DeployDir) { Remove-Item $DeployDir -Recurse -Force }
New-Item -ItemType Directory -Path $DeployDir | Out-Null

pip install --upgrade -r $ReqFile -t $DeployDir

Copy-Item $RootFile -Destination (Join-Path $DeployDir "lambda_function.py")

if (Test-Path $ZipFile) { Remove-Item $ZipFile -Force }
Compress-Archive -Path "$DeployDir\*" -DestinationPath $ZipFile -Force

aws --profile $Profile lambda update-function-code `
  --function-name $FunctionName `
  --zip-file fileb://$ZipFile `
  --region $Region | Out-String | Write-Host

Write-Host "✅ Built and deployed $ZipFile to Lambda function '$FunctionName' in region $Region using profile '$Profile'."
