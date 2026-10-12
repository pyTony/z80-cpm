import io
import os
import tempfile
import unittest

from z80sim import Z80, Z180, SF, ZF, HF, PF, NF, CF
from cpm import CPM, Console, BDOS_ENTRY
from z80dis import disasm


def run(code, steps=100000, setup=None):
    cpu = Z80()
    cpu.mem[0:len(code)] = bytes(code)
    if setup:
        setup(cpu)
    cpu.run_until_halt(steps)
    return cpu


class CpuTests(unittest.TestCase):
    def test_add(self):
        cpu = run([0x3E, 0x05, 0x06, 0x07, 0x80, 0x76])
        self.assertEqual(cpu.a, 12)

    def test_add_overflow_flags(self):
        cpu = run([0x3E, 0x7F, 0xC6, 0x01, 0x76])
        self.assertEqual(cpu.a, 0x80)
        self.assertEqual(cpu.f, SF | HF | PF)

    def test_sub_flags(self):
        cpu = run([0x3E, 0x80, 0xD6, 0x01, 0x76])
        self.assertEqual(cpu.a, 0x7F)
        self.assertEqual(cpu.f, 0x28 | HF | PF | NF)

    def test_daa(self):
        cpu = run([0x3E, 0x15, 0xC6, 0x27, 0x27, 0x76])
        self.assertEqual(cpu.a, 0x42)

    def test_djnz_loop_sum(self):
        cpu = run([0x06, 0x0A, 0xAF, 0x80, 0x10, 0xFD, 0x76])
        self.assertEqual(cpu.a, 55)

    def test_ldir(self):
        def setup(cpu):
            cpu.mem[0x100:0x105] = b"HELLO"
        cpu = run([0x21, 0x00, 0x01, 0x11, 0x00, 0x02, 0x01, 0x05, 0x00,
                   0xED, 0xB0, 0x76], setup=setup)
        self.assertEqual(bytes(cpu.mem[0x200:0x205]), b"HELLO")
        self.assertEqual(cpu.bc, 0)

    def test_call_ret(self):
        cpu = run([0x31, 0x00, 0x80, 0xCD, 0x07, 0x00, 0x76, 0x3E, 0x99, 0xC9])
        self.assertEqual(cpu.a, 0x99)

    def test_index_registers(self):
        cpu = run([0xDD, 0x21, 0x00, 0x10, 0xDD, 0x36, 0x02, 0x42,
                   0xDD, 0x7E, 0x02, 0x76])
        self.assertEqual(cpu.mem[0x1002], 0x42)
        self.assertEqual(cpu.a, 0x42)

    def test_cb_rlc(self):
        cpu = run([0x3E, 0x81, 0xCB, 0x07, 0x76])
        self.assertEqual(cpu.a, 0x03)
        self.assertTrue(cpu.f & CF)

    def test_bit_and_set(self):
        cpu = run([0x3E, 0x00, 0xCB, 0xFF, 0xCB, 0x7F, 0x76])   # SET 7,A ; BIT 7,A
        self.assertEqual(cpu.a, 0x80)
        self.assertFalse(cpu.f & ZF)
        self.assertTrue(cpu.f & SF)

    def test_add_hl_carry(self):
        cpu = run([0x21, 0x00, 0x80, 0x01, 0x00, 0x80, 0x09, 0x76])
        self.assertEqual(cpu.hl, 0)
        self.assertTrue(cpu.f & CF)

    def test_push_pop(self):
        cpu = run([0x01, 0x34, 0x12, 0xC5, 0xD1, 0x76])
        self.assertEqual(cpu.de, 0x1234)

    def test_exx_and_ex_af(self):
        cpu = run([0x01, 0x11, 0x11, 0xD9, 0x01, 0x22, 0x22, 0xD9, 0x76])
        self.assertEqual(cpu.bc, 0x1111)

    def test_neg(self):
        cpu = run([0x3E, 0x01, 0xED, 0x44, 0x76])
        self.assertEqual(cpu.a, 0xFF)
        self.assertTrue(cpu.f & CF)

    def test_irq_im1(self):
        cpu = Z80()
        cpu.mem[0:5] = bytes([0xED, 0x56, 0xFB, 0x76, 0x76])    # IM 1 ; EI ; HALT
        cpu.mem[0x38:0x3D] = bytes([0x3E, 0x77, 0xFB, 0xED, 0x4D])  # LD A,77 ; EI ; RETI
        cpu.run_until_halt()
        cpu.request_irq()
        cpu.step()
        self.assertEqual(cpu.pc, 0x38)
        cpu.step(); cpu.step(); cpu.step()
        self.assertEqual(cpu.a, 0x77)

    def test_timing(self):
        cpu = Z80()
        cpu.mem[0:3] = bytes([0x00, 0x3E, 0x01])
        self.assertEqual(cpu.step(), 4)
        self.assertEqual(cpu.step(), 7)
        cpu.pc = 0
        cpu.mem[0:3] = bytes([0xCD, 0x10, 0x00])
        self.assertEqual(cpu.step(), 17)


PROG_HELLO = bytes([0x11, 0x0B, 0x01, 0x0E, 0x09, 0xCD, 0x05, 0x00,
                    0xC3, 0x00, 0x00]) + b"Hello$"


def make_cpm(tmp, stdin="", drives=None):
    out = io.StringIO()
    return CPM(console=Console(io.StringIO(stdin), out), drives=drives or {"A": tmp}), out


class _SlowZ80(Z80):
    """Overriding rd disables the fast handlers, so run_fast uses plain step()."""
    def rd(self, addr):
        return self.mem[addr & 0xFFFF]


class DisasmTests(unittest.TestCase):
    @staticmethod
    def dis(bs, z180=False, addr=0x100):
        m = bytes(bs) + bytes(8)
        return disasm(lambda a: m[a - addr] if 0 <= a - addr < len(m) else 0, addr, z180)

    def test_known_forms(self):
        cases = [([0x18, 0xFE], 2, "JR 0100h"), ([0xDD, 0x7E, 0x05], 3, "LD A,(IX+05h)"),
                 ([0xDD, 0x36, 0xFB, 0x0A], 4, "LD (IX-05h),0Ah"),
                 ([0xFD, 0xCB, 0x02, 0x06], 4, "RLC (IY+02h)"),
                 ([0xDD, 0xCB, 0xFF, 0x00], 4, "RLC (IX-01h),B"),
                 ([0xDD, 0x66, 0x01], 3, "LD H,(IX+01h)"), ([0xDD, 0x64], 2, "LD IXH,IXH"),
                 ([0xED, 0xB0], 2, "LDIR"), ([0xED, 0x43, 0x34, 0x12], 4, "LD (1234h),BC"),
                 ([0xC3, 0xAB, 0x00], 3, "JP 00ABh"), ([0xDD, 0xE9], 2, "JP (IX)"),
                 ([0xCB, 0x7E], 2, "BIT 7,(HL)"), ([0xF7], 1, "RST 30h"),
                 ([0xD3, 0xFE], 2, "OUT (0FEh),A"), ([0xEB], 1, "EX DE,HL"),
                 ([0xED, 0x56], 2, "IM 1"), ([0x10, 0x00], 2, "DJNZ 0102h")]
        for bs, n, text in cases:
            self.assertEqual(self.dis(bs), (n, text), bs)

    def test_z180_only_with_flag(self):
        self.assertEqual(self.dis([0xED, 0x4C], z180=True), (2, "MLT BC"))
        self.assertEqual(self.dis([0xED, 0x4C]), (2, "NEG"))      # undocumented NEG copy on a Z80
        self.assertEqual(self.dis([0xED, 0x38, 0x12], z180=True), (3, "IN0 A,(12h)"))
        self.assertEqual(self.dis([0xED, 0x74, 0x0F], z180=True), (3, "TSTIO 0Fh"))

    def test_lengths_match_the_simulator(self):
        branches = ("JP", "JR", "DJNZ", "CALL", "RET", "RST", "HALT", "SLP")
        repeats = ("INIR", "INDR", "OTIR", "OTDR", "OTIMR", "OTDMR")
        for z180, cls in ((False, Z80), (True, Z180)):
            cpu = cls()
            seqs = []
            for op in range(256):
                seqs.append([op])
                for pre in (0xCB, 0xED, 0xDD, 0xFD):
                    seqs.append([pre, op])
                for pre in (0xDD, 0xFD):
                    seqs.append([pre, 0xCB, 0x05, op])
            for s in seqs:
                cpu.reset()
                cpu.mem[:] = bytes(65536)
                cpu.mem[0x4000:0x4008] = bytes(s) + bytes([0x12, 0x34, 0x56, 0x78])[:8 - len(s)]
                cpu.pc, cpu.sp, cpu.hl, cpu.ix, cpu.iy, cpu.bc = 0x4000, 0x8000, 0x5000, 0x5000, 0x5000, 1
                n, text = disasm(lambda a: cpu.mem[a], 0x4000, z180)
                word = text.split()[0]
                if word in branches or word in repeats or text.startswith("DB") or text in ("RETN", "RETI"):
                    continue
                cpu.step()
                self.assertEqual(cpu.pc - 0x4000, n, (bytes(s).hex(), text, z180))


class Z180Tests(unittest.TestCase):
    PROG = [0x01, 0x34, 0x12,            # LD BC,1234h
            0xED, 0x4C,                  # MLT BC      (12h*34h = 03A8h)
            0x11, 0x0A, 0x0B,            # LD DE,0B0Ah
            0xED, 0x5C,                  # MLT DE      (0Bh*0Ah = 006Eh)
            0x21, 0xFF, 0xFF,            # LD HL,FFFFh
            0xED, 0x6C,                  # MLT HL      (FEh01 = FE01h)
            0x31, 0x05, 0x07,            # LD SP,0705h
            0xED, 0x7C,                  # MLT SP      (07h*05h = 0023h)
            0x3E, 0xF0, 0xED, 0x64, 0x0F,  # LD A,F0h ; TST 0Fh
            0x76]

    def _run(self, fast):
        cpu = Z180()
        cpu.mem[0:len(self.PROG)] = bytes(self.PROG)
        if fast:
            cpu.run_fast()
        else:
            cpu.run_until_halt()
        return cpu

    def test_mlt_and_tst(self):
        for fast in (False, True):
            cpu = self._run(fast)
            self.assertEqual((cpu.bc, cpu.de, cpu.hl, cpu.sp), (0x03A8, 0x006E, 0xFE01, 0x0023))
            self.assertEqual(cpu.a, 0xF0)                    # TST leaves A alone
            self.assertEqual(cpu.f & (ZF | HF | NF | CF), ZF | HF)

    def test_z80_still_has_neg_aliases(self):
        cpu = run([0x3E, 0x01, 0xED, 0x4C, 0x76])             # undocumented NEG on a Z80
        self.assertEqual(cpu.a, 0xFF)

    def test_in0_out0_tstio(self):
        seen = []
        cpu = Z180(io_read=lambda p: 0x81 if p == 0x12 else 0x0F,
                   io_write=lambda p, v: seen.append((p, v)))
        code = [0x06, 0x55,              # LD B,55h
                0xED, 0x01, 0x34,        # OUT0 (34h),B
                0xED, 0x38, 0x12,        # IN0 A,(12h)
                0x0E, 0x20,              # LD C,20h
                0xED, 0x74, 0x0F,        # TSTIO 0Fh   (port C reads 0Fh)
                0x76]
        cpu.mem[0:len(code)] = bytes(code)
        cpu.run_until_halt()
        self.assertEqual(seen, [(0x34, 0x55)])
        self.assertEqual(cpu.a, 0x81)
        self.assertEqual(cpu.f & (ZF | HF | CF), HF)

    def test_mlt_edges_keep_flags(self):
        cpu = Z180()
        code = [0x01, 0xFF, 0xFF, 0x37, 0xED, 0x4C, 0x76]      # LD BC,FFFFh ; SCF ; MLT BC
        cpu.mem[0:len(code)] = bytes(code)
        cpu.run_until_halt()
        self.assertEqual(cpu.bc, 0xFE01)
        self.assertTrue(cpu.f & CF)                              # MLT leaves the flags alone

    def test_tst_flags(self):
        cpu = Z180()
        code = [0x3E, 0x81, 0x37, 0xED, 0x64, 0x80, 0x76]      # LD A,81h ; SCF ; TST 80h
        cpu.mem[0:len(code)] = bytes(code)
        cpu.run_until_halt()
        self.assertEqual(cpu.a, 0x81)
        self.assertEqual(cpu.f & (SF | ZF | HF | NF | CF), SF | HF)   # C cleared, H set

    def test_slp_wakes_on_interrupt(self):
        cpu = Z180()
        code = [0xED, 0x56, 0xFB, 0xED, 0x76]                  # IM 1 ; EI ; SLP
        cpu.mem[0:len(code)] = bytes(code)
        cpu.mem[0x38:0x3A] = bytes([0x3E, 0x2A])               # handler: LD A,2Ah
        cpu.run_until_halt()
        self.assertTrue(cpu.halted)
        cpu.request_irq()
        cpu.step()
        self.assertFalse(cpu.halted)
        self.assertEqual(cpu.pc, 0x38)

    def test_otdmr_goes_downward(self):
        seen = []
        cpu = Z180(io_write=lambda p, v: seen.append((p, v)))
        code = [0x21, 0x02, 0x10, 0x06, 0x03, 0x0E, 0x10, 0xED, 0x9B, 0x76]   # OTDMR
        cpu.mem[0:len(code)] = bytes(code)
        cpu.mem[0x1000:0x1003] = bytes([1, 2, 3])
        cpu.run_until_halt()
        self.assertEqual(seen, [(0x10, 3), (0x0F, 2), (0x0E, 1)])
        self.assertEqual((cpu.r[0], cpu.r[1], cpu.hl), (0, 0x0D, 0x0FFF))

    def test_otimr_and_slp(self):
        seen = []
        cpu = Z180(io_write=lambda p, v: seen.append((p, v)))
        code = [0x21, 0x00, 0x10,        # LD HL,1000h
                0x06, 0x03, 0x0E, 0x08,  # LD B,3 ; LD C,8
                0xED, 0x93,              # OTIMR
                0xED, 0x76]              # SLP
        cpu.mem[0:len(code)] = bytes(code)
        cpu.mem[0x1000:0x1003] = bytes([1, 2, 3])
        cpu.run_until_halt()
        self.assertEqual(seen, [(8, 1), (9, 2), (10, 3)])
        self.assertEqual((cpu.r[0], cpu.r[1], cpu.hl), (0, 11, 0x1003))
        self.assertTrue(cpu.halted)


class FastCoreTests(unittest.TestCase):
    """run_fast must be indistinguishable from repeated step() calls."""

    @staticmethod
    def _state(c):
        return (list(c.r), list(c.alt), c.f, c.f_alt, list(c.x), c.sp, c.pc, c.i,
                c.rr, c.iff1, c.iff2, c.im, c.halted, c.wz, c.tstates, c._ei)

    def test_random_programs_match_step(self):
        import random
        for seed in range(40):
            rnd = random.Random(seed)
            mem = bytearray(rnd.getrandbits(8) for _ in range(0x10000))
            if seed % 4:
                mem = bytearray(bytes(mem).replace(b"\x76", b"\x00"))   # no HALT
            fast, slow = Z80(bytearray(mem)), _SlowZ80(bytearray(mem))
            regs = [rnd.getrandbits(8) for _ in range(8)]
            pc = rnd.randrange(0x10000)
            for c in (fast, slow):
                c.r[:] = regs
                c.f = regs[0]
                c.pc = pc
                c.sp = (pc * 7) & 0xFFFF
                c.im = 1
            for _ in range(30):
                k = rnd.randint(1, 60)
                if seed % 2 and rnd.random() < 0.3:
                    for c in (fast, slow):
                        c.request_irq()
                        c.iff1 = c.iff2 = True
                n1 = fast.run_fast(max_steps=k)
                n2 = slow.run_fast(max_steps=k)
                self.assertEqual(n1, n2, "seed %d" % seed)
                self.assertEqual(self._state(fast), self._state(slow), "seed %d" % seed)
                self.assertEqual(fast.mem, slow.mem, "seed %d" % seed)
                if fast.halted:
                    break

    def test_block_instructions_match_step(self):
        import random
        rnd = random.Random(7)
        for case in range(120):
            op = rnd.choice([0xB0, 0xB8, 0xB1, 0xB9])
            pc = rnd.choice([0x100, 0x8000, 0xFFF0, 0x0, 0xFFFE])
            mem = bytearray(rnd.choice([0, 1, 2, 7, 255]) if rnd.random() < 0.5 else rnd.getrandbits(8)
                            for _ in range(0x10000))
            mem[pc] = 0xED
            mem[(pc + 1) & 0xFFFF] = op
            mem[(pc + 2) & 0xFFFF] = 0x76
            fast, slow = Z80(bytearray(mem)), _SlowZ80(bytearray(mem))
            hl = rnd.choice([rnd.randrange(0x10000), 0x2000, 0xFFF0, 5, pc, pc - 3])
            de = rnd.choice([rnd.randrange(0x10000), hl + 1, hl - 1, hl + 3, hl - 3, hl + 40, pc - 5, pc + 2])
            bc = rnd.choice([0, 1, 2, 3, 10, 100, 1000, rnd.randrange(0x10000)])
            regs = [rnd.getrandbits(8) for _ in range(8)]
            for c in (fast, slow):
                c.r[:] = regs
                c.f = case * 37 & 0xFF
                c.pc = pc
                c.hl, c.de, c.bc = hl & 0xFFFF, de & 0xFFFF, bc
                c.wz = 0x1234
                c.rr = 0x85
            if op in (0xB1, 0xB9) and rnd.random() < 0.7:        # make CPIR hit something
                a = fast.r[7]
                p = (hl + rnd.randrange(0, 50) * (1 if op == 0xB1 else -1)) & 0xFFFF
                for c in (fast, slow):
                    c.mem[p] = a
            for _ in range(4):
                k = rnd.choice([1, 2, 3, 5, 50, 100000])
                n1 = fast.run_fast(max_steps=k)
                n2 = slow.run_fast(max_steps=k)
                self.assertEqual(n1, n2, "case %d op %02X" % (case, op))
                self.assertEqual(self._state(fast), self._state(slow), "case %d op %02X" % (case, op))
                self.assertEqual(fast.mem, slow.mem, "case %d op %02X" % (case, op))
                if fast.halted:
                    break

    def test_every_prefixed_opcode_matches_step(self):
        import random
        rnd = random.Random(2026)
        for prefix in (0xCB, 0xED, 0xDD, 0xFD):
            for op in range(256):
                for rep in range(3):
                    mem = bytearray(rnd.randbytes(0x10000))
                    pc = rnd.choice([0x1000, 0x8000, 0xFFFE, 0xFFFF, 0x0])
                    mem[pc] = prefix
                    mem[(pc + 1) & 0xFFFF] = op
                    if prefix in (0xDD, 0xFD) and op == 0xCB and rep % 2:
                        mem[(pc + 3) & 0xFFFF] = rnd.choice([0x06, 0x46, 0x86, 0xC6, 0x16, 0x3E])
                    fast, slow = Z80(bytearray(mem)), _SlowZ80(bytearray(mem))
                    regs = [rnd.getrandbits(8) for _ in range(8)]
                    sp = rnd.choice([0x4000, 0x0, 0xFFFF, 0x1])
                    ix, iy = rnd.getrandbits(16), rnd.getrandbits(16)
                    f = rnd.getrandbits(8)
                    bc = rnd.choice([0, 1, 2, 5, rnd.getrandbits(16)])
                    for c in (fast, slow):
                        c.r[:] = regs
                        c.f, c.pc, c.sp, c.x[:] = f, pc, sp, [ix, iy]
                        c.bc = bc
                        c.wz = 0x1234
                        c.rr = 0x7E
                        c.iff1 = c.iff2 = bool(rep & 1)
                    n1 = fast.run_fast(max_steps=1)
                    n2 = slow.run_fast(max_steps=1)
                    msg = "prefix %02X op %02X rep %d" % (prefix, op, rep)
                    self.assertEqual(n1, n2, msg)
                    self.assertEqual(self._state(fast), self._state(slow), msg)
                    self.assertEqual(fast.mem, slow.mem, msg)

    def test_ldir_is_fast(self):
        c = Z80()
        c.mem[0:3] = bytes([0xED, 0xB0, 0x76])
        c.mem[0x4000:0x4100] = bytes(range(256))
        c.hl, c.de, c.bc = 0x4000, 0x5000, 0x100
        calls = []
        orig = c.step
        c.step = lambda: (calls.append(1), orig())[1]
        c.run_fast()
        self.assertEqual(c.mem[0x5000:0x5100], bytes(range(256)))
        self.assertLessEqual(len(calls), 1)             # just the final HALT
        self.assertEqual(c.tstates, 21 * 255 + 16 + 4)       # + HALT


    def test_stop_pc(self):
        c = Z80()
        c.mem[0:6] = bytes([0x00, 0x00, 0x00, 0xC3, 0x00, 0x20])    # 3 NOPs, JP 2000h
        n = c.run_fast(stop_pc=0x2000)
        self.assertEqual((n, c.pc), (4, 0x2000))
        self.assertEqual(c.tstates, 3 * 4 + 10)


class DiskImageTests(unittest.TestCase):
    @staticmethod
    def build(fmt, files, deleted=()):
        """A raw image with `files` ({(user, "NAME.EXT"): bytes}) laid out by the CP/M rules,
        written independently of diskimg.py (sectors in physical order via fmt's skew table)."""
        import diskimg
        img = bytearray(b"\xe5" * (fmt.offset + fmt.size))
        tab = diskimg.Image(bytes(1), fmt).skewtab
        def put_block(n, data):
            data = data.ljust(fmt.blocksize, b"\x1a")
            per = fmt.blocksize // fmt.seclen
            for i in range(per):
                lsec = fmt.boottrk * fmt.sectrk + n * per + i
                trk, sec = divmod(lsec, fmt.sectrk)
                pos = fmt.offset + (trk * fmt.sectrk + tab[sec]) * fmt.seclen
                img[pos:pos + fmt.seclen] = data[i * fmt.seclen:(i + 1) * fmt.seclen]
        dirblocks = -(-fmt.maxdir * 32 // fmt.blocksize)
        for b in range(dirblocks):
            put_block(b, b"\xe5" * fmt.blocksize)
        ptr16 = fmt.dsm >= 256
        exm = max(1, (8 if ptr16 else 16) * fmt.blocksize // 16384) - 1
        nextblk, entries = dirblocks, []
        for (user, name), data in files.items():
            records = -(-len(data) // 128)
            data = data.ljust(records * 128, b"\x1a")
            nb = -(-len(data) // fmt.blocksize)
            blocks = list(range(nextblk, nextblk + nb))
            nextblk += nb
            for i, b in enumerate(blocks):
                put_block(b, data[i * fmt.blocksize:(i + 1) * fmt.blocksize])
            nm, _, ext = name.partition(".")
            per_entry = (8 if ptr16 else 16)
            for n in range(max(1, -(-nb // per_entry))):
                chunk = blocks[n * per_entry:(n + 1) * per_entry]
                next = -(-records // 128)
                last = min(next - 1, n * (exm + 1) + exm)
                rc = 128 if last < next - 1 else records - last * 128
                e = bytearray(32)
                e[0] = user
                e[1:12] = (nm.ljust(8) + ext.ljust(3)).encode()
                e[12], e[14], e[15] = last & 31, last >> 5, rc
                for j, b in enumerate(chunk):
                    if ptr16:
                        e[16 + 2 * j], e[17 + 2 * j] = b & 255, b >> 8
                    else:
                        e[16 + j] = b
                entries.append(bytes(e))
        for u, nm in deleted:
            e = bytearray(32); e[0] = 0xE5; e[1:12] = nm.ljust(11).encode(); entries.append(bytes(e))
        raw = b"".join(entries).ljust(fmt.maxdir * 32, b"\xe5")
        for b in range(dirblocks):
            put_block(b, raw[b * fmt.blocksize:(b + 1) * fmt.blocksize])
        return bytes(img)

    def test_ibm3740_skew_table(self):
        import diskimg
        img = diskimg.Image(bytes(1), diskimg.BUILTIN["ibm-3740"])
        want = [x - 1 for x in (1, 7, 13, 19, 25, 5, 11, 17, 23, 3, 9, 15, 21,
                                2, 8, 14, 20, 26, 6, 12, 18, 24, 4, 10, 16, 22)]
        self.assertEqual(img.skewtab, want)

    def _roundtrip(self, fmt):
        import diskimg
        big = bytes((i * 7) % 251 for i in range(50000))          # several extents
        files = {(0, "HELLO.TXT"): b"hello\r\n", (3, "BIG.DAT"): big, (0, "EMPTYISH.COM"): b"x" * 128}
        data = self.build(fmt, files, deleted=[(0, "GONE.TXT")])
        img = diskimg.Image(data, fmt)
        self.assertEqual(img.sane()[1], 0)
        got = {(f.user, f.name): img.read(f) for f in img.files()}
        self.assertEqual(sorted(got), sorted(files))
        self.assertEqual(got[(0, "HELLO.TXT")], b"hello\r\n".ljust(128, b"\x1a"))
        self.assertEqual(got[(3, "BIG.DAT")][:50000], big)
        self.assertEqual(len(got[(3, "BIG.DAT")]), -(-50000 // 128) * 128)
        self.assertEqual(got[(0, "EMPTYISH.COM")], b"x" * 128)

    def test_image_8bit_pointers_skewed(self):
        import diskimg
        self._roundtrip(diskimg.Format("t8", 128, 77, 26, 1024, 64, skew=6, boottrk=2))

    def test_image_16bit_pointers_and_offset(self):
        import diskimg
        self._roundtrip(diskimg.Format("t16", 512, 300, 16, 4096, 128, 0, 2, offset=1 << 20))

    def test_detect_romwbw_images(self):
        import diskimg
        from dataclasses import replace
        files = {(0, "A.COM"): b"\xc3\x00\x01" * 100}
        hd1k = diskimg.BUILTIN["wbw_hd1k"]
        for off in (0, 1 << 20):                      # a bare slice, and one behind the 1 MB prefix
            data = self.build(replace(hd1k, offset=off, slice_prefix=0), files)
            img = diskimg.detect(data)
            self.assertIsNotNone(img, off)
            self.assertEqual(img.fmt.name, "wbw_hd1k")
            self.assertEqual([f.name for f in img.files()], ["A.COM"])
        self.assertIsNone(diskimg.detect(bytes(100000)))

    def test_parse_diskdefs(self):
        import diskimg
        defs = diskimg.parse_diskdefs("diskdef x\n seclen 512\n tracks 10\n sectrk 8\n"
                                      " blocksize 2048\n maxdir 64\n boottrk 1\n offset 3T\nend\n")
        self.assertEqual(defs["x"].offset, 3 * 8 * 512)
        self.assertEqual(defs["x"].dsm, 9 * 8 * 512 // 2048 - 1)


class CpmTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def test_disk_size_and_allocation_vector(self):
        os.makedirs(os.path.join(self.tmp, "A", "0"))
        os.makedirs(os.path.join(self.tmp, "A", "2"))
        with open(os.path.join(self.tmp, "A", "0", "ONE.DAT"), "wb") as f:
            f.write(b"x" * 10000)                       # 3 blocks of 4K
        with open(os.path.join(self.tmp, "A", "2", "TWO.DAT"), "wb") as f:
            f.write(b"y" * 100)                         # 1 block (other user area, same disk)
        cpm, out = make_cpm(self.tmp, drives={"A": os.path.join(self.tmp, "A")})
        m = cpm.cpu.mem
        dpb = cpm.bdos_call(31)
        self.assertEqual(dpb, 0xFD80)
        dsm = m[dpb + 5] | m[dpb + 6] << 8
        self.assertEqual(dsm, 2047)
        alv = cpm.bdos_call(27)
        used = sum(bin(m[alv + i]).count("1") for i in range(256))
        self.assertEqual(used, 8 + 4)                   # 8 directory blocks + 4 file blocks
        # a folder with more than half the disk switches to bigger blocks and never overflows
        with open(os.path.join(self.tmp, "A", "0", "BIG.DAT"), "wb") as f:
            f.truncate(40 * 1024 * 1024)
        dpb = cpm.bdos_call(31)
        self.assertEqual(m[dpb + 2], 7)                 # BSH for 16K blocks
        alv = cpm.bdos_call(27)
        self.assertEqual(sum(bin(m[alv + i]).count("1") for i in range(256)), 2048)

    def test_disk_size_and_allocation_vector(self):
        os.makedirs(os.path.join(self.tmp, "A", "0"))
        os.makedirs(os.path.join(self.tmp, "A", "2"))
        with open(os.path.join(self.tmp, "A", "0", "ONE.DAT"), "wb") as f:
            f.write(b"x" * 10000)                       # 3 blocks of 4K
        with open(os.path.join(self.tmp, "A", "2", "TWO.DAT"), "wb") as f:
            f.write(b"y" * 100)                         # 1 block (other user area, same disk)
        cpm, out = make_cpm(self.tmp, drives={"A": os.path.join(self.tmp, "A")})
        m = cpm.cpu.mem
        dpb = cpm.bdos_call(31)
        self.assertEqual(m[dpb + 5] | m[dpb + 6] << 8, 2047)
        alv = cpm.bdos_call(27)
        self.assertEqual(sum(bin(m[alv + i]).count("1") for i in range(256)), 8 + 4)
        # more than half the disk: bigger blocks, and a full disk never overflows
        with open(os.path.join(self.tmp, "A", "0", "BIG.DAT"), "wb") as f:
            f.truncate(40 * 1024 * 1024)
        dpb = cpm.bdos_call(31)
        self.assertEqual(m[dpb + 2], 7)                 # BSH of 16K blocks
        alv = cpm.bdos_call(27)
        self.assertEqual(sum(bin(m[alv + i]).count("1") for i in range(256)), 2048)

    def test_line_input_keeps_ctrl_z(self):
        cpm, out = make_cpm(self.tmp)
        con = cpm.console
        con._raw = "x"                      # behave like a keyboard: editing keys active
        con._buf = list(b"\x1a\r")
        self.assertEqual(con.read_line(), b"\x1a")          # CLINK's "^Z-<CR> to abort"
        con._buf = list(b"abcde\x13\x13X\r")
        self.assertEqual(con.read_line(), b"abcXde")         # cursor left, insert
        con._buf = list(b"hello\x15ok\r")
        self.assertEqual(con.read_line(), b"ok")

    def test_keymap_bbc(self):
        cpm, out = make_cpm(self.tmp)
        self.assertEqual(cpm.keymode, "auto")
        cpm._ccp_command("KEYMAP BBC")
        self.assertEqual(cpm.keymode, "bbc")
        cpm._ccp_command("KEYMAP WS")
        self.assertEqual(cpm.keymode, "ws")
        self.assertEqual(cpm.console.keymap, "ws")

    def test_play_frames_and_cp437(self):
        os.makedirs(os.path.join(self.tmp, "0"), exist_ok=True)
        data = b"head\x1b[HAAA\x1b[HBBB\x1b[HCCC\x1a" + b"SAUCE00"
        with open(os.path.join(self.tmp, "0", "T.ANS"), "wb") as f:
            f.write(data)
        cpm, out = make_cpm(self.tmp)
        cpm._ccp_command("PLAY T.ANS 200")
        s = out.getvalue()
        self.assertIn("\x1b[HAAA", s)
        self.assertIn("\x1b[HCCC", s)
        self.assertNotIn("SAUCE", s)
        self.assertIn("\x1b[?25h", s)
        out.truncate(0); out.seek(0)
        out2 = io.StringIO()
        con = Console(io.StringIO(""), out2, charset="cp437")
        con.write_bytes(b"\xdb\xb0")
        self.assertEqual(out2.getvalue(), "\u2588\u2591")

    def test_play_sauce_ansimation_default_rate(self):
        os.makedirs(os.path.join(self.tmp, "0"), exist_ok=True)
        sauce = b"SAUCE00" + b" " * 87 + bytes([1, 2]) + b"\0" * 32
        with open(os.path.join(self.tmp, "0", "S.ANS"), "wb") as f:
            f.write(b"x" * 3000 + b"\x1a" + sauce)
        cpm, out = make_cpm(self.tmp)
        import time
        t0 = time.time()
        cpm._ccp_command("PLAY S.ANS")          # 3000 bytes: 0.5 s at 57600, 3 s at 9600
        self.assertLess(time.time() - t0, 1.5)
        self.assertNotIn("SAUCE", out.getvalue())

    def test_ansi_screen_is_width_independent(self):
        from cpm import AnsiScreen
        a, b = AnsiScreen(), AnsiScreen()
        stream = b"\x1b[2J" + b"\xdb" * 85 + b"\x1b[3;5H\x1b[31mAB\x1b[0m\x1b[2C!"
        a.feed(stream)
        self.assertEqual(a.cells[0][79][0], 0xDB)       # wrapped at 80, not at the terminal width
        self.assertEqual(a.cells[1][4][0], 0xDB)
        self.assertEqual(a.cells[2][4][0], ord("A"))
        self.assertEqual(a.cells[2][8][0], ord("!"))
        out = a.render()
        self.assertIn(b"\x1b[3;5H", out)
        b.feed(out)
        self.assertEqual([[c for c, _ in r] for r in a.cells], [[c for c, _ in r] for r in b.cells])
        self.assertEqual(a.render(), b"")               # nothing changed, nothing sent

    def test_keymap_auto_by_bbc_version(self):
        cpm, out = make_cpm(self.tmp)
        v3 = b"\0" * 20 + b"BBC BASIC (Z80) Version 3.00  "
        v5 = b"\0" * 20 + b"BBC BASIC (Z80) Version 5.00  "
        for name, prog, want in (("BBCBASIC.COM", v3, "bbc3"), ("B:BBCBASIC.COM", v5, "bbc"),
                                 ("EDIT.COM", v3, "ws")):
            cpm._set_keymap(name, prog)
            self.assertEqual(cpm.console.keymap, want, name)
        cpm._ccp_command("KEYMAP BBC3")
        cpm._set_keymap("EDIT.COM", b"")
        self.assertEqual(cpm.console.keymap, "bbc3")

    def test_image_command(self):
        import diskimg
        os.makedirs(os.path.join(self.tmp, "0"), exist_ok=True)
        files = {(0, "HELLO.TXT"): b"hello\r\n", (0, "OTHER.DAT"): b"zz", (2, "USER2.TXT"): b"two"}
        data = DiskImageTests.build(diskimg.BUILTIN["ibm-3740"], files)
        with open(os.path.join(self.tmp, "0", "Disk.IMG"), "wb") as f:
            f.write(data)
        cpm, out = make_cpm(self.tmp)
        cpm._ccp_command("IMAGE disk.img")
        listing = out.getvalue()
        self.assertIn("HELLO.TXT", listing)
        self.assertIn("USER2.TXT", listing)
        cpm._ccp_command("IMAGE Disk.IMG IBM-3740 GET *.TXT")
        with open(os.path.join(self.tmp, "0", "hello.txt"), "rb") as f:
            self.assertEqual(f.read(), b"hello\r\n".ljust(128, b"\x1a"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "0", "other.dat")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "0", "user2.txt")))
        cpm._ccp_command("IMAGE disk.img GET 2:USER2.TXT")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "0", "user2.txt")))
        cpm._ccp_command("IMAGE ?")
        self.assertIn("wbw_hd1k", out.getvalue())

    def test_view_hex_dump(self):
        os.makedirs(os.path.join(self.tmp, "0"), exist_ok=True)
        data = bytes(range(256)) + b"Hello, Z3ENV\x1a tail"
        path = os.path.join(self.tmp, "0", "BIN.DAT")
        with open(path, "wb") as f:
            f.write(data)
        cpm, out = make_cpm(self.tmp, stdin="q")
        cpm._ccp_view(path, "BIN.DAT")
        text = out.getvalue()
        self.assertIn("00000000  00 01 02 03 04 05 06 07  08 09 0a 0b 0c 0d 0e 0f", text)
        self.assertIn("|Hello, Z3ENV.", text)               # past the ^Z: all bytes shown
        tpath = os.path.join(self.tmp, "0", "T.TXT")
        with open(tpath, "wb") as f:
            f.write(b"line one\r\nline two\r\n")
        cpm, out = make_cpm(self.tmp, stdin="q")
        cpm._ccp_view(tpath, "T.TXT")
        self.assertIn("line two", out.getvalue())
        self.assertNotIn("00000000", out.getvalue())
        cpm, out = make_cpm(self.tmp, stdin="xq")           # x toggles to hex
        cpm._ccp_view(tpath, "T.TXT")
        self.assertIn("00000000  6c 69 6e 65 20 6f 6e 65", out.getvalue())

    def test_z3_environment(self):
        import z3env
        # type 1 ZCPR3 program: JP 010Ch, 'Z3ENV', type, ENV address 0, then
        # LD HL,(0109h) ; LD (0200h),HL ; RET
        prog = (bytes([0xC3, 0x0C, 0x01]) + b"Z3ENV" + bytes([1, 0, 0, 0x00]) +
                bytes([0x2A, 0x09, 0x01, 0x22, 0x00, 0x02, 0xC9]))
        cpm, out = make_cpm(self.tmp)
        cpm.load(prog)                               # not in Z3 mode: untouched
        self.assertEqual(cpm.cpu.mem[0x0109:0x010B], b"\x00\x00")
        self.assertEqual(cpm.cpu.mem[6], BDOS_ENTRY & 0xFF)
        cpm._ccp_command("Z3 ON")
        self.assertIn("Z3ENV FB00", out.getvalue())
        cpm.load(prog)
        mem = cpm.cpu.mem
        self.assertEqual(cpm.cpu.hl, z3env.ENV)
        self.assertEqual(bytes(mem[z3env.ENV + 3:z3env.ENV + 8]), b"Z3ENV")
        self.assertEqual(mem[z3env.ENV + 0x1B] | mem[z3env.ENV + 0x1C] << 8, z3env.ENV)
        self.assertEqual(mem[7], z3env.BDOS >> 8)    # BDOS (top of TPA) lowered
        self.assertEqual(mem[z3env.WHEEL], 0xFF)
        self.assertEqual(bytes(mem[z3env.EXPATH:z3env.EXPATH + 5]), bytes([1, 15, 1, 0, 0]))
        self.assertEqual(bytes(mem[z3env.ENV + 0x80:z3env.ENV + 0x84]), b"ANSI")
        cpm.run()
        self.assertEqual(mem[0x0200] | mem[0x0201] << 8, z3env.ENV)
        mem[z3env.WHEEL] = 0                         # a tool clears the wheel byte ...
        cpm.load(prog)                               # ... and the next program sees it
        self.assertEqual(cpm.cpu.mem[z3env.WHEEL], 0)
        # BDOS calls still work through the lowered entry point
        cpm.load(bytes([0x0E, 0x02, 0x1E, 0x41, 0xCD, 0x05, 0x00, 0xC9]))
        cpm.run()
        self.assertIn("A", out.getvalue())
        cpm._ccp_command("Z3 OFF")
        cpm.load(prog)
        self.assertEqual(cpm.cpu.mem[6], BDOS_ENTRY & 0xFF)

    def test_debug_monitor(self):
        cpm, out = make_cpm(self.tmp, stdin=(
            "E 100 21 34 12 CD 10 01 76\nE 110 3E 41 5F 0E 02 CD 05 00 C9\n"
            "L 100 3\nB 103\nG\nT 2\nP\nP\nR BC 0102\nD 100 10f\nQ\n"))
        cpm._ccp_debug_empty()
        text = out.getvalue()
        self.assertIn("LD HL,1234h", text)
        self.assertIn("CALL 0110h", text)
        self.assertIn("Breakpoint at 0103", text)
        self.assertIn("LD A,41h", text)
        self.assertIn("HL=1234", text)
        self.assertIn("BC=0102", text)
        self.assertIn("21 34 12 CD 10 01 76", text)

    def test_copy_command(self):
        for d in ("A", "B"):
            os.makedirs(os.path.join(self.tmp, d, "0"))
        for n, data in (("A.TXT", b"one"), ("B.TXT", b"two"), ("P.COM", b"\x00")):
            with open(os.path.join(self.tmp, "A", "0", n), "wb") as f:
                f.write(data)
        cpm, out = make_cpm(self.tmp, drives={"A": os.path.join(self.tmp, "A"),
                                              "B": os.path.join(self.tmp, "B")})
        for line in ("COPY A.TXT B:", "COPY *.TXT B3:", "COPY A.TXT C.TXT", "COPY *.TXT *.BAK",
                     "COPY *.TXT X.TXT", "COPY A.TXT A.TXT", "COPY A.TXT Q:", "COPY NONE.TXT B:"):
            cpm._ccp_command(line)
        def rd(*p):
            with open(os.path.join(self.tmp, *p), "rb") as f:
                return f.read()
        self.assertEqual(rd("B", "0", "a.txt"), b"one")
        self.assertEqual(rd("B", "3", "b.txt"), b"two")
        self.assertEqual(rd("A", "0", "c.txt"), b"one")
        self.assertEqual(rd("A", "0", "b.bak"), b"two")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "A", "0", "x.txt")))
        text = out.getvalue()
        for word in ("SAME FILE", "NO DRIVE", "NO FILE", "SEVERAL FILES"):
            self.assertIn(word, text)

    def test_hello(self):
        cpm, out = make_cpm(self.tmp)
        cpm.load(PROG_HELLO)
        cpm.run()
        self.assertEqual(out.getvalue(), "Hello")

    def test_ret_to_warm_boot(self):
        cpm, out = make_cpm(self.tmp)
        cpm.load(bytes([0x0E, 0x02, 0x1E, 0x58, 0xCD, 0x05, 0x00, 0xC9]))
        cpm.run()
        self.assertEqual(out.getvalue(), "X")
        self.assertTrue(cpm.exited)

    def test_command_tail_and_fcbs(self):
        cpm, _ = make_cpm(self.tmp)
        cpm.load(b"\xC9", args=["foo.txt", "b:bar"])
        m = cpm.cpu.mem
        self.assertEqual(m[0x80], len(b" FOO.TXT B:BAR"))
        self.assertEqual(bytes(m[0x81:0x81 + m[0x80]]), b" FOO.TXT B:BAR")
        self.assertEqual(bytes(m[0x5C:0x5C + 12]), b"\x00FOO     TXT")
        self.assertEqual(bytes(m[0x6C:0x6C + 12]), b"\x02BAR        ")

    def test_read_buffer(self):
        cpm, _ = make_cpm(self.tmp, stdin="hello\n")
        prog = bytearray(0x200)
        prog[0:8] = bytes([0x11, 0x00, 0x02, 0x0E, 0x0A, 0xCD, 0x05, 0x00])
        prog[8] = 0xC9
        prog[0x100] = 20                                # buffer max length at 0x200
        cpm.load(bytes(prog))
        cpm.run()
        m = cpm.cpu.mem
        self.assertEqual(m[0x201], 5)
        self.assertEqual(bytes(m[0x202:0x207]), b"hello")

    def fcb(self, cpm, addr, name, ext):
        cpm.cpu.mem[addr:addr + 36] = bytes(36)
        cpm.cpu.mem[addr + 1:addr + 12] = (name.ljust(8) + ext.ljust(3)).encode()

    def test_file_roundtrip_sequential(self):
        cpm, _ = make_cpm(self.tmp)
        cpm.load(b"\xC9")
        self.fcb(cpm, 0x200, "T", "TXT")
        self.assertEqual(cpm.bdos_call(22, 0x200), 0)               # make
        cpm.bdos_call(26, 0x300)                                    # DMA
        cpm.cpu.mem[0x300:0x303] = b"ABC"
        self.assertEqual(cpm.bdos_call(21, 0x200), 0)               # write seq
        cpm.cpu.mem[0x300:0x303] = b"XYZ"
        self.assertEqual(cpm.bdos_call(21, 0x200), 0)
        self.assertEqual(cpm.bdos_call(16, 0x200), 0)               # close
        with open(os.path.join(self.tmp, "t.txt"), "rb") as f:
            data = f.read()
        self.assertEqual(len(data), 256)
        self.assertEqual(data[:3], b"ABC")
        self.assertEqual(data[128:131], b"XYZ")

        self.fcb(cpm, 0x200, "t", "txt")                            # lower case: still found
        self.assertEqual(cpm.bdos_call(15, 0x200), 0)               # open
        self.assertEqual(cpm.cpu.mem[0x200 + 15], 2)                # RC
        cpm.bdos_call(26, 0x400)
        self.assertEqual(cpm.bdos_call(20, 0x200), 0)
        self.assertEqual(bytes(cpm.cpu.mem[0x400:0x403]), b"ABC")
        self.assertEqual(cpm.bdos_call(20, 0x200), 0)
        self.assertEqual(bytes(cpm.cpu.mem[0x400:0x403]), b"XYZ")
        self.assertEqual(cpm.bdos_call(20, 0x200), 1)               # EOF

    def test_random_access_and_size(self):
        cpm, _ = make_cpm(self.tmp)
        cpm.load(b"\xC9")
        self.fcb(cpm, 0x200, "R", "DAT")
        cpm.bdos_call(22, 0x200)
        cpm.bdos_call(26, 0x300)
        cpm.cpu.mem[0x300:0x304] = b"REC5"
        cpm.cpu.mem[0x200 + 33] = 5
        self.assertEqual(cpm.bdos_call(34, 0x200), 0)               # write random rec 5
        self.assertEqual(cpm.bdos_call(35, 0x200), 0)               # file size
        self.assertEqual(cpm.cpu.mem[0x200 + 33], 6)
        cpm.cpu.mem[0x300:0x304] = b"\0\0\0\0"
        cpm.cpu.mem[0x200 + 33] = 5
        self.assertEqual(cpm.bdos_call(33, 0x200), 0)
        self.assertEqual(bytes(cpm.cpu.mem[0x300:0x304]), b"REC5")
        cpm.cpu.mem[0x200 + 33] = 50
        self.assertEqual(cpm.bdos_call(33, 0x200), 1)               # unwritten
        cpm.cpu.mem[0x200 + 35] = 1
        self.assertEqual(cpm.bdos_call(33, 0x200), 6)               # out of range

    def test_search_rename_delete(self):
        for n in ("a.txt", "b.txt", "c.dat"):
            with open(os.path.join(self.tmp, n), "wb") as f:
                f.write(b"x")
        cpm, _ = make_cpm(self.tmp)
        cpm.load(b"\xC9")
        cpm.cpu.mem[0x200:0x224] = bytes(36)
        cpm.cpu.mem[0x201:0x20C] = b"????????TXT"
        cpm.bdos_call(26, 0x300)
        self.assertEqual(cpm.bdos_call(17, 0x200), 0)
        self.assertEqual(bytes(cpm.cpu.mem[0x301:0x30C]), b"A       TXT")
        self.assertEqual(cpm.bdos_call(18, 0x200), 0)
        self.assertEqual(bytes(cpm.cpu.mem[0x301:0x30C]), b"B       TXT")
        self.assertEqual(cpm.bdos_call(18, 0x200), 0xFF)

        self.fcb(cpm, 0x200, "A", "TXT")
        cpm.cpu.mem[0x211:0x21C] = b"Z       TXT"
        self.assertEqual(cpm.bdos_call(23, 0x200), 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "z.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a.txt")))

        cpm.cpu.mem[0x201:0x20C] = b"????????TXT"
        self.assertEqual(cpm.bdos_call(19, 0x200), 0)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["c.dat"])
        self.assertEqual(cpm.bdos_call(19, 0x200), 0xFF)

    def test_open_missing(self):
        cpm, _ = make_cpm(self.tmp)
        cpm.load(b"\xC9")
        self.fcb(cpm, 0x200, "NOPE", "COM")
        self.assertEqual(cpm.bdos_call(15, 0x200), 0xFF)

    def test_user_areas(self):
        os.makedirs(os.path.join(self.tmp, "0"))
        os.makedirs(os.path.join(self.tmp, "3"))
        with open(os.path.join(self.tmp, "0", "A.TXT"), "wb") as f:
            f.write(b"x")
        cpm, _ = make_cpm(self.tmp)
        cpm.load(b"\xC9")
        self.fcb(cpm, 0x200, "A", "TXT")
        self.assertEqual(cpm.bdos_call(15, 0x200), 0)               # user 0: found
        cpm.bdos_call(32, 3)                                        # USER 3
        self.assertEqual(cpm.cpu.mem[4] >> 4, 3)
        self.assertEqual(cpm.bdos_call(15, 0x200), 0xFF)            # not in user 3
        self.assertEqual(cpm.bdos_call(22, 0x200), 0)               # make in user 3
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "3", "a.txt")))

    def test_shell(self):
        with open(os.path.join(self.tmp, "hello.com"), "wb") as f:
            f.write(PROG_HELLO)
        with open(os.path.join(self.tmp, "n.txt"), "wb") as f:
            f.write(b"line\r\n\x1a")
        cpm, out = make_cpm(self.tmp, stdin="DIR\nHELLO\nTYPE N.TXT\nREN M.TXT=N.TXT\nERA M.TXT\nNOPE\nEXIT\n")
        cpm.shell()
        text = out.getvalue()
        self.assertIn("A: HELLO    COM : N        TXT", text)
        self.assertIn("Hello", text)
        self.assertIn("line", text)
        self.assertIn("NOPE?", text)
        self.assertEqual(os.listdir(self.tmp), ["hello.com"])

    def test_console_finnish_charset(self):
        out = io.StringIO()
        con = Console(io.StringIO("Hyv\u00e4\u00e4 \u00c5\n"), out, charset="fi")
        self.assertEqual([con.read_char() for _ in range(6)],
                         [0x48, 0x79, 0x76, 0x7B, 0x7B, 0x20])
        self.assertEqual(con.read_char(), 0x5D)          # A-ring -> ]
        self.assertEqual(con.read_char(), 0x0D)          # Enter -> CR
        for b in b"x{|}[\x1b[2;3Hy":
            con.write(b)
        # letters substituted, but the "[" inside the ANSI sequence is untouched
        self.assertEqual(out.getvalue(), "x\u00e4\u00f6\u00e5\u00c4\x1b[2;3Hy")

    def test_command_falls_back_to_user0_and_drive_a(self):
        a, b = os.path.join(self.tmp, "A"), os.path.join(self.tmp, "B")
        for d in (os.path.join(a, "0"), os.path.join(a, "3"),
                  os.path.join(b, "0"), os.path.join(b, "3")):
            os.makedirs(d)
        for path in (os.path.join(a, "0", "sysprog.com"), os.path.join(b, "0", "bprog.com")):
            with open(path, "wb") as f:
                f.write(PROG_HELLO)
        out = io.StringIO()
        stdin = "USER 3\nSYSPROG\nB:\nSYSPROG\nBPROG\nA:BPROG\nEXIT\n"
        cpm = CPM(console=Console(io.StringIO(stdin), out), drives={"A": a, "B": b})
        cpm.shell()
        text = out.getvalue()
        # user 3 on A: -> A: user 0; user 3 on B: -> A: user 0 and B: user 0
        self.assertEqual(text.count("Hello"), 3)
        self.assertIn("BPROG?", text)            # explicit A: prefix does not search B:
        self.assertEqual(cpm.user, 3)            # shell state untouched

    def test_path_alias_semicolon_startup_color(self):
        a, b = os.path.join(self.tmp, "A"), os.path.join(self.tmp, "B")
        for d in (os.path.join(a, "0"), os.path.join(b, "0"), os.path.join(b, "5")):
            os.makedirs(d)
        with open(os.path.join(b, "5", "tool.com"), "wb") as f:
            f.write(PROG_HELLO)
        startup = os.path.join(self.tmp, "start.txt")
        with open(startup, "w") as f:
            f.write("# comment\nALIAS HI=TOOL ; TOOL\nPATH B5:\n")
        out = io.StringIO()
        cpm = CPM(console=Console(io.StringIO("TOOL\nPATH\nHI\nPATH NONE\nTOOL\nPATH Z9\nDIR\nEXIT\n"), out),
                  drives={"A": a, "B": b})
        cpm.startup = startup
        cpm.shell()
        text = out.getvalue()
        self.assertEqual(text.count("Hello"), 3)            # TOOL via PATH, then HI runs it twice
        self.assertIn("Path: B5:", text)
        self.assertIn("TOOL?", text)                        # after PATH NONE
        self.assertIn("Bad path element Z9", text)
        self.assertNotIn("\x1b", text)                      # colour is off by default
        out2 = io.StringIO()
        cpm2 = CPM(console=Console(io.StringIO("DIR\nEXIT\n"), out2), drives={"A": a, "B": b})
        cpm2.color = True
        cpm2.shell()
        self.assertIn("\x1b[32mA0\x1b[0m>", out2.getvalue())

    def test_shell_help(self):
        cpm, out = make_cpm(self.tmp, stdin="HELP\nHELP DIR\n?\nHELP KEYS\nHELP B:\nHELP NOPE\nEXIT\n")
        cpm.shell()
        text = out.getvalue()
        for word in ("Built-in commands", "DIR [spec]", "Wildcards", "Ctrl+]",
                     "Switch the current drive", "No help for NOPE"):
            self.assertIn(word, text)

    def test_bios_conout(self):
        cpm, out = make_cpm(self.tmp)
        # LD HL,(1) ; LD DE,6 ; ADD HL,DE (CONOUT = wboot+9) ; LD C,'Q' ; JP (HL) path via CALL
        prog = bytes([0x2A, 0x01, 0x00, 0x11, 0x09, 0x00, 0x19, 0x0E, 0x51,
                      0xCD, 0x0E, 0x01, 0xC9, 0x00, 0xE9])
        cpm.load(prog)
        cpm.run()
        self.assertEqual(out.getvalue(), "Q")


if __name__ == "__main__":
    unittest.main()
