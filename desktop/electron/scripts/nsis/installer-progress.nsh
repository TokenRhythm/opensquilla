!ifndef BUILD_UNINSTALLER
  !include "LogicLib.nsh"
  !include "nsDialogs.nsh"
  !include "WinMessages.nsh"

  !define /ifndef PBS_MARQUEE 0x08
  !define OPENSQUILLA_LANG_SIMPCHINESE 2052
  !define OPENSQUILLA_LANG_TRADCHINESE 1028

  Function OpenSquillaInstallerProgressShow
    ${IfNot} ${Silent}
      Push $0
      Push $1

      FindWindow $0 "#32770" "" $HWNDPARENT
      ${If} $0 != 0
        GetDlgItem $1 $0 1004
        ${If} $1 != 0
          ${NSD_AddStyle} $1 ${PBS_MARQUEE}
          SendMessage $1 ${PBM_SETMARQUEE} 1 40
        ${EndIf}

        GetDlgItem $1 $0 1006
        ${If} $1 != 0
          StrCmp $LANGUAGE ${OPENSQUILLA_LANG_SIMPCHINESE} opensquilla_progress_initial_zh_cn
          StrCmp $LANGUAGE ${OPENSQUILLA_LANG_TRADCHINESE} opensquilla_progress_initial_zh_tw
          SendMessage $1 ${WM_SETTEXT} 0 "STR:Preparing and unpacking OpenSquilla files. This may take several minutes…"
          Goto opensquilla_progress_initial_done

          opensquilla_progress_initial_zh_cn:
            SendMessage $1 ${WM_SETTEXT} 0 "STR:正在准备并解压 OpenSquilla 文件，请稍候…"
            Goto opensquilla_progress_initial_done

          opensquilla_progress_initial_zh_tw:
            SendMessage $1 ${WM_SETTEXT} 0 "STR:正在準備並解壓縮 OpenSquilla 檔案，請稍候…"

          opensquilla_progress_initial_done:
        ${EndIf}
      ${EndIf}

      Pop $1
      Pop $0
    ${EndIf}
  FunctionEnd

  Function OpenSquillaInstallerProgressFinish
    ${IfNot} ${Silent}
      Push $0
      Push $1

      FindWindow $0 "#32770" "" $HWNDPARENT
      ${If} $0 != 0
        GetDlgItem $1 $0 1006
        ${If} $1 != 0
          StrCmp $LANGUAGE ${OPENSQUILLA_LANG_SIMPCHINESE} opensquilla_progress_finish_zh_cn
          StrCmp $LANGUAGE ${OPENSQUILLA_LANG_TRADCHINESE} opensquilla_progress_finish_zh_tw
          SendMessage $1 ${WM_SETTEXT} 0 "STR:Finishing installation…"
          Goto opensquilla_progress_finish_done

          opensquilla_progress_finish_zh_cn:
            SendMessage $1 ${WM_SETTEXT} 0 "STR:正在完成安装…"
            Goto opensquilla_progress_finish_done

          opensquilla_progress_finish_zh_tw:
            SendMessage $1 ${WM_SETTEXT} 0 "STR:正在完成安裝…"

          opensquilla_progress_finish_done:
        ${EndIf}
      ${EndIf}

      Pop $1
      Pop $0
    ${EndIf}
  FunctionEnd

  !macro customPageAfterChangeDir
    !define MUI_PAGE_CUSTOMFUNCTION_SHOW OpenSquillaInstallerProgressShow
  !macroend

  !macro customInstall
    ${IfNot} ${Silent}
      Call OpenSquillaInstallerProgressFinish
    ${EndIf}
  !macroend
!endif
