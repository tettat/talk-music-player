param([ValidateRange(1024,65535)][int]$Port=43180,[ValidateRange(60,172800)][int]$Ttl=86400)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot 'runtime.ps1')
$config=Get-TalkConfig -Root $root
$python=$config.python
if(-not $PSBoundParameters.ContainsKey('Port')){$Port=$config.port}
if(-not $PSBoundParameters.ContainsKey('Ttl')){$Ttl=$config.ttl_seconds}
if(-not (Test-Path -LiteralPath $python -PathType Leaf)){throw 'PINNED_MUSIC_RUNTIME_REQUIRED'}
if(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue){throw 'LOCAL_PORT_ALREADY_IN_USE'}
New-Item -ItemType Directory -Force -Path $config.data_dir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root 'artifacts') | Out-Null
$ready=Join-Path $root 'artifacts\player-ready.json'
$started=(Get-Date).ToUniversalTime()
$process=Start-Process -FilePath $python -ArgumentList @('-I','-B',('"'+(Join-Path $root 'src\server.py')+'"'),'--port',"$Port",'--ttl',"$Ttl") -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $config.data_dir 'player-stdout.log') -RedirectStandardError (Join-Path $config.data_dir 'player-stderr.log')
for($i=0;$i -lt 80;$i++){
  Start-Sleep -Milliseconds 150
  if((Test-Path -LiteralPath $ready) -and (Get-Item -LiteralPath $ready).LastWriteTimeUtc -ge $started){
    $state=Get-Content -LiteralPath $ready -Raw | ConvertFrom-Json
    $native=Get-CimInstance Win32_Process -Filter "ProcessId=$($state.pid)"
    if(-not $native -or $native.CreationDate.ToUniversalTime() -lt $started.AddSeconds(-1)){throw 'PLAYER_INSTANCE_NOT_VERIFIED'}
    $fingerprint=[ordered]@{pid=$state.pid;path=$native.ExecutablePath;createdUtc=$native.CreationDate.ToUniversalTime().ToString('o');createdUtcTicks=$native.CreationDate.ToUniversalTime().Ticks.ToString();launcherPid=$process.Id;port=$Port;url=$state.url;instance=$state.instance;ttlSeconds=$Ttl;helperWindowStyle='Hidden';automaticOfficialClientLaunch=$false}
    $fingerprint | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'artifacts\player-process.json') -Encoding utf8
    $state | ConvertTo-Json
    return
  }
}
throw 'PLAYER_NOT_READY: no official-client launch fallback.'
