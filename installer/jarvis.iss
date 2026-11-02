; Jarvis installer (Inno Setup 6.7+). Built by build.ps1 -> dist\Jarvis-Setup.exe
#define AppName "J.A.R.V.I.S."
#define AppVersion "0.1.0"
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

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=Welcome to J.A.R.V.I.S.
WelcomeLabel2=This will install Jarvis {#AppVersion}, your personal AI assistant for Windows.%n%nJarvis runs its AI on your own PC using Ollama. If Ollama isn't installed yet, Setup will offer to open its download page at the end.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start Jarvis when &Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\app\dist\Jarvis\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Jarvis"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall Jarvis"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Jarvis"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Jarvis"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch Jarvis now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function OllamaInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe'))
    or FileExists(ExpandConstant('{commonpf}\Ollama\ollama.exe'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if (CurStep = ssPostInstall) and (not WizardSilent) and (not OllamaInstalled) then
    if MsgBox('Jarvis needs Ollama (free) to run its AI brain, and it is not installed yet.' + #13#10#13#10 +
              'Open the Ollama download page now?', mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://ollama.com/download', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;
