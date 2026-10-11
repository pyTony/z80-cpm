#!/usr/bin/env python3
"""
z80sim.py - A Zilog Z80 CPU simulator in pure Python.

Features
  * All documented opcodes plus the common undocumented ones
    (IXH/IXL/IYH/IYL, SLL, DDCB/FDCB register copies, ED NOP slots, ...)
  * Flags including the undocumented X (bit 3) and Y (bit 5) bits
  * T-state (cycle) counting per instruction
  * Maskable interrupts (IM 0/1/2), NMI, EI delay, HALT
  * Pluggable memory (any bytearray-like) and I/O (callbacks)
  * Approximate MEMPTR (WZ) so BIT n,(HL) sets X/Y like real silicon

Usage
    from z80sim import Z80
    cpu = Z80()
    cpu.mem[0:4] = bytes([0x3E, 0x2A, 0x3C, 0x76])   # LD A,42 ; INC A ; HALT
    cpu.run_until_halt()
    print(cpu.a)            # 43

Override Z80.rd / Z80.wr for memory-mapped devices or ROM protection.
For running CP/M programs see cpm.py.
"""

SF, ZF, YF, HF, XF, PF, NF, CF = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01

PARITY = [PF if bin(i).count("1") % 2 == 0 else 0 for i in range(256)]
SZ53 = [(i & 0xA8) | (ZF if i == 0 else 0) for i in range(256)]
SZ53P = [SZ53[i] | PARITY[i] for i in range(256)]

_COND_MASK = (ZF, ZF, CF, CF, PF, PF, SF, SF)   # NZ Z NC C PO PE P M


class Z80:
    def __init__(self, memory=None, io_read=None, io_write=None):
        self.mem = memory if memory is not None else bytearray(0x10000)
        self.io_read = io_read or (lambda port: 0xFF)
        self.io_write = io_write or (lambda port, value: None)
        self.reset()

    # ------------------------------------------------------------------ state
    def reset(self):
        self.r = [0] * 8          # B C D E H L (unused) A
        self.alt = [0] * 8        # shadow set (index 7 = A')
        self.r[7] = self.alt[7] = 0xFF
        self.f = self.f_alt = 0xFF
        self.x = [0xFFFF, 0xFFFF]  # IX, IY
        self.sp = 0xFFFF
        self.pc = 0
        self.i = 0
        self.rr = 0               # R register
        self.iff1 = self.iff2 = False
        self.im = 0
        self.halted = False
        self.wz = 0
        self.xy = None            # active index prefix: None / 0 (IX) / 1 (IY)
        self.irq = False
        self.irq_vector = 0xFF
        self.nmi = False
        self._ei = False
        self.tx = 0               # extra T-states accumulated by fast handlers
        self.tstates = 0

    # convenient register views ---------------------------------------------
    def _pair(i):
        def g(self): return (self.r[i] << 8) | self.r[i + 1]
        def s(self, v): self.r[i] = (v >> 8) & 0xFF; self.r[i + 1] = v & 0xFF
        return property(g, s)
    bc, de, hl = _pair(0), _pair(2), _pair(4)
    del _pair

    @property
    def a(self): return self.r[7]
    @a.setter
    def a(self, v): self.r[7] = v & 0xFF
    @property
    def af(self): return (self.r[7] << 8) | self.f
    @af.setter
    def af(self, v): self.r[7] = (v >> 8) & 0xFF; self.f = v & 0xFF
    @property
    def ix(self): return self.x[0]
    @ix.setter
    def ix(self, v): self.x[0] = v & 0xFFFF
    @property
    def iy(self): return self.x[1]
    @iy.setter
    def iy(self, v): self.x[1] = v & 0xFFFF

    def regs(self):
        f = "".join(c if self.f & m else "-" for c, m in
                    zip("SZYHXPNC", (SF, ZF, YF, HF, XF, PF, NF, CF)))
        return ("PC=%04X SP=%04X AF=%04X BC=%04X DE=%04X HL=%04X IX=%04X IY=%04X "
                "[%s] I=%02X R=%02X IM%d IFF=%d%d T=%d" %
                (self.pc, self.sp, self.af, self.bc, self.de, self.hl,
                 self.x[0], self.x[1], f, self.i, self.rr, self.im,
                 self.iff1, self.iff2, self.tstates))

    # ----------------------------------------------------------- bus access
    def rd(self, addr):
        return self.mem[addr & 0xFFFF]

    def wr(self, addr, v):
        self.mem[addr & 0xFFFF] = v & 0xFF

    def _rd16(self, addr):
        return self.rd(addr) | (self.rd(addr + 1) << 8)

    def _wr16(self, addr, v):
        self.wr(addr, v & 0xFF)
        self.wr(addr + 1, v >> 8)

    def _fetch(self):
        v = self.rd(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def _fetch16(self):
        lo = self._fetch()
        return lo | (self._fetch() << 8)

    def _fetch_op(self):
        self.rr = (self.rr & 0x80) | ((self.rr + 1) & 0x7F)
        return self._fetch()

    def _push(self, v):
        self.sp = (self.sp - 1) & 0xFFFF
        self.wr(self.sp, v >> 8)
        self.sp = (self.sp - 1) & 0xFFFF
        self.wr(self.sp, v & 0xFF)

    def _pop(self):
        lo = self.rd(self.sp)
        self.sp = (self.sp + 1) & 0xFFFF
        hi = self.rd(self.sp)
        self.sp = (self.sp + 1) & 0xFFFF
        return lo | (hi << 8)

    # ------------------------------------------------- register access helpers
    def _get_hl(self):
        return (self.r[4] << 8) | self.r[5] if self.xy is None else self.x[self.xy]

    def _set_hl(self, v):
        if self.xy is None:
            self.r[4] = v >> 8; self.r[5] = v & 0xFF
        else:
            self.x[self.xy] = v

    def _get_rp(self, p):
        r = self.r
        if p == 0: return (r[0] << 8) | r[1]
        if p == 1: return (r[2] << 8) | r[3]
        if p == 2: return self._get_hl()
        return self.sp

    def _set_rp(self, p, v):
        r = self.r
        if p == 0: r[0] = v >> 8; r[1] = v & 0xFF
        elif p == 1: r[2] = v >> 8; r[3] = v & 0xFF
        elif p == 2: self._set_hl(v)
        else: self.sp = v

    def _get8(self, n):
        if self.xy is not None:
            if n == 4: return self.x[self.xy] >> 8
            if n == 5: return self.x[self.xy] & 0xFF
        return self.r[n]

    def _set8(self, n, v):
        if self.xy is not None and (n == 4 or n == 5):
            xv = self.x[self.xy]
            self.x[self.xy] = ((v << 8) | (xv & 0xFF)) if n == 4 else ((xv & 0xFF00) | v)
        else:
            self.r[n] = v

    def _addr_hl(self):
        """Address of the (HL) operand: HL, or IX/IY+d (consumes d)."""
        if self.xy is None:
            return (self.r[4] << 8) | self.r[5]
        d = self._fetch()
        if d > 127:
            d -= 256
        a = (self.x[self.xy] + d) & 0xFFFF
        self.wz = a
        return a

    def _cond(self, y):
        return bool(self.f & _COND_MASK[y]) == bool(y & 1)

    # ------------------------------------------------------------------- ALU
    def _alu(self, y, v):
        a = self.r[7]
        if y < 2:                                   # ADD / ADC
            res = a + v + ((self.f & 1) if y == 1 else 0)
            self.f = (SZ53[res & 0xFF] | ((a ^ v ^ res) & 0x10) |
                      (((a ^ res) & (v ^ res) & 0x80) >> 5) | ((res >> 8) & 1))
            self.r[7] = res & 0xFF
        elif y == 2 or y == 3 or y == 7:            # SUB / SBC / CP
            res = a - v - ((self.f & 1) if y == 3 else 0)
            rr = res & 0xFF
            f = (((a ^ v ^ res) & 0x10) | (((a ^ v) & (a ^ res) & 0x80) >> 5) |
                 NF | ((res >> 8) & 1))
            if y == 7:
                self.f = f | (SZ53[rr] & (SF | ZF)) | (v & 0x28)
            else:
                self.f = f | SZ53[rr]
                self.r[7] = rr
        elif y == 4:
            a &= v
            self.r[7] = a
            self.f = SZ53P[a] | HF
        elif y == 5:
            a ^= v
            self.r[7] = a
            self.f = SZ53P[a]
        else:
            a |= v
            self.r[7] = a
            self.f = SZ53P[a]

    def _inc8(self, v):
        res = (v + 1) & 0xFF
        self.f = ((self.f & CF) | SZ53[res] | (HF if (v & 0xF) == 0xF else 0) |
                  (PF if v == 0x7F else 0))
        return res

    def _dec8(self, v):
        res = (v - 1) & 0xFF
        self.f = ((self.f & CF) | SZ53[res] | (HF if (v & 0xF) == 0 else 0) |
                  (PF if v == 0x80 else 0) | NF)
        return res

    def _add16(self, a, b):
        res = a + b
        self.f = ((self.f & (SF | ZF | PF)) | ((res >> 8) & 0x28) |
                  (((a ^ b ^ res) >> 8) & 0x10) | ((res >> 16) & 1))
        self.wz = (a + 1) & 0xFFFF
        return res & 0xFFFF

    def _adc16(self, b):
        a = self.hl
        res = a + b + (self.f & 1)
        r16 = res & 0xFFFF
        self.f = (((r16 >> 8) & 0xA8) | (ZF if r16 == 0 else 0) |
                  (((a ^ b ^ res) >> 8) & 0x10) |
                  (((a ^ r16) & (b ^ r16) & 0x8000) >> 13) | ((res >> 16) & 1))
        self.wz = (a + 1) & 0xFFFF
        self.hl = r16

    def _sbc16(self, b):
        a = self.hl
        res = a - b - (self.f & 1)
        r16 = res & 0xFFFF
        self.f = (((r16 >> 8) & 0xA8) | (ZF if r16 == 0 else 0) |
                  (((a ^ b ^ res) >> 8) & 0x10) |
                  (((a ^ b) & (a ^ r16) & 0x8000) >> 13) | NF | ((res >> 16) & 1))
        self.wz = (a + 1) & 0xFFFF
        self.hl = r16

    def _daa(self):
        a = self.r[7]
        f = self.f
        nf = f & NF
        corr = 0
        carry = f & CF
        if (f & HF) or (a & 0xF) > 9:
            corr |= 0x06
        if carry or a > 0x99:
            corr |= 0x60
            carry = CF
        res = ((a - corr) if nf else (a + corr)) & 0xFF
        if nf:
            h = HF if (f & HF) and (a & 0xF) < 6 else 0
        else:
            h = HF if (a & 0xF) > 9 else 0
        self.r[7] = res
        self.f = SZ53P[res] | nf | h | carry

    def _shift(self, y, v):
        cin = self.f & 1
        if y == 0:   c = v >> 7; res = ((v << 1) | c) & 0xFF          # RLC
        elif y == 1: c = v & 1;  res = (v >> 1) | (c << 7)            # RRC
        elif y == 2: c = v >> 7; res = ((v << 1) | cin) & 0xFF        # RL
        elif y == 3: c = v & 1;  res = (v >> 1) | (cin << 7)          # RR
        elif y == 4: c = v >> 7; res = (v << 1) & 0xFF                # SLA
        elif y == 5: c = v & 1;  res = (v >> 1) | (v & 0x80)          # SRA
        elif y == 6: c = v >> 7; res = ((v << 1) | 1) & 0xFF          # SLL
        else:        c = v & 1;  res = v >> 1                         # SRL
        self.f = SZ53P[res] | c
        return res

    # ------------------------------------------------------------ interrupts
    def request_irq(self, vector=0xFF):
        """Raise the INT line. The request is consumed when accepted."""
        self.irq = True
        self.irq_vector = vector & 0xFF

    def request_nmi(self):
        self.nmi = True

    def _accept_irq(self):
        self.irq = False
        self.halted = False
        self.iff1 = self.iff2 = False
        self._inc_r()
        self._push(self.pc)
        if self.im == 2:
            self.pc = self._rd16((self.i << 8) | (self.irq_vector & 0xFE))
            t = 19
        elif self.im == 0 and (self.irq_vector & 0xC7) == 0xC7:
            self.pc = self.irq_vector & 0x38           # RST n on the bus
            t = 13
        else:                                          # IM 1 (and IM 0 fallback)
            self.pc = 0x38
            t = 13
        self.wz = self.pc
        self.tstates += t
        return t

    def _inc_r(self):
        self.rr = (self.rr & 0x80) | ((self.rr + 1) & 0x7F)

    # ------------------------------------------------------------- execution
    def step(self):
        """Execute one instruction (or interrupt acceptance). Returns T-states."""
        ei_block = self._ei
        self._ei = False
        if self.nmi:
            self.nmi = False
            self.halted = False
            self.iff2 = self.iff1
            self.iff1 = False
            self._inc_r()
            self._push(self.pc)
            self.pc = self.wz = 0x66
            self.tstates += 11
            return 11
        if self.irq and self.iff1 and not ei_block:
            return self._accept_irq()
        if self.halted:
            self._inc_r()
            self.tstates += 4
            return 4

        self.xy = None
        t = 0
        op = self._fetch_op()
        while op == 0xDD or op == 0xFD:
            self.xy = 0 if op == 0xDD else 1
            t += 4
            op = self._fetch_op()
        if op == 0xCB:
            t += self._cb()
        elif op == 0xED:
            self.xy = None
            t += self._ed()
        else:
            t += self._main(op)
        self.tstates += t
        return t

    def run(self, tstates):
        """Run for at least `tstates` T-states (stops early on HALT)."""
        end = self.tstates + tstates
        while self.tstates < end and not self.halted:
            self.step()

    def run_fast(self, stop_pc=0x10000, max_steps=None):
        """Execute instructions in a tight loop; returns the number executed.

        Stops when PC >= stop_pc (checked before each instruction), when the
        CPU halts, or after max_steps instructions.  Behaves exactly like
        calling step() repeatedly, but common unprefixed opcodes run through
        pre-generated Python handlers (see _build_fast_ops).  Prefixed
        opcodes, interrupts, HALT and EI go through the regular step().
        """
        if stop_pc > 0x10000:
            stop_pc = 0x10000
        budget = (1 << 62) if max_steps is None else max_steps
        if not self._fast_ready():
            n = 0
            while n < budget and self.pc < stop_pc:
                self.step()
                n += 1
                if self.halted:
                    break
            return n
        ops = self._fast_ops
        tt = self._fast_t
        mem = self.mem
        step = self.step
        done = 0
        slow = bool(self.irq or self.nmi or self.halted or self._ei)
        while budget > 0:
            if slow:
                if self.halted or self.pc >= stop_pc:
                    break
                if (mem[self.pc] == 0xED and not (self.irq or self.nmi or self._ei)
                        and mem[(self.pc + 1) & 0xFFFF] in (0xB0, 0xB8, 0xB1, 0xB9)):
                    k = self._block_run(mem[(self.pc + 1) & 0xFFFF], budget)
                    if k:                       # a whole LDIR/LDDR/CPIR/CPDR in one go
                        done += k
                        budget -= k
                        slow = bool(self.irq or self.nmi or self.halted or self._ei)
                        continue
                step()
                done += 1
                budget -= 1
                slow = bool(self.irq or self.nmi or self.halted or self._ei)
                continue
            pc = self.pc
            ts = 0          # base T-states of the instructions run in this segment
            i = 0           # instructions completed in this segment
            try:
                for i in range(budget):
                    if pc >= stop_pc:
                        break
                    op = mem[pc]
                    ts += tt[op]
                    pc = ops[op]((pc + 1) & 0xFFFF)
                else:
                    i = budget
            except BaseException:
                self.pc = pc & 0xFFFF
                raise
            finally:
                # handler flag bits: 0x10000 = needs full step() next,
                # 0x20000 = this iteration executed nothing (prefix opcode)
                if pc & 0x20000:
                    i -= 1
                    ts -= 0              # prefix entries have a base time of 0
                self.rr = (self.rr & 0x80) | ((self.rr + i) & 0x7F)
                self.tstates += ts + self.tx
                self.tx = 0
            done += i
            budget -= i
            if pc & 0x30000:
                slow = True
            pc &= 0xFFFF
            self.pc = pc
            if self.halted or pc >= stop_pc:
                break
        return done

    def _block_run(self, op, budget):
        """Run up to `budget` iterations of LDIR/LDDR/CPIR/CPDR (op = ED's second byte)
        at once, with the same final state step() would give.  Returns the number of
        iterations done, or 0 to leave the instruction to step() (address wrap-around,
        or the instruction would overwrite itself)."""
        mem = self.mem
        pc = self.pc
        r = self.r
        back = op & 8
        bc = self.bc or 0x10000
        k = bc if bc < budget else budget
        hl, de = self.hl, self.de
        # stop at the end of memory; the next call carries on after the wrap-around
        if not back:
            k = min(k, 0x10000 - hl, 0x10000 - de) if op == 0xB0 else min(k, 0x10000 - hl)
        else:
            k = min(k, hl + 1, de + 1) if op == 0xB8 else min(k, hl + 1)
        step = -1 if back else 1
        wz0 = self.wz
        if op in (0xB0, 0xB8):                                 # LDIR / LDDR
            lo = de - k + 1 if back else de
            if lo <= pc + 1 and pc <= lo + k - 1:              # would patch itself:
                if not back and de < pc:                       # run up to the instruction
                    k = pc - de
                elif back and de > pc + 1:
                    k = de - pc - 1
                else:
                    return 0                                   # let step() do it
            if not back:
                d = de - hl
                if 0 < d < k:                                  # overlap: pattern repeats
                    pat = bytes(mem[hl:de])
                    mem[de:de + k] = (pat * (k // d + 1))[:k]
                else:
                    mem[de:de + k] = mem[hl:hl + k]
                last = de + k - 1
            else:
                d = hl - de
                if 0 < d < k:
                    pat = bytes(mem[hl - d + 1:hl + 1])[::-1]
                    mem[de - k + 1:de + 1] = ((pat * (k // d + 1))[:k])[::-1]
                else:
                    mem[de - k + 1:de + 1] = mem[hl - k + 1:hl + 1]
                last = de - k + 1
            v = mem[last]
            self.hl = (hl + step * k) & 0xFFFF
            self.de = (de + step * k) & 0xFFFF
            left = self.bc = (bc - k) & 0xFFFF
            n = (v + r[7]) & 0xFF
            f = (self.f & (SF | ZF | CF)) | (n & 0x08) | ((n & 0x02) << 4)
            if left:
                f |= PF
            self.f = f
            finished = left == 0
        else:                                                  # CPIR / CPDR
            a = r[7]
            if not back:
                j = mem.find(a, hl, hl + k)
                if j >= 0:
                    k = j - hl + 1
            else:
                j = mem.rfind(a, hl - k + 1, hl + 1)
                if j >= 0:
                    k = hl - j + 1
            last = hl + step * (k - 1)
            v = mem[last]
            res = (a - v) & 0xFF
            h = (a ^ v ^ res) & 0x10
            self.hl = (hl + step * k) & 0xFFFF
            left = self.bc = (bc - k) & 0xFFFF
            n = res - (1 if h else 0)
            f = ((self.f & CF) | NF | (res & SF) | (ZF if res == 0 else 0) |
                 h | (n & 0x08) | ((n & 0x02) << 4))
            if left:
                f |= PF
            self.f = f
            finished = left == 0 or res == 0
        if finished:
            self.pc = (pc + 2) & 0xFFFF
            self.tstates += 21 * (k - 1) + 16
            if op in (0xB1, 0xB9):
                self.wz = ((pc + 1 + step) if k > 1 else (wz0 + step)) & 0xFFFF
            elif k > 1:
                self.wz = (pc + 1) & 0xFFFF
        else:
            self.tstates += 21 * k
            self.wz = (pc + 1) & 0xFFFF
        self.rr = (self.rr & 0x80) | ((self.rr + 2 * k) & 0x7F)
        return k

    def _fast_ready(self):
        """Build (or reuse) the fast opcode table; False if rd/wr are overridden."""
        d = self.__dict__
        if type(self).rd is not Z80.rd or type(self).wr is not Z80.wr or "rd" in d or "wr" in d:
            return False
        refs = getattr(self, "_fast_refs", None)
        if refs is None or refs[0] is not self.r or refs[1] is not self.mem or refs[2] is not self.alt:
            self._fast_ops, self._fast_t = _build_fast_ops(self)
            self._fast_refs = (self.r, self.mem, self.alt)
        return True

    def run_until_halt(self, max_steps=10_000_000):
        n = 0
        while not self.halted and n < max_steps:
            self.step()
            n += 1
        return n

    # ------------------------------------------------- unprefixed / DD / FD
    def _main(self, op):
        x = op >> 6
        y = (op >> 3) & 7
        z = op & 7
        p = y >> 1
        q = y & 1
        r = self.r
        indexed = self.xy is not None

        if x == 1:
            if op == 0x76:                                   # HALT
                self.halted = True
                return 4
            if y == 6:                                       # LD (HL),r
                self.wr(self._addr_hl(), r[z])
                return 15 if indexed else 7
            if z == 6:                                       # LD r,(HL)
                r[y] = self.rd(self._addr_hl())
                return 15 if indexed else 7
            self._set8(y, self._get8(z))
            return 4

        if x == 2:                                           # ALU A,r
            if z == 6:
                v = self.rd(self._addr_hl())
                t = 15 if indexed else 7
            else:
                v = self._get8(z)
                t = 4
            self._alu(y, v)
            return t

        if x == 0:
            if z == 0:
                if y == 0:
                    return 4                                 # NOP
                if y == 1:                                   # EX AF,AF'
                    r[7], self.alt[7] = self.alt[7], r[7]
                    self.f, self.f_alt = self.f_alt, self.f
                    return 4
                d = self._fetch()
                if d > 127:
                    d -= 256
                if y == 2:                                   # DJNZ
                    r[0] = (r[0] - 1) & 0xFF
                    if r[0]:
                        self.pc = self.wz = (self.pc + d) & 0xFFFF
                        return 13
                    return 8
                if y == 3 or self._cond(y - 4):              # JR / JR cc
                    self.pc = self.wz = (self.pc + d) & 0xFFFF
                    return 12
                return 7
            if z == 1:
                if q == 0:                                   # LD rp,nn
                    self._set_rp(p, self._fetch16())
                    return 10
                self._set_hl(self._add16(self._get_hl(), self._get_rp(p)))
                return 11
            if z == 2:
                if q == 0:
                    if p == 0 or p == 1:                     # LD (BC/DE),A
                        addr = self._get_rp(p)
                        self.wr(addr, r[7])
                        self.wz = ((addr + 1) & 0xFF) | (r[7] << 8)
                        return 7
                    nn = self._fetch16()
                    if p == 2:                               # LD (nn),HL
                        self._wr16(nn, self._get_hl())
                        self.wz = (nn + 1) & 0xFFFF
                        return 16
                    self.wr(nn, r[7])                        # LD (nn),A
                    self.wz = ((nn + 1) & 0xFF) | (r[7] << 8)
                    return 13
                if p == 0 or p == 1:                         # LD A,(BC/DE)
                    addr = self._get_rp(p)
                    r[7] = self.rd(addr)
                    self.wz = (addr + 1) & 0xFFFF
                    return 7
                nn = self._fetch16()
                self.wz = (nn + 1) & 0xFFFF
                if p == 2:                                   # LD HL,(nn)
                    self._set_hl(self._rd16(nn))
                    return 16
                r[7] = self.rd(nn)                           # LD A,(nn)
                return 13
            if z == 3:                                       # INC/DEC rp
                v = self._get_rp(p)
                self._set_rp(p, (v + 1) & 0xFFFF if q == 0 else (v - 1) & 0xFFFF)
                return 6
            if z == 4:                                       # INC r
                if y == 6:
                    addr = self._addr_hl()
                    self.wr(addr, self._inc8(self.rd(addr)))
                    return 19 if indexed else 11
                self._set8(y, self._inc8(self._get8(y)))
                return 4
            if z == 5:                                       # DEC r
                if y == 6:
                    addr = self._addr_hl()
                    self.wr(addr, self._dec8(self.rd(addr)))
                    return 19 if indexed else 11
                self._set8(y, self._dec8(self._get8(y)))
                return 4
            if z == 6:                                       # LD r,n
                if y == 6:
                    addr = self._addr_hl()
                    self.wr(addr, self._fetch())
                    return 15 if indexed else 10
                self._set8(y, self._fetch())
                return 7
            # z == 7
            a = r[7]
            f = self.f
            if y == 0:                                       # RLCA
                c = a >> 7
                a = ((a << 1) | c) & 0xFF
                self.f = (f & (SF | ZF | PF)) | (a & 0x28) | c
            elif y == 1:                                     # RRCA
                c = a & 1
                a = (a >> 1) | (c << 7)
                self.f = (f & (SF | ZF | PF)) | (a & 0x28) | c
            elif y == 2:                                     # RLA
                c = a >> 7
                a = ((a << 1) | (f & 1)) & 0xFF
                self.f = (f & (SF | ZF | PF)) | (a & 0x28) | c
            elif y == 3:                                     # RRA
                c = a & 1
                a = (a >> 1) | ((f & 1) << 7)
                self.f = (f & (SF | ZF | PF)) | (a & 0x28) | c
            elif y == 4:
                self._daa()
                return 4
            elif y == 5:                                     # CPL
                a ^= 0xFF
                self.f = (f & (SF | ZF | PF | CF)) | (a & 0x28) | HF | NF
            elif y == 6:                                     # SCF
                self.f = (f & (SF | ZF | PF)) | (a & 0x28) | CF
            else:                                            # CCF
                self.f = ((f & (SF | ZF | PF)) | (a & 0x28) |
                          (HF if f & CF else 0) | (0 if f & CF else CF))
            r[7] = a
            return 4

        # x == 3
        if z == 0:                                           # RET cc
            if self._cond(y):
                self.pc = self.wz = self._pop()
                return 11
            return 5
        if z == 1:
            if q == 0:                                       # POP rp2
                v = self._pop()
                if p == 3:
                    r[7] = v >> 8
                    self.f = v & 0xFF
                else:
                    self._set_rp(p, v)
                return 10
            if p == 0:                                       # RET
                self.pc = self.wz = self._pop()
                return 10
            if p == 1:                                       # EXX
                for i in range(6):
                    r[i], self.alt[i] = self.alt[i], r[i]
                return 4
            if p == 2:                                       # JP (HL)
                self.pc = self._get_hl()
                return 4
            self.sp = self._get_hl()                         # LD SP,HL
            return 6
        if z == 2:                                           # JP cc,nn
            nn = self._fetch16()
            self.wz = nn
            if self._cond(y):
                self.pc = nn
            return 10
        if z == 3:
            if y == 0:                                       # JP nn
                self.pc = self.wz = self._fetch16()
                return 10
            if y == 2:                                       # OUT (n),A
                n = self._fetch()
                self.io_write((r[7] << 8) | n, r[7])
                self.wz = ((n + 1) & 0xFF) | (r[7] << 8)
                return 11
            if y == 3:                                       # IN A,(n)
                port = (r[7] << 8) | self._fetch()
                r[7] = self.io_read(port) & 0xFF
                self.wz = (port + 1) & 0xFFFF
                return 11
            if y == 4:                                       # EX (SP),HL
                lo = self.rd(self.sp)
                hi = self.rd(self.sp + 1)
                v = self._get_hl()
                self.wr(self.sp, v & 0xFF)
                self.wr(self.sp + 1, v >> 8)
                self._set_hl((hi << 8) | lo)
                self.wz = (hi << 8) | lo
                return 19
            if y == 5:                                       # EX DE,HL
                r[2], r[4] = r[4], r[2]
                r[3], r[5] = r[5], r[3]
                return 4
            if y == 6:                                       # DI
                self.iff1 = self.iff2 = False
                return 4
            self.iff1 = self.iff2 = True                     # EI
            self._ei = True
            return 4
        if z == 4:                                           # CALL cc,nn
            nn = self._fetch16()
            self.wz = nn
            if self._cond(y):
                self._push(self.pc)
                self.pc = nn
                return 17
            return 10
        if z == 5:
            if q == 0:                                       # PUSH rp2
                if p == 3:
                    self._push((r[7] << 8) | self.f)
                else:
                    self._push(self._get_rp(p))
                return 11
            nn = self._fetch16()                             # CALL nn
            self.wz = nn
            self._push(self.pc)
            self.pc = nn
            return 17
        if z == 6:                                           # ALU A,n
            self._alu(y, self._fetch())
            return 7
        self._push(self.pc)                                  # RST
        self.pc = self.wz = y * 8
        return 11

    # ----------------------------------------------------------------- CB
    def _cb(self):
        r = self.r
        if self.xy is None:
            op = self._fetch_op()
            x = op >> 6; y = (op >> 3) & 7; z = op & 7
            if z == 6:
                addr = (r[4] << 8) | r[5]
                v = self.rd(addr)
                t = 12 if x == 1 else 15
            else:
                addr = None
                v = r[z]
                t = 8
        else:
            d = self._fetch()
            if d > 127:
                d -= 256
            op = self._fetch()                    # not an M1 cycle: R unchanged
            x = op >> 6; y = (op >> 3) & 7; z = op & 7
            addr = (self.x[self.xy] + d) & 0xFFFF
            self.wz = addr
            v = self.rd(addr)
            t = 16 if x == 1 else 19

        if x == 1:                                            # BIT y,v
            b = v & (1 << y)
            src = v if (self.xy is None and z != 6) else (self.wz >> 8)
            f = (self.f & CF) | HF | (src & 0x28)
            if b == 0:
                f |= ZF | PF
            elif y == 7:
                f |= SF
            self.f = f
            return t
        if x == 0:
            res = self._shift(y, v)
        elif x == 2:
            res = v & ~(1 << y) & 0xFF
        else:
            res = v | (1 << y)
        if addr is not None:
            self.wr(addr, res)
            if self.xy is not None and z != 6:
                r[z] = res                                    # undocumented copy
        else:
            r[z] = res
        return t

    # ----------------------------------------------------------------- ED
    def _ed(self):
        r = self.r
        op = self._fetch_op()
        x = op >> 6; y = (op >> 3) & 7; z = op & 7; p = y >> 1; q = y & 1

        if x == 1:
            if z == 0:                                        # IN r,(C)
                bc = self.bc
                v = self.io_read(bc) & 0xFF
                if y != 6:
                    r[y] = v
                self.f = (self.f & CF) | SZ53P[v]
                self.wz = (bc + 1) & 0xFFFF
                return 12
            if z == 1:                                        # OUT (C),r
                self.io_write(self.bc, 0 if y == 6 else r[y])
                self.wz = (self.bc + 1) & 0xFFFF
                return 12
            if z == 2:                                        # SBC/ADC HL,rp
                rp = self._get_rp(p)
                if q == 0:
                    self._sbc16(rp)
                else:
                    self._adc16(rp)
                return 15
            if z == 3:                                        # LD (nn),rp / LD rp,(nn)
                nn = self._fetch16()
                if q == 0:
                    self._wr16(nn, self._get_rp(p))
                else:
                    self._set_rp(p, self._rd16(nn))
                self.wz = (nn + 1) & 0xFFFF
                return 20
            if z == 4:                                        # NEG
                v = r[7]
                r[7] = 0
                self._alu(2, v)
                return 8
            if z == 5:                                        # RETN / RETI
                self.iff1 = self.iff2
                self.pc = self.wz = self._pop()
                return 14
            if z == 6:                                        # IM n
                self.im = (0, 0, 1, 2, 0, 0, 1, 2)[y]
                return 8
            # z == 7
            if y == 0:
                self.i = r[7]; return 9                       # LD I,A
            if y == 1:
                self.rr = r[7]; return 9                      # LD R,A
            if y == 2 or y == 3:                              # LD A,I / LD A,R
                r[7] = self.i if y == 2 else self.rr
                self.f = ((self.f & CF) | SZ53[r[7]] | (PF if self.iff2 else 0))
                return 9
            if y == 4 or y == 5:                              # RRD / RLD
                hl = self.hl
                m = self.rd(hl)
                a = r[7]
                if y == 4:
                    self.wr(hl, ((a << 4) | (m >> 4)) & 0xFF)
                    r[7] = (a & 0xF0) | (m & 0x0F)
                else:
                    self.wr(hl, ((m << 4) | (a & 0x0F)) & 0xFF)
                    r[7] = (a & 0xF0) | (m >> 4)
                self.f = (self.f & CF) | SZ53P[r[7]]
                self.wz = (hl + 1) & 0xFFFF
                return 18
            return 8                                          # NOP
        if x == 2 and z < 4 and y >= 4:
            return self._block(y, z)
        return 8                                              # undefined ED = NOP

    def _block(self, y, z):
        r = self.r
        step = 1 if (y & 1) == 0 else -1
        rep = y >= 6
        hl = self.hl
        if z == 0:                                            # LDI/LDD/LDIR/LDDR
            v = self.rd(hl)
            self.wr(self.de, v)
            self.hl = (hl + step) & 0xFFFF
            self.de = (self.de + step) & 0xFFFF
            bc = self.bc = (self.bc - 1) & 0xFFFF
            n = (v + r[7]) & 0xFF
            f = (self.f & (SF | ZF | CF)) | (n & 0x08) | ((n & 0x02) << 4)
            if bc:
                f |= PF
            self.f = f
            if rep and bc:
                self.pc = (self.pc - 2) & 0xFFFF
                self.wz = (self.pc + 1) & 0xFFFF
                return 21
            return 16
        if z == 1:                                            # CPI/CPD/CPIR/CPDR
            v = self.rd(hl)
            a = r[7]
            res = (a - v) & 0xFF
            h = (a ^ v ^ res) & 0x10
            self.hl = (hl + step) & 0xFFFF
            bc = self.bc = (self.bc - 1) & 0xFFFF
            n = res - (1 if h else 0)
            f = ((self.f & CF) | NF | (res & SF) | (ZF if res == 0 else 0) |
                 h | (n & 0x08) | ((n & 0x02) << 4))
            if bc:
                f |= PF
            self.f = f
            self.wz = (self.wz + step) & 0xFFFF
            if rep and bc and res != 0:
                self.pc = (self.pc - 2) & 0xFFFF
                self.wz = (self.pc + 1) & 0xFFFF
                return 21
            return 16
        if z == 2:                                            # INI/IND/INIR/INDR
            bc = self.bc
            v = self.io_read(bc) & 0xFF
            self.wr(hl, v)
            self.wz = (bc + step) & 0xFFFF
            b = r[0] = (r[0] - 1) & 0xFF
            self.hl = (hl + step) & 0xFFFF
            k = v + ((r[1] + step) & 0xFF)
        else:                                                 # OUTI/OUTD/OTIR/OTDR
            v = self.rd(hl)
            b = r[0] = (r[0] - 1) & 0xFF
            self.io_write(self.bc, v)
            self.wz = (self.bc + step) & 0xFFFF
            self.hl = (hl + step) & 0xFFFF
            k = v + r[5]
        f = SZ53[b] | (NF if v & 0x80 else 0) | PARITY[(k & 7) ^ b]
        if k > 255:
            f |= HF | CF
        self.f = f
        if rep and b:
            self.pc = (self.pc - 2) & 0xFFFF
            return 21
        return 16


# ----------------------------------------------------------------------------
# Fast opcode handlers.  Each unprefixed opcode gets a small Python function
# h(pc), generated from a source template and compiled once per CPU.  `pc` is
# the address just after the opcode byte; the handler returns the new PC.
# Base T-states come from a parallel table; handlers add any extra time (taken
# branches) to cpu.tx.  A returned PC with bit 16 set asks run_fast to use the
# full step() next (HALT, EI, ...); bit 17 means "nothing was executed" (the
# CB/DD/ED/FD prefixes, which run through step()).
# ----------------------------------------------------------------------------
_HL = "((r[4]<<8)|r[5])"
_FETCH_N = ["n=mem[pc]", "pc=(pc+1)&65535"]
_FETCH_NN = ["nn=mem[pc]|(mem[(pc+1)&65535]<<8)", "pc=(pc+2)&65535"]
_PUSH_PC = ["sp=cpu.sp", "mem[(sp-1)&65535]=pc>>8", "mem[(sp-2)&65535]=pc&255",
            "cpu.sp=(sp-2)&65535"]
_POP_PC = ["sp=cpu.sp", "pc=mem[sp]|(mem[(sp+1)&65535]<<8)", "cpu.sp=(sp+2)&65535"]
_ALU = [
    ["a=r[7]", "res=a+v", "cpu.f=SZ53[res&255]|((a^v^res)&16)|(((a^res)&(v^res)&128)>>5)|((res>>8)&1)",
     "r[7]=res&255"],
    ["a=r[7]", "res=a+v+(cpu.f&1)", "cpu.f=SZ53[res&255]|((a^v^res)&16)|(((a^res)&(v^res)&128)>>5)|((res>>8)&1)",
     "r[7]=res&255"],
    ["a=r[7]", "res=a-v", "rr=res&255",
     "cpu.f=((a^v^res)&16)|(((a^v)&(a^res)&128)>>5)|2|((res>>8)&1)|SZ53[rr]", "r[7]=rr"],
    ["a=r[7]", "res=a-v-(cpu.f&1)", "rr=res&255",
     "cpu.f=((a^v^res)&16)|(((a^v)&(a^res)&128)>>5)|2|((res>>8)&1)|SZ53[rr]", "r[7]=rr"],
    ["a=r[7]&v", "r[7]=a", "cpu.f=SZ53P[a]|16"],
    ["a=r[7]^v", "r[7]=a", "cpu.f=SZ53P[a]"],
    ["a=r[7]|v", "r[7]=a", "cpu.f=SZ53P[a]"],
    ["a=r[7]", "res=a-v", "rr=res&255",
     "cpu.f=((a^v^res)&16)|(((a^v)&(a^res)&128)>>5)|2|((res>>8)&1)|(SZ53[rr]&192)|(v&40)"],
]
_CC_MASK = (64, 64, 1, 1, 4, 4, 128, 128)


def _cc(y):
    """Python expression for condition code y (NZ Z NC C PO PE P M)."""
    return ("cpu.f&%d" if y & 1 else "not cpu.f&%d") % _CC_MASK[y]


def _op_template(op):
    """Return (body_lines, base_tstates); body_lines None = fall back to _main,
    "prefix" = run through step()."""
    x, y, z = op >> 6, (op >> 3) & 7, op & 7
    p, q = y >> 1, y & 1
    R = ["return pc"]
    if op in (0xCB, 0xDD, 0xED, 0xFD):
        return ["return ((pc-1)&65535)|131072"], 0
    if op == 0x76:
        return ["cpu.halted=True", "return pc|65536"], 4
    if op == 0xFB:
        return ["cpu.iff1=cpu.iff2=True", "cpu._ei=True", "return pc|65536"], 4
    if x == 1:
        if y == 6:
            return ["mem[%s]=r[%d]" % (_HL, z)] + R, 7
        if z == 6:
            return ["r[%d]=mem[%s]" % (y, _HL)] + R, 7
        return ["r[%d]=r[%d]" % (y, z)] + R, 4
    if x == 2:
        if z == 6:
            return ["v=mem[%s]" % _HL] + _ALU[y] + R, 7
        return ["v=r[%d]" % z] + _ALU[y] + R, 4
    if x == 0:
        if z == 0:
            if y == 0:
                return R, 4
            if y == 1:
                return ["r[7],alt[7]=alt[7],r[7]", "cpu.f,cpu.f_alt=cpu.f_alt,cpu.f"] + R, 4
            body = ["d=mem[pc]", "pc=(pc+1)&65535"]
            if y == 2:
                return body + ["b=(r[0]-1)&255", "r[0]=b", "if b:",
                               "    if d>127: d-=256", "    pc=cpu.wz=(pc+d)&65535",
                               "    cpu.tx+=5"] + R, 8
            if y == 3:
                return body + ["if d>127: d-=256", "pc=cpu.wz=(pc+d)&65535"] + R, 12
            return body + ["if %s:" % _cc(y - 4), "    if d>127: d-=256",
                           "    pc=cpu.wz=(pc+d)&65535", "    cpu.tx+=5"] + R, 7
        if z == 1:
            if q == 0:
                if p == 3:
                    return _FETCH_NN + ["cpu.sp=nn"] + R, 10
                return _FETCH_NN + ["r[%d]=nn>>8" % (2 * p), "r[%d]=nn&255" % (2 * p + 1)] + R, 10
            src = {0: "(r[0]<<8)|r[1]", 1: "(r[2]<<8)|r[3]", 2: "(r[4]<<8)|r[5]", 3: "cpu.sp"}[p]
            return ["a=(r[4]<<8)|r[5]", "b=" + src, "res=a+b",
                    "cpu.f=(cpu.f&196)|((res>>8)&40)|(((a^b^res)>>8)&16)|((res>>16)&1)",
                    "cpu.wz=(a+1)&65535", "res&=65535", "r[4]=res>>8", "r[5]=res&255"] + R, 11
        if z == 2:
            if q == 0:
                if p < 2:
                    return ["addr=(r[%d]<<8)|r[%d]" % (2 * p, 2 * p + 1), "mem[addr]=r[7]",
                            "cpu.wz=((addr+1)&255)|(r[7]<<8)"] + R, 7
                if p == 2:
                    return _FETCH_NN + ["mem[nn]=r[5]", "mem[(nn+1)&65535]=r[4]",
                                        "cpu.wz=(nn+1)&65535"] + R, 16
                return _FETCH_NN + ["mem[nn]=r[7]", "cpu.wz=((nn+1)&255)|(r[7]<<8)"] + R, 13
            if p < 2:
                return ["addr=(r[%d]<<8)|r[%d]" % (2 * p, 2 * p + 1), "r[7]=mem[addr]",
                        "cpu.wz=(addr+1)&65535"] + R, 7
            if p == 2:
                return _FETCH_NN + ["cpu.wz=(nn+1)&65535", "r[5]=mem[nn]",
                                    "r[4]=mem[(nn+1)&65535]"] + R, 16
            return _FETCH_NN + ["cpu.wz=(nn+1)&65535", "r[7]=mem[nn]"] + R, 13
        if z == 3:
            if p == 3:
                return ["cpu.sp=(cpu.sp%s)&65535" % ("+1" if q == 0 else "-1")] + R, 6
            hi, lo = 2 * p, 2 * p + 1
            if q == 0:
                return ["if r[%d]!=255:" % lo, "    r[%d]+=1" % lo, "else:",
                        "    r[%d]=0" % lo, "    r[%d]=(r[%d]+1)&255" % (hi, hi)] + R, 6
            return ["if r[%d]:" % lo, "    r[%d]-=1" % lo, "else:",
                    "    r[%d]=255" % lo, "    r[%d]=(r[%d]-1)&255" % (hi, hi)] + R, 6
        if z == 4 or z == 5:
            if z == 4:
                calc = ["res=(v+1)&255",
                        "cpu.f=(cpu.f&1)|SZ53[res]|(16 if (v&15)==15 else 0)|(4 if v==127 else 0)"]
            else:
                calc = ["res=(v-1)&255",
                        "cpu.f=(cpu.f&1)|SZ53[res]|(16 if (v&15)==0 else 0)|(4 if v==128 else 0)|2"]
            if y == 6:
                return ["addr=" + _HL, "v=mem[addr]"] + calc + ["mem[addr]=res"] + R, 11
            return ["v=r[%d]" % y] + calc + ["r[%d]=res" % y] + R, 4
        if z == 6:
            if y == 6:
                return _FETCH_N + ["mem[%s]=n" % _HL] + R, 10
            return _FETCH_N + ["r[%d]=n" % y] + R, 7
        if y == 4:
            return None, 0                # DAA
        pre = ["a=r[7]", "f=cpu.f"]
        if y == 0:
            calc = ["c=a>>7", "a=((a<<1)|c)&255", "cpu.f=(f&196)|(a&40)|c"]
        elif y == 1:
            calc = ["c=a&1", "a=(a>>1)|(c<<7)", "cpu.f=(f&196)|(a&40)|c"]
        elif y == 2:
            calc = ["c=a>>7", "a=((a<<1)|(f&1))&255", "cpu.f=(f&196)|(a&40)|c"]
        elif y == 3:
            calc = ["c=a&1", "a=(a>>1)|((f&1)<<7)", "cpu.f=(f&196)|(a&40)|c"]
        elif y == 5:
            calc = ["a^=255", "cpu.f=(f&197)|(a&40)|18"]
        elif y == 6:
            calc = ["cpu.f=(f&196)|(a&40)|1"]
        else:
            calc = ["cpu.f=(f&196)|(a&40)|(16 if f&1 else 1)"]
        return pre + calc + ["r[7]=a"] + R, 4
    # x == 3
    if z == 0:
        return ["if %s:" % _cc(y)] + ["    " + l for l in _POP_PC] + \
               ["    cpu.wz=pc", "    cpu.tx+=6"] + R, 5
    if z == 1:
        if q == 0:
            pop = ["sp=cpu.sp", "lo=mem[sp]", "hi=mem[(sp+1)&65535]", "cpu.sp=(sp+2)&65535"]
            if p == 3:
                return pop + ["r[7]=hi", "cpu.f=lo"] + R, 10
            return pop + ["r[%d]=hi" % (2 * p), "r[%d]=lo" % (2 * p + 1)] + R, 10
        if p == 0:
            return _POP_PC + ["cpu.wz=pc"] + R, 10
        if p == 2:
            return ["return " + _HL], 4
        if p == 3:
            return ["cpu.sp=" + _HL] + R, 6
        return ["for i in (0,1,2,3,4,5):", "    r[i],alt[i]=alt[i],r[i]"] + R, 4   # EXX
    if z == 2:
        return _FETCH_NN + ["cpu.wz=nn", "if %s:" % _cc(y), "    pc=nn"] + R, 10
    if z == 3:
        if y == 0:
            return _FETCH_NN + ["cpu.wz=nn", "return nn"], 10
        if y == 4:
            return ["sp=cpu.sp", "sp1=(sp+1)&65535", "lo=mem[sp]", "hi=mem[sp1]",
                    "mem[sp]=r[5]", "mem[sp1]=r[4]", "r[4]=hi", "r[5]=lo",
                    "cpu.wz=(hi<<8)|lo"] + R, 19
        if y == 5:
            return ["r[2],r[4]=r[4],r[2]", "r[3],r[5]=r[5],r[3]"] + R, 4
        return None, 0
    if z == 4:
        return _FETCH_NN + ["cpu.wz=nn", "if %s:" % _cc(y)] + \
               ["    " + l for l in _PUSH_PC] + ["    pc=nn", "    cpu.tx+=7"] + R, 10
    if z == 5:
        if q == 0:
            if p == 3:
                val = ["v=(r[7]<<8)|cpu.f"]
            else:
                val = ["v=(r[%d]<<8)|r[%d]" % (2 * p, 2 * p + 1)]
            return val + ["sp=cpu.sp", "mem[(sp-1)&65535]=v>>8", "mem[(sp-2)&65535]=v&255",
                          "cpu.sp=(sp-2)&65535"] + R, 11
        return _FETCH_NN + ["cpu.wz=nn"] + _PUSH_PC + ["return nn"], 17
    if z == 6:
        return _FETCH_N + ["v=n"] + _ALU[y] + R, 7
    return _PUSH_PC + ["cpu.wz=%d" % (y * 8), "return %d" % (y * 8)], 11


def _build_fast_ops(cpu):
    """Compile the handler table and base-T-state table for this CPU instance."""
    ns = {}
    ops = []
    tt = []
    for op in range(256):
        body, t = _op_template(op)
        if body is None:
            body = ["cpu.xy=None", "cpu.pc=pc", "cpu.tx+=cpu._main(%d)" % op,
                    "if cpu.irq or cpu.nmi or cpu.halted or cpu._ei: return cpu.pc|65536",
                    "return cpu.pc"]
        src = "def make(cpu, r, alt, mem, SZ53, SZ53P):\n    def h(pc):\n" + \
              "".join("        %s\n" % l for l in body) + "    return h\n"
        exec(compile(src, "<z80 op %02X>" % op, "exec"), ns)
        ops.append(ns["make"](cpu, cpu.r, cpu.alt, cpu.mem, SZ53, SZ53P))
        tt.append(t)
    return ops, tt
