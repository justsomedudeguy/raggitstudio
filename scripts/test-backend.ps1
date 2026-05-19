$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONPATH = "backend/app"
.\.venv\Scripts\python.exe -m pytest backend/tests -q

