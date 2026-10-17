# z80-cpm

A Z80 CPU simulator and a CP/M 2.2 emulator, both in pure Python. No dependencies, except the optional `lupa` for Lua.

* `z80sim.py` - the CPU: all documented and the common undocumented opcodes, X/Y flags,
  T-state counting, IM 0/1/2, NMI, HALT. `Z80.run_fast()` runs common opcodes through
  pre-generated handlers (about 3x faster than `step()`); it is checked against plain
  `step()` by randomised tests, prefixed (CB/DD/ED/FD) opcodes included.
* `cpm.py` - CP/M 2.2 by trapping BDOS/BIOS calls: drives mapped to host folders,
  optional user areas (`A/0`, `A/1` ...), a small command prompt, ANSI/VT output,
  raw keyboard input on Windows and POSIX.
* `z80dis.py` - Z80/Z180 disassembler (used by `DEBUG`); `debugger.py` - the `DEBUG` monitor.
* `diskimg.py` - reads raw CP/M disk images for `IMAGE`; `z3env.py` - the ZCPR3 environment for `Z3`.
* `fix_turbo_ansi.py` - repairs the damaged ANSI entry in Turbo Pascal 3's `TURBO.COM`/`TINST.DTA`.
* `cc.ps1` - Windows PowerShell: compiles a C file with the `uc80`/`um80` pip packages
  (installed on first use) and runs it, e.g. `.\cc.ps1 hello` for `hello.c` (see the script's help).
* `test_z80sim.py` - unit tests (`python -m unittest test_z80sim`).

## Usage

    python cpm.py                          # CP/M prompt on ./disks (offers to create A..P there)
    python cpm.py PROGRAM.COM args         # run a program (starts on that program's disk)
    python cpm.py --disk DISKS PROGRAM.COM # DISKS/A, DISKS/B ... one folder per drive
    python cpm.py --dir FOLDER PROGRAM.COM # FOLDER is drive A: (default: the current folder)
    python cpm.py --drive B=some/folder
    python cpm.py --charset fi             # Finnish/Swedish letters in [ \ ] { | }
    python cpm.py --backspace del          # Backspace sends 7Fh instead of ^H
    python cpm.py --time PROGRAM.COM       # report run time, instructions, T-states
    python cpm.py --startup FILE           # run these commands at start (else A:0 STARTUP.CPM)
    python cpm.py --color auto|on|off      # ANSI colour in the Python-made prompt/DIR output
    python cpm.py --cpu z180               # Z180 extra instructions (see below)
    python cpm.py --z3                     # ZCPR3 environment (see Z3 below)

User areas are per drive: a drive folder with a subfolder `0` has user areas `A/0`, `A/1` ...
(the missing ones are created when a file is first written there); a drive folder without one is
flat, and every user area sees the same files. On a drive with user areas, files left in the
drive folder itself are not visible to CP/M; the prompt lists them at start.

At the prompt type `HELP`. `Ctrl+]` or `F12` stops the emulator immediately (F12 for keyboards where `]` needs AltGr, such as the Finnish one).

## The command prompt

There is no real CCP: the prompt is Python. Besides `DIR ERA REN TYPE USER` and drive
letters (`B:`), it has some ZCPR-style extras (see `HELP name`):

* `PATH [list|NONE]` - where commands are searched after the current drive/user.
  Elements are drive+user (`A0:`, `B3:`, `$0:` = user 0 of the current drive). Default
  `$0: A0:`. A typed drive (`B:PROG`) searches only that drive.
* `ALIAS name=text`, `UNALIAS name` - short names for command lines.
* Disk size - STAT and other programs see an 8 MB drive (4K blocks, 1024 directory
  entries) whose free space is computed from the real files in all user areas of the
  folder; with more than 4 MB the block size grows (up to 32 MB), beyond that it reads full.
  Nothing stops a write, the limit is only what STAT reports.
* `KEYMAP [AUTO|WS|BBC|BBC3]` - the codes the cursor keys, Home/End, Delete and Insert send:
  WordStar style by default, the keys BBC BASIC (Z80) expects for programs named `BBC*`
  (version 5; version 3.00 as in cpm-dist has no cursor editing, so Backspace sends DEL for
  it). Also when the program is started straight from the command line. Alt+letter is
  Ctrl+letter for terminals without a Ctrl key (e.g. Termux).
* Line editing - Left/Right, Home/End, Delete, Backspace, `^U` (clear), Up/Down (earlier
  commands) work in the prompt, so a pasted line can be corrected before Enter.
* `--cpu z180` or `CPU Z180` at the prompt - HD64180/Z180 instructions on top of the Z80: `MLT`,
  `TST`, `TSTIO`, `IN0`/`OUT0`, `OTIM`/`OTDM` (and `R` forms), `SLP`, and the MMU: 1 MB of
  memory mapped in 4K pages through `CBAR` (3Ah), `BBR` (39h) and `CBR` (38h). Each program
  starts with the plain 64K mapping; the rest of the 1 MB keeps its contents between programs.
  No other on-chip peripherals; undefined opcodes stay NOPs. A Z80 build of the same program
  still runs unchanged. In `DEBUG`, `R` shows the MMU, `R CBAR 84` sets it and `DP` dumps
  physical memory.
* `PLAY file [fps|baud] [RAW]` - play an ANSI/terminal stream (e.g. an ANSI "Bad Apple"). Frames
  start at `ESC[H`, `ESC[1;1H` or a form feed and are paced at fps (default 30 when the file
  has frames, 57600 baud for a SAUCE ANSiMation such as Bad Apple; a number above 240 is a baud rate, default 9600 otherwise). Stops at `^Z`
  (SAUCE record ignored); `q`/Esc aborts. Start with `--charset cp437` for IBM block
  characters (the terminal needs a UTF-8 font with block glyphs). Baud-rate streams are drawn
  through an internal 80x25 screen with absolute cursor moves, so the terminal width does not
  matter; `RAW` sends the bytes as they are.
* `IMAGE file [format[:slice]] [GET [n:]pattern [dest]]` - list or copy files out of a raw CP/M
  disk image (read only): 8" floppies, the RomWBW floppy and hard disk formats
  (`hd1k:2` = slice 2, found behind the 1 MB prefix of combined images) and anything a
  cpmtools `diskdefs` file in the folder describes. `IMAGE ?` lists the formats; without one
  it is guessed from the directory. `IMAGE rc.img GET 0:*.COM B3:` copies user 0's COM files to B3:.
* `cmd1 ; cmd2` - several commands on one line; lines starting with `#` are comments.
* Startup file - `--startup FILE`, or `STARTUP.CPM` on A:0, run when the prompt starts.
* `COPY src [dst]` - copy files; wildcards, a destination drive and user area (`B3:`), or a
  name/pattern (`COPY *.TXT *.BAK`). Existing files are replaced.
* `VIEW file` - pager for files of any size (memory-mapped, so no 64K limit): Space/b or
  PgDn/PgUp, Enter/arrows, Home/End or g/G, `/text` search with n/N, q or Esc to quit. Binary files open as a hex dump
  (`x` switches hex/text, `VIEW file HEX` forces it).
* `DEBUG [file [args]]` - a small monitor for the program before and while it runs:
  `L` disassemble (Z80, and the Z180 extras with `--cpu z180`; the CP/M `DDT` knows only the
  8080), `D` dump, `R` registers (`R HL 1234` sets one), `T [n]` trace n instructions with the
  registers after each, `P` step over a `CALL`, `DJNZ` or block loop, `G` go, `B`/`BC`
  breakpoints, `E`/`F` enter/fill bytes, `H` help, `Q` leave. Numbers are hexadecimal; an
  address can also be a register with an offset (`D HL`, `L PC+4`, `D SP`, `B IX-2`; write
  hex BC, DE or AF as 0BC ...). BDOS
  calls run as one step. `DEBUG` alone starts with empty memory to type code into.
* `OPEN file` - open a file of the current drive and user area in the host's own program, for
  example `OPEN Z80ASM.PDF` for a manual; `OPEN .` opens the folder. Programs and scripts
  (`.EXE`, `.COM`, `.BAT`, `.PS1` ...) are refused.
* `Z3 [ON|OFF]` (or `--z3`) - a ZCPR3 environment for individual Z-System tools such as
  `Z3LOC`, `ZSHOW` and `SETPATH` (`PATH.COM`; the built-in `PATH` command is a different
  thing): the 256-byte environment descriptor, path, wheel byte, message buffers and an ANSI
  terminal-capability block sit below a lowered BDOS address, and programs with the `Z3ENV`
  header are told where it is. Changes (for example a new path) last from program to program.
  There is no ZCPR3 command processor, so NZ-COM and tools that need the CCP, RCP or FCP
  do not work.
* `TIME [ON|OFF]` - after each program, real time, instructions, T-states and the time
  a 4 MHz Z80 would need (time spent waiting for keys is left out).
* A program that selects other drives (such as `STAT`) does not change the prompt drive.

## Building C programs (Manx Aztec C 1.06D)

The RomWBW disk image `hd1k_aztecc.img` holds the Aztec C compiler for CP/M-80. Copy it out
with `IMAGE` (the path is relative to where you started the emulator):

```
IMAGE disks\hd1k_aztecc.img GET *.* C:
```

Then, in the drive and user area that holds the tools (`CC.COM`, `AS.COM`, `LN.COM`, `C.LIB`)
and the source:

```
CC HELLO.C                 (C to 8080 assembly, HELLO.ASM)
AS HELLO.ASM               (assemble to HELLO.O)
LN HELLO.O C.LIB           (link with the C library, giving HELLO.COM)
```

Several commands can go on one line: `CC HELLO.C ; AS HELLO.ASM ; LN HELLO.O C.LIB`. Do not
paste the next command while a program runs, as the program may take the characters.
This is the 1980s K&R dialect of C for the 8080, not ANSI C; `int` is 16 bit.

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
