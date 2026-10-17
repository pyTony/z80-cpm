<#
.SYNOPSIS
  Compile a C file with uc80 + um80 + ul80 and run the result in the CP/M emulator.
.DESCRIPTION
  Calls cc.py, which does the work (and also runs on Linux and macOS: python cc.py --help).
  The screen shows a summary and the run time; all tool messages go to BASENAME.log.
.EXAMPLE
  .\cc.ps1 hello                  # hello.c -> hello.com, run it
  .\cc.ps1 hello -Disk B3         # hello.com -> disks\B\3 (drive B, user 3)
  .\cc.ps1 hello.c -Cpu z80 -NoRun -Keep
  .\cc.ps1 whetstone -Full        # every tool message on the screen too
#>
param(
    [Parameter(Mandatory = $true, Position = 0)] [string]$FileName,
    [ValidateSet("z80", "z180")] [string]$Cpu = "z180",
    [string]$Disk = "",
    [string]$DisksDir = ".\disks",
    [switch]$NoRun,
    [switch]$Keep,
    [switch]$Full
)
$pyArgs = @((Join-Path $PSScriptRoot "cc.py"), $FileName, "--cpu", $Cpu, "--disks-dir", $DisksDir)
if ($Disk) { $pyArgs += @("--disk", $Disk) }
if ($NoRun) { $pyArgs += "--no-run" }
if ($Keep) { $pyArgs += "--keep" }
if ($Full) { $pyArgs += "--full" }
python @pyArgs
exit $LASTEXITCODE
