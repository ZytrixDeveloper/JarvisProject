; Jarvis installer (Inno Setup 6.7+). Built by build.ps1 -> dist\Jarvis-Setup.exe
#define AppName "J.A.R.V.I.S."
; build.ps1 passes /DAppVersion from app/jarvis_core/config.py
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppExe "Jarvis.exe"

[Setup]
AppId={{8F3C1B2A-6D4E-4C1B-9F7A-2B5E1C0D9A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=ZytrixDeveloper
AppPublisherURL=https://zytrixdeveloper.github.io/JarvisProject/
AppSupportURL=https://github.com/ZytrixDeveloper/JarvisProject
DefaultDirName={autopf}\Jarvis
DefaultGroupName=Jarvis
DisableProgramGroupPage=yes
; Installs for the current user without admin rights (admin can still pick "all users").
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=Jarvis-Setup
SetupIconFile=..\app\assets\jarvis.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern dark
WizardBackColor=$0E0803
WizardImageFile=..\app\assets\wizard.bmp
WizardSmallImageFile=..\app\assets\wizard-small.bmp
CloseApplications=yes
; Always write %TEMP%\Setup Log *.txt so failed installs/updates can be diagnosed.
SetupLogging=yes
VersionInfoVersion={#AppVersion}
VersionInfoCompany=ZytrixDeveloper
VersionInfoDescription=J.A.R.V.I.S. Setup
VersionInfoProductName=J.A.R.V.I.S.
VersionInfoProductVersion={#AppVersion}
VersionInfoCopyright=Copyright (c) 2026 ZytrixDeveloper

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=Welcome to J.A.R.V.I.S.
WelcomeLabel2=This will install Jarvis {#AppVersion}, your personal AI assistant for Windows.%n%nJarvis runs its AI on your own PC using Ollama. If Ollama isn't installed yet, Setup will offer to open its download page at the end.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Run Jarvis in the background when &Windows starts (recommended)"; GroupDescription: "Startup:"

[Files]
Source: "..\app\dist\Jarvis\*"; Excludes: "\Jarvis.exe"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Jarvis.exe is placed by [Code] (PlaceJarvisExe) with retries: antivirus scanners lock every
; brand-new EXE for a few seconds, and Setup's own 4-second retry then aborts the whole install.
Source: "..\app\dist\Jarvis\Jarvis.exe"; Flags: dontcopy
; Last entry: once every other file is in place, Jarvis.exe goes in too (before [Run] opens it).
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion; AfterInstall: PlaceExeNow

[Icons]
Name: "{group}\Jarvis"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall Jarvis"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Jarvis"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

; "Start with Windows" is set in [Code] (EnableAutostart), not [Registry]: antivirus programs often
; block new apps from writing the Run key, and a failed [Registry] entry aborts the whole install.

[Run]
; Open Jarvis as soon as it's installed: after a normal install and after silent self-updates.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: ExePlaced

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
; settings, memory, login, speech + wake-word models, logs
Type: filesandordirs; Name: "{userappdata}\Jarvis"
Type: files; Name: "{userstartup}\Jarvis.lnk"

[Code]
var
  JarvisExePlaced: Boolean;

function ExePlaced: Boolean;
begin
  Result := JarvisExePlaced;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  // A running Jarvis (for example the one that started this update) locks its files.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(1000);
  Result := '';
end;

function PlaceJarvisExe: Boolean;
var
  Src, Dst: String;
  I: Integer;
begin
  ExtractTemporaryFile('{#AppExe}');
  Src := ExpandConstant('{tmp}\{#AppExe}');
  Dst := ExpandConstant('{app}\{#AppExe}');
  Result := False;
  for I := 1 to 90 do
  begin
    if CopyFile(Src, Dst, False) then
    begin
      Log(Format('Jarvis.exe placed on attempt %d.', [I]));
      Result := True;
      Exit;
    end;
    Log(Format('Jarvis.exe copy attempt %d failed (antivirus probably still scanning it), retrying.', [I]));
    Sleep(1000);
  end;
end;

procedure PlaceExeNow;
begin
  JarvisExePlaced := PlaceJarvisExe;
  if not JarvisExePlaced then
    SuppressibleMsgBox('Your antivirus (for example Avast, 360 Total Security or Windows Security) blocked Jarvis.exe ' +
      'while it was being installed.' + #13#10#13#10 +
      'Open your antivirus, restore Jarvis.exe if it is in quarantine, add this folder as an exception, ' +
      'then run this installer again:' + #13#10#13#10 + ExpandConstant('{app}'),
      mbError, MB_OK, IDOK);
end;

procedure EnableAutostart;
var
  Exe: String;
begin
  Exe := ExpandConstant('{app}\{#AppExe}');
  if RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Jarvis', '"' + Exe + '" --background') then
    Exit;
  Log('Run key blocked (antivirus?), trying the Startup folder instead.');
  try
    CreateShellLink(ExpandConstant('{userstartup}\Jarvis.lnk'), 'J.A.R.V.I.S.', Exe, '--background',
                    ExpandConstant('{app}'), '', 0, SW_SHOWNORMAL);
  except
    Log('Startup shortcut blocked too, skipping start-with-Windows: ' + GetExceptionMessage);
  end;
end;

function OllamaInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe'))
    or FileExists(ExpandConstant('{commonpf}\Ollama\ollama.exe'));
end;

function OllamaExe: String;
begin
  Result := ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe');
  if not FileExists(Result) then
    Result := ExpandConstant('{commonpf}\Ollama\ollama.exe');
end;

function HasParam(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then
      Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  RemoveModels: Boolean;
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Jarvis');
  if (CurUninstallStep = usPostUninstall) and FileExists(OllamaExe) then
  begin
    RemoveModels := HasParam('/REMOVEMODELS');
    if (not RemoveModels) and (not UninstallSilent) then
      RemoveModels := MsgBox('Also delete Jarvis''s AI models from Ollama (frees about 3-10 GB)?' + #13#10#13#10 +
                             'Choose No if you plan to install Jarvis again: the models take a while to download.',
                             mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
    if RemoveModels then
    begin
      Exec(OllamaExe, 'rm qwen3-vl:4b-instruct', '', SW_HIDE, ewWaitUntilTerminated, Code);
      Exec(OllamaExe, 'rm qwen3-vl:8b-instruct', '', SW_HIDE, ewWaitUntilTerminated, Code);
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('startup') then
      EnableAutostart;
  end;
  if (CurStep = ssPostInstall) and (not WizardSilent) and (not OllamaInstalled) then
    if MsgBox('Jarvis needs Ollama (free) to run its AI brain, and it is not installed yet.' + #13#10#13#10 +
              'Open the Ollama download page now?', mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://ollama.com/download', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;
