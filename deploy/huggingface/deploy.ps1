# ============================================================
#  Deploy HARVEST to a Hugging Face Space.
#
#  One-time setup:
#    1) huggingface.co -> New Space -> SDK: Docker (Blank), CPU basic, Public
#    2) git clone https://huggingface.co/spaces/YOUR_NAME/harvest C:\hf-harvest
#    3) run this script:
#         powershell -ExecutionPolicy Bypass -File deploy.ps1 -SpaceDir C:\hf-harvest
#       (password on push = write token: Settings -> Access Tokens -> Write)
#
#  Copies code + HF README + Dockerfile into the Space and pushes.
# ============================================================
param(
    [Parameter(Mandatory = $true)][string]$SpaceDir,
    [string]$Message = "deploy: harvest portal"
)

$ErrorActionPreference = 'Stop'
$Hf = $PSScriptRoot
$Root = Split-Path -Parent (Split-Path -Parent $Hf)   # ...\harvest

if (-not (Test-Path (Join-Path $SpaceDir '.git'))) {
    throw "SpaceDir '$SpaceDir' is not a git repo. First: git clone https://huggingface.co/spaces/USER/harvest `"$SpaceDir`""
}

Write-Host "Copying code to $SpaceDir ..." -ForegroundColor Cyan
foreach ($item in @('core', 'portal', 'abi')) {
    $dst = Join-Path $SpaceDir $item
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Copy-Item (Join-Path $Root $item) -Destination $dst -Recurse -Force
}
foreach ($f in @('requirements.txt', 'requirements-portal.txt',
                 'config_vibevibe.yaml', 'config_robinhood.yaml', 'config_flop.yaml',
                 'config_arc.yaml', 'config.simple.yaml', 'config.example.yaml')) {
    Copy-Item (Join-Path $Root $f) -Destination (Join-Path $SpaceDir $f) -Force
}
Copy-Item (Join-Path $Root 'Dockerfile') -Destination (Join-Path $SpaceDir 'Dockerfile') -Force
Copy-Item (Join-Path $Hf 'README.md') -Destination (Join-Path $SpaceDir 'README.md') -Force

Write-Host "Commit and push to Hugging Face..." -ForegroundColor Cyan
Set-Location $SpaceDir
git add -A
git commit -m $Message
git push

Write-Host "Done. Space build: https://huggingface.co/spaces/YOUR_NAME/harvest" -ForegroundColor Green
