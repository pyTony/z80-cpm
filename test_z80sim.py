import io
import os
import tempfile
import unittest

from z80sim import Z80, SF, ZF, HF, PF, NF, CF
from cpm import CPM, Console


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


def make_cpm(tmp, stdin=""):
    out = io.StringIO()
    return CPM(console=Console(io.StringIO(stdin), out), drives={"A": tmp}), out


class _SlowZ80(Z80):
    """Overriding rd disables the fast handlers, so run_fast uses plain step()."""
    def rd(self, addr):
        return self.mem[addr & 0xFFFF]


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

    def test_stop_pc(self):
        c = Z80()
        c.mem[0:6] = bytes([0x00, 0x00, 0x00, 0xC3, 0x00, 0x20])    # 3 NOPs, JP 2000h
        n = c.run_fast(stop_pc=0x2000)
        self.assertEqual((n, c.pc), (4, 0x2000))
        self.assertEqual(c.tstates, 3 * 4 + 10)


class CpmTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()

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
