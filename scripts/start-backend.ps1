$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONPATH = "backend/app"
.\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend/app --host 127.0.0.1 --port 8000 --reload

