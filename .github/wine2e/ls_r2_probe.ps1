# Live A1 probe (upd-txn LS r2, wine2e only): the Desktop runs as a STANDARD user, not the
# runner's elevated admin. Under a standard user, does Windows PowerShell 5.1 read a SYSTEM
# process's StartTime, does CIM, and what does the real windows.ps1 do with a marker that names
# that SYSTEM pid with a different creation time (the pid was reused after a reboot)?
$ErrorActionPreference = 'Continue'
$probe = 'C:\lsprobe'
Remove-Item $probe -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory "$probe\scripts" -Force | Out-Null
Copy-Item "$PSScriptRoot\..\..\scripts\desktop-update\*" "$probe\scripts" -Recurse -Force
icacls $probe /grant 'Everyone:(OI)(CI)F' /T /Q | Out-Null
Start-Service seclogon -ErrorAction SilentlyContinue
$svc = Get-CimInstance Win32_Process -Filter "Name='services.exe'" | Select-Object -First 1
"SYSTEM process: services.exe pid $($svc.ProcessId) created $($svc.CreationDate)"
$pw = 'Hp!' + [guid]::NewGuid().ToString('N').Substring(0, 12) + 'aA1'
$secure = ConvertTo-SecureString $pw -AsPlainText -Force
New-LocalUser -Name lsprobe -Password $secure -PasswordNeverExpires | Out-Null
Add-LocalGroupMember -Group Users -Member lsprobe -ErrorAction SilentlyContinue
$cred = New-Object System.Management.Automation.PSCredential("$env:COMPUTERNAME\lsprobe", $secure)

function Invoke-AsStandardUser([string]$Name, [string]$Body) {
    Set-Content -LiteralPath "$probe\$Name.ps1" -Value $Body -Encoding UTF8
    $p = Start-Process powershell.exe -Credential $cred -WorkingDirectory $probe -PassThru -Wait `
        -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "$probe\$Name.ps1" `
        -RedirectStandardOutput "$probe\$Name.out" -RedirectStandardError "$probe\$Name.err"
    "--- [$Name] exit $($p.ExitCode)"
    Get-Content "$probe\$Name.out", "$probe\$Name.err" -ErrorAction SilentlyContinue
}

Invoke-AsStandardUser 'identity' @"
whoami /groups | findstr /i "Mandatory.Label"
`$p = Get-Process -Id $($svc.ProcessId)
try { 'Get-Process StartTime = ' + `$p.StartTime } catch { 'Get-Process StartTime THROWN: ' + `$_.Exception.Message }
'CIM CreationDate = ' + (Get-CimInstance Win32_Process -Filter 'ProcessId=$($svc.ProcessId)').CreationDate
"@

$home2 = "$probe\home"
New-Item -ItemType Directory $home2 -Force | Out-Null
$body = "$($svc.ProcessId)`n$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())`nct:1000.000`n"
[IO.File]::WriteAllText("$home2\.hermes-update-in-progress", $body)
icacls $probe /grant 'Everyone:(OI)(CI)F' /T /Q | Out-Null
Invoke-AsStandardUser 'handoff' @"
`$env:HERMES_HOME = '$home2'
& '$probe\scripts\windows.ps1' -InstallRoot '$home2\hermes-agent' -NoUi -SelfTestMarker -NoMarkerCleanup
'HANDOFF EXIT = ' + `$LASTEXITCODE
"@
"--- marker after the hand-off:"
Get-Content "$home2\.hermes-update-in-progress" -ErrorAction SilentlyContinue
"--- hand-off log:"
Get-Content "$home2\logs\desktop-update-handoff.log" -ErrorAction SilentlyContinue
Remove-LocalUser -Name lsprobe
