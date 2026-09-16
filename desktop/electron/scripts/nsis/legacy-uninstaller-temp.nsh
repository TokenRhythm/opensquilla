; Compatibility boundary for already released electron-builder 26.15.3 uninstallers.
; Their atomicRMDir moves old files into $PLUGINSDIR\old-install using MAX_PATH APIs.
; Never change the first attempt, the machine environment, or the new application's
; environment. Only retry exit code 2 with a shorter, writable old-install parent.
; Capture the child environment and restore the parent BEFORE the old uninstaller
; starts: a restore failure must never strand an already removed old installation.

!include "LogicLib.nsh"

Var /GLOBAL osqLegacyRetry
Var /GLOBAL osqLegacyEntryErrors
Var /GLOBAL osqLegacyTempChanged
Var /GLOBAL osqLegacySavedTemp
Var /GLOBAL osqLegacySavedTmp
Var /GLOBAL osqLegacyRestoreFailed
Var /GLOBAL osqLegacyChildEnvironment
Var /GLOBAL osqLegacyCreateFailed

!if ${NSIS_PTR_SIZE} > 4
  !define OSQ_LEGACY_STARTUP_SIZE 104
  !define OSQ_LEGACY_PROCESS_SIZE 24
  !define OSQ_LEGACY_MESSAGE_SIZE 48
!else
  !define OSQ_LEGACY_STARTUP_SIZE 68
  !define OSQ_LEGACY_PROCESS_SIZE 16
  !define OSQ_LEGACY_MESSAGE_SIZE 28
!endif

!macro OpenSquillaLegacyRetry
  StrCpy $osqLegacyRetry 0
  ${If} $R5 > 1
  ${AndIf} $R0 == 2
    StrCpy $osqLegacyRetry 1
  ${EndIf}
!macroend

; Save the entire UTF-16 value in a native buffer, including values longer than
; NSIS_MAX_STRLEN. A null pointer means absent; an allocated empty string means
; present but empty. Registers $1..$4 belong to the enclosing Prepare function.
!macro OpenSquillaSaveLegacyEnvironment NAME POINTER
  System::Call 'kernel32::SetLastError(i 0)'
  System::Call 'kernel32::GetEnvironmentVariableW(w "${NAME}", p 0, i 0) i.r1 ?e'
  Pop $2
  ${If} $1 == 0
    ${If} $2 == 203 ; ERROR_ENVVAR_NOT_FOUND
      StrCpy ${POINTER} 0
    ${ElseIf} $2 == 0
      StrCpy $1 1
    ${Else}
      Goto osqLegacyPrepareFatal
    ${EndIf}
  ${EndIf}
  ${If} $1 > 0
    IntOp $3 $1 * 2
    System::Alloc $3
    Pop ${POINTER}
    ${If} ${POINTER} == 0
      Goto osqLegacyPrepareFatal
    ${EndIf}
    System::Call '*${POINTER}(&i2 0)'
    System::Call 'kernel32::SetLastError(i 0)'
    System::Call 'kernel32::GetEnvironmentVariableW(w "${NAME}", p ${POINTER}, i r1) i.r3 ?e'
    Pop $4
    ${If} $3 >= $1
      Goto osqLegacyPrepareFatal
    ${EndIf}
    ; Empty values return zero successfully and need not clear GetLastError.
    ; The size query established presence; the buffer was initialized to NUL.
    ${If} $3 == 0
    ${AndIf} $1 > 1
      Goto osqLegacyPrepareFatal
    ${EndIf}
  ${EndIf}
!macroend

Function OpenSquillaLegacyTempPrepare
  ; The caller pushes its registered old path. The upstream installationDir
  ; variable is declared later than this include, so do not reference it here.
  Exch $0
  Push $1
  Push $2
  Push $3
  Push $4
  StrCpy $osqLegacyTempChanged 0
  StrCpy $osqLegacySavedTemp 0
  StrCpy $osqLegacySavedTmp 0
  StrCpy $osqLegacyChildEnvironment 0
  ${If} $osqLegacyRetry != 1
    Goto osqLegacyPrepareDone
  ${EndIf}

  ; Use the registered OLD directory, not the new wizard's $INSTDIR. Root parents
  ; must stay absolute (D:\, never drive-relative D:). Short names are optional.
  ${If} $0 == ""
    Goto osqLegacyPrepareDone
  ${EndIf}
  ClearErrors
  GetFullPathName $0 "$0\.."
  ${If} ${Errors}
    Goto osqLegacyPrepareDone
  ${EndIf}
  ${If} $0 == ""
    Goto osqLegacyPrepareDone
  ${EndIf}
  System::Call 'kernel32::GetFileAttributesW(w r0) i.r1'
  ${If} $1 == -1
    Goto osqLegacyPrepareDone
  ${EndIf}
  IntOp $1 $1 & 0x10 ; FILE_ATTRIBUTE_DIRECTORY
  ${If} $1 == 0
    Goto osqLegacyPrepareDone
  ${EndIf}
  System::Call 'kernel32::GetShortPathNameW(w r0, w .r1, i ${NSIS_MAX_STRLEN}) i.r2'
  ${If} $2 > 0
  ${AndIf} $2 < ${NSIS_MAX_STRLEN}
    StrCpy $0 $1
  ${EndIf}
  StrLen $1 $0
  StrLen $2 $TEMP
  ${If} $1 >= $2
    Goto osqLegacyPrepareDone
  ${EndIf}

  ; Verify directory creation, not just file write access. GetTempFileName creates
  ; only our probe file. CreateDirectoryW must create a NEW directory; an existing
  ; path is never removed. No recursive deletion or shared-directory cleanup.
  ClearErrors
  GetTempFileName $3 "$0"
  ${If} ${Errors}
    Goto osqLegacyPrepareDone
  ${EndIf}
  System::Call 'kernel32::DeleteFileW(w r3) i.r4'
  ${If} $4 == 0
    Goto osqLegacyPrepareDone
  ${EndIf}
  System::Call 'kernel32::CreateDirectoryW(w r3, p 0) i.r4'
  ${If} $4 == 0
    Goto osqLegacyPrepareDone
  ${EndIf}
  System::Call 'kernel32::RemoveDirectoryW(w r3) i.r4'
  ${If} $4 == 0
    Goto osqLegacyPrepareDone
  ${EndIf}

  !insertmacro OpenSquillaSaveLegacyEnvironment "TEMP" $osqLegacySavedTemp
  !insertmacro OpenSquillaSaveLegacyEnvironment "TMP" $osqLegacySavedTmp
  System::Call 'kernel32::SetEnvironmentVariableW(w "TEMP", w r0) i.r1'
  ${If} $1 == 0
    Goto osqLegacyPrepareFatal
  ${EndIf}
  StrCpy $osqLegacyTempChanged 1
  System::Call 'kernel32::SetEnvironmentVariableW(w "TMP", w r0) i.r1'
  ${If} $1 == 0
    Call OpenSquillaLegacyTempRestore
    Goto osqLegacyPrepareFatal
  ${EndIf}
  ; Windows supplies the complete, sorted Unicode block, including hidden drive
  ; current-directory entries. Never rebuild or log the full environment.
  System::Call 'kernel32::GetEnvironmentStringsW() p.r1'
  StrCpy $osqLegacyChildEnvironment $1
  Call OpenSquillaLegacyTempRestore
  ${If} $osqLegacyChildEnvironment == 0
    Goto osqLegacyPrepareFatal
  ${EndIf}
  DetailPrint "Retrying the previous uninstaller with a shorter temporary path."
  Goto osqLegacyPrepareDone

  osqLegacyPrepareFatal:
    ; SetErrors alone would let upstream handleUninstallResult continue to
    ; overwrite the old installation. Stop before installing any new files.
    DetailPrint "Unable to safely preserve the uninstaller environment."
    SetErrorLevel 2
    Quit
  osqLegacyPrepareDone:
    Pop $4
    Pop $3
    Pop $2
    Pop $1
    Pop $0
FunctionEnd

Function OpenSquillaLegacyTempRestore
  Push $1
  StrCpy $osqLegacyRestoreFailed 0
  ${If} $osqLegacyTempChanged == 1
    System::Call 'kernel32::SetEnvironmentVariableW(w "TEMP", p $osqLegacySavedTemp) i.r1'
    ${If} $1 == 0
      StrCpy $osqLegacyRestoreFailed 1
    ${EndIf}
    System::Call 'kernel32::SetEnvironmentVariableW(w "TMP", p $osqLegacySavedTmp) i.r1'
    ${If} $1 == 0
      StrCpy $osqLegacyRestoreFailed 1
    ${EndIf}
  ${EndIf}
  ${If} $osqLegacySavedTemp != 0
    System::Free $osqLegacySavedTemp
  ${EndIf}
  ${If} $osqLegacySavedTmp != 0
    System::Free $osqLegacySavedTmp
  ${EndIf}
  StrCpy $osqLegacySavedTemp 0
  StrCpy $osqLegacySavedTmp 0
  StrCpy $osqLegacyTempChanged 0
  Pop $1
  ${If} $osqLegacyRestoreFailed != 0
    DetailPrint "Unable to restore the installer environment. Installation stopped."
    SetErrorLevel 2
    Quit
  ${EndIf}
FunctionEnd

Function OpenSquillaExecWithLegacyEnvironment
  ; The command line is identical to ExecWait and writable for CreateProcessW.
  Exch $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push $6
  Push $7
  StrCpy $osqLegacyCreateFailed 0
  StrCpy $1 0
  StrCpy $2 0
  StrCpy $3 0
  StrCpy $4 0

  ; Allocate all bookkeeping BEFORE starting the old uninstaller. GPTR zeroes
  ; STARTUPINFO; only cb is set, exactly as NSIS v3.04 myCreateProcess does.
  System::Call 'kernel32::GlobalAlloc(i 0x40, p ${OSQ_LEGACY_STARTUP_SIZE}) p.r1'
  System::Call 'kernel32::GlobalAlloc(i 0x40, p ${OSQ_LEGACY_PROCESS_SIZE}) p.r2'
  System::Call 'kernel32::GlobalAlloc(i 0x40, p ${OSQ_LEGACY_MESSAGE_SIZE}) p.r3'
  ${If} $1 == 0
  ${OrIf} $2 == 0
  ${OrIf} $3 == 0
    DetailPrint "Unable to prepare the previous uninstaller process."
    SetErrorLevel 2
    Quit
  ${EndIf}
  System::Call '*$1(i ${OSQ_LEGACY_STARTUP_SIZE})'

  ; Match NSIS v3.04 util.c: NULL application/CWD/security, no inherited handles,
  ; CREATE_DEFAULT_ERROR_MODE. Only add CREATE_UNICODE_ENVIRONMENT (0x400).
  System::Call 'kernel32::CreateProcessW(p 0, w r0, p 0, p 0, i 0, i 0x04000400, p $osqLegacyChildEnvironment, p 0, p r1, p r2) i.r5'
  ${If} $5 == 0
    StrCpy $osqLegacyCreateFailed 1
    Goto osqLegacyProcessCleanup
  ${EndIf}
  System::Call '*$2(p.r4, p.r5)'
  System::Call 'kernel32::CloseHandle(p r5)'

  osqLegacyProcessWait:
    System::Call 'kernel32::WaitForSingleObject(p r4, i 100) i.r7'
    ${If} $7 == 258 ; WAIT_TIMEOUT
      Goto osqLegacyProcessPaint
    ${EndIf}
    ; Reuse the preallocated PROCESS_INFORMATION buffer for an explicitly
    ; nonzero default. A failed exit query must not look like success or a
    ; CreateProcess failure that launches a second uninstaller in-place.
    System::Call '*$2(i 2)'
    System::Call 'kernel32::GetExitCodeProcess(p r4, p r2) i.r6'
    System::Call '*$2(i.r5)'
    ${If} $7 != 0 ; Wait failed: do not assume the child has stopped.
      ${If} $6 == 0
      ${OrIf} $5 == 259 ; STILL_ACTIVE
        Sleep 100
        Goto osqLegacyProcessPaint
      ${EndIf}
    ${EndIf}
    ${If} $6 == 0
      StrCpy $5 2 ; A failed API's output buffer is not trustworthy.
      DetailPrint "The previous uninstaller stopped, but its exit code could not be confirmed."
    ${EndIf}
    StrCpy $R0 $5
    Goto osqLegacyProcessCleanup

  osqLegacyProcessPaint:
    ; Same message pump as NSIS WaitForProcess: only WM_PAINT, no new cancel
    ; or other installer callbacks while the old uninstaller is running.
    System::Call 'user32::PeekMessageW(p r3, p 0, i 15, i 15, i 1) i.r6'
    ${If} $6 != 0
      System::Call 'user32::DispatchMessageW(p r3)'
      Goto osqLegacyProcessPaint
    ${EndIf}
    Goto osqLegacyProcessWait

  osqLegacyProcessCleanup:
    ${If} $4 != 0
      System::Call 'kernel32::CloseHandle(p r4)'
    ${EndIf}
    ; Cleanup cannot introduce a new abort after the previous files were removed.
    ; The parent's environment was already restored before CreateProcessW.
    System::Call 'kernel32::GlobalFree(p r1)'
    System::Call 'kernel32::GlobalFree(p r2)'
    System::Call 'kernel32::GlobalFree(p r3)'
    System::Call 'kernel32::FreeEnvironmentStringsW(p $osqLegacyChildEnvironment)'
    StrCpy $osqLegacyChildEnvironment 0
    Pop $7
    Pop $6
    Pop $5
    Pop $4
    Pop $3
    Pop $2
    Pop $1
    Pop $0
    ${If} $osqLegacyCreateFailed == 1
      SetErrors
    ${ElseIf} $osqLegacyEntryErrors == 1
      SetErrors
    ${Else}
      ClearErrors
    ${EndIf}
FunctionEnd

!macro OpenSquillaExecLegacyUninstaller EXECUTABLE
  ; Preserve the upstream incoming Errors state as well as ExecWait's result.
  ; The same retry decision is used if copied execution falls back to in-place.
  ${If} ${Errors}
    StrCpy $osqLegacyEntryErrors 1
  ${Else}
    StrCpy $osqLegacyEntryErrors 0
  ${EndIf}
  Push $installationDir
  Call OpenSquillaLegacyTempPrepare
  ${If} $osqLegacyEntryErrors == 1
    SetErrors
  ${Else}
    ClearErrors
  ${EndIf}
  ${If} $osqLegacyChildEnvironment == 0
    ExecWait '"${EXECUTABLE}" /S /KEEP_APP_DATA $0 _?=$installationDir' $R0
  ${Else}
    Push '"${EXECUTABLE}" /S /KEEP_APP_DATA $0 _?=$installationDir'
    Call OpenSquillaExecWithLegacyEnvironment
  ${EndIf}
!macroend
