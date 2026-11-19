#Requires -Version 5.1
$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Log = Join-Path $Root "publish-dash.log"
$Stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$Code = 1
"[$Stamp] publish start" | Out-File $Log -Append -Encoding utf8

try {
    $GitCommand = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $GitPath = if ($GitCommand) { $GitCommand.Source } else { Join-Path $env:USERPROFILE ".qoder\bin\git\cmd\git.exe" }
    if (-not (Test-Path -LiteralPath $GitPath -PathType Leaf)) {
        throw "git.exe not found in task PATH or Qoder Git"
    }
    $env:PATH = (Split-Path -Parent $GitPath) + ";" + $env:PATH
    $Python = Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1
    Push-Location $Root -ErrorAction Stop
    try {
        & $Python.Source (Join-Path $Root "publish_dash.py") 2>&1 |
            ForEach-Object { $_.ToString() } |
            Out-File $Log -Append -Encoding utf8
        if ($null -ne $LASTEXITCODE) { $Code = $LASTEXITCODE }
    }
    finally {
        Pop-Location
    }
}
catch {
    "[$Stamp] publish FAILED: $($_.Exception.Message)" | Out-File $Log -Append -Encoding utf8
}
"[$Stamp] publish exit=$Code" | Out-File $Log -Append -Encoding utf8
exit $Code
