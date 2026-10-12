"""Z80 (and optionally Z180) disassembler: disasm(read, addr) -> (length, text).

`read(addr)` returns the byte at addr.  Zilog mnemonics, hex numbers with an h suffix.
Undocumented forms (IXH/IXL, SLL, DDCB register copies) are shown too, as the
simulator executes them.
"""

R8 = ["B", "C", "D", "E", "H", "L", "(HL)", "A"]
RP = ["BC", "DE", "HL", "SP"]
RP2 = ["BC", "DE", "HL", "AF"]
CC = ["NZ", "Z", "NC", "C", "PO", "PE", "P", "M"]
ALU = ["ADD A,", "ADC A,", "SUB ", "SBC A,", "AND ", "XOR ", "OR ", "CP "]
ROT = ["RLC", "RRC", "RL", "RR", "SLA", "SRA", "SLL", "SRL"]
IM = [0, 0, 1, 2, 0, 0, 1, 2]
BLK = [["LDI", "CPI", "INI", "OUTI"], ["LDD", "CPD", "IND", "OUTD"],
       ["LDIR", "CPIR", "INIR", "OTIR"], ["LDDR", "CPDR", "INDR", "OTDR"]]
Z180_BLK = {0x83: "OTIM", 0x8B: "OTDM", 0x93: "OTIMR", 0x9B: "OTDMR"}


def h8(v):
    s = "%02X" % (v & 0xFF)
    return ("0" + s if s[0] in "ABCDEF" else s) + "h"


def h16(v):
    s = "%04X" % (v & 0xFFFF)
    return ("0" + s if s[0] in "ABCDEF" else s) + "h"


def disasm(read, addr, z180=False):
    pos = [addr & 0xFFFF]

    def fetch():
        b = read(pos[0]) & 0xFF
        pos[0] = (pos[0] + 1) & 0xFFFF
        return b

    def fetch16():
        lo = fetch()
        return lo | fetch() << 8

    def sdisp():
        d = fetch()
        return d - 256 if d > 127 else d

    def rel():
        d = sdisp()
        return h16(pos[0] + d)

    def ind(reg, d):
        return "(%s%s%s)" % (reg, "+" if d >= 0 else "-", h8(abs(d)))

    text = _decode(fetch, fetch16, sdisp, rel, ind, z180)
    return (pos[0] - addr) & 0xFFFF, text


def _decode(fetch, fetch16, sdisp, rel, ind, z180):
    op = fetch()
    idx = None
    if op in (0xDD, 0xFD):
        idx = "IX" if op == 0xDD else "IY"
        op = fetch()
        if op in (0xDD, 0xFD, 0xED):            # prefix with no effect on what follows
            return "DB %s" % h8(0xDD if idx == "IX" else 0xFD)
    if op == 0xCB:
        return _cb(fetch, sdisp, ind, idx)
    if op == 0xED and idx is None:
        return _ed(fetch, fetch16, z180)
    x, y, z = op >> 6, (op >> 3) & 7, op & 7
    p, q = y >> 1, y & 1
    uses_mem = False               # an (HL) operand present => (IX+d), other regs plain

    def reg(i):
        nonlocal uses_mem
        if i == 6:
            if idx:
                uses_mem = True
                return ind(idx, sdisp())
            return "(HL)"
        if idx and not mem_op:
            if i == 4:
                return idx + "H"
            if i == 5:
                return idx + "L"
        return R8[i]

    hl = idx or "HL"

    def rp(i):
        return hl if i == 2 else RP[i]

    def rp2(i):
        return hl if i == 2 else RP2[i]

    # does the instruction have an (HL) operand?  (then H/L stay plain H/L)
    mem_op = (x == 1 and (y == 6 or z == 6) and not (y == 6 and z == 6)) or \
             (x == 2 and z == 6) or (x == 0 and z in (4, 5, 6) and y == 6)
    if x == 0:
        if z == 0:
            if y == 0:
                return "NOP"
            if y == 1:
                return "EX AF,AF'"
            if y == 2:
                return "DJNZ " + rel()
            if y == 3:
                return "JR " + rel()
            return "JR %s,%s" % (CC[y - 4], rel())
        if z == 1:
            if q == 0:
                return "LD %s,%s" % (rp(p), h16(fetch16()))
            return "ADD %s,%s" % (hl, rp(p))
        if z == 2:
            if q == 0:
                return ["LD (BC),A", "LD (DE),A", None, None][p] or (
                    "LD (%s),%s" % (h16(fetch16()), hl) if p == 2 else "LD (%s),A" % h16(fetch16()))
            return ["LD A,(BC)", "LD A,(DE)", None, None][p] or (
                "LD %s,(%s)" % (hl, h16(fetch16())) if p == 2 else "LD A,(%s)" % h16(fetch16()))
        if z == 3:
            return ("INC " if q == 0 else "DEC ") + rp(p)
        if z == 4:
            return "INC " + reg(y)
        if z == 5:
            return "DEC " + reg(y)
        if z == 6:
            r = reg(y)
            return "LD %s,%s" % (r, h8(fetch()))
        return ["RLCA", "RRCA", "RLA", "RRA", "DAA", "CPL", "SCF", "CCF"][y]
    if x == 1:
        if y == 6 and z == 6:
            return "HALT"
        a = reg(y)
        b = reg(z)
        return "LD %s,%s" % (a, b)
    if x == 2:
        return ALU[y] + reg(z)
    if z == 0:
        return "RET " + CC[y]
    if z == 1:
        if q == 0:
            return "POP " + rp2(p)
        return ["RET", "EXX", "JP (%s)" % hl, "LD SP,%s" % hl][p]
    if z == 2:
        return "JP %s,%s" % (CC[y], h16(fetch16()))
    if z == 3:
        if y == 0:
            return "JP " + h16(fetch16())
        if y == 2:
            return "OUT (%s),A" % h8(fetch())
        if y == 3:
            return "IN A,(%s)" % h8(fetch())
        if y == 4:
            return "EX (SP),%s" % hl
        if y == 5:
            return "EX DE,HL"
        return "DI" if y == 6 else "EI"
    if z == 4:
        return "CALL %s,%s" % (CC[y], h16(fetch16()))
    if z == 5:
        if q == 0:
            return "PUSH " + rp2(p)
        return "CALL " + h16(fetch16())
    if z == 6:
        return ALU[y] + h8(fetch())
    return "RST " + h8(y * 8)


def _cb(fetch, sdisp, ind, idx):
    if idx:
        d = sdisp()
        op = fetch()
        mem = ind(idx, d)
    else:
        op = fetch()
        mem = None
    x, y, z = op >> 6, (op >> 3) & 7, op & 7
    target = mem if idx else R8[z]
    copy = "" if (not idx or z == 6) else "," + R8[z]       # undocumented DDCB copy
    if x == 0:
        return "%s %s%s" % (ROT[y], target, copy)
    name = ["BIT", "RES", "SET"][x - 1]
    if x == 1:
        copy = ""
    return "%s %d,%s%s" % (name, y, target, copy)


def _ed(fetch, fetch16, z180):
    op = fetch()
    x, y, z = op >> 6, (op >> 3) & 7, op & 7
    p, q = y >> 1, y & 1
    if z180:
        if x == 0 and z == 0:
            return "IN0 %s,(%s)" % ("F" if y == 6 else R8[y], h8(fetch()))
        if x == 0 and z == 1 and y != 6:
            return "OUT0 (%s),%s" % (h8(fetch()), R8[y])
        if x == 0 and z == 4:
            return "TST " + R8[y]
        if op == 0x64:
            return "TST " + h8(fetch())
        if op == 0x74:
            return "TSTIO " + h8(fetch())
        if x == 1 and z == 4 and q == 1 and op in (0x4C, 0x5C, 0x6C, 0x7C):
            return "MLT " + RP[p]
        if op == 0x76:
            return "SLP"
        if op in Z180_BLK:
            return Z180_BLK[op]
    if x == 1:
        if z == 0:
            return "IN %s,(C)" % ("F" if y == 6 else R8[y])
        if z == 1:
            return "OUT (C),%s" % ("0" if y == 6 else R8[y])
        if z == 2:
            return ("SBC" if q == 0 else "ADC") + " HL," + RP[p]
        if z == 3:
            nn = h16(fetch16())
            return "LD (%s),%s" % (nn, RP[p]) if q == 0 else "LD %s,(%s)" % (RP[p], nn)
        if z == 4:
            return "NEG"
        if z == 5:
            return "RETI" if y == 1 else "RETN"
        if z == 6:
            return "IM %d" % IM[y]
        return ["LD I,A", "LD R,A", "LD A,I", "LD A,R", "RRD", "RLD", "NOP", "NOP"][y]
    if x == 2 and y >= 4 and z <= 3:
        return BLK[y - 4][z]
    return "NOP"                  # undefined ED xx: the Z80 executes it as NOP
