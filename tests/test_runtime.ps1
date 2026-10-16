$ErrorActionPreference='Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts/runtime.ps1')
$root=Join-Path ([IO.Path]::GetTempPath()) ('talk-runtime-'+[guid]::NewGuid().ToString('N'))
$null=New-Item -ItemType Directory -Path $root
$file=Join-Path $root 'config.json'
$previous=$env:TALK_CONFIG_FILE
try{
  $env:TALK_CONFIG_FILE=$file
  [IO.File]::WriteAllText($file,'{"schema_version":1,"port":43309,"ttl_seconds":120}')
  $config=Get-TalkConfig -Root $root
  if($config.port -ne 43309 -or $config.ttl_seconds -ne 120 -or $config.port -isnot [int]){throw 'VALID_JSON_INTEGER_REJECTED'}
  foreach($json in @('{"port":true}','{"port":"43309"}','{"port":43309.5}',
    '{"port":65536}','{"ttl_seconds":false}','{"ttl_seconds":59}',
    '{"schema_version":true}','{"schema_version":"1"}')){
    [IO.File]::WriteAllText($file,$json)
    $rejected=$false
    try{$null=Get-TalkConfig -Root $root}catch{
      if($_.Exception.Message -notlike 'CONFIG_*'){throw}
      $rejected=$true
    }
    if(-not $rejected){throw 'INVALID_JSON_NUMBER_ACCEPTED'}
  }
  Write-Output 'POWERSHELL_RUNTIME_CONFIG_OK'
}finally{
  $env:TALK_CONFIG_FILE=$previous
  # Only our two known temporary paths; no recursive filesystem operation.
  if(Test-Path -LiteralPath $file){Remove-Item -LiteralPath $file -Force}
  Remove-Item -LiteralPath $root -Force
}
