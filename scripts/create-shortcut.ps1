param([switch]$Browser)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$label=-join ([char[]]@(0x5c0f,0x9ec4,0x82b1))
$shortcutPath=Join-Path $root ($label+' Usic.lnk')
$oldLabel=-join ([char[]]@(0x64ad,0x653e,0x5668))
$legacyPath=Join-Path $root ('talk '+$oldLabel+'.lnk')
$target=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$openScript=Join-Path $root 'scripts\open-desktop.ps1'
if($Browser){$openScript=Join-Path $root 'scripts\open-player.ps1'}
if(-not $Browser -and -not (Test-Path -LiteralPath (Join-Path $root 'node_modules\electron\dist\electron.exe'))){throw 'RUN_NPM_CI_AND_SETUP_DESKTOP_FIRST'}
$shell=New-Object -ComObject WScript.Shell
if(-not (Test-Path -LiteralPath $shortcutPath) -and (Test-Path -LiteralPath $legacyPath)){
  $legacy=$shell.CreateShortcut($legacyPath)
  $ownedBrowser=Join-Path $root 'scripts\open-player.ps1'
  $ownedDesktop=Join-Path $root 'scripts\open-desktop.ps1'
  if($legacy.TargetPath -eq $target -and ($legacy.Arguments -like "*$ownedBrowser*" -or $legacy.Arguments -like "*$ownedDesktop*")){
    Move-Item -LiteralPath $legacyPath -Destination $shortcutPath
  }
}
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
$shortcut.Arguments='-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "'+$openScript+'"'
$shortcut.WorkingDirectory=$root
$shortcut.WindowStyle=7
$shortcut.Description='Usic music player'
$shortcut.IconLocation=(Join-Path $root 'desktop\assets\usic.ico')+',0'
$shortcut.Save()
Write-Output $shortcutPath
