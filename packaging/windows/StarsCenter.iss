; Установщик Windows-версии Центра (Inno Setup 6). Обычно вызывается из build.py --installer:
;   ISCC /DAppVersion=1.0.0 /DSourceDir=dist\windows\StarsCenter /DOutputDir=dist\windows StarsCenter.iss
; Ставится в профиль пользователя без прав администратора; данные (center-data) живут отдельно
; в %LOCALAPPDATA%\StarsCenter и не удаляются при обновлении или удалении программы.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\windows\StarsCenter"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist\windows"
#endif

[Setup]
AppId={{6B0E5B8C-3C1F-4C9E-9C55-2F1D7A4E8C01}
AppName=Центр (StarsBot · MannBot)
AppVersion={#AppVersion}
AppPublisher=FrayZ
DefaultDirName={localappdata}\Programs\StarsCenter
DefaultGroupName=Центр
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=StarsCenter-{#AppVersion}-setup
SetupIconFile={#SourceDir}\app\center.ico
UninstallDisplayIcon={app}\app\center.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Ярлыки:"
Name: "autostart"; Description: "Запускать Центр при входе в Windows (без открытия браузера)"; GroupDescription: "Автозапуск:"; Flags: unchecked

[InstallDelete]
; старая версия кода удаляется целиком, чтобы не осталось файлов, которых больше нет в сборке
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\node"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{localappdata}\StarsCenter"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\Центр"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\launch.pyw"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\center.ico"; Comment: "Панель управления ботами"
Name: "{group}\Центр (с окном журнала)"; Filename: "{app}\Центр.bat"; WorkingDir: "{app}"; IconFilename: "{app}\app\center.ico"
Name: "{group}\Центр — демо без аккаунтов"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\launch.pyw"" --demo"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\center.ico"
Name: "{group}\Папка данных"; Filename: "{localappdata}\StarsCenter"
Name: "{group}\Прочти меня"; Filename: "{app}\ПРОЧТИ МЕНЯ.txt"
Name: "{group}\Удалить Центр"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Центр"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\launch.pyw"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\center.ico"; Tasks: desktopicon
Name: "{userstartup}\Центр"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\launch.pyw"" --no-browser"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\center.ico"; Tasks: autostart

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\launch.pyw"""; WorkingDir: "{app}\app"; Description: "Запустить Центр"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; аккуратно останавливаем запущенный центр (и его ботов) перед удалением файлов
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -like '{app}\runtime\*' } | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }"""; Flags: runhidden; RunOnceId: "StopCenter"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\node"

[Code]
// Перед обновлением останавливаем запущенную копию: иначе файлы python.exe/pyd заняты.
procedure StopRunningCenter();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like ''' +
    ExpandConstant('{app}') + '\runtime\*'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"',
    '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  if DirExists(ExpandConstant('{app}\runtime')) then
    StopRunningCenter();
  Result := '';
end;
