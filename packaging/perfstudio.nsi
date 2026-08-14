; ============================================================================
; PerfStudio - Windows installer
;
;   pyinstaller perfstudio.spec --noconfirm     (writes dist\perfstudio)
;   makensis packaging\perfstudio.nsi           (writes releases\...Setup.exe)
;
; English and Turkish, because the application is. The interface picks its language from
; --lang, PERFSTUDIO_LANG or the system locale; an installer that could only speak
; English would be the one part of the product that does not.
; ============================================================================

!define APP_NAME    "PerfStudio"
!define APP_VENDOR  "Medinstech"
!define APP_WEBSITE "https://github.com/medinstech/perfstudio"
!define APP_HELPURL "https://github.com/medinstech/perfstudio/issues"
!define EXE_NAME    "perfstudio.exe"

; NSIS resolves the file arguments of its own directives - !searchparse, LicenseData,
; MUI_ICON, File - against the directory holding the *script*, not against wherever
; makensis was invoked from. So the repository root is one level up from here, spelled
; relatively. The preprocessor's `!if /FileExists` is the exception and wants
; ${__FILEDIR__}; both appear below.
!define ROOT ".."

; Overridable so that CI can compile this script against a stub folder. That check takes
; seconds and catches a broken page order or a missing string long before anyone waits
; for the real bundle to compress.
!ifndef BUILD_DIR
  !define BUILD_DIR "${ROOT}\dist\perfstudio"
!endif

; The version comes out of the one line that carries it - see docs/RELEASING.md. A text
; match, so the format of that line is a contract; tests/test_version.py holds both ends
; of it.
!searchparse /file "${ROOT}\src\perfstudio\version.py" `__version__ = "` APP_VERSION `"`

!define SETUP_NAME "PerfStudio_v${APP_VERSION}_Setup.exe"

; Qt and VTK together are a few hundred megabytes and LZMA over that is most of the wall
; clock here. FASTPACK swaps in zlib for internal test builds only: much faster to pack,
; much larger installer.
!ifdef FASTPACK
  SetCompressor /SOLID zlib
!else
  SetCompressor /SOLID lzma
  SetCompressorDictSize 64
!endif

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

; ----------------------------------------------------------------- system --
Name "${APP_NAME} ${APP_VERSION}"
OutFile "${ROOT}\releases\${SETUP_NAME}"
BrandingText "${APP_VENDOR}  -  ${APP_NAME} ${APP_VERSION}"
InstallDir "$PROGRAMFILES64\${APP_NAME}"
InstallDirRegKey HKLM "Software\${APP_VENDOR}\${APP_NAME}" "Install_Dir"
RequestExecutionLevel admin
Unicode true

; VIProductVersion wants exactly four integers and the project version has three, so the
; build field is a constant zero. The human-readable strings below carry the real
; version, including any .devN suffix.
VIProductVersion "0.0.0.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "CompanyName" "${APP_VENDOR}"
VIAddVersionKey "LegalCopyright" "Copyright 2026 ${APP_VENDOR}. Apache-2.0."
VIAddVersionKey "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"

; ------------------------------------------------------------------- look --
!define MUI_ABORTWARNING

!if /FileExists "${ROOT}\src\perfstudio\ui\assets\perfstudio.ico"
  !define MUI_ICON   "${ROOT}\src\perfstudio\ui\assets\perfstudio.ico"
  !define MUI_UNICON "${ROOT}\src\perfstudio\ui\assets\perfstudio.ico"
!endif

; ------------------------------------------------------------------ pages --
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${ROOT}\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE_NAME}"
!define MUI_FINISHPAGE_RUN_TEXT "$(S_RunNow)"
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\examples"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "$(S_OpenExamples)"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION ShowExamples
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; MUI_LANGUAGE must follow the pages. English first, so it is the fallback when the
; machine's locale is neither.
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "Turkish"

; -------------------------------------------------------------- strings ----
LangString S_SecMain      ${LANG_ENGLISH} "PerfStudio (required)"
LangString S_SecMain      ${LANG_TURKISH} "PerfStudio (gerekli)"
LangString S_SecStartMenu ${LANG_ENGLISH} "Start Menu shortcuts"
LangString S_SecStartMenu ${LANG_TURKISH} "Başlat Menüsü kısayolları"
LangString S_SecDesktop   ${LANG_ENGLISH} "Desktop shortcut"
LangString S_SecDesktop   ${LANG_TURKISH} "Masaüstü kısayolu"
LangString S_SecAssoc     ${LANG_ENGLISH} "Open .perf files with PerfStudio"
LangString S_SecAssoc     ${LANG_TURKISH} ".perf dosyalarını PerfStudio ile aç"

LangString S_DescMain      ${LANG_ENGLISH} "The application, the 61 generated footprints, and the example boards."
LangString S_DescMain      ${LANG_TURKISH} "Uygulama, üretilen 61 footprint ve örnek kartlar."
LangString S_DescStartMenu ${LANG_ENGLISH} "Add PerfStudio to the Start Menu."
LangString S_DescStartMenu ${LANG_TURKISH} "PerfStudio'yu Başlat Menüsüne ekler."
LangString S_DescDesktop   ${LANG_ENGLISH} "Put a shortcut on the desktop."
LangString S_DescDesktop   ${LANG_TURKISH} "Masaüstüne bir kısayol koyar."
LangString S_DescAssoc     ${LANG_ENGLISH} "Double-clicking a board document opens it in PerfStudio."
LangString S_DescAssoc     ${LANG_TURKISH} "Bir kart dosyasına çift tıklamak onu PerfStudio'da açar."

LangString S_RunNow       ${LANG_ENGLISH} "Run PerfStudio"
LangString S_RunNow       ${LANG_TURKISH} "PerfStudio'yu çalıştır"
LangString S_OpenExamples ${LANG_ENGLISH} "Open the example boards"
LangString S_OpenExamples ${LANG_TURKISH} "Örnek kartları aç"
LangString S_FileType     ${LANG_ENGLISH} "PerfStudio board"
LangString S_FileType     ${LANG_TURKISH} "PerfStudio kartı"

LangString S_RemovingOld  ${LANG_ENGLISH} "Removing the previous version..."
LangString S_RemovingOld  ${LANG_TURKISH} "Önceki sürüm kaldırılıyor..."
LangString S_Copying      ${LANG_ENGLISH} "Copying files..."
LangString S_Copying      ${LANG_TURKISH} "Dosyalar kopyalanıyor..."
LangString S_StillRunning ${LANG_ENGLISH} "PerfStudio appears to be running. Close it and press Retry."
LangString S_StillRunning ${LANG_TURKISH} "PerfStudio çalışıyor görünüyor. Kapatıp Yeniden Dene'ye basın."
LangString S_WriteError   ${LANG_ENGLISH} "Cannot write to the installation folder. Try installing somewhere else."
LangString S_WriteError   ${LANG_TURKISH} "Kurulum klasörüne yazılamıyor. Başka bir konum deneyin."
LangString S_LnkUninstall ${LANG_ENGLISH} "Uninstall PerfStudio"
LangString S_LnkUninstall ${LANG_TURKISH} "PerfStudio'yu kaldır"
LangString S_LnkWebsite   ${LANG_ENGLISH} "PerfStudio on GitHub"
LangString S_LnkWebsite   ${LANG_TURKISH} "GitHub'da PerfStudio"

; ----------------------------------------------------------------- funcs ---
Function .onInit
  ; Before anything reads the registry -- InstallDirRegKey above resolves during init, and
  ; a 32-bit installer resolves it in the 32-bit view unless told otherwise. The default
  ; install directory is the same either way; setting it here is what keeps every lookup
  ; in this file talking about one set of keys.
  SetRegView 64

  ; Pick the installer's language from the machine rather than asking. Somebody who set
  ; their Windows to Turkish has already answered this question.
  !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

Function un.onInit
  !insertmacro MUI_UNGETLANGUAGE
FunctionEnd

Function ShowExamples
  ExecShell "open" "$INSTDIR\examples"
FunctionEnd

; Installing over a running copy leaves half the old bundle in place, and the symptom is
; an application that starts and fails on an import - which reads as a bug in the
; application rather than as a failed install.
Function AssertExeClosed
  Pop $R0
  ${If} ${FileExists} "$R0\${EXE_NAME}"
    retry:
      ClearErrors
      FileOpen $R1 "$R0\${EXE_NAME}" a
      ${If} ${Errors}
        MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "$(S_StillRunning)" IDRETRY retry
        Abort
      ${Else}
        FileClose $R1
      ${EndIf}
  ${EndIf}
FunctionEnd

; --------------------------------------------------------------- install ---
Section "$(S_SecMain)" SecMain
  SectionIn RO

  ; THIS INSTALL IS MACHINE-WIDE, SO ITS SHORTCUTS AND ITS REGISTRY ENTRIES MUST BE TOO.
  ; Both defaults are wrong here and both were, until a real install was inspected:
  ;
  ; $SMPROGRAMS and $DESKTOP default to the CURRENT user, and under RequestExecutionLevel
  ; admin that is whoever answered the elevation prompt. Installing for a standard user
  ; from an administrator account therefore puts every shortcut in the administrator's
  ; profile and none in the profile of the person who will use the program.
  ;
  ; makensis produces a 32-bit installer, so HKLM\Software goes through WOW64 redirection
  ; into Software\WOW6432Node -- a 64-bit application, installed into $PROGRAMFILES64,
  ; registering itself where 32-bit applications live. Add/Remove Programs reads both
  ; views so it still appears, but every script and inventory tool that looks in the
  ; 64-bit view sees nothing.
  SetShellVarContext all
  SetRegView 64

  ; Upgrade in place by clearing the old install first. The previous uninstaller is
  ; deliberately not called - it can prompt even when run silently - and installing
  ; *over* a PyInstaller bundle is worse than either: leftover modules and DLLs from the
  ; old version load in preference to the new ones.
  ;
  ; This has to run before SetOutPath "$INSTDIR": if the working directory is inside the
  ; folder being removed, RMDir cannot remove the folder itself.
  ;
  ; BOTH REGISTRY VIEWS ARE SEARCHED, and that is not belt-and-braces. 0.4.0 shipped
  ; without the SetRegView above, so every copy of it in the world recorded itself in the
  ; 32-bit view; a 0.5.0 installer that looked only where it writes would find nothing,
  ; skip this whole block, and unpack itself over the bundle it was meant to replace --
  ; which is the exact failure the block exists to prevent.
  SetOutPath "$TEMP"
  ReadRegStr $R2 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation"
  ${If} $R2 == ""
    SetRegView 32
    ReadRegStr $R2 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation"
    ${If} $R2 != ""
      DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
      DeleteRegKey HKLM "Software\${APP_VENDOR}\${APP_NAME}"
    ${EndIf}
    SetRegView 64
  ${EndIf}
  ${If} $R2 != ""
  ${AndIf} ${FileExists} "$R2\${EXE_NAME}"     ; never RMDir a path from a broken key
    DetailPrint "$(S_RemovingOld)"
    Push "$R2"
    Call AssertExeClosed
    RMDir /r "$R2"
    ; Both contexts, for the same reason: 0.4.0's shortcuts went into the profile of
    ; whoever ran it, and leaving them behind points a Start menu entry at an executable
    ; this installer has just deleted.
    Delete "$DESKTOP\${APP_NAME}.lnk"
    RMDir /r "$SMPROGRAMS\${APP_NAME}"
    SetShellVarContext current
    Delete "$DESKTOP\${APP_NAME}.lnk"
    RMDir /r "$SMPROGRAMS\${APP_NAME}"
    SetShellVarContext all
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
  ${EndIf}

  Push "$INSTDIR"
  Call AssertExeClosed

  ClearErrors
  CreateDirectory "$INSTDIR"
  IfErrors 0 +3
    MessageBox MB_OK|MB_ICONSTOP "$(S_WriteError)"
    Abort
  SetOutPath "$INSTDIR"

  DetailPrint "$(S_Copying)"
  File /r "${BUILD_DIR}\*.*"

  ; The licence and the notice travel with the installed copy. Apache-2.0 section 4
  ; requires both to be handed on with any distribution, and this is a distribution.
  File "${ROOT}\LICENSE"
  File "${ROOT}\NOTICE"
  File "${ROOT}\README.md"
  File "${ROOT}\CHANGELOG.md"

  WriteUninstaller "$INSTDIR\uninstall.exe"

  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "Publisher" "${APP_VENDOR}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayIcon" "$INSTDIR\${EXE_NAME}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "URLInfoAbout" "${APP_WEBSITE}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "HelpLink" "${APP_HELPURL}"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "EstimatedSize" "$0"

  WriteRegStr HKLM "Software\${APP_VENDOR}\${APP_NAME}" "Install_Dir" "$INSTDIR"
  WriteRegStr HKLM "Software\${APP_VENDOR}\${APP_NAME}" "Version" "${APP_VERSION}"
SectionEnd

Section "$(S_SecStartMenu)" SecStartMenu
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\$(S_LnkUninstall).lnk" "$INSTDIR\uninstall.exe"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\$(S_LnkWebsite).lnk" "${APP_WEBSITE}"
SectionEnd

Section "$(S_SecDesktop)" SecDesktop
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
SectionEnd

; A .perf is a project document and there is exactly one application that reads it, so
; claiming the extension is not presumptuous - but it is still a separate, unticked-able
; section, because a machine with two versions side by side should not have this decided
; for it.
Section "$(S_SecAssoc)" SecAssoc
  WriteRegStr HKCR ".perf" "" "PerfStudio.Board"
  WriteRegStr HKCR "PerfStudio.Board" "" "$(S_FileType)"
  WriteRegStr HKCR "PerfStudio.Board\DefaultIcon" "" "$INSTDIR\${EXE_NAME},0"
  WriteRegStr HKCR "PerfStudio.Board\shell\open\command" "" '"$INSTDIR\${EXE_NAME}" "%1"'
  ; Tell the shell the association changed, or Explorer keeps showing the old icon until
  ; the next sign-in.
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "$(S_DescMain)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStartMenu} "$(S_DescStartMenu)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "$(S_DescDesktop)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAssoc} "$(S_DescAssoc)"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ------------------------------------------------------------ uninstall ----
Section "Uninstall"
  ; Matching the install section, and it has to: an uninstaller that reads the other
  ; shell context finds no shortcuts to delete and leaves them pointing at nothing.
  SetShellVarContext all
  SetRegView 64

  Delete "$DESKTOP\${APP_NAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"

  ; Only if it is still ours. A newer install, or another tool, may have taken the
  ; extension since - and removing somebody else's association is worse than leaving a
  ; stale one behind.
  ReadRegStr $R0 HKCR ".perf" ""
  ${If} $R0 == "PerfStudio.Board"
    DeleteRegKey HKCR ".perf"
  ${EndIf}
  DeleteRegKey HKCR "PerfStudio.Board"

  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
  DeleteRegKey HKLM "Software\${APP_VENDOR}\${APP_NAME}"
  RMDir /r "$INSTDIR"

  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'
  SetAutoClose true
SectionEnd

; --------------------------------------------------------------- signing ---
; Both hooks matter: an unsigned uninstaller shows "Unknown Publisher" at the UAC prompt
; even when the installer itself is signed. Nothing passes SIGNCMD today - see
; docs/RELEASING.md on what a signed build would need.
!ifdef SIGNCMD
  !finalize       '${SIGNCMD} "%1"'
  !uninstfinalize '${SIGNCMD} "%1"'
!endif
