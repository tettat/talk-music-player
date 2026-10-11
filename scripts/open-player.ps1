param([ValidateRange(1024,65535)][int]$Port=43180,[switch]$NoBrowser)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot 'runtime.ps1')
$config=Get-TalkConfig -Root $root
if(-not $PSBoundParameters.ContainsKey('Port')){$Port=$config.port}
$instanceFile=Join-Path $root 'artifacts\player-process.json'
$serverFile=Join-Path $root 'src\server.py'
$reusable=$false
if(Test-Path -LiteralPath $instanceFile){
  $instance=Get-Content -LiteralPath $instanceFile -Raw | ConvertFrom-Json
  if($instance.port -eq $Port){
    $native=Get-CimInstance Win32_Process -Filter "ProcessId=$($instance.pid)"
    $listener=Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if($native -and $native.ExecutablePath -eq $instance.path -and
       $native.CreationDate.ToUniversalTime().Ticks.ToString() -eq [string]$instance.createdUtcTicks -and
       ($listener | Where-Object { $_.OwningProcess -eq $instance.pid -and $_.LocalAddress -eq '127.0.0.1' }) -and
       $native.CommandLine -match [regex]::Escape($serverFile)){
      try{
        $response=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
        $reusable=$response.ok -eq $true -and $response.instance -eq $instance.instance
      }catch{ $reusable=$false }
    }
  }
}
if(-not $reusable){
  & (Join-Path $root 'scripts\start-player.ps1') -Port $Port | Out-Null
  if(-not $?){throw 'PLAYER_START_FAILED'}
}
if(-not $NoBrowser){Start-Process -FilePath "http://127.0.0.1:$Port/" -WindowStyle Normal}
else{Write-Output "PLAYER_READY http://127.0.0.1:$Port/"}
