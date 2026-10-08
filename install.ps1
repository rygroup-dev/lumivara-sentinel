# Lumivara Sentinel - Windows one-line installer
#
#   irm https://raw.githubusercontent.com/rygroup-dev/lumivara-sentinel/main/install.ps1 | iex
#
# Installs into %USERPROFILE%\lumivara-sentinel (override with $env:LUMIVARA_DIR),
# makes sure Python 3.11+ exists, installs dependencies, asks for your Telegram
# bot + game login, and puts a "Lumivara Sentinel" shortcut on the Desktop.
# Re-running it updates the code and keeps your .env / data.

& {
    $ErrorActionPreference = 'Stop'
    $Repo = 'rygroup-dev/lumivara-sentinel'
    $Dir  = if ($env:LUMIVARA_DIR) { $env:LUMIVARA_DIR } else { Join-Path $env:USERPROFILE 'lumivara-sentinel' }

    function Say($m)  { Write-Host "[lumivara] $m" -ForegroundColor Cyan }
    function Warn($m) { Write-Host "[lumivara] $m" -ForegroundColor Yellow }

    function Find-Python {
        foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
            $exe = $cand[0]; $pyArgs = @($cand | Select-Object -Skip 1)
            if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
            try {
                # (the Microsoft Store "python" stub prints nothing here, so it is skipped)
                $v = & $exe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
                if ($v -match '^3\.(\d+)$' -and [int]$Matches[1] -ge 11) { return ,([string[]]$cand) }
            } catch { }
        }
        return $null
    }

    Say "Install folder: $Dir"

    # --- Python ---------------------------------------------------------
    $py = Find-Python
    if (-not $py) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Say 'Python 3.11+ not found - installing Python 3.12 with winget...'
            winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
            $py = Find-Python
        }
        if (-not $py) {
            Warn 'Could not find or install Python 3.11+.'
            Warn 'Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this again.'
            return
        }
    }
    Say ("Python: " + (& $py[0] @($py | Select-Object -Skip 1) --version))

    # --- code -----------------------------------------------------------
    if (Get-Command git -ErrorAction SilentlyContinue) {
        if (Test-Path (Join-Path $Dir '.git')) {
            Say 'Updating code (git pull)...'
            git -C $Dir pull --ff-only | Out-Host
        } else {
            Say 'Downloading code (git clone)...'
            git clone --depth 1 "https://github.com/$Repo.git" $Dir | Out-Host
        }
    } else {
        Say 'Downloading code (zip)...'
        $zip = Join-Path $env:TEMP 'lumivara-sentinel.zip'
        $tmp = Join-Path $env:TEMP 'lumivara-sentinel-src'
        Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
        Expand-Archive $zip -DestinationPath $tmp -Force
        $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        Copy-Item (Join-Path $src.FullName '*') $Dir -Recurse -Force   # .env, data and logs are not in the zip, so they survive
        Remove-Item $zip, $tmp -Recurse -Force
    }

    # --- virtualenv + deps ------------------------------------------------
    Set-Location $Dir
    $venvPy = Join-Path $Dir '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
        Say 'Creating virtual environment...'
        & $py[0] @($py | Select-Object -Skip 1) -m venv .venv
    }
    Say 'Installing dependencies...'
    & $venvPy -m pip install --disable-pip-version-check --no-warn-script-location -q --upgrade pip | Out-Null
    & $venvPy -m pip install --disable-pip-version-check --no-warn-script-location -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Warn 'Dependency install failed - check your internet and run the installer again.'; return }

    # --- setup wizard -----------------------------------------------------
    & $venvPy -m lumivara.setup_wizard --check
    if ($LASTEXITCODE -ne 0) {
        & $venvPy -m lumivara.setup_wizard
        if ($LASTEXITCODE -ne 0) { Warn 'Setup was not finished. Run run.bat later to continue.'; return }
    } else {
        Say 'Existing .env found - keeping your settings. (Refresh the cookie with: .venv\Scripts\python -m lumivara.setup_wizard --cookie)'
    }

    # --- Desktop shortcut ---------------------------------------------------
    try {
        $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Lumivara Sentinel.lnk'
        $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
        $sh.TargetPath = Join-Path $Dir 'run.bat'
        $sh.WorkingDirectory = $Dir
        $sh.Save()
        Say "Shortcut created on your Desktop: Lumivara Sentinel"
    } catch { Warn 'Could not create a Desktop shortcut (start run.bat from the folder instead).' }

    Say 'Done.'
    $ans = Read-Host 'Start the bot now? [Y/n]'
    if ($ans -notmatch '^[nN]') {
        Start-Process -FilePath (Join-Path $Dir 'run.bat') -WorkingDirectory $Dir
        Say 'Bot window opened. In Telegram, send /menu to your bot.'
    } else {
        Say "Start it later with the Desktop shortcut or $Dir\run.bat"
    }
}
