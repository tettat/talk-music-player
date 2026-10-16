; Assisted install, but always the current user: no all-users mode or elevation.
!macro preInit
  ; Refuse the stock installer's hidden machine-wide command-line override too.
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/allusers" $R1
  ${IfNot} ${Errors}
    SetErrorLevel 2
    Abort "This Usic installer supports the current user only."
  ${EndIf}
!macroend

!macro customInstallMode
  StrCpy $isForceCurrentInstall "1"
  StrCpy $isForceMachineInstall "0"
!macroend

!macro customInit
  StrCpy $hasPerMachineInstallation "0"
  StrCpy $hasPerUserInstallation "1"
  !insertmacro setInstallModePerUser
  ; Windows 10/11 x64 supply the UCRT required by CPython embeddable.
  IfFileExists "$SYSDIR\ucrtbase.dll" +3 0
    MessageBox MB_OK|MB_ICONSTOP "Windows Universal C Runtime is required. Install Windows updates first."
    Abort
!macroend
