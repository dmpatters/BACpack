; The BACpack setup wizard (Inno Setup 6). Build it with build_installer.ps1.
;
; The setup carries the same files as the release zip and runs the same
; install.ps1, elevated, as its own PowerShell: the service, its account, the
; folder permissions, the firewall rules and the upgrade rules are all
; install.ps1's. The wizard asks (port, login), runs it, shows its output and
; reports a failure.
;
; The install runs in PrepareToInstall, before Inno writes anything of its own:
; a non-empty result there stops setup with exit code 7 and no uninstall entry
; (an Abort in CurStepChanged(ssPostInstall) would not stop it).
;
; Inno's own folder holds only the uninstaller: install.ps1 -Uninstall deletes
; C:\Program Files\BACpack whole, and neither tool should delete the other's files.

#ifndef AppVersion
  #error Pass the version: ISCC /DAppVersion=x.y.z
#endif
#ifndef BundleDir
  #error Pass the bundle folder: ISCC /DBundleDir=dist\bundle
#endif

[Setup]
; Fixed for good: it is how a newer setup finds the install it upgrades.
AppId={{8728BC0C-AF8A-4BA1-9FCE-1CA761573BA5}
AppName=BACpack
AppVersion={#AppVersion}
AppPublisher=BACpack
AppPublisherURL=https://github.com/dmpatters/BACpack
AppComments=BACnet server (device simulator + gateway)
DefaultDirName={commonpf}\BACpack Setup
DefaultGroupName=BACpack
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=BACpack-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=BACpack
CloseApplications=no
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; The bundle is only carried: PrepareToInstall extracts it into the setup's temp
; folder (deleted when setup exits) and install.ps1 copies what belongs in
; Program Files.
Source: "{#BundleDir}\*"; DestDir: "{tmp}\bacpack-bundle"; Flags: dontcopy noencryption recursesubdirs createallsubdirs

[INI]
Filename: "{group}\BACpack web UI.url"; Section: "InternetShortcut"; Key: "URL"; String: "https://localhost:{code:GetPort}"

[Icons]
Name: "{group}\Uninstall BACpack"; Filename: "{uninstallexe}"

[UninstallDelete]
Type: files; Name: "{group}\BACpack web UI.url"

[Run]
Filename: "https://localhost:{code:GetPort}"; Description: "Open the BACpack web UI"; Flags: postinstall shellexec skipifsilent nowait

[Code]
// The login's password reaches install.ps1 only through this process's
// environment, set around the run: never a command line or a file.
function SetEnvironmentVariableW(Name, Value: String): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

// A line break, built with Chr: Pascal's hash-number character literals
// read as issue numbers to the public-export check.
function NL(): String;
begin
  Result := Chr(13) + Chr(10);
end;

const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8728BC0C-AF8A-4BA1-9FCE-1CA761573BA5}_is1';

var
  PortPage: TInputQueryWizardPage;
  LoginChoicePage: TInputOptionWizardPage;
  CredPage: TInputQueryWizardPage;
  OutputMemo: TNewMemo;
  OutputLines: TArrayOfString;
  PortValue: String;
  HadInstall: Boolean;
  HadData: Boolean;

function GetPort(Param: String): String;
begin
  Result := PortValue;
end;

function InstalledXml: String;
begin
  Result := ExpandConstant('{commonpf}\BACpack\bacpack-service.xml');
end;

function IsDigits(S: String): Boolean;
var
  I: Integer;
begin
  Result := S <> '';
  for I := 1 to Length(S) do
    if (S[I] < '0') or (S[I] > '9') then
      Result := False;
end;

// The port the installed service was registered with: install.ps1 writes it
// into the service XML as "--port N".
function InstalledPort: String;
var
  Text: AnsiString;
  Rest: String;
  P, I: Integer;
begin
  Result := '';
  if not LoadStringFromFile(InstalledXml, Text) then Exit;
  Rest := String(Text);
  P := Pos('--port ', Rest);
  if P = 0 then Exit;
  Rest := Copy(Rest, P + 7, 10);
  I := 1;
  while (I <= Length(Rest)) and (Rest[I] >= '0') and (Rest[I] <= '9') do
    I := I + 1;
  Result := Copy(Rest, 1, I - 1);
end;

function VersionPart(var S: String): Integer;
var
  P: Integer;
begin
  P := Pos('.', S);
  if P = 0 then
  begin
    Result := StrToIntDef(S, 0);
    S := '';
  end
  else
  begin
    Result := StrToIntDef(Copy(S, 1, P - 1), 0);
    S := Copy(S, P + 1, Length(S));
  end;
end;

function CompareVersions(A, B: String): Integer;
var
  X, Y: Integer;
begin
  Result := 0;
  while (Result = 0) and ((A <> '') or (B <> '')) do
  begin
    X := VersionPart(A);
    Y := VersionPart(B);
    if X < Y then Result := -1
    else if X > Y then Result := 1;
  end;
end;

// The version of the BACpack package an install holds: install.ps1 puts it in
// the bundle's python folder, or in the venv of a source install.
function DistInfoVersion(SitePackages: String): String;
var
  Found: TFindRec;
  Name: String;
begin
  Result := '';
  if FindFirst(SitePackages + '\bacpack-*.dist-info', Found) then
  begin
    try
      Name := Found.Name;
      Result := Copy(Name, Length('bacpack-') + 1, Length(Name) - Length('bacpack-') - Length('.dist-info'));
    finally
      FindClose(Found);
    end;
  end;
end;

// What is installed, whether setup or the zip put it there; Inno's own entry
// is the fallback.
function InstalledVersion: String;
begin
  Result := DistInfoVersion(ExpandConstant('{commonpf}\BACpack\python\Lib\site-packages'));
  if Result = '' then
    Result := DistInfoVersion(ExpandConstant('{commonpf}\BACpack\.venv\Lib\site-packages'));
  if Result = '' then
    if not RegQueryStringValue(HKLM64, UninstallKey, 'DisplayVersion', Result) then
      Result := '';
end;

function InitializeSetup: Boolean;
var
  Installed, Given: String;
  PortNumber: Integer;
begin
  Result := True;
  HadInstall := FileExists(InstalledXml);
  HadData := FileExists(ExpandConstant('{commonappdata}\BACpack\bacpack.db'));
  PortValue := InstalledPort;
  if PortValue = '' then PortValue := '8000';
  Given := ExpandConstant('{param:PORT|}');
  if Given <> '' then
  begin
    PortNumber := 0;
    if IsDigits(Given) and (Length(Given) <= 5) then PortNumber := StrToIntDef(Given, 0);
    if (PortNumber < 1) or (PortNumber > 65535) then
    begin
      SuppressibleMsgBox('/PORT must be a number from 1 to 65535, not ' + Given + '.', mbError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
    PortValue := IntToStr(PortNumber);
  end;
  // An older release refuses the data a newer one saved (exit 81).
  Installed := InstalledVersion;
  if (Installed <> '') and (CompareVersions(Installed, '{#AppVersion}') > 0) then
  begin
    SuppressibleMsgBox('BACpack ' + Installed + ' is installed, which is newer than this setup ({#AppVersion}). ' +
      'Going back to an older release is not supported: it would refuse the data the newer one saved ' +
      '(see "Going back to an older release" in the Upgrading guide). ' +
      'Run the setup of ' + Installed + ' or a newer release instead.', mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure InitializeWizard;
var
  PortHint: String;
begin
  PortHint := 'The web UI is served over HTTPS on this port. Change it only if 8000 is already in use.';
  if HadData and not HadInstall then
    PortHint := PortHint + ' Data saved by an earlier install was found; it is kept, with its web login.';
  PortPage := CreateInputQueryPage(wpWelcome, 'Web UI port', 'Which port should the web UI use?', PortHint);
  PortPage.Add('Port:', False);
  PortPage.Values[0] := PortValue;

  LoginChoicePage := CreateInputOptionPage(PortPage.ID, 'Web login', 'How should the web UI be protected?',
    'Whoever can reach the web UI can control BACpack, so it should have a login.', True, False);
  LoginChoicePage.Add('Choose on the first visit to the web UI');
  LoginChoicePage.Add('Create a login now');
  LoginChoicePage.Values[0] := True;

  CredPage := CreateInputQueryPage(LoginChoicePage.ID, 'Create a login', 'Username and password for the web UI',
    'The password needs at least 12 characters.');
  CredPage.Add('Username:', False);
  CredPage.Add('Password:', True);
  CredPage.Add('Password again:', True);

  // install.ps1's own lines show here while it runs (PrepareToInstall).
  OutputMemo := TNewMemo.Create(WizardForm);
  OutputMemo.Parent := WizardForm.PreparingPage;
  OutputMemo.Left := 0;
  OutputMemo.Top := WizardForm.PreparingLabel.Top + WizardForm.PreparingLabel.Height + ScaleY(8);
  OutputMemo.Width := WizardForm.PreparingPage.Width;
  OutputMemo.Height := WizardForm.PreparingPage.Height - OutputMemo.Top;
  OutputMemo.ReadOnly := True;
  OutputMemo.ScrollBars := ssVertical;
  OutputMemo.Visible := False;
end;

// An upgrade, and data kept from an earlier install, keep their login: the
// login pages are for a first install with no saved data.
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = LoginChoicePage.ID then
    Result := HadInstall or HadData
  else if PageID = CredPage.ID then
    Result := HadInstall or HadData or (not LoginChoicePage.Values[1]);
end;

// The server's own rules (auth.check_username_rules, check_password_rules), plus
// what would break the command line the installer builds: the login is made
// through a .cmd file, which reads & | < > ^ % ! and a quote as syntax.
// Returns the message, or an empty string.
function CheckLogin(User, Password, Again: String): String;
var
  Name: String;
  I: Integer;
begin
  Result := '';
  Name := Trim(User);
  if Name = '' then
    Result := 'Type a username.'
  else if Length(Name) > 64 then
    Result := 'The username can be at most 64 characters.'
  else
  begin
    for I := 1 to Length(Name) do
    begin
      if (Ord(Name[I]) < 32) or (Ord(Name[I]) = 127) then
        Result := 'The username can''t contain control characters.'
      else if (Pos(Name[I], '&|<>^%!\') > 0) or (Name[I] = '"') then
        Result := 'The username can''t contain any of these characters: " & | < > ^ % ! \';
    end;
  end;
  if Result <> '' then Exit;
  if Length(Password) < 12 then
    Result := 'The password needs at least 12 characters.'
  else if Length(Password) > 256 then
    Result := 'The password can be at most 256 characters.'
  else if Password = Name then
    Result := 'The password can''t be the same as the username.'
  else if Password <> Again then
    Result := 'The two passwords differ.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Number: Integer;
  Problem: String;
begin
  Result := True;
  if CurPageID = PortPage.ID then
  begin
    Number := 0;
    if IsDigits(Trim(PortPage.Values[0])) and (Length(Trim(PortPage.Values[0])) <= 5) then
      Number := StrToIntDef(Trim(PortPage.Values[0]), 0);
    if (Number < 1) or (Number > 65535) then
    begin
      MsgBox('The port must be a number from 1 to 65535.', mbError, MB_OK);
      Result := False;
    end
    else
      PortValue := IntToStr(Number);
  end
  else if CurPageID = CredPage.ID then
  begin
    Problem := CheckLogin(CredPage.Values[0], CredPage.Values[1], CredPage.Values[2]);
    if Problem <> '' then
    begin
      MsgBox(Problem, mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function PowerShellExe: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function TailLines(const Lines: TArrayOfString; Count: Integer): String;
var
  I, First: Integer;
begin
  Result := '';
  First := GetArrayLength(Lines) - Count;
  if First < 0 then First := 0;
  for I := First to GetArrayLength(Lines) - 1 do
    Result := Result + Lines[I] + NL;
end;

// Each line install.ps1 writes (both streams), kept for the failure message and
// shown in the memo as it comes.
procedure OnOutputLine(const S: String; const Error, FirstLine: Boolean);
var
  N: Integer;
begin
  N := GetArrayLength(OutputLines);
  SetArrayLength(OutputLines, N + 1);
  OutputLines[N] := S;
  Log(S);
  if OutputMemo <> nil then OutputMemo.Lines.Add(S);
end;

// Runs install.ps1 from Dir as its own PowerShell and returns its exit code, or
// -1 when PowerShell could not be started.
function RunInstallScript(Dir, Arguments: String): Integer;
var
  Code: Integer;
begin
  SetArrayLength(OutputLines, 0);
  Result := -1;
  try
    if ExecAndLogOutput(PowerShellExe,
         '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Dir + '\install.ps1" ' + Arguments,
         Dir, SW_SHOWNORMAL, ewWaitUntilTerminated, Code, @OnOutputLine) then
      Result := Code
    else
      OnOutputLine('error: PowerShell could not be started.', True, False);
  except
    OnOutputLine('error: ' + GetExceptionMessage, True, False);
  end;
end;

// The install. A non-empty result stops setup before Inno registers anything
// (exit code 7); a failed first install is taken out again, its data kept.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Dir, Arguments, LogFile, RollbackLog, Report: String;
  Code: Integer;
  CreateLogin: Boolean;
begin
  Result := '';
  Dir := ExpandConstant('{tmp}\bacpack-bundle');
  LogFile := ExpandConstant('{%TEMP}\BACpack-setup-install.log');
  RollbackLog := ExpandConstant('{%TEMP}\BACpack-setup-rollback.log');
  ExtractTemporaryFiles('*');
  WizardForm.CancelButton.Enabled := False;
  WizardForm.PreparingLabel.Caption := 'Installing BACpack...';
  if OutputMemo <> nil then OutputMemo.Visible := True;

  Arguments := '-Port ' + PortValue;
  CreateLogin := (not HadInstall) and (not HadData) and (LoginChoicePage <> nil) and LoginChoicePage.Values[1];
  if CreateLogin then
    Arguments := Arguments + ' -LoginUser "' + Trim(CredPage.Values[0]) + '"';
  try
    if CreateLogin then
      SetEnvironmentVariableW('BACPACK_LOGIN_PASSWORD', CredPage.Values[1]);
    Code := RunInstallScript(Dir, Arguments);
  finally
    SetEnvironmentVariableW('BACPACK_LOGIN_PASSWORD', '');
  end;

  if Code = 0 then
  begin
    DeleteFile(LogFile);
    WizardForm.CancelButton.Enabled := True;
    Exit;
  end;

  SaveStringsToUTF8File(LogFile, OutputLines, False);
  Report := 'BACpack could not be installed. The last lines of the install output:' + NL + NL +
    TailLines(OutputLines, 15) + NL + 'The whole output is in ' + LogFile + '.' + NL + NL;
  if HadInstall then
    Report := Report + 'The install that was there before is as the installer left it: the previous version was ' +
      'started again only if the failure came before BACpack''s files were replaced. Run setup again to retry.'
  else
  begin
    OnOutputLine('--- removing the unfinished install (your data is kept)', False, False);
    RunInstallScript(Dir, '-Uninstall');
    SaveStringsToUTF8File(RollbackLog, OutputLines, False);
    Report := Report + 'The unfinished install was removed again; any data it saved was kept. ' +
      'Run setup again to retry (the removal''s output is in ' + RollbackLog + ').';
  end;
  WizardForm.CancelButton.Enabled := True;
  Result := Report;
end;

// Uninstall: the Inno entry runs install.ps1 -Uninstall from a temp copy of the
// copy install.ps1 keeps in its folder (the script deletes that folder), after
// asking whether the data goes too. A silent uninstall keeps the data. A failure
// aborts, so the entry stays for another try.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Kept, Dir, LogFile, Arguments: String;
  Purge: Boolean;
  Code: Integer;
begin
  if CurUninstallStep <> usUninstall then Exit;
  Kept := ExpandConstant('{commonpf}\BACpack\install.ps1');
  if not FileExists(Kept) then
  begin
    SuppressibleMsgBox('BACpack''s installed files were not found, so there is nothing to remove but this entry.',
      mbInformation, MB_OK, IDOK);
    Exit;
  end;
  Purge := False;
  if not UninstallSilent then
    Purge := MsgBox('Also delete BACpack''s data (devices, trends, logs, settings)?', mbConfirmation,
      MB_YESNO or MB_DEFBUTTON2) = IDYES;
  Dir := ExpandConstant('{%TEMP}\bacpack-uninstall-') + IntToStr(Random(1000000));
  ForceDirectories(Dir);
  CopyFile(Kept, Dir + '\install.ps1', False);
  LogFile := Dir + '.log';
  Arguments := '-Uninstall';
  if Purge then Arguments := Arguments + ' -Purge';
  Code := RunInstallScript(Dir, Arguments);
  DelTree(Dir, True, True, True);
  if Code <> 0 then
  begin
    SaveStringsToUTF8File(LogFile, OutputLines, False);
    SuppressibleMsgBox('BACpack could not be removed completely. The last lines of the output:' + NL + NL +
      TailLines(OutputLines, 15) + NL + 'The whole output is in ' + LogFile + '.', mbError, MB_OK, IDOK);
    Abort;
  end;
  DeleteFile(LogFile);
end;
