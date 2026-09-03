param(
    [ValidateSet("Acquire", "Release", "Run")]
    [string]$Mode = "Run",
    [string]$Token = "",
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"
$stateDirectory = Join-Path ([IO.Path]::GetTempPath()) "tse-crm-bot-focus-shield"
$daemonFile = Join-Path $stateDirectory "daemon.json"
$readyFile = Join-Path $stateDirectory "daemon.ready"
$lockFile = Join-Path $stateDirectory "state.lock"
$leasePrefix = "client-"

function Test-WindowsPlatform {
    return $env:OS -eq "Windows_NT"
}

function Get-ProcessInfo([int]$ProcessId) {
    try {
        return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Get-OwnerProcessId {
    # O PowerShell chamado por FOR /F costuma ter um cmd.exe intermediario.
    # Guardamos o cmd.exe mais externo ainda vivo para o daemon poder remover
    # leases abandonados quando uma janela for fechada a forca.
    $current = Get-ProcessInfo $PID
    $candidate = 0
    for ($depth = 0; $depth -lt 8 -and $null -ne $current; $depth++) {
        $parentId = [int]$current.ParentProcessId
        if ($parentId -le 0) { break }
        $parent = Get-ProcessInfo $parentId
        if ($null -eq $parent) { break }
        if ([IO.Path]::GetFileName($parent.Name) -ieq "cmd.exe") {
            $candidate = $parentId
        }
        $current = $parent
    }
    return $candidate
}

function Invoke-WithStateLock([scriptblock]$Action) {
    New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
    $stream = $null
    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        try {
            $stream = [IO.File]::Open(
                $lockFile,
                [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None
            )
            break
        }
        catch [IO.IOException] {
            Start-Sleep -Milliseconds 25
        }
    }
    if ($null -eq $stream) {
        throw "Nao foi possivel adquirir o lock do focus shield."
    }
    try {
        & $Action
    }
    finally {
        $stream.Dispose()
    }
}

function Test-ProcessAlive([int]$ProcessId) {
    if ($ProcessId -le 0) { return $false }
    try {
        Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Get-DaemonPid {
    if (-not (Test-Path -LiteralPath $daemonFile)) { return 0 }
    try {
        $state = Get-Content -LiteralPath $daemonFile -Raw | ConvertFrom-Json
        return [int]$state.pid
    }
    catch {
        return 0
    }
}

function Start-ShieldDaemon {
    $scriptPath = $PSCommandPath
    $escapedPath = $scriptPath.Replace("'", "''")
    $command = "& '$escapedPath' -Mode Run"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    Remove-Item -LiteralPath $readyFile -Force -ErrorAction SilentlyContinue
    $process = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-EncodedCommand", $encoded
    ) -WindowStyle Hidden -PassThru
    @{ pid = $process.Id; started_at = (Get-Date).ToString("o") } |
        ConvertTo-Json -Compress |
        Set-Content -LiteralPath $daemonFile -Encoding UTF8

    # Evita a corrida em que o navegador abre antes de o daemon memorizar a
    # janela atual do usuario (normalmente o proprio terminal iniciador).
    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        try {
            if ((Get-Content -LiteralPath $readyFile -Raw -ErrorAction Stop).Trim() -eq [string]$process.Id) {
                break
            }
        }
        catch {}
        Start-Sleep -Milliseconds 20
    }
}

function Acquire-Shield {
    if (-not (Test-WindowsPlatform)) { return }
    if ($env:BOT_FOCUS_SHIELD -eq "0") { return }

    $newToken = [Guid]::NewGuid().ToString("N")
    $root = ""
    if ($ProjectRoot) {
        try { $root = [IO.Path]::GetFullPath($ProjectRoot) } catch { $root = $ProjectRoot }
    }
    $leaseFile = Join-Path $stateDirectory "$leasePrefix$newToken.json"

    Invoke-WithStateLock {
        @{
            token = $newToken
            owner_pid = (Get-OwnerProcessId)
            project_root = $root
            created_at = (Get-Date).ToString("o")
        } | ConvertTo-Json -Compress | Set-Content -LiteralPath $leaseFile -Encoding UTF8

        $daemonPid = Get-DaemonPid
        if (-not (Test-ProcessAlive $daemonPid)) {
            Remove-Item -LiteralPath $daemonFile -Force -ErrorAction SilentlyContinue
            Start-ShieldDaemon
        }
    }

    # A saida e consumida pelo .cmd; nao escrever mensagens adicionais aqui.
    Write-Output $newToken
}

function Release-Shield {
    if (-not $Token) { return }
    Invoke-WithStateLock {
        $leaseFile = Join-Path $stateDirectory "$leasePrefix$Token.json"
        Remove-Item -LiteralPath $leaseFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-ActiveLeases {
    $leases = @()
    foreach ($file in Get-ChildItem -LiteralPath $stateDirectory -Filter "$leasePrefix*.json" -File -ErrorAction SilentlyContinue) {
        try {
            $lease = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            $ownerPid = [int]$lease.owner_pid
            if ($ownerPid -gt 0 -and -not (Test-ProcessAlive $ownerPid)) {
                Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
                continue
            }
            $leases += $lease
        }
        catch {
            Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
        }
    }
    return $leases
}

function Start-ShieldLoop {
    if (-not (Test-WindowsPlatform)) { return }

    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class FocusShieldNative {
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool IsWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool IsWindowVisible(IntPtr hWnd);
}
"@

    $lastUserWindow = [IntPtr]::Zero
    $lastForeground = [IntPtr]::Zero
    $leases = @()
    $roots = @()
    $iteration = 0
    $classificationCache = @{}
    $daemonReady = $false

    function Refresh-Leases {
        $activeLeases = @(Get-ActiveLeases)
        $activeRoots = @(
            $activeLeases |
                ForEach-Object { [string]$_.project_root } |
                Where-Object { $_ } |
                ForEach-Object { $_.TrimEnd('\').ToLowerInvariant() } |
                Select-Object -Unique
        )
        Set-Variable -Name leases -Scope 1 -Value $activeLeases
        Set-Variable -Name roots -Scope 1 -Value $activeRoots
    }

    function Test-BotBrowserProcess([int]$ProcessId) {
        $now = [DateTime]::UtcNow
        if ($classificationCache.ContainsKey($ProcessId)) {
            $cached = $classificationCache[$ProcessId]
            if (($now - $cached.checked_at).TotalSeconds -lt 3) {
                return [bool]$cached.protected
            }
        }

        $currentId = $ProcessId
        $foregroundIsBrave = $false
        $queryFailed = $false
        $isProtected = $false
        for ($depth = 0; $depth -lt 10 -and $currentId -gt 0; $depth++) {
            $info = Get-ProcessInfo $currentId
            if ($null -eq $info) {
                $queryFailed = $true
                break
            }
            $name = [IO.Path]::GetFileName([string]$info.Name).ToLowerInvariant()
            if ($depth -eq 0 -and $name -eq "brave.exe") {
                $foregroundIsBrave = $true
            }
            $isBrowser = $name -in @("brave.exe", "chrome.exe", "msedge.exe")
            if ($isBrowser) {
                $commandLine = ([string]$info.CommandLine).ToLowerInvariant()
                if (-not $commandLine) {
                    $queryFailed = $true
                }
                $hasProjectProfile = $false
                foreach ($root in $roots) {
                    if ($commandLine.Contains($root)) {
                        $hasProjectProfile = $true
                        break
                    }
                }
                $hasBotProfileMarker =
                    $commandLine.Contains("perfil-tse-brave") -or
                    $commandLine.Contains("perfil-crm") -or
                    $commandLine.Contains(".tse-brave-profile") -or
                    $commandLine.Contains(".tse-chrome-profile") -or
                    $commandLine.Contains(".browser-profile")
                if ($hasProjectProfile -or $hasBotProfileMarker) {
                    $isProtected = $true
                    break
                }
            }
            $currentId = [int]$info.ParentProcessId
        }

        # Fallback conservador apenas quando o Windows nao deixa inspecionar o
        # Brave em foreground. Brave pessoal com command line legivel nao e
        # bloqueado; o bot continua protegido se a consulta WMI/CIM falhar.
        if (-not $isProtected -and $foregroundIsBrave -and $queryFailed) {
            $isProtected = $true
        }
        $classificationCache[$ProcessId] = @{
            protected = $isProtected
            checked_at = $now
        }
        return $isProtected
    }

    function Restore-UserWindow([IntPtr]$BrowserWindow, [IntPtr]$TargetWindow) {
        if ($TargetWindow -eq [IntPtr]::Zero) { return }
        if (-not [FocusShieldNative]::IsWindow($TargetWindow)) { return }
        if (-not [FocusShieldNative]::IsWindowVisible($TargetWindow)) { return }

        [uint32]$browserPid = 0
        [uint32]$targetPid = 0
        $browserThread = [FocusShieldNative]::GetWindowThreadProcessId($BrowserWindow, [ref]$browserPid)
        $targetThread = [FocusShieldNative]::GetWindowThreadProcessId($TargetWindow, [ref]$targetPid)
        $currentThread = [FocusShieldNative]::GetCurrentThreadId()
        $attachedBrowser = $false
        $attachedTarget = $false
        try {
            if ($browserThread -gt 0 -and $browserThread -ne $currentThread) {
                $attachedBrowser = [FocusShieldNative]::AttachThreadInput($currentThread, $browserThread, $true)
            }
            if ($targetThread -gt 0 -and $targetThread -ne $currentThread) {
                $attachedTarget = [FocusShieldNative]::AttachThreadInput($currentThread, $targetThread, $true)
            }
            [FocusShieldNative]::BringWindowToTop($TargetWindow) | Out-Null
            [FocusShieldNative]::SetForegroundWindow($TargetWindow) | Out-Null
        }
        finally {
            if ($attachedTarget) {
                [FocusShieldNative]::AttachThreadInput($currentThread, $targetThread, $false) | Out-Null
            }
            if ($attachedBrowser) {
                [FocusShieldNative]::AttachThreadInput($currentThread, $browserThread, $false) | Out-Null
            }
        }
    }

    try {
        Refresh-Leases
        $initialForeground = [FocusShieldNative]::GetForegroundWindow()
        if ($initialForeground -ne [IntPtr]::Zero) {
            [uint32]$initialPid = 0
            [FocusShieldNative]::GetWindowThreadProcessId($initialForeground, [ref]$initialPid) | Out-Null
            if (-not (Test-BotBrowserProcess ([int]$initialPid))) {
                $lastUserWindow = $initialForeground
                $lastForeground = $initialForeground
            }
        }
        Set-Content -LiteralPath $readyFile -Value $PID -Encoding ASCII
        $daemonReady = $true

        while ($leases.Count -gt 0) {
            $foreground = [FocusShieldNative]::GetForegroundWindow()
            if ($foreground -ne [IntPtr]::Zero -and $foreground -ne $lastForeground) {
                [uint32]$foregroundPid = 0
                [FocusShieldNative]::GetWindowThreadProcessId($foreground, [ref]$foregroundPid) | Out-Null
                if (Test-BotBrowserProcess ([int]$foregroundPid)) {
                    Restore-UserWindow $foreground $lastUserWindow
                }
                else {
                    $lastUserWindow = $foreground
                }
                $lastForeground = $foreground
            }

            $iteration++
            if (($iteration % 100) -eq 0) {
                Refresh-Leases
                if ($classificationCache.Count -gt 128) {
                    $classificationCache.Clear()
                }
            }
            Start-Sleep -Milliseconds 15
        }
    }
    finally {
        Invoke-WithStateLock {
            $daemonPid = Get-DaemonPid
            if ($daemonPid -eq $PID) {
                Remove-Item -LiteralPath $daemonFile -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $readyFile -Force -ErrorAction SilentlyContinue
                # Fecha a janela de corrida entre a ultima leitura dos leases e
                # uma nova execucao que tenha acabado de se registrar.
                if ($daemonReady -and @(Get-ActiveLeases).Count -gt 0) {
                    Start-ShieldDaemon
                }
            }
        }
    }
}

switch ($Mode) {
    "Acquire" { Acquire-Shield }
    "Release" { Release-Shield }
    "Run" { Start-ShieldLoop }
}
