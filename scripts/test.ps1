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
& node --check (Join-Path $root 'web\aggregation.mjs')
if($LASTEXITCODE -ne 0){throw 'PLAYER_AGGREGATION_PARSE_FAILED'}
$oldPreview=$env:TALK_TEST_PREVIEW
try{
  if($Preview){$env:TALK_TEST_PREVIEW='1'}else{$env:TALK_TEST_PREVIEW='0'}
  & node --test (Join-Path $root 'tests\test_listening.mjs')
  if($LASTEXITCODE -ne 0){throw 'PLAYER_LISTENING_CONTRACTS_FAILED'}
}finally{$env:TALK_TEST_PREVIEW=$oldPreview}
& node --test (Join-Path $root 'tests\test_desktop.cjs')
if($LASTEXITCODE -ne 0){throw 'DESKTOP_CONTRACTS_FAILED'}
& node --test (Join-Path $root 'tests\test_aggregation.mjs')
if($LASTEXITCODE -ne 0){throw 'AGGREGATION_CONTRACTS_FAILED'}
foreach($file in @('desktop\lyrics-window.cjs','desktop\lyrics-preload.cjs','desktop\lyrics\overlay.js','web\desktop-lyrics.mjs')){
  & node --check (Join-Path $root $file)
  if($LASTEXITCODE -ne 0){throw ('DESKTOP_LYRICS_PARSE_FAILED: '+$file)}
}
& node --test (Join-Path $root 'tests\test_desktop_lyrics.cjs') (Join-Path $root 'tests\test_lyrics_timeline.mjs')
if($LASTEXITCODE -ne 0){throw 'DESKTOP_LYRICS_CONTRACTS_FAILED'}
foreach($file in @('scripts\open-score-lab.cjs','capabilities\score_lab\web\app.mjs','capabilities\score_lab\web\timeline.mjs')){
  & node --check (Join-Path $root $file)
  if($LASTEXITCODE -ne 0){throw ('SCORE_LAB_PARSE_FAILED: '+$file)}
}
& node --test (Join-Path $root 'tests\test_score_lab_timeline.mjs')
if($LASTEXITCODE -ne 0){throw 'SCORE_LAB_CONTRACTS_FAILED'}
foreach($file in @('desktop\score-bundle.cjs','web\score-extension.mjs','capabilities\score_lab\web\editor.mjs')){
  & node --check (Join-Path $root $file)
  if($LASTEXITCODE -ne 0){throw ('SCORE_EXTENSION_PARSE_FAILED: '+$file)}
}
& node --test (Join-Path $root 'tests\test_score_bundle.cjs') (Join-Path $root 'tests\test_score_extension.mjs')
if($LASTEXITCODE -ne 0){throw 'SCORE_EXTENSION_CONTRACTS_FAILED'}
foreach($file in @('desktop\score-pdf.cjs','capabilities\score_lab\web\print.mjs','capabilities\score_lab\web\print-layout.mjs')){
  & node --check (Join-Path $root $file)
  if($LASTEXITCODE -ne 0){throw ('SCORE_PDF_PARSE_FAILED: '+$file)}
}
& node --test (Join-Path $root 'tests\test_score_pdf.cjs') (Join-Path $root 'tests\test_score_pdf.mjs')
if($LASTEXITCODE -ne 0){throw 'SCORE_PDF_CONTRACTS_FAILED'}
Write-Output 'PLAYER_TESTS_OK'
