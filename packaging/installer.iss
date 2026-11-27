; Inno Setup script for the PCTX installer. Built by packaging/build_release.py,
; which passes /DAppVersion=... and /DSourceDir=... (the PyInstaller output).
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\PCTX, so the
; app can keep its caches next to itself just like the portable build.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "build\dist\PCTX"
#endif

[Setup]
AppId={{6F0C4B2E-8A31-4C7D-9E55-2D1A7B3F9C10}
AppName=PCTX
AppVersion={#AppVersion}
AppVerName=PCTX {#AppVersion}
AppPublisher=Dah Sidi Abdallah
AppPublisherURL=https://github.com/DahSidiAbdallah/PCSX2-Patch_Texture-Manager
DefaultDirName={localappdata}\Programs\PCTX
DefaultGroupName=PCTX
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=build\out
OutputBaseFilename=PCTX-{#AppVersion}-Setup
SetupIconFile=..\app_icon.ico
UninstallDisplayIcon={app}\PCTX.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\PCTX"; Filename: "{app}\PCTX.exe"
Name: "{autodesktop}\PCTX"; Filename: "{app}\PCTX.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PCTX.exe"; Description: "{cm:LaunchProgram,PCTX}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; caches PCTX writes inside its own folder
Type: filesandordirs; Name: "{app}\_internal\cheat_cache"
