# z80-cpm

A Z80 CPU simulator and a CP/M 2.2 emulator, both in pure Python (no dependencies).

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

At the prompt type `HELP`. `Ctrl+]` stops the emulator immediately.

CP/M software is not included; bring your own disks.

Speed: roughly 5-7x a real 4 MHz Z80 on MBASIC (CPython 3.13/3.14, desktop CPU).
