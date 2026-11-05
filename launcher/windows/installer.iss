; Framework only: build a reviewed, allowlisted payload on Windows first.
#ifndef PayloadDir
  #error PayloadDir must be supplied by Build.ps1
#endif
[Setup]
AppId={{D476E59C-3886-4F70-AFC2-704146BE8AC2}
AppName=鱼太咸
AppVersion=1.0.0
DefaultDirName={localappdata}\Programs\Yutaixian
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=Yutaixian-Setup
OutputDir=..\..\build_output\windows\installer
UninstallDisplayIcon={app}\启动鱼太咸.bat
[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: ".env,yutaixian_data\*,secrets\*,keyring.json,*.log"
[Icons]
Name: "{userdesktop}\启动鱼太咸"; Filename: "{app}\启动鱼太咸.bat"
Name: "{userdesktop}\停止鱼太咸"; Filename: "{app}\停止鱼太咸.bat"
; No data or key deletion on uninstall. Stop owned processes in the completed adapter.
