# Pre-deploy gate - run before `railway up`. Fails if anything doesn't compile,
# a test fails, or a stored library command no longer parses against the runner DSL.
$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"

& $py -m compileall -q engine ui eval models run.py
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: compile errors"; exit 1 }

& $py -m pytest tests/ -q
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: tests"; exit 1 }

& $py -m eval.dsl_check
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: stored commands no longer parse (dsl_check)"; exit 1 }

Write-Host "predeploy OK - safe to railway up"
