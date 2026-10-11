# z80-cpm

A Z80 CPU simulator and a CP/M 2.2 emulator, both in pure Python. No dependencies, except the optional `lupa` for Lua.

* `z80sim.py` - the CPU: all documented and the common undocumented opcodes, X/Y flags,
  T-state counting, IM 0/1/2, NMI, HALT. `Z80.run_fast()` runs common opcodes through
  pre-generated handlers (about 3x faster than `step()`); it is checked against plain
  `step()` by randomised tests.
* `cpm.py` - CP/M 2.2 by trapping BDOS/BIOS calls: drives mapped to host folders,
  optional user areas (`A/0`, `A/1` ...), a small command prompt, ANSI/VT output,
  raw keyboard input on Windows and POSIX.
* `fix_turbo_ansi.py` - repairs the damaged ANSI entry in Turbo Pascal 3's `TURBO.COM`/`TINST.DTA`.
* `test_z80sim.py` - unit tests (`python -m unittest test_z80sim`).

## Usage

    python cpm.py                          # CP/M prompt, drive A: = current folder
    python cpm.py PROGRAM.COM args         # run a program (starts on that program's disk)
    python cpm.py --disk DISKS PROGRAM.COM # DISKS/A, DISKS/B ... one folder per drive
    python cpm.py --drive B=some/folder
    python cpm.py --charset fi             # Finnish/Swedish letters in [ \ ] { | }
    python cpm.py --backspace del          # Backspace sends 7Fh instead of ^H
    python cpm.py --time PROGRAM.COM       # report run time, instructions, T-states
    python cpm.py --startup FILE           # run these commands at start (else A:0 STARTUP.CPM)
    python cpm.py --color auto|on|off      # ANSI colour in the Python-made prompt/DIR output

At the prompt type `HELP`. `Ctrl+]` stops the emulator immediately.

## The command prompt

There is no real CCP: the prompt is Python. Besides `DIR ERA REN TYPE USER` and drive
letters (`B:`), it has some ZCPR-style extras (see `HELP name`):

* `PATH [list|NONE]` - where commands are searched after the current drive/user.
  Elements are drive+user (`A0:`, `B3:`, `$0:` = user 0 of the current drive). Default
  `$0: A0:`. A typed drive (`B:PROG`) searches only that drive.
* `ALIAS name=text`, `UNALIAS name` - short names for command lines.
* `cmd1 ; cmd2` - several commands on one line; lines starting with `#` are comments.
* Startup file - `--startup FILE`, or `STARTUP.CPM` on A:0, run when the prompt starts.
* `TIME [ON|OFF]` - after each program, real time, instructions, T-states and the time
  a 4 MHz Z80 would need (time spent waiting for keys is left out).
* A program that selects other drives (such as `STAT`) does not change the prompt drive.

## RunCPM extensions

Programs from the RunCPM system disk work: `INFO`, `CONSOLE7`/`CONSOLE8`, `FORMAT`,
`EXIT` and `LUA`. BDOS calls 230 (console 7/8 bit), 249 (make disk folder), 250-253
(host OS, version, CCP type and address; the version values are placeholders) and 254
(run a Lua script) are implemented.

Lua needs the optional `lupa` package:  `pip install lupa`  (`python -m pip install lupa`
on Windows). Scripts get standard Lua plus `print`, `BdosCall(fn, arg)`, `RamRead(addr)`
and `RamWrite(addr, value)`. Note that `LUA.COM` itself always makes the file type `LUA`.

CP/M software is not included; bring your own disks.

Speed: roughly 5-7x a real 4 MHz Z80 on MBASIC (CPython 3.13/3.14, desktop CPU).
