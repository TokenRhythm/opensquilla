!ifndef BUILD_UNINSTALLER
  !include "LogicLib.nsh"

  Var /GLOBAL opensquillaRecoveryRoot
  Var /GLOBAL opensquillaRecoveryPhase
  Var /GLOBAL opensquillaRecoveryCommit

  Function OpenSquillaRecoveryWritePhase
    Exch $0
    FileOpen $1 "$opensquillaRecoveryPhase" w
    FileWrite $1 $0
    FileClose $1
    Pop $0
  FunctionEnd

  Function OpenSquillaPrepareUpdateRecovery
    Push $0
    Push $1
    Push $2
    Push $3
    Push $4
    Push $5

    ReadRegStr $0 SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" InstallLocation
    ${If} $0 == ""
      Goto opensquilla_recovery_done
    ${EndIf}
    ${IfNot} ${FileExists} "$0\${APP_EXECUTABLE_FILENAME}"
      Goto opensquilla_recovery_done
    ${EndIf}

    System::Call 'kernel32::GetCurrentProcessId() i .s'
    Pop $1
    StrCpy $opensquillaRecoveryRoot "$TEMP\OpenSquilla-update-recovery-$1"
    StrCpy $opensquillaRecoveryPhase "$opensquillaRecoveryRoot\phase.txt"
    StrCpy $opensquillaRecoveryCommit "$opensquillaRecoveryRoot\commit"
    CreateDirectory "$opensquillaRecoveryRoot"
    File /oname=$PLUGINSDIR\OpenSquillaUpdateRecovery.ps1 "${PROJECT_DIR}\scripts\nsis\installer-recovery.ps1"
    CopyFiles /SILENT "$PLUGINSDIR\OpenSquillaUpdateRecovery.ps1" "$opensquillaRecoveryRoot\watchdog.ps1"

    ${If} $installMode == "CurrentUser"
      StrCpy $2 "HKCU"
      StrCpy $5 ""
    ${Else}
      StrCpy $2 "HKLM"
      StrCpy $5 "HKCU"
    ${EndIf}
    StrCpy $3 '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$opensquillaRecoveryRoot\watchdog.ps1" -ParentPid $1 -InstallRoot "$0" -RecoveryRoot "$opensquillaRecoveryRoot" -RegistryRoot $2'
    ${If} $5 != ""
      StrCpy $3 '$3 -SecondaryRegistryRoot $5'
    ${EndIf}
    StrCpy $3 '$3 -InstallRegistryKey "${INSTALL_REGISTRY_KEY}" -UninstallRegistryKey "${UNINSTALL_REGISTRY_KEY}"'
    !ifdef UNINSTALL_REGISTRY_KEY_2
      StrCpy $3 '$3 -UninstallRegistryKey2 "${UNINSTALL_REGISTRY_KEY_2}"'
    !endif
    Exec $3

    StrCpy $4 0
    opensquilla_recovery_wait:
      IfFileExists "$opensquillaRecoveryRoot\error.txt" opensquilla_recovery_error
      IfFileExists "$opensquillaRecoveryRoot\ready" opensquilla_recovery_done
      Sleep 250
      IntOp $4 $4 + 1
      ${If} $4 >= 240
        Goto opensquilla_recovery_error
      ${EndIf}
      Goto opensquilla_recovery_wait

    opensquilla_recovery_error:
      MessageBox MB_OK|MB_ICONSTOP "OpenSquilla could not prepare a recoverable Windows update. The existing installation was left unchanged."
      Abort

    opensquilla_recovery_done:
    Pop $5
    Pop $4
    Pop $3
    Pop $2
    Pop $1
    Pop $0
  FunctionEnd

  Function OpenSquillaCommitUpdateRecovery
    ${If} $opensquillaRecoveryCommit != ""
      FileOpen $0 "$opensquillaRecoveryCommit" w
      FileWrite $0 "committed"
      FileClose $0
    ${EndIf}
  FunctionEnd

  Function OpenSquillaRecoveryPauseIfRequested
    ReadEnvStr $0 "OPENSQUILLA_NSIS_RECOVERY_PAUSE_MS"
    ${If} $0 != ""
      IntOp $0 $0 + 0
      ${If} $0 > 0
        Sleep $0
      ${EndIf}
    ${EndIf}
  FunctionEnd

  !macro OpenSquillaMarkUpdateRecoveryPhase phase
    ${If} $opensquillaRecoveryPhase != ""
      Push "${phase}"
      Call OpenSquillaRecoveryWritePhase
    ${EndIf}
  !macroend
!endif
