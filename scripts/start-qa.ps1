param([ValidateRange(1024,65535)][int]$Port=43181,[ValidateRange(60,172800)][int]$Ttl=1800,[switch]$Preview)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot 'runtime.ps1')
$config=Get-TalkConfig -Root $root
$python=$config.python
New-Item -ItemType Directory -Force -Path $config.data_dir | Out-Null
if(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue){throw 'QA_PORT_ALREADY_IN_USE'}
$started=(Get-Date).ToUniversalTime()
$ready=Join-Path $root 'artifacts\qa-player-ready.json'
$arguments=@('-I','-B',('"'+(Join-Path $root 'src\server.py')+'"'),'--port',"$Port",'--ttl',"$Ttl",'--qa')
if($Preview){$arguments += '--preview'}
$process=Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $config.data_dir 'qa-stdout.log') -RedirectStandardError (Join-Path $config.data_dir 'qa-stderr.log')
for($i=0;$i -lt 80;$i++){
  Start-Sleep -Milliseconds 150
  if((Test-Path -LiteralPath $ready) -and (Get-Item -LiteralPath $ready).LastWriteTimeUtc -ge $started){
    $state=Get-Content -LiteralPath $ready -Raw | ConvertFrom-Json
    $native=Get-CimInstance Win32_Process -Filter "ProcessId=$($state.pid)"
    if(-not $native -or $native.CreationDate.ToUniversalTime() -lt $started.AddSeconds(-1)){throw 'QA_INSTANCE_NOT_VERIFIED'}
    $state | ConvertTo-Json
    return
  }
}
throw 'QA_PLAYER_NOT_READY'
