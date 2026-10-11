#!/usr/bin/env python3
"""
cpm.py - A CP/M 2.2 environment for the z80sim Z80 simulator.

Runs CP/M .COM programs (load address 0100h) with BDOS and BIOS calls
trapped and implemented in Python, so no real CP/M system image is needed.

  * Page zero set up like the real thing: warm-boot JP at 0000h, IOBYTE,
    current drive/user, BDOS JP at 0005h, top-of-TPA at 0006h, default FCBs
    at 005Ch / 006Ch built from the command line, command tail and default
    DMA buffer at 0080h
  * BDOS functions 0-37 and 40: console I/O, line input, file open/close/
    make/delete/rename, sequential + random read/write, directory search
    (multi-extent), compute file size, DMA, drives, user numbers, DPB, ...
  * BIOS jump table at FE00h (console, list, stubs for disk primitives)
  * Each CP/M drive (A:..P:) maps to a directory on the host; file names are
    matched case-insensitively and new files are created in lower case

Usage
    python cpm.py --disk DIR            # CP/M prompt (A0>, DIR, TYPE, ERA, REN, USER, B:, EXIT)
    python cpm.py PROGRAM.COM [args...] [--dir DIR] [--drive B=DIR ...]

  --disk DIR holds one folder per drive letter (A, B, ...); a drive folder that
  contains sub-folders 0, 1, 2 ... is split into CP/M user areas.

    from cpm import CPM
    cpm = CPM(drives={"A": "./disk"})
    cpm.load(open("hello.com", "rb").read(), args=["FOO.TXT"])
    cpm.run()
"""
import codecs
import os
import re
import sys

from z80sim import Z80

TPA = 0x0100
BDOS_BASE = 0xFD00
BDOS_ENTRY = BDOS_BASE + 6
DPB_ADDR = BDOS_BASE + 0x80
BIOS_BASE = 0xFE00
BIOS_ENTRIES = ("BOOT", "WBOOT", "CONST", "CONIN", "CONOUT", "LIST", "PUNCH",
                "READER", "HOME", "SELDSK", "SETTRK", "SETSEC", "SETDMA",
                "READ", "WRITE", "LISTST", "SECTRAN")
# Each jump-table slot is a real "JP routine" so programs that read the table
# (e.g. MBASIC patches its console calls with the target address) work.
BIOS_IMPL = BIOS_BASE + 0x80
BIOS_END = BIOS_IMPL + len(BIOS_ENTRIES)

CTRL_Z = 0x1A
_BAD_NAME_CHARS = set('<>,;:=?*[]|/\\" ')


# Finnish/Swedish 7-bit national character set (ISO 646-FI): the letters sit in
# the positions of [ \ ] { | }.  Opt-in with --charset fi.
_FI_IN = {"ä": 0x7B, "ö": 0x7C, "å": 0x7D, "Ä": 0x5B, "Ö": 0x5C, "Å": 0x5D}
_FI_OUT = {v: k for k, v in _FI_IN.items()}

# Cursor/editing keys -> WordStar "diamond" control codes
_KEYS_NT = {"H": [0x05], "P": [0x18], "K": [0x13], "M": [0x04],     # up down left right
            "G": [0x11, 0x13], "O": [0x11, 0x04],                    # home end  (^QS ^QD)
            "I": [0x12], "Q": [0x03], "S": [0x07], "R": [0x16]}      # pgup pgdn del ins
_KEYS_ANSI = {"A": [0x05], "B": [0x18], "D": [0x13], "C": [0x04],
              "H": [0x11, 0x13], "F": [0x11, 0x04],
              "1~": [0x11, 0x13], "4~": [0x11, 0x04], "5~": [0x12],
              "6~": [0x03], "3~": [0x07], "2~": [0x16]}
QUIT_KEY = 0x1D                                                      # Ctrl-]


from time import perf_counter as _clock


def _count_idle(fn):
    """Add the time spent inside a (possibly blocking) input call to self.idle."""
    import time

    def wrapper(self, *a, **k):
        t0 = time.perf_counter()
        try:
            return fn(self, *a, **k)
        finally:
            self.idle += time.perf_counter() - t0
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class Console:
    """Console device.

    On a real terminal keys are read one at a time with no local echo (like a
    CP/M terminal), so programs such as WordStar draw their own screen without
    every key appearing twice.  Cursor keys become WordStar control codes and
    Ctrl-] stops the emulator.  When stdin is a pipe or file, input is read as
    text instead (Enter = CR) and nothing is echoed.
    """

    def __init__(self, stdin=None, stdout=None, charset=None, backspace="bs"):
        interactive = stdin is None
        self.backspace = backspace      # key sends "bs" = ^H (08h) or "del" = 7Fh
        self.stdin = sys.stdin if stdin is None else stdin
        self.stdout = sys.stdout if stdout is None else stdout
        self.charset = charset
        self._buf = []
        self._eof_reads = 0
        self._last_poll = 0.0
        self.idle = 0.0                # seconds spent waiting for input
        self._eof = False
        self._esc = 0                  # output escape-sequence state
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._fd = None
        self._raw = None               # "nt" / "posix": single-key input
        self._saved = None
        try:
            self._fd = self.stdin.fileno()
        except Exception:
            pass
        if interactive and self._fd is not None:
            try:
                is_tty = self.stdin.isatty()
            except Exception:
                is_tty = False
            if is_tty:
                self._init_tty()
        if stdout is None:
            if charset:
                try:
                    self.stdout.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            self._enable_vt()

    # -- terminal setup -----------------------------------------------------
    def _enable_vt(self):
        """Make the Windows console understand ANSI/VT escape sequences."""
        if os.name != "nt":
            return
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            pass

    def _pass_ctrl_c(self):
        """Windows: stop the console from turning Ctrl+C into a Python interrupt
        so the program receives it as the key ^C (MBASIC break, WordStar, ...).
        Ctrl+Break still interrupts the emulator."""
        try:
            import atexit
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-10)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                old = mode.value
                k.SetConsoleMode(h, old & ~0x0001)       # ENABLE_PROCESSED_INPUT off
                atexit.register(lambda: k.SetConsoleMode(h, old))
        except Exception:
            pass

    def _init_tty(self):
        if os.name == "nt":
            try:
                import msvcrt  # noqa: F401
            except ImportError:
                return
            self._raw = "nt"
            self._pass_ctrl_c()
            return
        try:
            import atexit
            import termios
            self._saved = termios.tcgetattr(self._fd)
            attrs = termios.tcgetattr(self._fd)
            attrs[0] &= ~(termios.IXON | termios.ICRNL | termios.INLCR | termios.ISTRIP)
            attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSADRAIN, attrs)
            atexit.register(self.close)
            self._raw = "posix"
        except Exception:
            self._saved = None

    def close(self):
        if self._saved is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass
            self._saved = None

    # -- output ---------------------------------------------------------------
    def write(self, b):
        b &= 0x7F
        ch = chr(b)
        if self._esc == 0:
            if b == 0x1B:
                self._esc = 1
            elif self.charset == "fi":
                ch = _FI_OUT.get(b, ch)
        elif self._esc == 1:                       # after ESC
            self._esc = 2 if b == 0x5B else 0
        elif 0x40 <= b <= 0x7E:                    # final byte of ESC [ ... seq
            self._esc = 0
        self.stdout.write(ch)
        if self._esc == 0:          # flush at once, except inside an ESC sequence
            self.flush()

    def flush(self):
        try:
            self.stdout.flush()
        except Exception:
            pass

    # -- input ----------------------------------------------------------------
    def _map_in(self, ch):
        if self.charset == "fi" and ch in _FI_IN:
            return _FI_IN[ch]
        if ord(ch) < 0x80:
            return ord(ch)
        try:
            return ch.encode("latin-1")[0]
        except UnicodeEncodeError:
            return 0x3F

    def _feed_text(self, text):
        for ch in text:
            if ch == "\n":
                self._buf.append(0x0D)             # Enter arrives as CR
            elif ch != "\r":
                self._buf.append(self._map_in(ch))

    def _fill(self):
        """Line/pipe input. Returns False at end of input."""
        self.flush()
        if self._fd is not None:
            if self._eof:
                return False
            try:
                data = os.read(self._fd, 4096)
            except OSError:
                data = b""
            if not data:
                self._eof = True
                return False
            self._feed_text(self._dec.decode(data))
            return True
        line = self.stdin.readline()
        if not line:
            return False
        self._feed_text(line)
        return True

    def _key_nt(self):
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):                 # cursor / function key
            seq = _KEYS_NT.get(msvcrt.getwch())
            if not seq:
                return None
            self._buf.extend(seq[1:])
            return seq[0]
        if ch == "\x08":                           # Backspace key
            return 0x08 if self.backspace == "bs" else 0x7F
        return self._map_in(ch)

    def _key_posix(self):
        import select
        b = os.read(self._fd, 1)
        if not b:
            self._eof = True
            return None
        c = b[0]
        if c in (0x08, 0x7F):                      # Backspace key
            return 0x08 if self.backspace == "bs" else 0x7F
        if c >= 0x80:                              # UTF-8 byte
            ch = self._dec.decode(b)
            return self._map_in(ch) if ch else None
        if c != 0x1B:
            return c
        if not select.select([self._fd], [], [], 0.03)[0]:
            return 0x1B                            # a lone Esc key
        n = os.read(self._fd, 1)
        if n not in (b"[", b"O"):
            self._buf.extend(n)
            return 0x1B
        params = b""
        while True:
            x = os.read(self._fd, 1)
            if not x:
                return None
            if 0x40 <= x[0] <= 0x7E:
                final = x.decode("latin-1")
                break
            params += x
        key = (params.decode("latin-1") + final) if final == "~" else final
        seq = _KEYS_ANSI.get(key)
        if not seq:
            return None
        self._buf.extend(seq[1:])
        return seq[0]

    def _read_raw(self):
        self.flush()
        while not self._eof:
            b = self._key_nt() if self._raw == "nt" else self._key_posix()
            if b is None:
                continue
            if b == QUIT_KEY:
                raise SystemExit("\r\n[emulator stopped with Ctrl-]]")
            return b
        return None

    @_count_idle
    def read_char(self):
        """Blocking read of one byte. None at end of input."""
        if self._buf:
            return self._buf.pop(0)
        if self._raw:
            return self._read_raw()
        while not self._buf:
            if not self._fill():
                # Piped input is used up.  A program that keeps asking for more
                # (MBASIC at its Ok prompt, say) would spin forever: stop instead.
                self._eof_reads += 1
                if self._eof_reads > 50:
                    raise SystemExit(0)
                return None
        return self._buf.pop(0)

    def has_input(self):
        if self._buf:
            return True
        if self._raw == "nt" or (self._fd is not None and not self._eof):
            # MBASIC and friends poll the keyboard every few hundred instructions;
            # an OS call each time is slow (very slow with the Windows console),
            # so ask the OS at most every few milliseconds.
            now = _clock()
            if now - self._last_poll < 0.004:
                return False
            self._last_poll = now
        if self._raw == "nt":
            import msvcrt
            return msvcrt.kbhit()
        if self._fd is not None:
            if self._eof:
                return False
            try:
                import select
                if not select.select([self._fd], [], [], 0)[0]:
                    return False
            except Exception:
                return False
            return True if self._raw else self._fill()
        try:
            pos = self.stdin.tell()
            ch = self.stdin.read(1)
            self.stdin.seek(pos)
            return bool(ch)
        except Exception:
            return False

    @_count_idle
    def read_line(self):
        """One edited line as bytes without the terminator; None at EOF.
        On a real terminal the line is echoed and Backspace/^U/^X edit it."""
        out = []
        while True:
            ch = self.read_char()
            if ch is None:
                return bytes(out) if out else None
            if self._raw:
                if ch == 0x0D:
                    self.write(0x0D)
                    self.write(0x0A)
                    return bytes(out)
                if ch in (0x08, 0x7F):
                    if out:
                        out.pop()
                        for c in (8, 32, 8):
                            self.write(c)
                elif ch in (0x15, 0x18):
                    while out:
                        out.pop()
                        for c in (8, 32, 8):
                            self.write(c)
                elif ch >= 0x20:
                    out.append(ch)
                    self.write(ch)
                self.flush()
                continue
            if ch == 0x0D:
                return bytes(out)
            out.append(ch)


class CPM:
    def __init__(self, cpu=None, console=None, drives=None, printer=None):
        self.cpu = cpu if cpu is not None else Z80()
        self.console = console if console is not None else Console()
        self.drives = {k.upper(): os.path.abspath(v)
                       for k, v in (drives or {"A": "."}).items()}
        # A drive directory that contains a sub-directory "0" is treated as
        # having user areas (DRIVE/0, DRIVE/1, ...); otherwise it is flat.
        self._users = {k: os.path.isdir(os.path.join(v, "0"))
                       for k, v in self.drives.items()}
        self.printer = printer or (lambda b: None)
        self.dma = 0x80
        self.drive = 0           # 0 = A:
        self.user = 0
        self.exited = False
        self.timing = False
        self.color = False                  # ANSI colour in the built-in prompt/DIR/HELP
        self.path = ["$0:", "A0:"]         # command search path (see HELP PATH)
        self.aliases = {}
        self._expanding = set()
        self.startup = None                 # host file with start-up commands
        self._steps_last = 0
        self._search = []
        self._warned = set()
        self._fn = {
            0: self._f_exit, 1: self._f_conin, 2: self._f_conout,
            3: lambda de: CTRL_Z, 4: lambda de: None,
            5: lambda de: self.printer(de & 0xFF), 6: self._f_rawio,
            7: lambda de: self.cpu.mem[3], 8: self._f_set_iobyte,
            9: self._f_print_string, 10: self._f_read_buffer,
            11: lambda de: 0xFF if self.console.has_input() else 0,
            12: lambda de: 0x0022, 13: self._f_reset_disks,
            14: self._f_select_disk, 15: self._f_open, 16: lambda de: 0,
            17: self._f_search_first, 18: self._f_search_next,
            19: self._f_delete, 20: self._f_read_seq, 21: self._f_write_seq,
            22: self._f_make, 23: self._f_rename, 24: self._f_login_vector,
            25: lambda de: self.drive, 26: self._f_set_dma,
            27: lambda de: 0, 28: lambda de: 0, 29: lambda de: 0,
            30: lambda de: 0, 31: lambda de: DPB_ADDR, 32: self._f_user,
            33: self._f_read_rand, 34: self._f_write_rand,
            35: self._f_file_size, 36: self._f_set_rand,
            37: lambda de: 0, 40: self._f_write_rand,
        }
        self._init_memory()

    # ---------------------------------------------------------------- memory
    def _get(self, addr, n):
        mem = self.cpu.mem
        return bytes(mem[(addr + i) & 0xFFFF] for i in range(n))

    def _put(self, addr, data):
        mem = self.cpu.mem
        for i, b in enumerate(data):
            mem[(addr + i) & 0xFFFF] = b

    def _init_memory(self):
        mem = self.cpu.mem
        mem[0:0x10000] = bytes(0x10000)
        self._put(0x0000, bytes([0xC3, (BIOS_BASE + 3) & 0xFF, (BIOS_BASE + 3) >> 8]))
        mem[3] = 0x00                                   # IOBYTE
        mem[4] = ((self.user << 4) | self.drive) & 0xFF  # current drive / user
        self._put(0x0005, bytes([0xC3, BDOS_ENTRY & 0xFF, BDOS_ENTRY >> 8]))
        self._put(0x0038, bytes([0xC9]))                # RST 7: just RET
        for i in range(BDOS_BASE, BDOS_BASE + 0x100):
            mem[i] = 0xC9                               # BDOS area: RETs
        for i in range(len(BIOS_ENTRIES)):
            impl = BIOS_IMPL + i
            self._put(BIOS_BASE + 3 * i, bytes([0xC3, impl & 0xFF, impl >> 8]))
            mem[impl] = 0xC9                            # trapped, then RET
        # Disk parameter block (8" SSSD-like) returned by BDOS 31
        self._put(DPB_ADDR, bytes([26, 0, 3, 7, 0, 242, 0, 63, 0, 0xC0, 0, 16, 0, 2, 0]))

    @staticmethod
    def _parse_fcb(spec):
        drive = 0
        if len(spec) >= 2 and spec[1] == ":":
            drive = (ord(spec[0].upper()) - 64) & 0x1F
            spec = spec[2:]
        name, _, ext = spec.partition(".")

        def pad(s, n):
            s = s.upper()
            i = s.find("*")
            if i >= 0:
                s = s[:i].ljust(n, "?")
            return s.ljust(n)[:n]
        return bytes([drive]) + pad(name, 8).encode("latin1") + pad(ext, 3).encode("latin1")

    def load(self, program, args=(), addr=TPA):
        """Load a .COM image and set up page zero, FCBs and CPU registers."""
        self._init_memory()
        self.dma = 0x80
        self.exited = False
        cpu = self.cpu
        cpu.mem[addr:addr + len(program)] = program
        tail = (" " + " ".join(args)).upper()[:127] if args else ""
        cpu.mem[0x80] = len(tail)
        self._put(0x81, tail.encode("latin1", "replace"))
        fcb1 = self._parse_fcb(args[0]) if len(args) > 0 else bytes([0]) + b" " * 11
        fcb2 = self._parse_fcb(args[1]) if len(args) > 1 else bytes([0]) + b" " * 11
        self._put(0x5C, fcb1)
        self._put(0x6C, fcb2)
        cpu.reset()
        cpu.r[7] = 0
        cpu.f = 0
        cpu.pc = addr
        cpu.sp = BDOS_BASE
        cpu._push(0x0000)                               # RET => warm boot

    def load_file(self, path, args=()):
        with open(path, "rb") as f:
            self.load(f.read(), args)

    # ------------------------------------------------------------------- run
    def run(self, max_steps=None):
        """Run until the program exits (warm boot / BDOS 0 / HALT)."""
        cpu = self.cpu
        n = 0
        try:
            while not self.exited:
                left = None if max_steps is None else max_steps - n
                if left is not None and left <= 0:
                    break
                n += cpu.run_fast(BDOS_BASE, left)
                pc = cpu.pc
                if pc >= BDOS_BASE:
                    if pc == BDOS_ENTRY:
                        self.bdos()
                        continue
                    if BIOS_IMPL <= pc < BIOS_END:
                        self.bios(pc - BIOS_IMPL)
                        continue
                    cpu.step()                    # code living above BDOS_BASE
                    n += 1
                if cpu.halted:
                    break
        finally:
            self._steps_last = n
        self.console.flush()
        return n

    # ------------------------------------------------------------------ BDOS
    def bdos_call(self, fn, de=0):
        """Invoke a BDOS function from Python. Returns the result (A/HL)."""
        cpu = self.cpu
        cpu.r[1] = fn & 0xFF
        cpu.de = de & 0xFFFF
        return self._dispatch(fn, de & 0xFFFF)

    def _dispatch(self, fn, de):
        h = self._fn.get(fn)
        if h is None:
            if fn not in self._warned:
                self._warned.add(fn)
                sys.stderr.write("cpm: unimplemented BDOS function %d\n" % fn)
            return 0
        return h(de)

    def bdos(self):
        cpu = self.cpu
        res = self._dispatch(cpu.r[1], cpu.de)
        if res is not None:
            cpu.r[7] = res & 0xFF
            cpu.r[5] = res & 0xFF
            cpu.r[4] = cpu.r[0] = (res >> 8) & 0xFF
        if not self.exited:
            cpu.pc = cpu._pop()

    # -- console ----------------------------------------------------------------
    def _f_exit(self, de):
        self.exited = True

    def _f_conin(self, de):
        ch = self.console.read_char()
        if ch is None:
            return CTRL_Z
        self.console.write(ch)
        return ch

    def _f_conout(self, de):
        self.console.write(de & 0xFF)

    def _f_rawio(self, de):
        e = de & 0xFF
        if e == 0xFF:
            if self.console.has_input():
                ch = self.console.read_char()
                return 0 if ch is None else ch
            return 0
        if e == 0xFE:
            return 0xFF if self.console.has_input() else 0
        self.console.write(e)

    def _f_set_iobyte(self, de):
        self.cpu.mem[3] = de & 0xFF

    def _f_print_string(self, de):
        mem = self.cpu.mem
        while True:
            ch = mem[de & 0xFFFF]
            if ch == 0x24:                              # '$'
                break
            self.console.write(ch)
            de += 1
        self.console.flush()

    def _f_read_buffer(self, de):
        mem = self.cpu.mem
        maxlen = mem[de & 0xFFFF]
        line = self.console.read_line()
        line = (line or b"")[:maxlen]
        mem[(de + 1) & 0xFFFF] = len(line)
        self._put(de + 2, line)

    # -- disks ---------------------------------------------------------------------
    def _f_reset_disks(self, de):
        self.drive = 0
        self.dma = 0x80
        return 0

    def _f_select_disk(self, de):
        e = de & 0xFF
        if chr(65 + e) in self.drives:
            self.drive = e
            self.cpu.mem[4] = (self.cpu.mem[4] & 0xF0) | e
            return 0
        return 0xFF

    def _f_login_vector(self, de):
        v = 0
        for letter in self.drives:
            v |= 1 << (ord(letter) - 65)
        return v

    def _f_set_dma(self, de):
        self.dma = de & 0xFFFF

    def _f_user(self, de):
        e = de & 0xFF
        if e == 0xFF:
            return self.user
        self.user = e & 0x0F
        self.cpu.mem[4] = ((self.user << 4) | self.drive) & 0xFF
        return 0

    # -- file name mapping -----------------------------------------------------
    def _drive_dir(self, fcb, create=False):
        d = self.cpu.mem[fcb & 0xFFFF]
        letter = chr(65 + (self.drive if d in (0, 0x3F) else d - 1))
        base = self.drives.get(letter)
        if base is None:
            return None
        path = os.path.join(base, str(self.user)) if self._users.get(letter) else base
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    def _fcb_path(self, fcb, create=False):
        dirpath = self._drive_dir(fcb, create)
        if dirpath is None:
            return None
        raw = self._get(fcb + 1, 11)
        name = bytes(b & 0x7F for b in raw[:8]).decode("latin1").rstrip(" ").upper()
        ext = bytes(b & 0x7F for b in raw[8:]).decode("latin1").rstrip(" ").upper()
        host = name + ("." + ext if ext else "")
        if not name or any(c in _BAD_NAME_CHARS or ord(c) < 33 for c in name + ext):
            return None
        try:
            for entry in os.listdir(dirpath):
                if entry.upper() == host:
                    return os.path.join(dirpath, entry)
        except OSError:
            return None
        return os.path.join(dirpath, host.lower()) if create else None

    @staticmethod
    def _to_83(entry):
        name, dot, ext = entry.rpartition(".") if "." in entry else (entry, "", "")
        if not name or len(name) > 8 or len(ext) > 3:
            return None
        if any(c in _BAD_NAME_CHARS or ord(c) < 33 or ord(c) > 126 for c in name + ext):
            return None
        return (name.upper().ljust(8) + ext.upper().ljust(3)).encode("latin1")

    def _dir_entries(self, fcb, all_extents):
        dirpath = self._drive_dir(fcb)
        if dirpath is None:
            return []
        pat = bytes(b & 0x7F for b in self._get(fcb + 1, 11))
        ex_byte = self.cpu.mem[(fcb + 12) & 0xFFFF]
        out = []
        try:
            names = sorted(os.listdir(dirpath), key=str.upper)
        except OSError:
            return []
        for entry in names:
            path = os.path.join(dirpath, entry)
            if not os.path.isfile(path):
                continue
            fn = self._to_83(entry)
            if fn is None or any(c != 0x3F and c != b for c, b in zip(pat, fn)):
                continue
            recs = (os.path.getsize(path) + 127) // 128
            for ex in range(max(1, (recs + 127) // 128)):
                if not all_extents and ex_byte != 0x3F and ex != (ex_byte & 0x1F):
                    continue
                out.append((fn, ex, max(0, min(128, recs - ex * 128)), path))
        return out

    # -- FCB helpers -----------------------------------------------------------
    def _fcb_rec(self, fcb):
        m = self.cpu.mem
        return (((m[(fcb + 14) & 0xFFFF] & 0x3F) << 12) |
                ((m[(fcb + 12) & 0xFFFF] & 0x1F) << 7) |
                (m[(fcb + 32) & 0xFFFF] & 0x7F))

    def _set_fcb_rec(self, fcb, rec):
        m = self.cpu.mem
        m[(fcb + 32) & 0xFFFF] = rec & 0x7F
        m[(fcb + 12) & 0xFFFF] = (rec >> 7) & 0x1F
        m[(fcb + 14) & 0xFFFF] = (rec >> 12) & 0x3F

    @staticmethod
    def _read_record(path, rec):
        try:
            with open(path, "rb") as f:
                f.seek(rec * 128)
                data = f.read(128)
        except OSError:
            return None
        return data.ljust(128, bytes([CTRL_Z])) if data else None

    @staticmethod
    def _write_record(path, rec, data):
        try:
            with open(path, "r+b" if os.path.exists(path) else "w+b") as f:
                f.seek(rec * 128)
                f.write(data)
        except OSError:
            return False
        return True

    # -- file functions ----------------------------------------------------------
    def _f_open(self, de):
        path = self._fcb_path(de)
        if path is None:
            return 0xFF
        m = self.cpu.mem
        recs = (os.path.getsize(path) + 127) // 128
        ext_no = ((m[(de + 14) & 0xFFFF] & 0x3F) << 5) | (m[(de + 12) & 0xFFFF] & 0x1F)
        m[(de + 15) & 0xFFFF] = max(0, min(128, recs - ext_no * 128))
        return 0

    def _f_make(self, de):
        path = self._fcb_path(de, create=True)
        if path is None:
            return 0xFF
        try:
            open(path, "wb").close()
        except OSError:
            return 0xFF
        m = self.cpu.mem
        m[(de + 15) & 0xFFFF] = 0
        return 0

    def _f_delete(self, de):
        paths = {e[3] for e in self._dir_entries(de, True)}
        if not paths:
            return 0xFF
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                return 0xFF
        return 0

    def _f_rename(self, de):
        old = self._fcb_path(de)
        if old is None:
            return 0xFF
        new_fcb = (de + 16) & 0xFFFF
        m = self.cpu.mem
        saved = m[new_fcb]
        m[new_fcb] = m[de & 0xFFFF]                     # same drive as the old name
        new = self._fcb_path(new_fcb, create=True)
        m[new_fcb] = saved
        if new is None or (os.path.exists(new) and new.upper() != old.upper()):
            return 0xFF
        try:
            os.rename(old, new)
        except OSError:
            return 0xFF
        return 0

    def _f_search_first(self, de):
        self._search = self._dir_entries(de, False)
        return self._f_search_next(de)

    def _f_search_next(self, de):
        if not self._search:
            return 0xFF
        fn, ex, rc, _ = self._search.pop(0)
        buf = bytearray([0xE5] * 128)
        e = bytearray(32)
        e[0] = self.user
        e[1:12] = fn
        e[12] = ex & 0x1F
        e[14] = ex >> 5
        e[15] = rc
        for i in range((rc + 7) // 8):
            e[16 + i] = (ex * 16 + i + 1) & 0xFF
        buf[0:32] = e
        self._put(self.dma, buf)
        return 0

    def _f_read_seq(self, de):
        path = self._fcb_path(de)
        if path is None:
            return 9
        rec = self._fcb_rec(de)
        data = self._read_record(path, rec)
        if data is None:
            return 1
        self._put(self.dma, data)
        self._set_fcb_rec(de, rec + 1)
        return 0

    def _f_write_seq(self, de):
        path = self._fcb_path(de)
        if path is None:
            return 9
        rec = self._fcb_rec(de)
        if not self._write_record(path, rec, self._get(self.dma, 128)):
            return 2
        self._set_fcb_rec(de, rec + 1)
        return 0

    def _rand_rec(self, de):
        m = self.cpu.mem
        if m[(de + 35) & 0xFFFF] != 0:
            return None
        return m[(de + 33) & 0xFFFF] | (m[(de + 34) & 0xFFFF] << 8)

    def _f_read_rand(self, de):
        rec = self._rand_rec(de)
        if rec is None:
            return 6
        path = self._fcb_path(de)
        if path is None:
            return 9
        data = self._read_record(path, rec)
        if data is None:
            return 1
        self._put(self.dma, data)
        self._set_fcb_rec(de, rec)
        return 0

    def _f_write_rand(self, de):
        rec = self._rand_rec(de)
        if rec is None:
            return 6
        path = self._fcb_path(de)
        if path is None:
            return 9
        if not self._write_record(path, rec, self._get(self.dma, 128)):
            return 2
        self._set_fcb_rec(de, rec)
        return 0

    def _store_rand(self, de, rec):
        m = self.cpu.mem
        m[(de + 33) & 0xFFFF] = rec & 0xFF
        m[(de + 34) & 0xFFFF] = (rec >> 8) & 0xFF
        m[(de + 35) & 0xFFFF] = (rec >> 16) & 0xFF

    def _f_file_size(self, de):
        path = self._fcb_path(de)
        if path is None:
            return 0xFF
        self._store_rand(de, (os.path.getsize(path) + 127) // 128)
        return 0

    def _f_set_rand(self, de):
        self._store_rand(de, self._fcb_rec(de))

    # ------------------------------------------------------------------- CCP
    # A small command processor: drive switch (B:), DIR, ERA, REN, TYPE, USER,
    # EXIT, and running NAME.COM from the current drive/user area.
    def _print(self, text):
        for ch in text.replace("\n", "\r\n"):
            self.console.write(ord(ch))
        self.console.flush()

    _COLORS = {"red": "31", "green": "32", "yellow": "33", "blue": "34", "cyan": "36",
               "bold": "1", "dim": "2"}

    def _c(self, name, text):
        """Wrap text in an ANSI colour when colour is on."""
        if not self.color:
            return text
        return "\x1b[%sm%s\x1b[0m" % (self._COLORS[name], text)

    def _set_drive(self, drive):
        self.drive = drive
        self.cpu.mem[4] = ((self.user << 4) | self.drive) & 0xFF

    def _make_fcb(self, addr, spec):
        self._put(addr, self._parse_fcb(spec) + bytes(24))

    def _ccp_dir(self, spec):
        self._make_fcb(0x5C, spec or "*.*")
        d = self.cpu.mem[0x5C]
        letter = chr(65 + (self.drive if d == 0 else d - 1))
        names = []
        for fn, _, _, _ in self._dir_entries(0x5C, True):
            if fn not in names:
                names.append(fn)
        if not names:
            self._print("\n" + self._c("red", "NO FILE"))
            return
        for i in range(0, len(names), 4):
            row = " : ".join(
                self._c("green" if f[8:].decode() == "COM" else "bold",
                        "%s %s" % (f[:8].decode(), f[8:].decode()))
                for f in names[i:i + 4])
            self._print("\n%s %s" % (self._c("cyan", letter + ":"), row))

    def run_timed(self):
        """run(), then report host time, instruction count and the time a real
        4 MHz Z80 would have needed (when self.timing is on)."""
        import time
        if not self.timing:
            return self.run()
        t0, ts0, idle0 = time.perf_counter(), self.cpu.tstates, self.console.idle
        try:
            n = self.run()
        finally:
            wait = self.console.idle - idle0
            dt = time.perf_counter() - t0 - wait
            ts = self.cpu.tstates - ts0
            real = ts / 4e6
            self._print("\n" + self._c("dim", "[time]") + " %.2f s here; %s instructions, %s T-states = %.3f s on a "
                        "4 MHz Z80 (%.2fx real-time speed)%s\n" %
                        (dt, format(self._steps_last, ","),
                         format(ts, ","), real, real / dt if dt else 0,
                         "; %.1f s waiting for keys not counted" % wait if wait >= 0.05 else ""))
            self.console.flush()
        return n

    def _ccp_run(self, cmd, args):
        if "." not in cmd.split(":")[-1]:
            cmd += ".COM"
        self._make_fcb(0x5C, cmd)
        path = self._fcb_path(0x5C)
        if path is None:
            # Not in the current drive/user: try the PATH (a typed drive prefix such as
            # B:PROG searches only that drive, current user then user 0).
            user, drive_byte = self.user, self.cpu.mem[0x5C]
            if drive_byte:
                tries = [(0, drive_byte)] if user != 0 else []
            else:
                tries = self._path_dirs()
            try:
                for u, d in tries:
                    self.user = u
                    self.cpu.mem[0x5C] = d
                    path = self._fcb_path(0x5C)
                    if path is not None:
                        break
            finally:
                self.user = user
        if path is None:
            self._print("\n" + self._c("red", "%s?" % cmd.rsplit(".", 1)[0]))
            return
        with open(path, "rb") as f:
            program = f.read()
        self.load(program, args)
        self._print("\n")
        self.run_timed()

    _HELP = {
        "DIR": ("DIR [spec]", "List files. Wildcards: ? matches one character, * the rest of\n"
                "the name or type.   DIR   DIR *.COM   DIR B:*.TXT   DIR A*.*"),
        "ERA": ("ERA spec", "Delete files.   ERA OLD.TXT   ERA *.BAK"),
        "REN": ("REN new=old", "Rename a file.   REN NEW.TXT=OLD.TXT"),
        "TYPE": ("TYPE file", "Show a text file on the screen.   TYPE READ.ME"),
        "TIME": ("TIME [ON|OFF]", "Show timing after every program run: real time here, instructions,\n"
                 "T-states and the time a 4 MHz Z80 would need. Also --time on the command line."),
        "PATH": ("PATH [list|NONE]", "Where commands are searched after the current drive and user.\n"
                 "Elements are DRIVE USER :  A0:  B3:  B:  $0:  ($ = current drive or user).\n"
                 "Default is  $0: A0:  (user 0 of this drive, then of A:).   PATH A0: B0: A15:\n"
                 "A typed drive, as in B:PROG, searches only that drive."),
        "ALIAS": ("ALIAS name=text   UNALIAS name", "Define a short name for a command line.   ALIAS L=DIR *.COM ; USER 0\n"
                  "ALIAS alone lists them; ALIAS name= removes one. Several commands can share a\n"
                  "line when separated by ;   Lines starting with # are comments."),
        "STARTUP": ("Start-up commands", "Commands run when the prompt starts: the file given with --startup,\n"
                    "else STARTUP.CPM on A: user 0. Put PATH and ALIAS lines there."),
        "USER": ("USER n", "Switch to user area n (0-15). Needs a disk laid out as\n"
                 "DRIVE/0, DRIVE/1 ... on the host. The prompt shows it: A0>, A3>"),
        "d:": ("A: B: ... P:", "Switch the current drive, e.g. B:"),
        "HELP": ("HELP [command]  or  ?", "This list, or details about one command.   HELP KEYS"),
        "EXIT": ("EXIT  (also BYE, QUIT)", "Leave the emulator."),
        "RUN": ("NAME [args]", "Any other word runs NAME.COM from the current drive/user,\n"
                "passing the arguments on the command tail:   MBASIC   DUMP FILE.TXT\n"
                "B:PROG runs PROG.COM from drive B. If NAME.COM is not in the current\n"
                "user area it is also looked for in user 0, then on drive A: (user 0).\n"
                "Try  DIR *.COM  to see what is there."),
        "KEYS": ("Keyboard", "Ctrl+]            stop the emulator immediately (unsaved work is lost)\n"
                 "Arrow keys, Home/End, PgUp/PgDn, Delete, Insert  ->  WordStar keys\n"
                 "                   (^E ^X ^S ^D, ^QS ^QD, ^R ^C, ^G, ^V)\n"
                 "Backspace          sends ^H (start with --backspace del for 7Fh)\n"
                 "Ctrl+letter        passes straight through, e.g. ^K ^Q for editors\n"
                 "Finnish letters    start with --charset fi (a-umlaut etc. as 7-bit codes)"),
    }

    def _ccp_help(self, topic=None):
        h = self._HELP
        if topic:
            key = topic.upper().rstrip(":") + (":" if len(topic.rstrip(":")) == 1 else "")
            key = "d:" if len(topic.rstrip(":")) == 1 and topic.rstrip(":").isalpha() else key
            key = {"BYE": "EXIT", "QUIT": "EXIT", "?": "HELP"}.get(key, key)
            if key not in h:
                self._print("\nNo help for %s. Type HELP for the list." % topic.upper())
                return
            usage, text = h[key]
            self._print("\n%s\n%s" % (usage, text))
            return
        self._print(
            "\nBuilt-in commands:"
            "\n  DIR [spec]      list files            ERA spec       delete files"
            "\n  REN new=old     rename a file         TYPE file      show a text file"
            "\n  USER n          switch user area      B:             switch drive"
            "\n  PATH [list]     command search path   ALIAS n=text   short names"
            "\n  TIME [ON|OFF]   show run timing       cmd ; cmd      several commands"
            "\n  HELP [command]  this help (or ?)      EXIT           leave (BYE, QUIT)"
            "\n"
            "\nAnything else runs NAME.COM:  MBASIC   DUMP FILE.TXT   B:PROG"
            "\nHELP DIR, HELP RUN, HELP KEYS ... give details.  Ctrl+] stops the emulator.")

    _PATH_RE = re.compile(r"([A-P$]?)(\d{1,2}|\$)?:?$")

    def _path_dirs(self):
        """PATH as (user, drive byte) pairs: drive byte 0 = current drive."""
        out = []
        for entry in self.path:
            m = self._PATH_RE.match(entry)
            if not m:
                continue
            letter, num = m.groups()
            drive_byte = ord(letter) - 64 if letter and letter != "$" else 0
            user = int(num) if num and num != "$" else self.user
            if user < 16 and (user, drive_byte) not in out:
                out.append((user, drive_byte))
        return out

    def _ccp_path(self, args):
        if not args:
            self._print("\nPath: " + (" ".join(self.path) or "(none)"))
            return
        if [a.upper() for a in args] == ["NONE"]:
            self.path = []
            return
        entries = [e.upper() for a in args for e in a.replace(",", " ").split()]
        bad = [e for e in entries
               if not self._PATH_RE.match(e) or (e.rstrip(":")[1:].isdigit() and int(e.rstrip(":")[1:]) > 15)]
        if bad:
            self._print("\n" + self._c("red", "Bad path element %s" % bad[0]))
            return
        self.path = [e if e.endswith(":") else e + ":" for e in entries]

    def _ccp_alias(self, args, text):
        if not args:
            for k, v in sorted(self.aliases.items()):
                self._print("\n%s = %s" % (self._c("cyan", k), v))
            if not self.aliases:
                self._print("\nNo aliases. ALIAS name=command text defines one.")
            return
        name, eq, value = text.partition("=")
        name = name.strip().upper()
        if not eq or not name or " " in name:
            self._print("\n" + self._c("red", "Usage: ALIAS name=command text"))
        elif value.strip():
            self.aliases[name] = value.strip()
        else:
            self.aliases.pop(name, None)

    def _ccp_line(self, line):
        """Run one input line (commands separated by ;). False means leave the shell."""
        if line.strip().upper().startswith("ALIAS"):       # the text may itself contain ;
            return self._ccp_command(line)
        for part in line.split(";"):
            if not self._ccp_command(part):
                return False
        return True

    def _ccp_command(self, text):
        words = text.strip().upper().split()
        if not words or words[0].startswith("#"):
            return True
        if words[0] in self.aliases and words[0] not in self._expanding:
            self._expanding.add(words[0])                  # an alias may use its own name
            try:
                return self._ccp_line(" ".join([self.aliases[words[0]]] + words[1:]))
            finally:
                self._expanding.discard(words[0])
        cmd, args = words[0], words[1:]
        if len(cmd) == 2 and cmd[1] == ":" and "A" <= cmd[0] <= "P":
            if cmd[0] in self.drives:
                self._set_drive(ord(cmd[0]) - 65)
            else:
                self._print("\n" + self._c("red", "BDOS Err On %s: Select" % cmd[0]))
        elif cmd in ("EXIT", "BYE", "QUIT"):
            return False
        elif cmd in ("HELP", "?"):
            self._ccp_help(args[0] if args else None)
        elif cmd == "DIR":
            self._ccp_dir(args[0] if args else None)
        elif cmd == "ERA" and args:
            self._make_fcb(0x5C, args[0])
            if self._f_delete(0x5C) != 0:
                self._print("\n" + self._c("red", "NO FILE"))
        elif cmd == "REN" and args and "=" in args[0]:
            new, _, old = args[0].partition("=")
            self._put(0x5C, self._parse_fcb(old) + bytes(4) +
                      self._parse_fcb(new) + bytes(8))
            if self._f_rename(0x5C) != 0:
                self._print("\n" + self._c("red", "NO FILE"))
        elif cmd == "TYPE" and args:
            self._make_fcb(0x5C, args[0])
            path = self._fcb_path(0x5C)
            if path is None:
                self._print("\n" + self._c("red", "NO FILE"))
            else:
                self._print("\n")
                with open(path, "rb") as f:
                    for b in f.read():
                        if b == CTRL_Z:
                            break
                        self.console.write(b)
                self.console.flush()
        elif cmd == "TIME":
            if args and args[0].upper() in ("ON", "OFF"):
                self.timing = args[0].upper() == "ON"
            else:
                self.timing = not self.timing
            self._print("\nTiming %s" % ("ON" if self.timing else "OFF"))
        elif cmd == "PATH":
            self._ccp_path(args)
        elif cmd == "ALIAS":
            self._ccp_alias(args, text.strip()[5:])
        elif cmd == "UNALIAS" and args:
            self.aliases.pop(args[0], None)
        elif cmd == "USER" and args and args[0].isdigit() and int(args[0]) < 16:
            self.user = int(args[0])
            self._set_drive(self.drive)
        else:
            self._ccp_run(cmd, args)
        return True

    def _run_startup(self):
        """Commands from --startup FILE, else STARTUP.CPM on A:0 if there is one."""
        lines = None
        if self.startup:
            with open(self.startup, "rb") as f:
                lines = f.read().decode("latin1").splitlines()
        elif "A" in self.drives:
            drive, user = self.drive, self.user
            self.user = 0
            try:
                self._make_fcb(0x5C, "A:STARTUP.CPM")
                path = self._fcb_path(0x5C)
            finally:
                self.drive, self.user = drive, user
            if path:
                with open(path, "rb") as f:
                    lines = f.read().decode("latin1").splitlines()
        for line in lines or []:
            if not self._ccp_line(line):
                return False
        return True

    def shell(self):
        """Interactive command prompt. Returns at EOF or on EXIT."""
        self._print("\n" + self._c("bold", "CP/M 2.2 emulator") + " - type HELP for commands")
        if not self._run_startup():
            return
        while True:
            self._print("\n%s>" % self._c("green", "%s%d" % (chr(65 + self.drive), self.user)))
            line = self.console.read_line()
            if line is None:
                break
            if not self._ccp_line(line.decode("latin1")):
                break

    # ------------------------------------------------------------------ BIOS
    def bios(self, idx):
        cpu = self.cpu
        name = BIOS_ENTRIES[idx]
        if name in ("BOOT", "WBOOT"):
            self.exited = True
            return
        if name == "CONST":
            cpu.r[7] = 0xFF if self.console.has_input() else 0
        elif name == "CONIN":
            ch = self.console.read_char()
            cpu.r[7] = CTRL_Z if ch is None else ch
        elif name == "CONOUT":
            self.console.write(cpu.r[1])
        elif name == "LIST":
            self.printer(cpu.r[1])
        elif name == "READER":
            cpu.r[7] = CTRL_Z
        elif name == "SETDMA":
            self.dma = cpu.bc
        elif name in ("READ", "WRITE"):
            cpu.r[7] = 1                                # disk primitives: error
        elif name == "LISTST":
            cpu.r[7] = 0xFF
        elif name == "SECTRAN":
            cpu.hl = cpu.bc
        elif name in ("HOME", "SELDSK", "SETTRK", "SETSEC"):
            cpu.hl = 0
        cpu.pc = cpu._pop()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Run a CP/M 2.2 .COM program on the Z80 simulator")
    ap.add_argument("program", nargs="?",
                    help="host path to a .COM file (omit to get the CP/M prompt)")
    ap.add_argument("args", nargs="*", help="command-line arguments for the program")
    ap.add_argument("--dir", "-dir", help="host directory for drive A: (default: current)")
    ap.add_argument("--drive", "-drive", action="append", default=[], metavar="X=DIR",
                    help="map another drive, e.g. --drive B=/tmp/disk")
    ap.add_argument("--disk", "-disk", metavar="DIR",
                    help="directory holding one sub-directory per drive letter "
                         "(A, B, ...), each optionally split into user areas 0..15")
    ap.add_argument("--charset", "-charset", choices=["fi"],
                    help="fi = Finnish/Swedish 7-bit letters (a-umlaut etc. in the "
                         "[ \\ ] { | } positions)")
    ap.add_argument("--backspace", "-backspace", choices=["bs", "del"], default="bs",
                    help="code sent by the Backspace key: bs = ^H (default; right for "
                         "MBASIC and most programs) or del = 7Fh (rubout)")
    ap.add_argument("--startup", "-startup", metavar="FILE",
                    help="host file with commands to run when the prompt starts "
                         "(default: STARTUP.CPM on A: user 0, if present)")
    ap.add_argument("--color", "-color", choices=["auto", "on", "off"], default="auto",
                    help="ANSI colour in the built-in prompt, DIR and HELP (auto = on for a terminal)")
    ap.add_argument("--time", "-time", action="store_true",
                    help="after each program run print elapsed time, instruction count and "
                         "the equivalent time on a 4 MHz Z80")
    ns = ap.parse_args(argv)
    drives = {}
    if ns.disk:
        for entry in sorted(os.listdir(ns.disk)):
            if len(entry) == 1 and "A" <= entry.upper() <= "P" and \
                    os.path.isdir(os.path.join(ns.disk, entry)):
                drives[entry.upper()] = os.path.join(ns.disk, entry)
    if ns.dir:
        drives["A"] = ns.dir
    for spec in ns.drive:
        letter, _, path = spec.partition("=")
        drives[letter.upper()] = path
    if not drives and not ns.program:
        drives["A"] = "."
    start_drive, start_user = 0, 0
    if ns.program:
        # Start in the program's own disk: if its folder is a mapped drive (or one
        # of its user areas) make that current, otherwise map the folder to the
        # first free drive letter (A: unless taken).
        pdir = os.path.abspath(os.path.dirname(ns.program) or ".")
        found = None
        for letter, path in drives.items():
            base = os.path.abspath(path)
            tail = os.path.basename(pdir)
            if pdir == base:
                found = (letter, 0)
            elif (os.path.dirname(pdir) == base and tail.isdigit() and int(tail) < 16
                  and os.path.isdir(os.path.join(base, "0"))):
                found = (letter, int(tail))
            if found:
                break
        if not found:
            letter = next(c for c in "ABCDEFGHIJKLMNOP" if c not in drives)
            drives[letter] = pdir
            found = (letter, 0)
        start_drive, start_user = ord(found[0]) - 65, found[1]
    cpm = CPM(console=Console(charset=ns.charset, backspace=ns.backspace), drives=drives)
    cpm.drive, cpm.user = start_drive, start_user
    cpm.timing = ns.time
    cpm.startup = ns.startup
    cpm.color = (ns.color == "on" or
                 (ns.color == "auto" and not os.environ.get("NO_COLOR") and
                  hasattr(sys.stdout, "isatty") and sys.stdout.isatty()))
    try:
        if ns.program:
            cpm.load_file(ns.program, ns.args)
            cpm.run_timed()
        else:
            cpm.shell()
    except KeyboardInterrupt:
        cpm.console.flush()
        sys.stderr.write("\r\n[emulator interrupted]\r\n")
        return 130
    finally:
        cpm.console.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
