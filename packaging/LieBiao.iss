#define AppVersion "1.0.3"
[Setup]
AppId={{B7DF54C2-A232-4BF0-82E4-42BDCAD2973F}
AppName=猎标招标公告采集系统
AppVersion={#AppVersion}
AppPublisher=猎标
DefaultDirName={localappdata}\Programs\LieBiao
DisableDirPage=no
DefaultGroupName=猎标招标公告采集系统
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\release
OutputBaseFilename=LieBiao-Setup-{#AppVersion}-win-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\LieBiao.exe
CloseApplications=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Files]
Source: "..\dist\LieBiao\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userdesktop}\猎标招标公告采集系统"; Filename: "{app}\LieBiao.exe"
Name: "{group}\猎标招标公告采集系统"; Filename: "{app}\LieBiao.exe"
Name: "{group}\卸载猎标"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\LieBiao.exe"; Description: "启动猎标"; Flags: nowait postinstall skipifsilent

[Code]
var
  DataPage: TInputDirWizardPage;

function StorageConfigPath: String;
begin
  Result := ExpandConstant('{param:CONFIG_DIR|{localappdata}\LieBiaoDesktop}\storage.json');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = DataPage.ID) and FileExists(StorageConfigPath);
end;

procedure InitializeWizard;
begin
  DataPage := CreateInputDirPage(wpSelectDir, '选择数据存放位置',
    '数据库和公告附件保存到哪里？',
    '请选择独立的数据文件夹。卸载程序不会删除此目录。选择已有猎标数据目录可继续使用原数据；选择新目录不会自动迁移旧数据。', False, '');
  DataPage.Add('数据目录：');
  DataPage.Values[0] := ExpandConstant('{param:DATA_DIR|{localappdata}\LieBiaoDesktop\data}');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = DataPage.ID then begin
    if (Length(Trim(DataPage.Values[0])) < 4) or
       (CompareText(Copy(AddBackslash(ExpandFileName(DataPage.Values[0])), 1, Length(AddBackslash(WizardDirValue))), AddBackslash(WizardDirValue)) = 0) then begin
      MsgBox('请选择程序安装目录以外的专用数据文件夹。', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExitCode: Integer;
begin
  if (CurStep = ssPostInstall) and not FileExists(StorageConfigPath) then begin
    if not Exec(ExpandConstant('{app}\LieBiao.exe'),
      '--configure-data "' + DataPage.Values[0] + '" --config-dir "' + ExpandConstant('{param:CONFIG_DIR|{localappdata}\LieBiaoDesktop}') + '"', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
      RaiseException('数据目录配置失败。请确认目录可写且数据库有效。下次启动可重新选择目录。');
  end;
end;
