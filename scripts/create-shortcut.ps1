param([switch]$Browser)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$label=-join ([char[]]@(0x64ad,0x653e,0x5668))
$shortcutPath=Join-Path $root ('talk '+$label+'.lnk')
$target=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$openScript=Join-Path $root 'scripts\open-desktop.ps1'
if($Browser){$openScript=Join-Path $root 'scripts\open-player.ps1'}
if(-not $Browser -and -not (Test-Path -LiteralPath (Join-Path $root 'node_modules\electron\dist\electron.exe'))){throw 'RUN_NPM_CI_AND_SETUP_DESKTOP_FIRST'}
$shell=New-Object -ComObject WScript.Shell
if(Test-Path -LiteralPath $shortcutPath){
  $existing=$shell.CreateShortcut($shortcutPath)
  $oldBrowser=Join-Path $root 'scripts\open-player.ps1'
  $oldDesktop=Join-Path $root 'scripts\open-desktop.ps1'
  if($existing.TargetPath -ne $target -or ($existing.Arguments -notlike "*$oldBrowser*" -and $existing.Arguments -notlike "*$oldDesktop*")){
    throw 'UNRELATED_SHORTCUT_WILL_NOT_BE_OVERWRITTEN'
  }
}
$shortcut=$shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath=$target
$shortcut.Arguments='-NoProfile -WindowStyle Hidden -File "'+$openScript+'"'
$shortcut.WorkingDirectory=$root
$shortcut.WindowStyle=7
$shortcut.Description='talk desktop music player'
$shortcut.IconLocation=(Join-Path $root 'node_modules\electron\dist\electron.exe')+',0'
$shortcut.Save()
Write-Output $shortcutPath
