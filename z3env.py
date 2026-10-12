"""Minimal ZCPR3 environment for the CP/M emulator (opt-in: --z3 or the Z3 command).

ZCPR3 tools (PATH, Z3LOC, ZSHOW, ...) find the system through a 256-byte environment
descriptor: bytes 00-7F hold the addresses and sizes of the ZCPR3 elements (path,
wheel byte, message buffers, named directories, ...), bytes 80-FF the terminal
capabilities (TCAP).  The emulator has no ZCPR3 command processor, so only the data
structures exist; programs can read and change them, the changes survive from one
program to the next.  The layout follows the usual Z3BASE arrangement, growing
downwards from just below the emulated BDOS.
"""

MEMTOP = 0xFCFF

Z3CLS = 256 - 5
Z3CL = MEMTOP + 1 - Z3CLS - 5                 # multiple command line buffer
Z3ENVS = 2
ENV = (Z3CL - Z3ENVS * 128) & 0xFF00          # environment descriptor (page aligned)
WHEEL = ENV - 1
EXPATHS = 5
EXPATH = WHEEL - EXPATHS * 2 - 1
EXTFCB = EXPATH - 36
MSG = EXTFCB - 80
SHSTKS, SHSIZE = 4, 32
SHSTK = MSG - SHSTKS * SHSIZE
EXTSTK = SHSTK - 48
NDIRS = 25
NDIR = SHSTK - (NDIRS * 18 + 1) // 256 * 256 - 256
FCP = NDIR                                    # FCP, RCP and IOP are not provided
RCP = FCP
IOP = RCP
LOW = NDIR                                    # lowest address used by the structures
BDOS = (LOW - 0x100) & 0xFF00                 # where page 0 says the BDOS lives

_TCAP_NAME = b"ANSI"
# Z3TCAP bytes 0D-12 then the capability strings (cl, cm, ce, so, se, to, te, ...)
_TCAP_HEAD = bytes([0x59, 0x80, 0x14, 0x05, 0x18, 0x04, 0x13, 0, 0, 0])
_TCAP_STRINGS = [b"\x1b[;H\x1b[J", b"\x1b[%i%d;%dH", b"\x1b[K", b"\x1b[1m", b"\x1b[m",
                 b"\x1b[1m", b"\x1b[m", b"\x1b[M", b"\x1b[L", b"\x1b[J", b"\x1b[%Dm",
                 b"0574", b"", b"\x1b(0", b"\x1b(B", b"\x1b[25l", b"\x1b[25h",
                 b"lkmjqxaawvnut"]


def tcap():
    t = _TCAP_NAME.ljust(13) + _TCAP_HEAD
    for s in _TCAP_STRINGS:
        t += s + b"\0"
    return t.ljust(128, b"\0")


def _w(v):
    return bytes([v & 0xFF, v >> 8])


def env_descriptor(cols=80, rows=24):
    """The 256-byte environment descriptor, modelled on NZ-COM's NZCOM.ENV."""
    d = bytearray(256)
    d[0:3] = b"\xC3" + _w(0xFE00)                 # JP to the BIOS
    d[3:8] = b"Z3ENV"
    d[8] = 0x80                                   # extended environment
    d[0x09:0x0C] = _w(EXPATH) + bytes([EXPATHS])
    d[0x0C:0x0F] = _w(RCP) + bytes([0])
    d[0x0F:0x12] = _w(IOP) + bytes([0])
    d[0x12:0x15] = _w(FCP) + bytes([0])
    d[0x15:0x18] = _w(NDIR) + bytes([NDIRS])
    d[0x18:0x1B] = _w(Z3CL) + bytes([Z3CLS])
    d[0x1B:0x1E] = _w(ENV) + bytes([Z3ENVS])
    d[0x1E:0x22] = _w(SHSTK) + bytes([SHSTKS, SHSIZE])
    d[0x22:0x24] = _w(MSG)
    d[0x24:0x26] = _w(EXTFCB)
    d[0x26:0x28] = _w(EXTSTK)
    d[0x28] = 0                                   # quiet flag off
    d[0x29:0x2B] = _w(WHEEL)
    d[0x2B] = 4                                   # processor speed, MHz
    d[0x2C] = 16                                  # highest drive (P)
    d[0x2D] = 31                                  # highest user
    d[0x2E] = 1                                   # DU: form allowed
    d[0x2F:0x31] = b"\0\0"                        # CRT 0, printer 0 selected
    d[0x31:0x34] = bytes([cols, rows, rows - 2])  # CRT 0 columns, lines, text lines
    d[0x34:0x37] = b"\xFF\xFF\x00"
    d[0x37:0x3A] = bytes([0x50, 0x42, 0x3A])      # printer 0 (as in NZCOM.ENV)
    d[0x3A] = 1
    d[0x47:0x52] = b"SH      VAR"                 # shell variable file
    d[0x80:0x100] = tcap()
    return bytes(d)


def initial_image(cols=80, rows=24):
    """(address, bytes) pairs that make up a fresh Z3 system."""
    parts = [(ENV, env_descriptor(cols, rows))]
    parts.append((WHEEL, b"\xFF"))                # wheel on
    path = bytes([1, 15, 1, 0, 0])                # A15: A0: and the terminator
    parts.append((EXPATH, path.ljust(EXPATHS * 2 + 1, b"\0")))
    parts.append((Z3CL, _w(Z3CL + 4) + bytes([Z3CLS, 0, 0])))
    return parts


def describe():
    rows = [("CCP/BDOS", BDOS), ("IOP", IOP), ("RCP", RCP), ("FCP", FCP), ("NDR", NDIR),
            ("SHSTK", SHSTK), ("MSG", MSG), ("EXTFCB", EXTFCB), ("EXPATH", EXPATH),
            ("Z3WHL", WHEEL), ("Z3CL", Z3CL), ("EXTSTK", EXTSTK), ("Z3ENV", ENV)]
    return "  ".join("%s %04X" % r for r in rows)
