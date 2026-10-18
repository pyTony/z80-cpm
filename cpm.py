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
import shutil
import sys
import time

import z3env
from debugger import Debugger
from z80dis import disasm
from z80sim import Z80, Z180

TPA = 0x0100
BDOS_BASE = 0xFD00
BDOS_ENTRY = BDOS_BASE + 6
DPB_ADDR = BDOS_BASE + 0x80
ALV_ADDR = 0xFF00                    # allocation vector (BDOS 27), up to 256 bytes
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
# The same keys as BBC BASIC (Z80) expects them (the key table in its start-up page):
# ^G up, ^O down, ^J left, ^L right, ^F start of line, ^N end of line, ^R delete character,
# ^Q insert/overwrite.  (BBC BASIC swallows ^S, so the WordStar codes do not work there.)
_KEYS_NT_BBC = {"H": [0x07], "P": [0x0F], "K": [0x0A], "M": [0x0C],
                "G": [0x06], "O": [0x0E], "S": [0x12], "R": [0x11]}
_KEYS_ANSI_BBC = {"A": [0x07], "B": [0x0F], "D": [0x0A], "C": [0x0C],
                  "H": [0x06], "F": [0x0E], "1~": [0x06], "4~": [0x0E],
                  "3~": [0x12], "2~": [0x11]}
# BBC BASIC (Z80) 3.00 (the generic build in cpm-dist) has no cursor editing: only DEL (7F)
# deletes a character, and ^A/^E/^U wipe the line.  So the cursor keys send nothing, and
# Backspace must send DEL there (^H is ignored).
_KEYS_NT_BBC3 = {}
_KEYS_ANSI_BBC3 = {}
_KEYTABS = {"ws": (_KEYS_NT, _KEYS_ANSI), "bbc": (_KEYS_NT_BBC, _KEYS_ANSI_BBC),
            "bbc3": (_KEYS_NT_BBC3, _KEYS_ANSI_BBC3)}
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
    Ctrl-] or F12 stops the emulator.  When stdin is a pipe or file, input is read as
    text instead (Enter = CR) and nothing is echoed.
    """

    def __init__(self, stdin=None, stdout=None, charset=None, backspace="bs"):
        interactive = stdin is None
        self.backspace = backspace      # key sends "bs" = ^H (08h) or "del" = 7Fh
        self.stdin = sys.stdin if stdin is None else stdin
        self.stdout = sys.stdout if stdout is None else stdout
        self.charset = charset
        self._cp437 = bytes(range(128, 256)).decode("cp437") if charset == "cp437" else None
        self._buf = []
        self.history = []
        self.keymap = "ws"             # cursor keys: WordStar codes, or "bbc" (BBC BASIC)
        self._eof_reads = 0
        self.mask = 0xFF if charset == "cp437" else 0x7F   # 7-bit output; 0xFF after CONSOLE8
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
        b &= self.mask
        ch = chr(b)
        if self._esc == 0:
            if b == 0x1B:
                self._esc = 1
            elif self.charset == "fi":
                ch = _FI_OUT.get(b, ch)
            elif self._cp437 and b >= 0x80:
                ch = self._cp437[b - 128]
        elif self._esc == 1:                       # after ESC
            self._esc = 2 if b == 0x5B else 0
        elif 0x40 <= b <= 0x7E:                    # final byte of ESC [ ... seq
            self._esc = 0
        self.stdout.write(ch)
        if self._esc == 0:          # flush at once, except inside an ESC sequence
            self.flush()

    def write_bytes(self, data):
        """Write many output bytes at once (same translation as write(); one flush)."""
        data = bytes(b & self.mask for b in data) if self.mask != 0xFF else data
        if self._cp437:
            text = "".join(self._cp437[b - 128] if b >= 0x80 else chr(b) for b in data)
        elif self.charset == "fi":
            text = "".join(_FI_OUT.get(b, chr(b)) for b in data)
        else:
            text = data.decode("latin-1")
        self._esc = 0
        self.stdout.write(text)
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
            code = msvcrt.getwch()
            if code == "\x86":                     # F12 stops the emulator (no AltGr needed)
                return QUIT_KEY
            seq = _KEYTABS[self.keymap][0].get(code)
            if not seq:
                return None
            self._buf.extend(seq[1:])
            return seq[0]
        if ch == "\x08":                           # Backspace key
            return 0x7F if self.keymap == "bbc3" or self.backspace != "bs" else 0x08
        return self._map_in(ch)

    def _key_posix(self):
        import select
        b = os.read(self._fd, 1)
        if not b:
            self._eof = True
            return None
        c = b[0]
        if c in (0x08, 0x7F):                      # Backspace key
            return 0x7F if self.keymap == "bbc3" or self.backspace != "bs" else 0x08
        if c >= 0x80:                              # UTF-8 byte
            ch = self._dec.decode(b)
            return self._map_in(ch) if ch else None
        if c != 0x1B:
            return c
        if not select.select([self._fd], [], [], 0.03)[0]:
            return 0x1B                            # a lone Esc key
        n = os.read(self._fd, 1)
        if n not in (b"[", b"O"):
            if n.isalpha():                        # Alt+letter (ESC letter) = Ctrl+letter
                return n[0] & 0x1F
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
        if key == "24~":                           # F12 stops the emulator
            return QUIT_KEY
        seq = _KEYTABS[self.keymap][1].get(key)
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
                raise SystemExit("\r\n[emulator stopped with Ctrl-] or F12]")
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
        On a real terminal the line is echoed and can be edited: Left/Right (^S/^D),
        Home/End, Delete (^G), Backspace, ^U clears, Up/Down (^E/^X) recall earlier lines."""
        out, pos = [], 0
        hist = self.history
        hi = len(hist)                       # index into history; len = the new line
        fresh = []

        def put(bs):
            for c in bs:
                self.write(c)

        def redraw(frm, old_len, new_pos):
            """Rewrite out[frm:], blank leftovers, put the cursor at new_pos."""
            put(out[frm:])
            extra = max(0, old_len - len(out))
            put([32] * extra)
            put([8] * (len(out) + extra - new_pos))

        def replace(text):
            nonlocal pos
            put([8] * pos)
            old = len(out)
            out[:] = list(text)
            put(out)
            gap = max(0, old - len(out))
            put([32] * gap)
            put([8] * gap)
            pos = len(out)

        while True:
            ch = self.read_char()
            if ch is None:
                return bytes(out) if out else None
            if not self._raw:
                if ch == 0x0D:
                    return bytes(out)
                out.append(ch)
                continue
            if ch == 0x0D:
                self.write(0x0D)
                self.write(0x0A)
                if out and (not hist or hist[-1] != bytes(out)):
                    hist.append(bytes(out))
                return bytes(out)
            has_ctrl = any(c < 0x20 for c in out)       # ^Z etc. show as two columns
            if ch in (0x08, 0x7F) and has_ctrl:
                if out:
                    c = out.pop()
                    pos = len(out)
                    put([8, 32, 8] * (2 if c < 0x20 else 1))
            elif ch in (0x13, 0x04, 0x07, 0x11) and has_ctrl:
                pass
            elif ch in (0x08, 0x7F):                    # Backspace
                if pos:
                    old = len(out)
                    pos -= 1
                    del out[pos]
                    put([8])
                    redraw(pos, old, pos)
            elif ch == 0x07:                            # Delete
                if pos < len(out):
                    old = len(out)
                    del out[pos]
                    redraw(pos, old, pos)
            elif ch == 0x13:                            # left
                if pos:
                    pos -= 1
                    put([8])
            elif ch == 0x04:                            # right
                if pos < len(out):
                    self.write(out[pos])
                    pos += 1
            elif ch == 0x11:                            # ^Q prefix: ^QS home, ^QD end
                k = self.read_char()
                if k in (0x13, 0x53, 0x73):
                    put([8] * pos)
                    pos = 0
                elif k in (0x04, 0x44, 0x64):
                    put(out[pos:])
                    pos = len(out)
            elif ch == 0x15:                            # ^U: clear the line
                replace(b"")
            elif ch == 0x05:                            # up: older line
                if hi > 0:
                    if hi == len(hist):
                        fresh = list(out)
                    hi -= 1
                    replace(hist[hi])
            elif ch == 0x18:                            # down: newer line
                if hi < len(hist):
                    hi += 1
                    replace(hist[hi] if hi < len(hist) else bytes(fresh))
            elif ch == 0x0A:                            # LF ends a line too
                self.write(0x0D)
                self.write(0x0A)
                return bytes(out)
            elif ch < 0x20 and ch not in (0x0D, 0x0A):
                # any other control key (^Z = end of file for CLINK, PIP, ...) is
                # stored in the line and echoed as ^Z, like the real BDOS does
                put(out[pos:])
                pos = len(out)
                out.append(ch)
                pos += 1
                put([0x5E, ch + 64])
            elif ch >= 0x20:
                old = len(out)
                out.insert(pos, ch)
                self.write(ch)
                pos += 1
                if pos < len(out):
                    redraw(pos, old + 1, pos)
            self.flush()


class AnsiScreen:
    """Minimal ANSI-art screen (80x25 by default): feed() it the byte stream, render() returns
    the bytes that bring a real terminal up to date with absolute cursor addressing, so the
    picture does not depend on the width of the terminal (ANSiMation files assume 80
    columns and wrap on their own)."""

    def __init__(self, cols=80, rows=25):
        self.cols, self.rows = cols, rows
        self.cells = [[(32, "")] * cols for _ in range(rows)]
        self.shown = [[(32, "")] * cols for _ in range(rows)]     # the terminal was cleared
        self.term_attr = None
        self.x = self.y = 0
        self.st = {"b": 0, "bl": 0, "rv": 0, "fg": None, "bg": None}
        self.pending = b""
        self.saved = (0, 0)

    def _attr(self):
        st, p = self.st, []
        if st["b"]: p.append("1")
        if st["bl"]: p.append("5")
        if st["rv"]: p.append("7")
        if st["fg"] is not None: p.append(str(st["fg"]))
        if st["bg"] is not None: p.append(str(st["bg"]))
        return ";".join(p)

    def _sgr(self, params):
        st = self.st
        for n in params or [0]:
            if n == 0: st.update(b=0, bl=0, rv=0, fg=None, bg=None)
            elif n == 1: st["b"] = 1
            elif n == 22: st["b"] = 0
            elif n == 5: st["bl"] = 1
            elif n == 25: st["bl"] = 0
            elif n == 7: st["rv"] = 1
            elif n == 27: st["rv"] = 0
            elif 30 <= n <= 37 or 90 <= n <= 97: st["fg"] = n
            elif n == 39: st["fg"] = None
            elif 40 <= n <= 47 or 100 <= n <= 107: st["bg"] = n
            elif n == 49: st["bg"] = None

    def _scroll(self):
        self.cells.pop(0)
        self.cells.append([(32, "")] * self.cols)
        self.y = self.rows - 1

    def _csi(self, final, params):
        n = params[0] if params else 0
        one = n or 1
        if final in "Hf":
            self.y = min(max((params[0] if params else 1) or 1, 1), self.rows) - 1
            self.x = min(max((params[1] if len(params) > 1 else 1) or 1, 1), self.cols) - 1
        elif final == "A": self.y = max(0, self.y - one)
        elif final == "B": self.y = min(self.rows - 1, self.y + one)
        elif final == "C": self.x = min(self.cols - 1, self.x + one)
        elif final == "D": self.x = max(0, self.x - one)
        elif final == "J" and n == 2:
            self.cells = [[(32, "")] * self.cols for _ in range(self.rows)]
            self.x = self.y = 0
        elif final == "K":
            for i in range(self.x, self.cols):
                self.cells[self.y][i] = (32, self._attr())
        elif final == "m": self._sgr(params)
        elif final == "s": self.saved = (self.x, self.y)
        elif final == "u": self.x, self.y = self.saved

    def feed(self, data):
        data = self.pending + data
        self.pending = b""
        i, n = 0, len(data)
        while i < n:
            c = data[i]
            if c == 0x1B:
                if i + 1 >= n:
                    self.pending = data[i:]
                    break
                if data[i + 1] != 0x5B:
                    i += 2
                    continue
                j = i + 2
                while j < n and (0x30 <= data[j] <= 0x3F):
                    j += 1
                if j >= n:
                    self.pending = data[i:]
                    break
                body = data[i + 2:j].decode("ascii", "replace").lstrip("?=<>")
                params = [int(p) if p.isdigit() else 0 for p in body.split(";")] if body else []
                self._csi(chr(data[j]), params)
                i = j + 1
                continue
            i += 1
            if c == 13: self.x = 0
            elif c == 10:
                self.y += 1
                if self.y >= self.rows: self._scroll()
            elif c == 8: self.x = max(0, self.x - 1)
            elif c == 9: self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
            elif c == 7 or c == 0: pass
            else:
                self.cells[self.y][self.x] = (c, self._attr())
                self.x += 1
                if self.x >= self.cols:
                    self.x = 0
                    self.y += 1
                    if self.y >= self.rows: self._scroll()

    def render(self):
        out = bytearray()
        last = self.term_attr
        for y in range(self.rows):
            row, shown = self.cells[y], self.shown[y]
            x = 0
            while x < self.cols:
                if row[x] == shown[x] or (y == self.rows - 1 and x == self.cols - 1):
                    x += 1      # (the bottom right cell is never written: it could scroll)
                    continue
                out += b"\x1b[%d;%dH" % (y + 1, x + 1)
                while x < self.cols and row[x] != shown[x]:
                    if y == self.rows - 1 and x == self.cols - 1:
                        break
                    ch, at = row[x]
                    if at != last:
                        out += b"\x1b[0m" if not at else b"\x1b[0;" + at.encode() + b"m"
                        last = at
                    out.append(ch)
                    shown[x] = row[x]
                    x += 1
        self.term_attr = last
        return bytes(out)


class CPM:
    def __init__(self, cpu=None, console=None, drives=None, printer=None):
        self._z180_warned = False
        self.cpu = cpu if cpu is not None else Z80()
        self.console = console if console is not None else Console()
        self.drives = {k.upper(): os.path.abspath(v)
                       for k, v in (drives or {"A": "."}).items()}
        # A drive directory that contains a sub-directory "0" is treated as
        # having user areas (DRIVE/0, DRIVE/1, ...); otherwise it is flat.
        self._users = {k: os.path.isdir(os.path.join(v, "0"))
                       for k, v in self.drives.items()}
        self.printer = printer or (lambda b: None)
        self._bls, self._exm = 4096, 1
        self.dma = 0x80
        self.drive = 0           # 0 = A:
        self.z3 = False           # Z3: ZCPR3 environment (see z3env.py)
        self._z3_keep = None
        self.keymode = "auto"     # KEYMAP: auto (BBC keys for BBC* programs), ws or bbc
        self.user = 0
        self.exited = False
        self.timing = False
        self.mhz = None            # clock for the [time] comparison; None: 4 (Z80) or 18.432 (Z180)
        self.throttle = False      # True: run no faster than that clock
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
            27: self._f_alloc_vector, 28: lambda de: 0, 29: lambda de: 0,
            30: lambda de: 0, 31: self._f_get_dpb, 32: self._f_user,
            33: self._f_read_rand, 34: self._f_write_rand,
            35: self._f_file_size, 36: self._f_set_rand,
            37: lambda de: 0, 40: self._f_write_rand,
            45: lambda de: 0,                           # CP/M 3 set error mode: nothing to do
            48: lambda de: 0,                           # CP/M 3 flush buffers: nothing is cached
            # RunCPM extensions used by its INFO, LUA, FORMAT, CONSOLE7/8 programs
            230: self._f_console_mask, 249: self._f_make_disk,
            250: lambda de: 0 if sys.platform.startswith("win") else 2,   # host OS
            251: lambda de: 0x64,                                           # "version" 6.4
            252: lambda de: 0x10,                                           # internal CCP 1.0
            253: lambda de: 0xF500,                                         # CCP address
            254: self._f_lua, 248: self._f_ticks,
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
        keep = None
        if self.z3 and self._z3_keep is not None:     # path, wheel byte ... survive
            keep = bytes(mem[z3env.LOW:BDOS_BASE])
        mem[0:0x10000] = bytes(0x10000)
        self._put(0x0000, bytes([0xC3, (BIOS_BASE + 3) & 0xFF, (BIOS_BASE + 3) >> 8]))
        mem[3] = 0x00                                   # IOBYTE
        mem[4] = ((self.user << 4) | self.drive) & 0xFF  # current drive / user
        bdos = BDOS_ENTRY
        if self.z3:
            top = z3env.BDOS
            if keep is not None:
                mem[z3env.LOW:BDOS_BASE] = keep
            else:
                for a, data in z3env.initial_image(*self._z3_screen()):
                    self._put(a, data)
            self._z3_keep = True
            for i in range(top, top + 0x100):
                mem[i] = 0xC9
            self._put(top + 6, bytes([0xC3, BDOS_ENTRY & 0xFF, BDOS_ENTRY >> 8]))
            bdos = top + 6                              # page 0 says the BDOS is lower
        self._put(0x0005, bytes([0xC3, bdos & 0xFF, bdos >> 8]))
        self._put(0x0038, bytes([0xC9]))                # RST 7: just RET
        for i in range(BDOS_BASE, BDOS_BASE + 0x100):
            mem[i] = 0xC9                               # BDOS area: RETs
        for i in range(len(BIOS_ENTRIES)):
            impl = BIOS_IMPL + i
            self._put(BIOS_BASE + 3 * i, bytes([0xC3, impl & 0xFF, impl >> 8]))
            mem[impl] = 0xC9                            # trapped, then RET
        self._refresh_disk_params()

    def _z3_screen(self):
        cols, rows = shutil.get_terminal_size((80, 24))
        return (min(cols, 255), min(rows, 255))

    def _z3_patch(self, program):
        """ZCPR3 tools carry 'Z3ENV' at 0103h; the command processor stores the address
        of the environment descriptor at 0109h when it loads them."""
        mem = self.cpu.mem
        if program[3:8] != b"Z3ENV" or len(program) < 0x0D:
            return
        kind = program[8]
        if kind in (1, 3) and (kind == 1 or program[0x0B:0x0D] == b"\x00\x01"):
            mem[TPA + 9] = z3env.ENV & 0xFF
            mem[TPA + 10] = z3env.ENV >> 8
        else:
            self._print("\n(ZCPR3 program of type %d is not supported)" % kind)

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

    @property
    def cpu(self):
        return self._cpu

    @cpu.setter
    def cpu(self, cpu):
        self._cpu = cpu
        cpu.on_z180_op = None if cpu.z180 else self._z180_op

    def _z180_op(self, cpu, op):
        """A Z180-only instruction on the Z80: say so once per program."""
        if self._z180_warned:
            return
        self._z180_warned = True
        pc = (cpu.pc - 2) & 0xFFFF
        name = disasm(lambda a: cpu.mem[a & 0xFFFF], pc, True)[1]
        self._print("\n" + self._c("yellow", "[%04X: %s is a Z180 instruction; this is a Z80. "
                                    "Run Z180 programs with --cpu z180 (or CPU Z180)]" % (pc, name)) + "\n")

    def clock_mhz(self):
        """Clock of the real machine the [time] line compares with: 4 MHz for a Z80 (a typical
        CP/M machine), 18.432 MHz for a Z180 (RomWBW boards such as the SC126), or --mhz."""
        return self.mhz or (18.432 if self.cpu.z180 else 4.0)

    def cpu_line(self):
        if self.cpu.z180:
            return "CPU: Z180 (1 MB with the MMU)"
        return "CPU: Z80 (--cpu z180 or CPU Z180 for Z180 programs)"

    def load(self, program, args=(), addr=TPA):
        """Load a .COM image and set up page zero, FCBs and CPU registers."""
        self._z180_warned = False
        self.cpu.reset()                    # first: a Z180 MMU goes back to the plain 64K
        self._init_memory()
        self.dma = 0x80
        self.exited = False
        cpu = self.cpu
        cpu.mem[addr:addr + len(program)] = program
        if self.z3 and addr == TPA:
            self._z3_patch(program)
        tail = (" " + " ".join(args)).upper()[:127] if args else ""
        cpu.mem[0x80] = len(tail)
        self._put(0x81, tail.encode("latin1", "replace"))
        fcb1 = self._parse_fcb(args[0]) if len(args) > 0 else bytes([0]) + b" " * 11
        fcb2 = self._parse_fcb(args[1]) if len(args) > 1 else bytes([0]) + b" " * 11
        self._put(0x5C, fcb1)
        self._put(0x6C, fcb2)
        cpu.r[7] = 0
        cpu.f = 0
        cpu.pc = addr
        cpu.sp = z3env.BDOS if self.z3 else BDOS_BASE
        if self.z3 and program[3:8] == b"Z3ENV":
            cpu.hl = z3env.ENV                          # ZCPR3 passes the ENV address in HL too
        cpu._push(0x0000)                               # RET => warm boot

    def load_file(self, path, args=()):
        with open(path, "rb") as f:
            self.load(f.read(), args)
        self._set_keymap(path, None)

    def _set_keymap(self, name, program=None):
        """Pick the cursor-key codes for the program just loaded (see KEYMAP)."""
        mode = self.keymode
        if mode == "auto":
            mode = "ws"
            if os.path.basename(name.replace(":", "/")).upper().startswith("BBC"):
                if program is None:
                    with open(name, "rb") as f:
                        program = f.read()
                m = re.search(rb"BBC BASIC \(Z80\) Version (\d+)", program)
                mode = "bbc3" if m and int(m.group(1)) < 4 else "bbc"
        self.console.keymap = mode

    # ------------------------------------------------------------------- run
    def run(self, max_steps=None):
        """Run until the program exits (warm boot / BDOS 0 / HALT)."""
        cpu = self.cpu
        n = 0
        if self.throttle:                   # run no faster than clock_mhz(): see _pace
            hz = self.clock_mhz() * 1e6
            t0, ts0, idle0 = _clock(), cpu.tstates, self.console.idle
        try:
            while not self.exited:
                left = None if max_steps is None else max_steps - n
                if left is not None and left <= 0:
                    break
                if self.throttle:
                    left = 2000 if left is None else min(left, 2000)
                    ahead = (cpu.tstates - ts0) / hz - (_clock() - t0 - (self.console.idle - idle0))
                    if ahead > 0.002:       # sleep in small steps: the timer is coarse on Windows
                        time.sleep(ahead)
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

    # -- RunCPM extensions ----------------------------------------------------------
    def _f_ticks(self, de):
        """BDOS 248 (RunCPM): elapsed time as a 32-bit count, low word in HL, high word in DE.
        One tick is a millisecond: BBC BASIC (Z80) turns it into centiseconds with a
        32-step shift-and-compare loop that, because of its loop order, divides by 10."""
        t = int(_clock() * 1000) & 0xFFFFFFFF
        self.cpu.de = t >> 16
        return t & 0xFFFF

    def _f_console_mask(self, de):
        self.console.mask = de & 0xFF            # 7Fh = 7-bit console, FFh = 8-bit

    def _f_make_disk(self, de):
        """BDOS 249: create the folder of the drive named in the FCB (FORMAT.COM)."""
        d = self.cpu.mem[de & 0xFFFF]
        if not 1 <= d <= 16:
            return 0xFF
        letter = chr(64 + d)
        if letter in self.drives:
            return 0
        try:
            first = next(iter(self.drives.values()))
            parent = os.path.dirname(first.rstrip("/\\")) if self._users.get(next(iter(self.drives))) \
                else os.path.dirname(first.rstrip("/\\"))
            path = os.path.join(parent, letter)
            users = bool(self._users.get(next(iter(self.drives))))
            os.makedirs(os.path.join(path, "0") if users else path, exist_ok=True)
        except OSError:
            return 0xFF
        self.drives[letter] = path
        self._users[letter] = users
        return 0

    def _f_lua(self, de):
        """BDOS 254: run the Lua script named in the FCB (LUA.COM does this)."""
        try:
            from lupa import LuaRuntime, LuaError
        except ImportError:
            self._print("\nLua needs the lupa package:  pip install lupa")
            return 0xFF
        mem = self.cpu.mem
        if all((mem[(de + i) & 0xFFFF] & 0x7F) == 0x20 for i in (9, 10, 11)):   # no extension typed
            mem[(de + 9) & 0xFFFF], mem[(de + 10) & 0xFFFF], mem[(de + 11) & 0xFFFF] = 0x4C, 0x55, 0x41
        path = self._fcb_path(de)
        if path is None:
            self._print("\n" + self._c("red", "Lua script not found"))
            return 0xFF
        lua = LuaRuntime(unpack_returned_tuples=True)

        def out(text):
            self._print(text if text.endswith("\n") or True else text)

        def bdos_call(fn, arg=0):
            saved = (self.cpu.r[1], self.cpu.de)
            try:
                res = self.bdos_call(int(fn), int(arg))
            finally:
                self.cpu.r[1] = saved[0]
                self.cpu.de = saved[1]
            return int(res or 0) & 0xFFFF

        env = lua.eval("function(out, bdos, rd, wr) "
                       "BdosCall = bdos; RamRead = rd; RamWrite = wr; "
                       "print = function(...) local t = {} "
                       "for i = 1, select('#', ...) do t[#t + 1] = tostring((select(i, ...))) end "
                       "out(table.concat(t, '\\t') .. '\\n') end end")
        env(out, bdos_call, lambda a: mem[int(a) & 0xFFFF],
            lambda a, v: mem.__setitem__(int(a) & 0xFFFF, int(v) & 0xFF))
        try:
            with open(path, "rb") as f:
                lua.execute(f.read().decode("latin1"))
        except LuaError as e:
            self._print("\n" + self._c("red", str(e)) + "\n")
            return 0xFF
        self.console.flush()
        return 0

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

    # -- disk size reported to programs (STAT, PIP, ...) -------------------------
    # Host folders have no real limit, so the emulated disk is made big enough: 4K
    # blocks (8 MB) normally, 8K/16K blocks (16/32 MB) when the folder holds more than
    # half of that.  1024 directory entries.  The allocation vector is built from the
    # real file sizes (all user areas share a disk); beyond 32 MB it simply reads full.
    _DISK_SIZES = ((4096, 5, 31, 1, 0xFF), (8192, 6, 63, 3, 0xF0), (16384, 7, 127, 7, 0xC0))

    def _drive_files(self, letter):
        """Sizes of all files on drive `letter` (every user area)."""
        base = self.drives.get(letter)
        if base is None:
            return []
        dirs = [os.path.join(base, str(u)) for u in range(16)] if self._users.get(letter) else [base]
        sizes = []
        for d in dirs:
            try:
                for e in os.scandir(d):
                    if e.is_file() and self._to_83(e.name):
                        sizes.append(e.stat().st_size)
            except OSError:
                pass
        return sizes

    def _outside_user_areas(self):
        """{letter: [names]} of CP/M-named files lying in the top folder of a drive that has
        user areas: CP/M cannot see them, only the files in DRIVE/0, DRIVE/1 ..."""
        out = {}
        for letter, base in sorted(self.drives.items()):
            if self._users.get(letter):
                try:
                    names = sorted(e.name for e in os.scandir(base) if e.is_file() and self._to_83(e.name))
                except OSError:
                    names = []
                if names:
                    out[letter] = names
        return out

    def _refresh_disk_params(self):
        letter = chr(65 + self.drive)
        sizes = self._drive_files(letter) if letter in self.drives else []
        for bls, bsh, blm, exm, al0 in self._DISK_SIZES:
            used = sum(-(-n // bls) for n in sizes)
            if used * 2 <= 2048 or bls == 16384:
                break
        dsm = 2047
        self._bls, self._exm = bls, exm
        dpb = bytes([0, 1]) + bytes([bsh, blm, exm, dsm & 255, dsm >> 8, 0xFF, 3, al0, 0, 0, 0, 0, 0])
        self._put(DPB_ADDR, dpb)
        dirblocks = 32768 // bls
        total = min(2048, dirblocks + used)
        alv = bytearray(256)
        for b in range(total):
            alv[b >> 3] |= 0x80 >> (b & 7)
        self._put(ALV_ADDR, bytes(alv))

    def _f_get_dpb(self, de):
        self._refresh_disk_params()
        return DPB_ADDR

    def _f_alloc_vector(self, de):
        self._refresh_disk_params()
        return ALV_ADDR

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
        if self.cpu.mem[(de + 12) & 0xFFFF] == 0x3F:
            self._refresh_disk_params()
            self._search = self._group_extents(self._search)
        return self._f_search_next(de)

    def _group_extents(self, entries):
        """Directory entries as the DPB describes them: one entry covers EXM+1
        logical (16K) extents.  Each carries its last logical extent number, that
        extent's record count and the number of 16-bit block pointers it holds."""
        span = self._exm + 1
        groups = {}
        for fn, ex, rc, path in entries:
            g = groups.setdefault((path, ex // span), [fn, ex, rc, path])
            g[1], g[2] = ex, rc
        out = []
        for (path, k), g in groups.items():
            fn, ex, rc = g[0], g[1], g[2]
            nbytes = (ex - k * span) * 16384 + rc * 128
            out.append((fn, ex, rc, path, max(1, -(-nbytes // self._bls)) if nbytes else 0))
        return out

    def _f_search_next(self, de):
        if not self._search:
            return 0xFF
        fn, ex, rc, _, *nblk = self._search.pop(0)
        buf = bytearray([0xE5] * 128)
        e = bytearray(32)
        e[0] = self.user
        e[1:12] = fn
        e[12] = ex & 0x1F
        e[14] = ex >> 5
        e[15] = rc
        if nblk:                                    # 16-bit block numbers
            for i in range(min(8, nblk[0])):
                b = (ex * 8 + i + 1) & 0xFFFF
                e[16 + 2 * i], e[17 + 2 * i] = b & 255, b >> 8
        else:
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

    _DU_RE = re.compile(r"^([A-P]?)(\d{1,2}):(.*)$")

    def _ccp_dir(self, spec):
        m = self._DU_RE.match((spec or "").upper())
        if m and int(m.group(2)) < 16:            # DIR C3: or DIR C3:*.COM - another user area
            user = self.user
            self.user = int(m.group(2))
            try:
                return self._ccp_dir((m.group(1) + ":" if m.group(1) else "") + m.group(3))
            finally:
                self.user = user
        if spec and spec.endswith(":"):          # "DIR B:" lists everything on B:
            spec += "*.*"
        self._make_fcb(0x5C, spec or "*.*")
        d = self.cpu.mem[0x5C]
        letter = chr(65 + (self.drive if d == 0 else d - 1))
        names = []
        for fn, _, _, _ in self._dir_entries(0x5C, True):
            if fn not in names:
                names.append(fn)
        if not names:
            self._print("\n" + self._c("red", "NO FILE"))
        for i in range(0, len(names), 4):
            row = " : ".join(
                self._c("green" if f[8:].decode() == "COM" else "bold",
                        "%s %s" % (f[:8].decode(), f[8:].decode()))
                for f in names[i:i + 4])
            self._print("\n%s %s" % (self._c("cyan", letter + ":"), row))
        if (spec or "").upper().split(":")[-1] in ("", "*.*"):
            # host files whose names do not fit 8.3 are invisible to CP/M: say so
            dirpath = self._drive_dir(0x5C)
            try:
                bad = sorted(e for e in os.listdir(dirpath or ".")
                             if dirpath and os.path.isfile(os.path.join(dirpath, e))
                             and self._to_83(e) is None)
            except OSError:
                bad = []
            if bad:
                self._print("\n" + self._c("dim", "Not shown (name is not 8.3 or has no name): " +
                                             ", ".join(bad[:8]) + (" ..." if len(bad) > 8 else "")))

    def step_one(self):
        """One instruction for the debugger; BDOS and BIOS entries run as part of it."""
        cpu = self.cpu
        while not self.exited:
            pc = cpu.pc
            if pc == BDOS_ENTRY:
                self.bdos()
            elif BIOS_IMPL <= pc < BIOS_END:
                self.bios(pc - BIOS_IMPL)
            else:
                break
        if self.exited:
            return
        cpu.step()
        while not self.exited:
            pc = cpu.pc
            if pc == BDOS_ENTRY:
                self.bdos()
            elif BIOS_IMPL <= pc < BIOS_END:
                self.bios(pc - BIOS_IMPL)
            else:
                break
        self.console.flush()

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
            mhz = self.clock_mhz()
            real = ts / (mhz * 1e6)
            # T-states are Z80 cycle counts; a Z180 needs fewer for many instructions
            real_cpu = ("a %g MHz Z180 would take about" % mhz if self.cpu.z180
                        else "a real %g MHz Z80 takes" % mhz)
            self._print("\n" + self._c("dim", "[time]") + " ran %.2f s on this computer; %s "
                        "%.2f s (%.2fx real speed); %s instructions, %s T-states%s\n" %
                        (dt, real_cpu, real, real / dt if dt else 0, format(self._steps_last, ","),
                         format(ts, ","),
                         "; %.1f s waiting for keys not counted" % wait if wait >= 0.05 else ""))
            self.console.flush()
        return n

    def _ccp_run(self, cmd, args, debug=False):
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
        drive = self.drive          # BDOS "select disk" (e.g. STAT probing every drive)
        self.load(program, args)    # must not change the prompt drive, as on real CP/M
        self._print("\n")
        keymap = self.console.keymap
        self._set_keymap(cmd, program)
        try:
            if debug:
                Debugger(self).loop()
            else:
                self.run_timed()
        finally:
            self.console.keymap = keymap
            self._set_drive(drive)

    _NO_OPEN = {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
                ".msi", ".scr", ".lnk", ".reg", ".sh", ".py", ".pif", ".jar", ".hta", ".dll"}

    @staticmethod
    def _host_open(path):
        """Open a file with the host's default program."""
        import subprocess
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            for tool in ("termux-open", "xdg-open", "wslview"):
                if shutil.which(tool):
                    subprocess.Popen([tool, path], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    return
            raise OSError("no opener found (xdg-open, termux-open ...)")

    def _ccp_open(self, args):
        """OPEN file: show a file of the current drive/user in the host's own program (PDF
        viewer, image viewer ...).  OPEN . opens the folder.  Programs and scripts are refused."""
        if args[0] == ".":
            self.cpu.mem[0x5C] = 0                      # current drive
            path = self._drive_dir(0x5C)
        else:
            self._make_fcb(0x5C, args[0])
            path = self._fcb_path(0x5C)
            if path is None:
                self._print("\n" + self._c("red", "NO FILE"))
                return
            if os.path.splitext(path)[1].lower() in self._NO_OPEN:
                self._print("\n" + self._c("red", "NOT OPENED") +
                            " (programs and scripts are not started from here; run them as CP/M programs)")
                return
        try:
            self._host_open(path)
            self._print("\nOpened " + ("the folder" if args[0] == "." else os.path.basename(path)))
        except OSError as e:
            self._print("\n" + self._c("red", "CANNOT OPEN") + " %s" % e)

    def _ccp_debug_empty(self):
        self._init_memory()
        self.exited = False
        self.cpu.reset()
        self.cpu.pc = TPA
        self.cpu.sp = BDOS_BASE
        Debugger(self).loop()

    _FRAME_RE = re.compile(rb"\x1b\[(?:1;1)?H|\x0c")

    def _ccp_play(self, args):
        """PLAY file [fps|baud] [RAW]: send a terminal/ANSI stream to the screen.  A number up to
        240 is frames per second (a frame starts at ESC[H, ESC[1;1H or a form feed);
        a larger number is a baud rate (10 bits per byte).  Default: 30 fps when the file
        has frames, 57600 baud for a SAUCE ANSiMation, else 9600 baud.  Stops at ^Z (a SAUCE record may follow), or on q/Esc."""
        import time
        self._make_fcb(0x5C, args[0])
        path = self._fcb_path(0x5C)
        if path is None:
            self._print("\n" + self._c("red", "NO FILE"))
            return
        rate = None
        raw = any(a.upper() == "RAW" for a in args[1:])
        args = [a for a in args if a.upper() != "RAW"]
        if len(args) > 1:
            try:
                rate = float(args[1].lower().rstrip("bfps"))
            except ValueError:
                self._print("\n" + self._c("red", "BAD RATE %s" % args[1]))
                return
            if rate <= 0:
                self._print("\n" + self._c("red", "BAD RATE %s" % args[1]))
                return
        con = self.console
        with open(path, "rb") as f:
            head = f.read(1 << 20)
            f.seek(0)
            if rate is None:
                rate = 30.0 if len(self._FRAME_RE.findall(head)) >= 2 else 9600.0
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 128))
                tail = f.read()
                f.seek(0)
                if tail[:5] == b"SAUCE" and tail[94:96] == b"\x01\x02":
                    rate = 57600.0      # SAUCE says ANSiMation: cursor moves, no frame marker
            frames = rate <= 240
            scr = None if (frames or raw) else AnsiScreen()
            t0 = time.perf_counter()
            sent = 0                                    # frames (or bytes) sent
            buf = b""
            done = False
            con.write_bytes(b"\x1b[2J\x1b[H\x1b[?25l")
            try:
                while not done:
                    data = f.read(1 << 20)
                    if not data:
                        done = True
                    buf += data
                    z = buf.find(b"\x1a")
                    if z >= 0:
                        buf, done = buf[:z], True
                    if frames:
                        cuts = [m.start() for m in self._FRAME_RE.finditer(buf)]
                        segs = [buf[a:b] for a, b in zip([0] + cuts, cuts + [len(buf)]) if b > a]
                        if not done and segs:
                            buf = segs.pop()            # the last one may be incomplete
                        else:
                            buf = b""
                    else:
                        step = max(1, int(rate / 10 * 0.02))
                        segs = [buf[i:i + step] for i in range(0, len(buf), step)]
                        if not done and segs and len(segs[-1]) < step:
                            buf = segs.pop()
                        else:
                            buf = b""
                    for seg in segs:
                        if scr:
                            scr.feed(seg)
                            con.write_bytes(scr.render())
                        else:
                            con.write_bytes(seg)
                        sent += 1 if frames else len(seg)
                        due = t0 + (sent / rate if frames else sent * 10 / rate)
                        wait = due - time.perf_counter()
                        if wait > 0.002:
                            time.sleep(wait)
                        if con.has_input():
                            k = con.read_char()
                            if k in (0x71, 0x51, 0x1B, 0x03, None):
                                done = True
                                break
            finally:
                con.write_bytes(b"\x1b[0m\x1b[?25h" + (b"\x1b[%d;1H" % (scr.rows + 1) if scr else b"") + b"\r\n")

    _SPEC_RE = re.compile(r"^(?:([A-Za-z])(\d{1,2})?:)?(.*)$")

    def _spec_dir(self, letter, user):
        """Host folder of drive `letter` (None = current) in user area `user` (None = current)."""
        letter = (letter or chr(65 + self.drive)).upper()
        base = self.drives.get(letter)
        if base is None or (user is not None and user > 15):
            return None, letter
        if self._users.get(letter):
            return os.path.join(base, str(self.user if user is None else user)), letter
        return base, letter

    def _ccp_copy(self, src, dst):
        """COPY src [dst]: src may have wildcards; dst is a drive (B:), a drive and user
        (B3:), a name or a name pattern (*.BAK).  Existing files are replaced."""
        sm, dm = self._SPEC_RE.match(src), self._SPEC_RE.match(dst or "")
        sdir, sl = self._spec_dir(sm.group(1), int(sm.group(2)) if sm.group(2) else None)
        ddir, dl = self._spec_dir(dm.group(1), int(dm.group(2)) if dm.group(2) else None)
        if sdir is None or ddir is None:
            self._print("\n" + self._c("red", "NO DRIVE OR USER AREA %s:" % (sl if sdir is None else dl)))
            return
        pat = self._parse_fcb(sm.group(3))[1:]
        dpat = self._parse_fcb(dm.group(3))[1:] if dm.group(3) else None
        if dpat is not None and not dpat[:8].strip():
            self._print("\n" + self._c("red", "BAD DESTINATION"))
            return
        files = []
        try:
            for entry in sorted(os.listdir(sdir), key=str.upper):
                fn = self._to_83(entry)
                if (fn and os.path.isfile(os.path.join(sdir, entry)) and
                        all(p == 0x3F or p == c for p, c in zip(pat, fn))):
                    files.append((entry, fn))
        except OSError:
            pass
        if not files:
            self._print("\n" + self._c("red", "NO FILE"))
            return
        if dpat is not None and len(files) > 1 and 0x3F not in dpat:
            self._print("\n" + self._c("red", "SEVERAL FILES: GIVE A DRIVE OR A PATTERN AS DESTINATION"))
            return
        os.makedirs(ddir, exist_ok=True)
        done = 0
        for entry, fn in files:
            out = fn if dpat is None else bytes(c if p == 0x3F else p for p, c in zip(dpat, fn))
            name = out[:8].decode("latin1").rstrip().lower()
            ext = out[8:].decode("latin1").rstrip().lower()
            host = name + ("." + ext if ext else "")
            target = os.path.join(ddir, host)
            for existing in os.listdir(ddir):          # reuse the name's spelling if present
                if existing.lower() == host:
                    target = os.path.join(ddir, existing)
            source = os.path.join(sdir, entry)
            if os.path.abspath(source) == os.path.abspath(target):
                self._print("\n%s: SAME FILE" % host.upper())
                continue
            shutil.copyfile(source, target)
            self._print("\n%s -> %s%s:%s" % (entry.upper(), dl, "" if not self._users.get(dl)
                        else (dm.group(2) if dm.group(2) else str(self.user)), host.upper()))
            done += 1
        self._print("\n%d file%s copied" % (done, "" if done == 1 else "s"))

    # ----------------------------------------------------------------- IMAGE
    def _host_file(self, name):
        """A host file by (case-insensitive) name in the current drive folder, then the
        working directory, then as given."""
        for base in (self._spec_dir(None, None)[0], os.getcwd()):
            try:
                for e in os.listdir(base or "."):
                    if e.lower() == name.lower() and os.path.isfile(os.path.join(base, e)):
                        return os.path.join(base, e)
            except OSError:
                pass
        return name if os.path.isfile(name) else None

    def _ccp_image(self, args):
        """IMAGE file [format[:slice]] [GET [n:]pattern [dest]]: list, or copy files out of, a
        raw CP/M disk image (floppy or RomWBW hard disk slices).  IMAGE ? lists the formats."""
        import diskimg
        defs = {}
        dd = self._host_file("diskdefs")
        if dd:
            with open(dd, errors="replace") as f:
                defs = diskimg.parse_diskdefs(f.read())
        if not args or args[0] == "?":
            self._print("\nFormats (IMAGE file name[:slice]):")
            for f in dict(diskimg.BUILTIN, **defs).values():
                self._print("\n  %-10s %4d tracks x %2d sectors x %3d bytes, %4d dir entries, %dK blocks"
                            % (f.name, f.tracks, f.sectrk, f.seclen, f.maxdir, f.blocksize // 1024))
            self._print("\nWithout a format it is guessed.  A cpmtools DISKDEFS file here adds more.")
            return
        path = self._host_file(args[0])
        if path is None:
            self._print("\n" + self._c("red", "NO FILE %s" % args[0]))
            return
        rest = list(args[1:])
        spec = rest.pop(0) if rest and rest[0].upper() != "GET" else None
        with open(path, "rb") as f:
            data = f.read()
        if spec:
            fmt, sl = diskimg.resolve(spec, defs)
            if fmt is None:
                self._print("\n" + self._c("red", "UNKNOWN FORMAT %s  (IMAGE ? lists them)" % spec))
                return
            img = diskimg.Image(data, fmt, sl)
        else:
            img = diskimg.detect(data, defs)
            if img is None:
                self._print("\n" + self._c("red", "FORMAT? None of the known formats fits; try IMAGE ? or DISKDEFS"))
                return
        f = img.fmt
        if not img.fits():
            self._print("\n" + self._c("red", "Image is too small for %s" % f.name))
            return
        files = img.files()
        if rest and rest[0].upper() == "GET":
            self._image_get(img, files, rest[1:])
            return
        self._print("\nImage %s: %s, %d files" % (os.path.basename(path), f.name, len(files)))
        for user in sorted({x.user for x in files}):
            self._print("\nUser %d:" % user)
            col = 0
            for x in sorted((x for x in files if x.user == user), key=lambda x: x.name):
                self._print("%s%-12s%4dK%s" % ("\n  " if col == 0 else "  ", x.name,
                                              -(-x.size // 1024), "*" if "R" in x.attrs else " "))
                col = (col + 1) % 3
        self._print("\n(* = read-only.  IMAGE %s GET [n:]pattern [dest] copies files out.)" % args[0])

    def _image_get(self, img, files, args):
        if not args:
            self._print("\n" + self._c("red", "GET what?  GET [n:]pattern [drive[user]:]"))
            return
        user, pat = 0, args[0]
        m = re.match(r"^(\d{1,2}):(.*)$", pat)
        if m:
            user, pat = int(m.group(1)), m.group(2)
        pb = self._parse_fcb(pat.upper())[1:]
        dm = self._SPEC_RE.match(args[1] if len(args) > 1 else "")
        ddir, dl = self._spec_dir(dm.group(1), int(dm.group(2)) if dm.group(2) else None)
        if ddir is None:
            self._print("\n" + self._c("red", "NO DRIVE OR USER AREA %s:" % dl))
            return
        done = 0
        for x in files:
            if x.user != user:
                continue
            nm, _, ext = x.name.partition(".")
            fn = (nm.ljust(8) + ext.ljust(3)).encode("latin1")
            if not all(p == 0x3F or p == c for p, c in zip(pb, fn)):
                continue
            if not done:
                os.makedirs(ddir, exist_ok=True)
            host = x.name.lower()
            target = os.path.join(ddir, host)
            for existing in os.listdir(ddir):
                if existing.lower() == host:
                    target = os.path.join(ddir, existing)
            with open(target, "wb") as out:
                out.write(img.read(x))
            self._print("\n%-12s %6d bytes -> %s:" % (x.name, x.size, dl))
            done += 1
        self._print("\n%d file%s copied from user %d" % (done, "" if done == 1 else "s", user)
                    if done else "\n" + self._c("red", "NO FILE"))

    # ------------------------------------------------------------------ VIEW
    def _ccp_view(self, path, name, mode="auto"):
        """Pager for files of any size: the file is memory-mapped and only an array of
        line starts is kept, so it is not limited by the 64K of CP/M.  Binary files (and
        mode "hex") are shown as a hex dump; the x key switches between the two views."""
        import mmap, shutil
        from array import array
        from bisect import bisect_right
        size = os.path.getsize(path)
        if size == 0:
            self._print("\n" + self._c("red", "EMPTY FILE"))
            return
        out = self.console
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            head = bytes(mm[:4096]).split(b"\x1a")[0].rstrip(b"\0")   # ^Z / NUL padding is not binary
            binary = b"\0" in head or sum(1 for c in head if c < 32 and c not in b"\t\r\n\f\x1b\x08") * 10 > len(head)
            hexmode = [mode == "hex" or (mode == "auto" and binary)]
            end = mm.find(b"\x1a")
            end = size if end < 0 or hexmode[0] else end
            starts = array("Q", [0])
            pos = 0
            while pos < end:                      # index in 16 MB blocks, in C speed
                parts = mm[pos:min(end, pos + (16 << 20))].split(b"\n")
                p = pos
                for part in parts[:-1]:
                    p += len(part) + 1
                    starts.append(p)
                pos += sum(map(len, parts)) + len(parts) - 1
            if starts[-1] >= end:                 # text ends with a newline
                starts.pop()
            ntext = len(starts)
            nhex = (size + 15) // 16

            def line(i):
                if hexmode[0]:
                    raw = mm[i * 16:i * 16 + 16]
                    hx = " ".join("%02x" % c for c in raw[:8]) + "  " + " ".join("%02x" % c for c in raw[8:])
                    asc = "".join(chr(c) if 32 <= c < 127 else "." for c in raw)
                    return ("%08x  %-48s |%s|" % (i * 16, hx, asc)).encode("latin-1")
                e = starts[i + 1] if i + 1 < ntext else end
                return mm[starts[i]:e].rstrip(b"\r\n")

            def emit(text):
                for ch in text:
                    out.write(ord(ch))

            def cells(raw, col, width):
                res, x = [], 0
                for b in raw:
                    if b == 9:
                        w = 8 - x % 8
                        piece = [" "] * w
                    elif b < 32:
                        piece = ["^", chr(b + 64)]
                    elif b == 127:
                        piece = ["^", "?"]
                    else:
                        piece = [chr(b)]
                    for p in piece:
                        if col <= x < col + width:
                            res.append(p)
                        x += 1
                return "".join(res)

            top, col, last, msg = 0, 0, "", ""
            while True:
                n = nhex if hexmode[0] else ntext
                cols, rows = shutil.get_terminal_size((80, 24))
                page = max(1, rows - 1)
                top = max(0, min(top, max(0, n - page)))
                buf = ["\x1b[H"]
                for r in range(page):
                    i = top + r
                    buf.append((cells(line(i), col, cols) if i < n else "~") + "\x1b[K\r\n")
                shown = min(n, top + page)
                pct = 100 * shown // n
                stat = " %s  %s %d-%d of %d  %d%%  %s" % (name, "rows" if hexmode[0] else "lines", top + 1, shown, n, pct, msg)
                buf.append("\x1b[7m" + stat[:cols - 1].ljust(cols - 1) + "\x1b[0m")
                emit("".join(buf))
                out.flush()
                msg = ""
                k = out.read_char()
                if k is None or k in (0x1B, 0x71, 0x51):             # Esc q Q
                    break
                if k in (0x20, 0x03, 0x66):                          # space ^C f
                    top += page
                elif k in (0x12, 0x62):                              # ^R b
                    top -= page
                elif k in (0x0D, 0x18, 0x6A, 0x0A):                  # Enter ^X j
                    top += 1
                elif k in (0x05, 0x6B):                              # ^E k
                    top -= 1
                elif k == 0x04 or k == 0x6C:                         # ^D l
                    col += 8
                elif k == 0x13 or k == 0x68:                         # ^S h
                    col = max(0, col - 8)
                elif k == 0x11:                                      # ^Q then S / D (Home, End)
                    k2 = out.read_char()
                    if k2 in (0x13, 0x53, 0x73):
                        top, col = 0, 0
                    elif k2 in (0x04, 0x44, 0x64):
                        top = n
                elif k in (0x78, 0x58):                              # x: text <-> hex view
                    if hexmode[0]:
                        top = max(0, bisect_right(starts, top * 16) - 1)
                    else:
                        top = starts[min(top, ntext - 1)] // 16
                    hexmode[0] = not hexmode[0]
                    col = 0
                elif k in (0x67,):                                   # g
                    top, col = 0, 0
                elif k == 0x47:                                      # G
                    top = n
                elif k in (0x2F, 0x6E, 0x4E):                        # / n N
                    if k == 0x2F:
                        emit("\x1b[%d;1H\x1b[K/" % rows)
                        out.flush()
                        text = b""
                        while True:
                            c = out.read_char()
                            if c in (None, 0x0D, 0x0A):
                                break
                            if c == 0x1B:
                                text = b""
                                break
                            if c in (0x08, 0x7F):
                                if text:
                                    text = text[:-1]
                                    emit("\b \b")
                            elif c >= 32:
                                text += bytes([c])
                                out.write(c)
                            out.flush()
                        if text:
                            last = text
                    if not last:
                        continue
                    pat = last.lower()
                    found = None
                    step = -1 if k == 0x4E else 1
                    first = top if k == 0x2F else top + step
                    if hexmode[0]:                    # search the bytes, case ignored
                        rx = re.compile(re.escape(last), re.I)
                        if step > 0:
                            m = rx.search(mm, first * 16) or rx.search(mm, 0)
                            found = m.start() // 16 if m else None
                        else:
                            hits = [m.start() // 16 for m in rx.finditer(mm)]
                            before = [h for h in hits if h < top]
                            found = before[-1] if before else (hits[-1] if hits else None)
                    else:
                        for j in range(n):            # wraps round the file
                            i = (first + step * j) % n
                            if pat in line(i).lower():
                                found = i
                                break
                    if found is None:
                        msg = "Not found: " + last.decode("latin-1")
                    else:
                        top = found
            emit("\x1b[%d;1H\x1b[K" % rows)
            out.flush()

    _HELP = {
        "DIR": ("DIR [spec]", "List files. Wildcards: ? matches one character, * the rest of\n"
                "the name or type.   DIR   DIR *.COM   DIR B:*.TXT   DIR A*.*\n"
                "Another user area: DIR C3:   DIR C3:*.COM   DIR 3:   From the host: cpm.py -c \"DIR C3:\""),
        "ERA": ("ERA spec", "Delete files.   ERA OLD.TXT   ERA *.BAK"),
        "REN": ("REN new=old", "Rename a file.   REN NEW.TXT=OLD.TXT"),
        "COPY": ("COPY source [destination]  (also CP)", "Copy files.   COPY A.TXT B:   COPY *.COM B3:   COPY A.TXT B.TXT   COPY *.TXT *.BAK\n"
                 "The destination is a drive (B:), a drive and user area (B3:), a name or a pattern;\n"
                 "with no destination the files go to the current drive and user area (same name =\n"
                 "SAME FILE).  Existing files are replaced.  Several sources need a drive or a pattern."),
        "VIEW": ("VIEW file [HEX]", "Page through a file of any size.  Space/b or PgDn/PgUp: page;\n"
                 "Enter or Down/Up (j/k): line;  Left/Right: sideways;  g/G or Home/End: start/end;\n"
                 "/text: search (case ignored), n/N: next/previous;  q or Esc: quit.\n"
                 "Binary files open as a hex dump; x switches hex/text, VIEW file HEX forces hex."),
        "TYPE": ("TYPE file", "Show a text file on the screen.   TYPE READ.ME"),
        "SPEED": ("SPEED [MHz|FULL]", "Run programs no faster than a real CPU at that clock (SPEED 4, SPEED 18.432),\n"
                  "so games and delay loops behave as on the real machine; FULL = as fast as possible\n"
                  "(default).  Only slows down: about 25 MHz is the most this computer manages.  Also --speed."),
        "TIME": ("TIME [ON|OFF|MHz]", "Show timing after every program run: time on this computer, instructions,\n"
                 "T-states and the time a real CPU would need: 4 MHz for a Z80, 18.432 MHz for a Z180,\n"
                 "or TIME 6.144 (turns timing on at that clock).  Also --time and --mhz."),
        "IMAGE": ("IMAGE file [format[:slice]] [GET [n:]pattern [dest]]",
                  "List the files of a raw CP/M disk image (an .img of a floppy or a RomWBW hard disk\n"
                  "slice, read only), or copy them out into the current or a given drive and user.\n"
                  "The format is guessed unless named (hd1k:2 = slice 2).  IMAGE ? lists the formats;\n"
                  "a cpmtools DISKDEFS file in the folder adds more.   IMAGE RC.IMG GET 0:*.COM B3:"),
        "PLAY": ("PLAY file [fps|baud] [RAW]", "Show an ANSI/terminal stream (BBS art, ANSI animations such as Bad Apple).\n"
                 "A number up to 240 is frames per second (frames start at ESC[H); a larger one\n"
                 "is a modem speed such as 9600.  Default: 30 fps for animations, 57600 baud for a\n"
                 "SAUCE ANSiMation, else 9600 baud.  RAW sends a baud-rate stream as it is instead\n"
                 "of drawing it through an 80x25 screen.  q or Esc stops.  Start with --charset cp437\n"
                 "for the IBM block characters."),
        "CPU": ("CPU [Z80|Z180]", "Show or switch the processor (also --cpu).  Z180 adds MLT, TST, TSTIO,\n"
                "IN0/OUT0, OTIM/OTDM(R), SLP and the MMU: 1 MB of memory banked in 4K pages\n"
                "through CBAR (3Ah), BBR (39h) and CBR (38h).  Each program starts with the plain 64K."),
        "Z3": ("Z3 [ON|OFF]", "ZCPR3 environment for Z-System tools (Z3LOC, ZSHOW, SETPATH, ...): the\n"
               "256-byte environment descriptor with a path, wheel byte, message buffers and an\n"
               "ANSI terminal capability block, below a lowered BDOS address.  There is no ZCPR3\n"
               "command processor, so each tool is started on its own.  Also --z3."),
        "DEBUG": ("DEBUG [file [args]]  (also DBG)", "Monitor/debugger: load a program and look at it before it runs.  L disassemble\n"
                  "(Z80, Z180 with --cpu z180), D dump, R registers (R HL 1234 sets one), T [n] trace n\n"
                  "instructions, P step over CALL/loops, G go, B/BC breakpoints, E/F enter/fill bytes,\n"
                  "H help, Q leave.  Numbers are hex.  DEBUG alone gives an empty memory to E-nter code in."),
        "OPEN": ("OPEN file | OPEN .", "Open a file of the current drive and user area in the host's own program, such as\n"
                 "a PDF manual or a picture:   OPEN Z80ASM.PDF.   OPEN . opens the folder.  Programs\n"
                 "and scripts (.EXE, .COM, .BAT, .PS1 ...) are refused."),
        "KEYMAP": ("KEYMAP [AUTO|WS|BBC|BBC3]", "Codes sent by the cursor keys, Home/End, Delete and Insert.  WS: WordStar\n"
                   "(^E ^X ^S ^D ...) for TE, WordStar, ...  BBC: the keys BBC BASIC expects\n"
                   "(^G ^O ^J ^L, ^F ^N, ^R, ^Q).  BBC3: BBC BASIC 3.00 (no cursor editing; Backspace\n"
                   "sends DEL).  AUTO (default) uses BBC/BBC3 for programs named BBC*, by version.\n"
                   "On a phone: Alt+letter (or ESC then letter, sent together) is Ctrl+letter."),
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
                 "DRIVE/0, DRIVE/1 ... on the host (a folder 0 turns them on; the others are made\n"
                 "when a file is first written there).  On a drive without them every user area\n"
                 "shows the same files.  The prompt shows it: A0>, A3>"),
        "d:": ("A: B: ... P:  or  B3:", "Switch the current drive, e.g. B:, or drive and user area: B3: (3: keeps the drive)"),
        "HELP": ("HELP [command]  or  ?", "This list, or details about one command.   HELP KEYS"),
        "EXIT": ("EXIT  (also BYE, QUIT)", "Leave the emulator."),
        "RUN": ("NAME [args]", "Any other word runs NAME.COM from the current drive/user,\n"
                "passing the arguments on the command tail:   MBASIC   DUMP FILE.TXT\n"
                "B:PROG runs PROG.COM from drive B. If NAME.COM is not in the current\n"
                "user area it is also looked for in user 0, then on drive A: (user 0).\n"
                "Try  DIR *.COM  to see what is there."),
        "KEYS": ("Keyboard", "Ctrl+] or F12       stop the emulator immediately (unsaved work is lost);\n"
                 "                   F12 is for keyboards where ] needs AltGr (Finnish, German ...)\n"
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
            key = {"BYE": "EXIT", "QUIT": "EXIT", "?": "HELP", "CP": "COPY", "DBG": "DEBUG"}.get(key, key)
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
            "\n  SPEED [MHz]     run at a real clock"
            "\n  COPY src [dst]  copy files (HELP COPY)  VIEW file    page through a file of any size"
            "\n  KEYMAP [mode]   cursor key codes      PLAY file [fps] show an ANSI stream/animation"
            "\n  IMAGE file ...  list/extract files of a CP/M disk image"
            "\n  DEBUG [file]    disassemble, trace, breakpoints   Z3 [ON|OFF]  ZCPR3 environment"
            "\n  CPU [Z80|Z180]  show or switch the processor"
            "\n  OPEN file       open a file (PDF, picture ...) in the host's own program"
            "\n  HELP [command]  this help (or ?)      EXIT           leave (BYE, QUIT)"
            "\n"
            "\nAnything else runs NAME.COM:  MBASIC   DUMP FILE.TXT   B:PROG"
            "\nHELP DIR, HELP RUN, HELP KEYS ... give details.  Ctrl+] or F12 stops the emulator.")

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
        m = self._DU_RE.match(cmd)
        if m and not m.group(3) and int(m.group(2)) < 16:   # B3: or 3: - drive and user at once
            if m.group(1) and m.group(1) not in self.drives:
                self._print("\n" + self._c("red", "BDOS Err On %s: Select" % m.group(1)))
            else:
                self.user = int(m.group(2))
                self._set_drive(ord(m.group(1)) - 65 if m.group(1) else self.drive)
        elif len(cmd) == 2 and cmd[1] == ":" and "A" <= cmd[0] <= "P":
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
        elif cmd in ("COPY", "CP") and args:
            self._ccp_copy(args[0], args[1] if len(args) > 1 else "")
        elif cmd == "VIEW" and args:
            self._make_fcb(0x5C, args[0])
            path = self._fcb_path(0x5C)
            if path is None:
                self._print("\n" + self._c("red", "NO FILE"))
            elif not self.console._raw:
                self._ccp_command("TYPE " + args[0])      # no keyboard to page with
            else:
                self._ccp_view(path, os.path.basename(path),
                               "hex" if len(args) > 1 and args[1].upper() == "HEX" else "auto")
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
        elif cmd == "SPEED":
            if args and re.fullmatch(r"\d+(\.\d+)?", args[0]) and 0 < float(args[0]) <= 1000:
                self.throttle, self.mhz = True, float(args[0])
            elif args and args[0].upper() in ("FULL", "OFF"):
                self.throttle = False
            self._print("\nSpeed: %s" % ("%g MHz %s" % (self.clock_mhz(), "Z180" if self.cpu.z180 else "Z80")
                                          if self.throttle else "as fast as possible"))
        elif cmd == "TIME":
            if args and args[0].upper() in ("ON", "OFF"):
                self.timing = args[0].upper() == "ON"
            elif args and re.fullmatch(r"\d+(\.\d+)?", args[0]) and 0 < float(args[0]) <= 1000:
                self.timing, self.mhz = True, float(args[0])
            else:
                self.timing = not self.timing
            self._print("\nTiming %s" % ("ON (compared with a %g MHz %s)" % (
                self.clock_mhz(), "Z180" if self.cpu.z180 else "Z80") if self.timing else "OFF"))
        elif cmd == "IMAGE":
            self._ccp_image(text.strip().split()[1:])          # file names keep their case
        elif cmd == "PLAY" and args:
            self._ccp_play(args)
        elif cmd == "KEYMAP":
            if args and args[0].lower() in ("auto", "ws", "bbc", "bbc3"):
                self.keymode = args[0].lower()
            self._print("\nKeymap: %s%s" % (self.keymode.upper(),
                        "  (BBC for programs named BBC*: BBC3 for version 3, else WordStar)" if self.keymode == "auto" else ""))
        elif cmd == "CPU":
            if args and args[0].upper() in ("Z80", "Z180"):
                cls = Z180 if args[0].upper() == "Z180" else Z80
                if type(self.cpu) is not cls:
                    self.cpu = cls(self.cpu.mem)
            self._print("\nCPU: %s" % ("Z180" if self.cpu.z180 else "Z80"))
            if self.cpu.z180:
                self._print("\n  " + self.cpu.mmu_info())
        elif cmd == "Z3":
            if args and args[0].upper() in ("ON", "OFF"):
                self.z3 = args[0].upper() == "ON"
                self._z3_keep = None
            self._print("\nZCPR3 environment: %s" % ("ON" if self.z3 else "OFF"))
            if self.z3:
                self._print("\n  " + z3env.describe())
        elif cmd == "OPEN" and args:
            self._ccp_open(args)
        elif cmd in ("DEBUG", "DBG"):
            self._ccp_run(args[0], args[1:], debug=True) if args else self._ccp_debug_empty()
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
            m = self._DU_RE.match(cmd)
            if m and m.group(3) and int(m.group(2)) < 16:   # C3:PROG - from that user area
                user = self.user
                self.user = int(m.group(2))
                try:
                    self._ccp_run((m.group(1) + ":" if m.group(1) else "") + m.group(3), args)
                finally:
                    self.user = user
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
        self._print("\n" + self._c("dim", self.cpu_line()))
        for letter, names in self._outside_user_areas().items():
            self._print("\n" + self._c("red", "%s: %d file%s outside the user area folders, not visible: %s%s"
                                        % (letter, len(names), "" if len(names) == 1 else "s",
                                           " ".join(names[:5]), " ..." if len(names) > 5 else "")) +
                        "\n   (move them into %s to use them in user 0)" % os.path.join(self.drives[letter], "0"))
        if not self._run_startup():
            return
        while True:
            self._print("\n%s>" % self._c("green", "%s%d" % (chr(65 + self.drive), self.user)))
            line = self.console.read_line()
            if line is None:
                break
            line = bytes(c for c in line if c >= 0x20)       # prompt ignores control keys
            if not self._ccp_line(line.decode("latin1")):
                break

    # ------------------------------------------------------------------ BIOS
    def bios(self, idx):
        cpu = self.cpu
        name = BIOS_ENTRIES[idx]
        if name == "BOOT":                  # cold start: EXIT.COM jumps here to leave
            self.console.flush()
            raise SystemExit(0)
        if name == "WBOOT":
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
                         "(A, B, ...), each optionally split into user areas 0..15 "
                         "(default with no program and no drives: ./disks if present, "
                         "else you are asked whether to create it with A..P, user area 0)")
    ap.add_argument("--charset", "-charset", choices=["fi", "cp437"],
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
    ap.add_argument("--cpu", "-cpu", choices=["z80", "z180"], default="z80",
                    help="z180: also MLT, TST, IN0/OUT0, OTIM/OTDM(R), SLP and the MMU (1 MB)")
    ap.add_argument("--z3", "-z3", action="store_true",
                    help="provide a ZCPR3 environment for Z-System tools (see the Z3 command)")
    ap.add_argument("--command", "-c", metavar="CMDS",
                    help="run prompt commands and exit, e.g. -c \"DIR C0:\" (; separates several)")
    ap.add_argument("--time", "-time", action="store_true",
                    help="after each program run print elapsed time, instruction count and "
                         "the equivalent time on a real CPU (see --mhz)")
    ap.add_argument("--speed", "-speed", type=float, metavar="MHZ",
                    help="run no faster than a real CPU at this clock, e.g. --speed 4 (see SPEED)")
    ap.add_argument("--mhz", "-mhz", type=float, metavar="MHZ",
                    help="clock of the real CPU that --time compares with "
                         "(default 4 for the Z80, 18.432 for the Z180)")
    ns = ap.parse_args(argv)
    if not (ns.disk or ns.dir or ns.drive or ns.program):
        # No disks given: use ./disks if it exists; else offer to create it (A..P, user 0).
        if os.path.isdir("disks"):
            ns.disk = "disks"
        elif sys.stdin.isatty():
            try:
                ans = input("No disks given. Create folder %s with drives A: to P: here? [Y/n] "
                            % os.path.abspath("disks")).strip().lower()
            except EOFError:
                ans = "n"
            if ans in ("", "y", "yes"):
                ns.disk = "disks"
                for c in "ABCDEFGHIJKLMNOP":
                    os.makedirs(os.path.join(ns.disk, c, "0"), exist_ok=True)
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
        if not re.fullmatch(r"[A-Pa-p]:?", letter) or not path:
            ap.error("--drive wants X=FOLDER, e.g. --drive B=/tmp/disk (got %r); "
                     "to start in a drive, use -c \"C0:; PROG\" or -c C0:PROG" % spec)
        drives[letter[0].upper()] = path
    if not drives and not ns.program:
        drives["A"] = "."      # --disk given but empty
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
    cpm = CPM(console=Console(charset=ns.charset, backspace=ns.backspace), drives=drives,
              cpu=Z180() if ns.cpu == "z180" else None)
    cpm.drive, cpm.user = start_drive, start_user
    cpm.timing = ns.time
    cpm.mhz = ns.speed or ns.mhz
    cpm.throttle = bool(ns.speed)
    cpm.z3 = ns.z3
    cpm.startup = ns.startup
    cpm.color = (ns.color == "on" or
                 (ns.color == "auto" and not os.environ.get("NO_COLOR") and
                  hasattr(sys.stdout, "isatty") and sys.stdout.isatty()))
    try:
        if ns.command is not None:
            cpm._ccp_line(ns.command)
            cpm._print("\n")
        elif ns.program:
            cpm.load_file(ns.program, ns.args)
            sys.stderr.write("[%s]\n" % cpm.cpu_line())     # stderr: program output stays clean
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
