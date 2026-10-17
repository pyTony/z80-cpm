"""DEBUG: a small monitor for the emulated program (disassemble, dump, registers, step).

Commands (numbers are hexadecimal):
  L [addr [count]]   disassemble          D [addr [end]]   dump memory
  (addresses may be hex numbers or registers, with + and -:  D HL   L PC+4   D SP  B IX+3)
  R [reg value]      registers / set one  E addr byte...   enter bytes
  T [n]              trace n instructions F start end byte fill memory
  P                  like T, but runs a CALL/RST/DJNZ/block instruction as one step
  G [addr]           go to the next breakpoint or the end of the program
  B [addr...]        set / list breakpoints,  BC [addr]  clear one or all
  DP [addr [end]]    Z180: dump physical memory (5 hex digits); R CBAR 84 sets an MMU register
  Q                  leave
"""
from z80dis import disasm

_PAIRS = ("AF", "BC", "DE", "HL", "IX", "IY", "SP", "PC")
_BYTES = {"B": 0, "C": 1, "D": 2, "E": 3, "H": 4, "L": 5, "A": 7}


class Debugger:
    def __init__(self, cpm):
        self.cpm = cpm
        self.cpu = cpm.cpu
        self.bps = []
        self.list_next = None
        self.dump_next = None
        self.dump_phys = 0

    # ------------------------------------------------------------- helpers
    def out(self, text):
        self.cpm._print(text)

    def z180(self):
        return getattr(self.cpu, "__class__").__name__ == "Z180"

    def dis(self, addr):
        mem = self.cpu.mem
        n, text = disasm(lambda a: mem[a & 0xFFFF], addr, self.z180())
        raw = " ".join("%02X" % mem[(addr + i) & 0xFFFF] for i in range(min(n, 4)))
        return n, raw, text

    def line(self, addr):
        n, raw, text = self.dis(addr)
        return n, "%04X  %-11s %s" % (addr, raw, text)

    def flags(self):
        cpu = self.cpu
        return "".join(c if cpu.f & m else "-" for c, m in zip(
            "SZ-H-PNC", (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01)))

    def regs_short(self):
        c = self.cpu
        return "AF=%04X BC=%04X DE=%04X HL=%04X SP=%04X [%s]" % (
            c.af, c.bc, c.de, c.hl, c.sp, self.flags())

    def show_regs(self):
        c = self.cpu
        alt = c.alt
        self.out("\nPC=%04X SP=%04X AF=%04X BC=%04X DE=%04X HL=%04X IX=%04X IY=%04X" %
                 (c.pc, c.sp, c.af, c.bc, c.de, c.hl, c.ix, c.iy))
        self.out("\nAF'=%04X BC'=%04X DE'=%04X HL'=%04X  [%s]  I=%02X R=%02X IM%d IFF=%d%d" %
                 ((alt[7] << 8) | c.f_alt, (alt[0] << 8) | alt[1], (alt[2] << 8) | alt[3],
                  (alt[4] << 8) | alt[5], self.flags(), c.i, c.rr, c.im,
                  c.iff1, c.iff2))
        if self.z180():
            self.out("\n" + c.mmu_info())
        self.out("\n" + self.line(c.pc)[1])

    def dump(self, read, start, end, width):
        a = start & ~0xF
        while a <= end:
            row = ["%02X" % read(a + i) if start <= a + i <= end else "  " for i in range(16)]
            asc = "".join(chr(read(a + i)) if start <= a + i <= end and 32 <= read(a + i) < 127
                          else " " for i in range(16))
            self.out("\n%0*X  %s  %s  |%s|" % (width, a, " ".join(row[:8]), " ".join(row[8:]), asc))
            a += 16

    def num(self, text):
        """An address: hex numbers and register names (PC SP HL BC DE IX IY AF), joined
        with + and -.  A name wins over hex, so write BC, DE or AF as 0BC, 0DE, 0AF."""
        total, sign, term = 0, 1, ""
        for ch in text.replace(" ", "") + "+":
            if ch in "+-":
                if term:
                    total += sign * self.term(term)
                    term = ""
                elif ch == "-" and not term and total == 0 and sign == 1:
                    sign = -1
                    continue
                sign = 1 if ch == "+" else -1
            else:
                term += ch
        return total & 0xFFFF

    def term(self, t):
        u = t.upper()
        if u in _PAIRS:
            return getattr(self.cpu, u.lower())
        if u in _BYTES:
            return self.cpu.r[_BYTES[u]]
        return int(u.rstrip("H"), 16)

    # --------------------------------------------------------------- stepping
    def step(self):
        """Execute one instruction; True while the program is still running."""
        self.cpm.step_one()
        return not self.cpm.exited and not self.cpu.halted

    def run_to(self, stop):
        """Run until pc is in stop (a set), a breakpoint hits or the program ends."""
        first = True
        count = 0
        while True:
            if not first and (self.cpu.pc in stop or self.cpu.pc in self.bps):
                return "break"
            first = False
            if not self.step():
                return "end"
            count += 1
            if count % 50000 == 0 and self.cpm.console.has_input():
                return "key"

    _OVER = ("CALL", "RST", "DJNZ", "LDIR", "LDDR", "CPIR", "CPDR", "INIR", "INDR",
             "OTIR", "OTDR", "OTIMR", "OTDMR")

    def trace(self, n, over=False):
        for _ in range(n):
            pc = self.cpu.pc
            length, text = self.line(pc)
            if over and self.dis(pc)[2].split()[0] in self._OVER:
                alive = self.run_to({(pc + length) & 0xFFFF}) != "end"
            else:
                alive = self.step()
            self.out("\n%-31s %s" % (text, self.regs_short()))
            if not alive:
                self.end_message()
                return
            if self.cpu.pc in self.bps:
                self.out("\nBreakpoint at %04X" % self.cpu.pc)
                return

    def end_message(self):
        if self.cpm.exited:
            self.out("\nProgram ended (warm boot)")
        elif self.cpu.halted:
            self.out("\nHALT at %04X" % self.cpu.pc)

    # ---------------------------------------------------------------- commands
    def command(self, text):
        """One command line.  False when the user quits."""
        words = text.split()
        if not words:
            return True
        cmd, args = words[0].upper(), words[1:]
        try:
            if cmd in ("Q", "QUIT", "EXIT"):
                return False
            if cmd == "L":
                addr = self.num(args[0]) if args else (
                    self.list_next if self.list_next is not None else self.cpu.pc)
                count = int(args[1], 16) if len(args) > 1 else 12
                for _ in range(count):
                    n, line = self.line(addr)
                    mark = "=>" if addr == self.cpu.pc else "  "
                    self.out("\n" + mark + line)
                    addr = (addr + n) & 0xFFFF
                self.list_next = addr
            elif cmd == "D":
                start = self.num(args[0]) if args else (
                    self.dump_next if self.dump_next is not None else 0x100)
                end = self.num(args[1]) if len(args) > 1 else start + 0x7F
                if end < start:
                    end = 0xFFFF
                mem = self.cpu.mem
                self.dump(lambda a: mem[a & 0xFFFF], start, end, 4)
                self.dump_next = (end + 1) & 0xFFFF
            elif cmd == "DP" and self.z180():
                phys = self.cpu.physical()
                start = int(args[0].rstrip("hH"), 16) if args else self.dump_phys
                end = int(args[1].rstrip("hH"), 16) if len(args) > 1 else start + 0x7F
                end = min(max(end, start), len(phys) - 1)
                self.dump(lambda a: phys[a], start, end, 5)
                self.dump_phys = (end + 1) % len(phys)
            elif cmd == "R":
                if len(args) >= 2 or (args and "=" in args[0]):
                    name, _, val = (args[0] if "=" in args[0] else " ".join(args[:2])).replace("=", " ").partition(" ")
                    self.set_reg(name.upper(), self.num(val.strip() or args[-1]))
                self.show_regs()
            elif cmd == "E" and len(args) >= 2:
                a = self.num(args[0])
                for b in args[1:]:
                    self.cpu.mem[a & 0xFFFF] = int(b.rstrip("hH"), 16) & 0xFF
                    a += 1
            elif cmd == "F" and len(args) == 3:
                a, e, v = self.num(args[0]), self.num(args[1]), int(args[2].rstrip("hH"), 16)
                for i in range(a, e + 1):
                    self.cpu.mem[i & 0xFFFF] = v & 0xFF
            elif cmd in ("T", "P"):
                self.trace(int(args[0], 16) if args else 1, over=(cmd == "P"))
            elif cmd == "G":
                if args:
                    self.cpu.pc = self.num(args[0])
                why = self.run_to(set())
                if why == "key":
                    self.cpm.console.read_char()
                    self.out("\nInterrupted at %04X" % self.cpu.pc)
                elif why == "break":
                    self.out("\nBreakpoint at %04X" % self.cpu.pc)
                else:
                    self.end_message()
                self.show_regs()
            elif cmd == "B":
                for a in args:
                    v = self.num(a)
                    if v not in self.bps:
                        self.bps.append(v)
                self.out("\nBreakpoints: " + (" ".join("%04X" % b for b in self.bps) or "none"))
            elif cmd == "BC":
                if args:
                    for a in args:
                        v = self.num(a)
                        if v in self.bps:
                            self.bps.remove(v)
                else:
                    self.bps.clear()
                self.out("\nBreakpoints: " + (" ".join("%04X" % b for b in self.bps) or "none"))
            elif cmd in ("H", "?", "HELP"):
                self.out("\n" + __doc__.split("Commands", 1)[1].split("\n", 1)[1].rstrip())
            else:
                self.out("\n?")
        except (ValueError, IndexError):
            self.out("\n?")
        return True

    def set_reg(self, name, value):
        c = self.cpu
        if name in _PAIRS:
            if name == "PC":
                c.pc = value
            elif name == "SP":
                c.sp = value
            else:
                setattr(c, name.lower(), value)
        elif name in _BYTES:
            c.r[_BYTES[name]] = value & 0xFF
        elif name == "F":
            c.f = value & 0xFF
        elif self.z180() and name in ("CBR", "BBR", "CBAR"):
            c.io_write(("CBR", "BBR", "CBAR").index(name) + 0x38, value & 0xFF)
        else:
            raise ValueError(name)

    def loop(self):
        self.out("\nDEBUG - type H for help, Q to leave")
        self.show_regs()
        while True:
            self.out("\n-")
            line = self.cpm.console.read_line()
            if line is None:
                break
            line = bytes(c for c in line if c >= 0x20).decode("latin1")
            if not self.command(line):
                break
