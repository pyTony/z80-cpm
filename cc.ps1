<#
.SYNOPSIS
  Compile a C file with uc80 + um80 + ul80 and run the result in the CP/M emulator.
.DESCRIPTION
  Builds BASENAME.c into BASENAME.com.  The intermediate .mac and .rel files are deleted
  afterwards (use -Keep to keep them).  With -Disk the .com goes to a CP/M disk folder
  instead of the current folder, and the emulator is started on that disk.
.EXAMPLE
  .\cc.ps1 hello                  # hello.c -> hello.com, run it
  .\cc.ps1 hello -Disk B          # hello.com -> disks\B (user 0 if the disk has user areas)
  .\cc.ps1 hello -Disk B3         # hello.com -> disks\B\3 (drive B, user 3)
  .\cc.ps1 hello.c -Cpu z80 -NoRun -Keep
#>
param(
    [Parameter(Mandatory = $true, Position = 0)] [string]$FileName,
    [ValidateSet("z80", "z180")] [string]$Cpu = "z180",
    [string]$Disk = "",
    [string]$DisksDir = ".\disks",
    [switch]$NoRun,
    [switch]$Keep
)

# Native tools print progress on stderr; do not turn that into script errors (Windows PowerShell 5.1)
$ErrorActionPreference = "Continue"
function Fail($message) { Write-Host "ERROR: $message" -ForegroundColor Red; exit 1 }

# Accept hello, hello.c or hello.com
$BaseName = $FileName -replace '\.(c|com)$', ''
$SourceFile = "$BaseName.c"
$AsmFile = "$BaseName.mac"
$RelFile = "$BaseName.rel"
$OutputFile = "$BaseName.com"

Write-Host "=== Step 1: Checking dependencies ===" -ForegroundColor Cyan
if (-not (Get-Command "python" -ErrorAction SilentlyContinue)) {
    Fail "Python was not found on PATH. Install Python 3 and tick 'Add Python to PATH'."
}
foreach ($package in @("uc80", "um80")) {
    $null = python -m pip show $package 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Package '$package' is missing, installing with pip..." -ForegroundColor Yellow
        python -m pip install $package
        if ($LASTEXITCODE -ne 0) { Fail "pip could not install $package" }
    } else {
        Write-Host "Found: $package" -ForegroundColor Green
    }
}

Write-Host "`n=== Step 2: Source file ===" -ForegroundColor Cyan
if (-not (Test-Path $SourceFile)) {
    Write-Host "$SourceFile not found, creating a small example." -ForegroundColor Yellow
    $example = @'
#include <stdio.h>

int main(void) {
    printf("Hello from uc80\n");
    return 0;
}
'@
    Set-Content -Path $SourceFile -Value $example -Encoding ascii
} else {
    Write-Host "Using $SourceFile" -ForegroundColor Green
}

Write-Host "`n=== Step 3: Compiling ===" -ForegroundColor Cyan
foreach ($f in @($AsmFile, $RelFile, $OutputFile)) {
    if (Test-Path $f) { Remove-Item $f }
}
# python -m avoids needing pip's Scripts folder on PATH
$lib = (python -m uc80.main --print-lib-dir).Trim()

python -m uc80.main $SourceFile -o $AsmFile
if ($LASTEXITCODE -ne 0) { Fail "uc80 (C compiler) failed" }

python -m um80.um80 $AsmFile -o $RelFile
if ($LASTEXITCODE -ne 0) { Fail "um80 (assembler) failed" }

python -m um80.ul80 $RelFile "$lib\libc.lib" "$lib\runtime.lib" -o $OutputFile
if ($LASTEXITCODE -ne 0) { Fail "ul80 (linker) failed" }

if (-not (Test-Path $OutputFile)) {
    Fail "No $OutputFile was produced."
}
if (-not $Keep) {
    Remove-Item $AsmFile, $RelFile -ErrorAction SilentlyContinue
}
Write-Host "`nBuilt $OutputFile" -ForegroundColor Green

# Optional: put the program on a CP/M disk (drive letter, optionally followed by a user number)
if ($Disk -ne "") {
    if ($Disk -notmatch '^([A-Pa-p])(\d{0,2})$') { Fail "-Disk should look like B or B3 (drive, optional user 0-15)" }
    $drive = $Matches[1].ToUpper()
    $user = if ($Matches[2] -ne "") { [int]$Matches[2] } else { 0 }
    if ($user -gt 15) { Fail "CP/M user numbers go from 0 to 15" }
    $name = Split-Path $BaseName -Leaf
    if ($name.Length -gt 8) { Fail "CP/M file names have at most 8 characters before the dot: '$name'" }
    $target = Join-Path $DisksDir $drive
    if (Test-Path (Join-Path $target "0")) { $target = Join-Path $target "$user" }   # drive has user areas
    elseif ($user -ne 0) { Fail "Drive $drive has no user areas (no folder named 0)" }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $copy = Join-Path $target ("$name.com")
    Move-Item -Force $OutputFile $copy
    $OutputFile = $copy
    Write-Host "Moved to $copy (drive ${drive}:, user $user)" -ForegroundColor Green
}

if (-not $NoRun) {
    Write-Host "Running in the emulator (--cpu $Cpu), leave with Ctrl+]" -ForegroundColor Yellow
    if ($Disk -ne "") {
        python .\cpm.py --cpu $Cpu --disk $DisksDir $OutputFile
    } else {
        python .\cpm.py --cpu $Cpu $OutputFile
    }
}