param([switch]$Preview)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot 'runtime.ps1')
$config=Get-TalkConfig -Root $root
$python=$config.python
& $python -I -B -m unittest discover -s (Join-Path $root 'tests') -p 'test_*.py' -v
if($LASTEXITCODE -ne 0){throw 'PLAYER_CONTRACTS_FAILED'}
$web='web'
if($Preview){$web='web-next'}
foreach($file in @('app.js','listening.mjs','window-controls.mjs')){
  & node --check (Join-Path $root ($web+'\'+$file))
  if($LASTEXITCODE -ne 0){throw ('PLAYER_JS_PARSE_FAILED: '+$file)}
}
$oldPreview=$env:TALK_TEST_PREVIEW
try{
  if($Preview){$env:TALK_TEST_PREVIEW='1'}else{$env:TALK_TEST_PREVIEW='0'}
  & node --test (Join-Path $root 'tests\test_listening.mjs')
  if($LASTEXITCODE -ne 0){throw 'PLAYER_LISTENING_CONTRACTS_FAILED'}
}finally{$env:TALK_TEST_PREVIEW=$oldPreview}
& node --test (Join-Path $root 'tests\test_desktop.cjs')
if($LASTEXITCODE -ne 0){throw 'DESKTOP_CONTRACTS_FAILED'}
Write-Output 'PLAYER_TESTS_OK'
