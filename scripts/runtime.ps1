# Machine settings stay in config.local.json or explicit TALK_* overrides, not source.
function Get-TalkConfig {
  param([Parameter(Mandatory=$true)][string]$Root)
  $values=@{schema_version=1;python='.venv/Scripts/python.exe';data_dir='.private';qq_session_dir='.private/qq';analysis_python='.private/runtime-analysis/Scripts/python.exe';basic_pitch_model='.private/models/basic_pitch/nmp.onnx';port=43180;ttl_seconds=86400}
  $file=Join-Path $Root 'config.local.json'
  if($env:TALK_CONFIG_FILE){
    $file=$env:TALK_CONFIG_FILE
    if(-not [IO.Path]::IsPathRooted($file)){$file=Join-Path $Root $file}
    if(-not (Test-Path -LiteralPath $file -PathType Leaf)){throw 'CONFIG_FILE_NOT_FOUND'}
  }
  if(Test-Path -LiteralPath $file -PathType Leaf){
    if((Get-Item -LiteralPath $file).Length -gt 32768){throw 'CONFIG_SIZE_REJECTED'}
    $data=Get-Content -LiteralPath $file -Encoding UTF8 -Raw | ConvertFrom-Json
    if($null -eq $data -or $data -is [array] -or $data -is [string]){throw 'CONFIG_KEYS_INVALID'}
    foreach($property in $data.PSObject.Properties){
      if(-not $values.ContainsKey($property.Name)){throw 'CONFIG_KEYS_INVALID'}
      $values[$property.Name]=$property.Value
    }
  }
  # ConvertFrom-Json uses Int32 on Windows PowerShell and Int64 on PowerShell 7.
  # Accept both integer representations, never coercible strings/bools/floats.
  if(($values.schema_version -isnot [int] -and $values.schema_version -isnot [long]) -or $values.schema_version -ne 1){throw 'CONFIG_VERSION_INVALID'}
  foreach($name in @('python','data_dir','qq_session_dir','analysis_python','basic_pitch_model')){
    $override=[Environment]::GetEnvironmentVariable('TALK_'+$name.ToUpperInvariant())
    if($null -ne $override){$values[$name]=$override}
    $value=$values[$name]
    if($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value) -or $value.Contains([char]0)){throw 'CONFIG_PATH_INVALID'}
    if($value.StartsWith('~/') -or $value.StartsWith('~\')){$value=Join-Path ([Environment]::GetFolderPath('UserProfile')) $value.Substring(2)}
    if(-not [IO.Path]::IsPathRooted($value)){$value=Join-Path $Root $value}
    $values[$name]=[IO.Path]::GetFullPath($value)
  }
  if(($values.port -isnot [int] -and $values.port -isnot [long]) -or $values.port -lt 1024 -or $values.port -gt 65535){throw 'CONFIG_NUMBER_INVALID'}
  if(($values.ttl_seconds -isnot [int] -and $values.ttl_seconds -isnot [long]) -or $values.ttl_seconds -lt 60 -or $values.ttl_seconds -gt 172800){throw 'CONFIG_NUMBER_INVALID'}
  $values.schema_version=[int]$values.schema_version
  $values.port=[int]$values.port
  $values.ttl_seconds=[int]$values.ttl_seconds
  return [pscustomobject]$values
}
