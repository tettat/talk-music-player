$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$electron=Join-Path $root 'node_modules\electron\dist\electron.exe'
if(-not (Test-Path -LiteralPath $electron -PathType Leaf)){throw 'RUN_NPM_CI_FIRST'}
$old=$env:ELECTRON_RUN_AS_NODE
try {
  Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
  # The GUI must be visible; only this launcher and its Python helper stay hidden.
  Start-Process -FilePath $electron -ArgumentList ('"'+$root+'"') -WorkingDirectory $root -WindowStyle Normal | Out-Null
} finally { $env:ELECTRON_RUN_AS_NODE=$old }
