; Inno Setup 6 script for FEM Tracker. Built by build.bat, or by hand:
;   ISCC.exe /DAppVersion=1.0.0 /DDistDir=C:\path\build\dist\FEMTracker packaging\windows\installer.iss
; Installs for the current user by default (no administrator rights needed);
; the first wizard page offers "Install for all users" to administrators.

#define AppName "FEM Tracker"
#define AppExe "FEMTracker.exe"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef DistDir
  #define DistDir "..\..\build\dist\FEMTracker"
#endif
#ifndef OutDir
  #define OutDir "..\..\dist"
#endif

[Setup]
AppId={{D7E135F2-1B1D-4093-AEBF-C34726459BB7}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=FEM Tracker
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=FEMTracker-{#AppVersion}-setup
SetupIconFile=femtracker.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start FEM Tracker when I sign in (minimised, no browser)"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; files from older versions that are no longer shipped
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "start"; Comment: "Start FEM Tracker and open it in the browser"
Name: "{group}\{#AppName} settings"; Filename: "{app}\{#AppExe}"; Parameters: "config"; Comment: "HPC connection and folders (config.ini)"
Name: "{group}\Test HPC connection"; Filename: "{app}\{#AppExe}"; Parameters: "check"
Name: "{group}\{#AppName} user guide"; Filename: "{app}\_internal\femtracker\static\guide.html"
Name: "{group}\{#AppName} data folder"; Filename: "{app}\{#AppExe}"; Parameters: "folder"; Comment: "Database, logs and results"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "start"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "start --no-browser"; Flags: runminimized; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "config"; Description: "Enter the HPC connection settings now"; Flags: postinstall nowait skipifsilent runhidden
Filename: "{app}\{#AppExe}"; Parameters: "start"; Description: "Start {#AppName}"; Flags: postinstall nowait skipifsilent unchecked

[UninstallRun]
; stop a running copy so its files can be removed; the database and settings are kept
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#AppExe} /F"; Flags: runhidden; RunOnceId: "StopFEMTracker"

[Messages]
FinishedLabel=Setup has installed [name] on your computer.%n%nNext: enter your HPC user, host, PuTTY key and remote folder in the settings file, then run "Test HPC connection" from the Start menu.
