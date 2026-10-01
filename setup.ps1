param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $PythonPath) {
    $taskCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($taskCommand) { $PythonPath = $taskCommand.Source }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Provide Python 3.11 or newer: .\setup.ps1 -PythonPath C:\path\to\python.exe'
}
& $PythonPath -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
& '.\.venv\Scripts\python.exe' -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host 'Ready. Open START.cmd, then type the practice you want. You will be asked for the API key in the terminal.'
$taskLatex = @('pdflatex', 'xelatex', 'lualatex', 'tectonic') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if (-not $taskLatex) {
    Write-Host 'No LaTeX engine found. Install one for LaTeX PDFs: winget install MiKTeX.MiKTeX'
    Write-Host 'PDFs can use the built-in renderer; DOCX does not require LaTeX.'
}
