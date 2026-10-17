"""A window for the debugger: disassembly, registers, stack, memory, the program's
console and the debugger's command line side by side.

    python debug_gui.pyw [--cpu z180] [program.com [args...]]

Keys:  F7 step   F8 step over   F4 run to the selected line   F5 run   F6 / Esc stop
       F9 breakpoint on the selected line (double-click a line does the same)
       Ctrl+O open   Ctrl+R restart   Ctrl+L focus the command line

The command line takes every debugger command (H lists them); G runs in the window,
so the screen stays live and F6 stops it.  Program output goes to the CONSOLE pane;
click it to type to the program.  Layout and colours borrow from lispy_machine.
"""
import argparse
import io
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cpm import BDOS_BASE, CPM, TPA, Console  # noqa: E402
from debugger import Debugger                  # noqa: E402
from z80sim import Z80, Z180                   # noqa: E402

COLORS = {
    "bg": "#1e1e1e", "fg": "#d4d4d4", "cursor": "#ffffff", "selection": "#264f78",
    "listener_bg": "#0c0c0c", "prompt": "#569cd6", "result": "#6a9955",
    "error": "#f44747", "info": "#dcdcaa", "status_bg": "#007acc", "status_fg": "#ffffff",
    "side_bg": "#252526", "side_fg": "#cccccc", "side_sel": "#37373d",
    "entry_bg": "#000000", "entry_fg": "#ffffff",
    "pc_bg": "#3a3d41", "bp": "#f44747", "changed": "#dcdcaa", "console_fg": "#c8c8c8",
}

# Cursor keys as the WordStar control codes (the console's default keymap)
_KEYS = {"Return": 0x0D, "KP_Enter": 0x0D, "Tab": 0x09, "Escape": 0x1B,
         "Up": 0x05, "Down": 0x18, "Left": 0x13, "Right": 0x04, "Delete": 0x07,
         "Home": 0x01, "End": 0x06, "Prior": 0x12, "Next": 0x03}


class Stopped(Exception):
    """The user pressed Stop (or closed the window) while the program ran or waited."""


class GuiConsole(Console):
    """The CP/M console in a Text widget: keys come from Tk, output goes to the pane."""

    def __init__(self, app, backspace="bs"):
        super().__init__(stdin=io.StringIO(), stdout=io.StringIO(), backspace=backspace)
        self.app = app
        self._raw = "gui"                 # single keys: read_line echoes and edits
        self._pumped = 0.0

    def write(self, b):
        self.app.term.put(b & self.mask)

    def write_bytes(self, data):
        for b in data:
            self.app.term.put(b & self.mask)

    def flush(self):
        pass

    def read_char(self):
        if not self._buf:
            self.app.wait_key()
        return self._buf.pop(0)

    def has_input(self):
        now = time.perf_counter()
        if not self._buf and now - self._pumped > 0.03:     # keep the window alive
            self._pumped = now
            self.app.pump()
        return bool(self._buf)

    def key(self, code):
        self._buf.append(code & 0xFF)


class Terminal:
    """A plain glass terminal on a Text widget: CR, LF, BS, TAB, BEL and form feed;
    escape sequences are dropped."""

    MAX_LINES = 3000

    def __init__(self, text):
        self.text = text
        self.esc = 0
        text.mark_set("cur", "end-1c")
        text.mark_gravity("cur", "right")

    def clear(self):
        self.text.delete("1.0", "end")
        self.text.mark_set("cur", "1.0")

    def col(self):
        return int(self.text.index("cur").split(".")[1])

    def put(self, b):
        t = self.text
        if self.esc:
            if self.esc == 1:
                self.esc = 2 if b == 0x5B else 0
            elif 0x40 <= b <= 0x7E:
                self.esc = 0
            return
        if b == 0x1B:
            self.esc = 1
        elif b == 0x0D:
            t.mark_set("cur", "cur linestart")
        elif b == 0x0A:
            col = self.col()
            line = int(t.index("cur").split(".")[0])
            if t.compare("%d.0" % (line + 1), ">=", "end"):
                t.insert("end-1c", "\n")
                t.mark_set("cur", "end-1c")
            else:
                t.mark_set("cur", "%d.0" % (line + 1))
            short = col - self.col_end()
            if short > 0:
                t.insert("cur lineend", " " * short)
            t.mark_set("cur", "cur linestart +%dc" % col)
            n = int(t.index("end-1c").split(".")[0])
            if n > self.MAX_LINES:
                t.delete("1.0", "%d.0" % (n - self.MAX_LINES))
        elif b == 0x08:
            if self.col():
                t.mark_set("cur", "cur -1c")
        elif b == 0x09:
            for _ in range(8 - self.col() % 8):
                self.char(" ")
        elif b == 0x0C:
            self.clear()
        elif b == 0x07:
            t.bell()
        elif b >= 0x20 and b != 0x7F:
            self.char(chr(b))
        t.see("cur")

    def col_end(self):
        return int(self.text.index("cur lineend").split(".")[1])

    def char(self, ch):
        t = self.text
        if t.compare("cur", "<", "cur lineend"):
            t.delete("cur")
        t.insert("cur", ch)


class GuiDebugger(Debugger):
    """The text debugger with its output sent to the DEBUG pane."""

    def __init__(self, cpm, sink):
        super().__init__(cpm)
        self.sink = sink

    def out(self, text):
        self.sink(text)


def _mono():
    have = set(tkfont.families())
    for name in ("Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Menlo", "Courier New"):
        if name in have:
            return name
    return "Courier"


class App(tk.Tk):
    def __init__(self, cpu="z80"):
        super().__init__()
        self.title("Z80 debugger")
        self.geometry("1200x880")
        self.configure(bg=COLORS["bg"])
        mono = _mono()
        self.font = (mono, 11)
        self.font_bold = (mono, 11, "bold")
        self.font_ui = ("Segoe UI", 9)
        self.font_ui_bold = ("Segoe UI", 9, "bold")

        self.program = None              # path of the loaded .COM
        self.busy = False                # running, stepping or waiting for a key
        self.stop_req = False
        self.closing = False
        self.steps = 0
        self.view_start = TPA
        self.view_addrs = []
        self.selected = None
        self.mem_addr = TPA
        self.prev_regs = {}
        self.prev_mem = None
        self.history, self.hist_pos = [], 0

        self._style()
        self._build()
        self.console = GuiConsole(self)
        self.cpm = CPM(cpu=Z180() if cpu == "z180" else Z80(), console=self.console,
                       drives={"A": os.getcwd()})
        self.dbg = GuiDebugger(self.cpm, self.log)
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.empty()

    # ------------------------------------------------------------------ layout
    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TPanedwindow", background=COLORS["bg"])
        style.configure("TCombobox", fieldbackground=COLORS["side_sel"], background=COLORS["side_sel"],
                        foreground=COLORS["side_fg"], arrowcolor=COLORS["side_fg"])
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["side_sel"])],
                  foreground=[("readonly", COLORS["side_fg"])])
        self.option_add("*TCombobox*Listbox.background", COLORS["side_bg"])
        self.option_add("*TCombobox*Listbox.foreground", COLORS["side_fg"])
        self.option_add("*Entry.highlightThickness", 1)
        self.option_add("*Entry.highlightBackground", COLORS["side_sel"])
        self.option_add("*Entry.highlightColor", COLORS["prompt"])
        style.configure("TCheckbutton", background=COLORS["side_bg"], foreground=COLORS["side_fg"])

    def _text(self, parent, **kw):
        opts = dict(bg=COLORS["bg"], fg=COLORS["fg"], insertbackground=COLORS["cursor"],
                    selectbackground=COLORS["selection"], font=self.font, relief="flat",
                    borderwidth=4, highlightthickness=0, wrap="none", undo=False)
        opts.update(kw)
        return tk.Text(parent, **opts)

    def _panel(self, parent, title):
        frame = tk.Frame(parent, bg=COLORS["side_bg"])
        head = tk.Frame(frame, bg=COLORS["side_bg"])
        head.pack(fill="x")
        tk.Label(head, text=title, bg=COLORS["side_bg"], fg=COLORS["side_fg"],
                 font=self.font_ui_bold, anchor="w", padx=6).pack(side="left")
        return frame, head

    def _button(self, parent, text, cmd):
        b = tk.Button(parent, text=text, command=cmd, bg=COLORS["side_sel"], fg=COLORS["side_fg"],
                      activebackground=COLORS["selection"], activeforeground=COLORS["fg"],
                      relief="flat", font=self.font_ui, padx=8, pady=2, borderwidth=0)
        b.pack(side="left", padx=2, pady=3)
        return b

    def _build(self):
        bar = tk.Frame(self, bg=COLORS["side_bg"])
        bar.pack(side="top", fill="x")
        for text, cmd in (("Open", self.open_file), ("Restart", self.restart),
                          ("Step F7", self.step), ("Over F8", self.step_over),
                          ("Run F5", self.go), ("Stop F6", self.stop)):
            self._button(bar, text, cmd)
        tk.Label(bar, text="  args", bg=COLORS["side_bg"], fg=COLORS["side_fg"],
                 font=self.font_ui).pack(side="left")
        self.args_entry = tk.Entry(bar, bg=COLORS["entry_bg"], fg=COLORS["entry_fg"],
                                   insertbackground=COLORS["cursor"], font=self.font,
                                   relief="flat", width=30)
        self.args_entry.pack(side="left", padx=4)
        self.cpu_var = tk.StringVar(value="Z80")
        cpu_box = ttk.Combobox(bar, textvariable=self.cpu_var, values=("Z80", "Z180"),
                               width=6, state="readonly")
        cpu_box.pack(side="right", padx=6)
        cpu_box.bind("<<ComboboxSelected>>", lambda e: self.set_cpu(self.cpu_var.get()))

        self.status = tk.Label(self, anchor="w", bg=COLORS["status_bg"], fg=COLORS["status_fg"],
                               font=self.font_ui, padx=6)
        self.status.pack(side="bottom", fill="x")

        outer = tk.PanedWindow(self, orient="vertical", bg=COLORS["bg"], sashwidth=4,
                               borderwidth=0)
        outer.pack(fill="both", expand=True)
        top = tk.PanedWindow(outer, orient="horizontal", bg=COLORS["bg"], sashwidth=4,
                             borderwidth=0)
        bottom = tk.PanedWindow(outer, orient="horizontal", bg=COLORS["bg"], sashwidth=4,
                                borderwidth=0)
        outer.add(top, stretch="always", height=520)
        outer.add(bottom, stretch="always")

        # disassembly
        frame, head = self._panel(top, "CODE")
        tk.Label(head, text="go to", bg=COLORS["side_bg"], fg=COLORS["side_fg"],
                 font=self.font_ui).pack(side="right")
        self.goto = tk.Entry(head, bg=COLORS["entry_bg"], fg=COLORS["entry_fg"], width=10,
                             insertbackground=COLORS["cursor"], font=self.font, relief="flat")
        self.goto.pack(side="right", padx=4, pady=2)
        self.goto.bind("<Return>", self.on_goto)
        self.code = self._text(frame, cursor="arrow")
        self.code.pack(fill="both", expand=True)
        self.code.tag_configure("pc", background=COLORS["pc_bg"], foreground=COLORS["cursor"])
        self.code.tag_configure("bp", foreground=COLORS["bp"])
        self.code.tag_configure("chosen", background=COLORS["selection"])
        self.code.tag_raise("chosen")
        self.code.bind("<Button-1>", self.on_code_click)
        self.code.bind("<Double-Button-1>", self.on_code_double)
        self.code.bind("<MouseWheel>", lambda e: self.scroll_code(-1 if e.delta > 0 else 1))
        self.code.bind("<Button-4>", lambda e: self.scroll_code(-1))
        self.code.bind("<Button-5>", lambda e: self.scroll_code(1))
        self.code.bind("<Configure>", lambda e: self.show_code())
        top.add(frame, stretch="always", width=470)

        side = tk.PanedWindow(top, orient="vertical", bg=COLORS["bg"], sashwidth=4, borderwidth=0)
        top.add(side, stretch="always")
        upper = tk.PanedWindow(side, orient="horizontal", bg=COLORS["bg"], sashwidth=4,
                               borderwidth=0)
        side.add(upper, stretch="never", height=275)
        frame, _ = self._panel(upper, "REGISTERS")
        self.regs = self._text(frame, bg=COLORS["side_bg"], fg=COLORS["side_fg"])
        self.regs.pack(fill="both", expand=True)
        self.regs.tag_configure("chg", foreground=COLORS["changed"])
        self.regs.tag_configure("dim", foreground="#808080")
        upper.add(frame, stretch="never", width=300)
        frame, _ = self._panel(upper, "STACK")
        self.stack = self._text(frame, bg=COLORS["side_bg"], fg=COLORS["side_fg"])
        self.stack.pack(fill="both", expand=True)
        upper.add(frame, stretch="always")
        frame, head = self._panel(side, "MEMORY")
        self.phys_var = tk.BooleanVar(value=False)
        self.phys_check = ttk.Checkbutton(head, text="physical", variable=self.phys_var,
                                          command=self.show_memory)
        self.mem_entry = tk.Entry(head, bg=COLORS["entry_bg"], fg=COLORS["entry_fg"], width=10,
                                  insertbackground=COLORS["cursor"], font=self.font, relief="flat")
        self.mem_entry.pack(side="right", padx=4, pady=2)
        self.mem_entry.bind("<Return>", self.on_mem_addr)
        self.mem = self._text(frame, height=8, bg=COLORS["side_bg"], fg=COLORS["side_fg"])
        self.mem.pack(fill="both", expand=True)
        self.mem.tag_configure("chg", foreground=COLORS["changed"])
        for w, d in (("<Button-4>", -3), ("<Button-5>", 3)):
            self.mem.bind(w, lambda e, d=d: self.scroll_mem(d))
        self.mem.bind("<MouseWheel>", lambda e: self.scroll_mem(-3 if e.delta > 0 else 3))
        side.add(frame, stretch="always")

        # console and debugger
        frame, head = self._panel(bottom, "CONSOLE")
        self._button(head, "clear", lambda: self.term.clear()).pack_configure(side="right")
        self.term_text = self._text(frame, bg=COLORS["listener_bg"], fg=COLORS["console_fg"],
                                    insertwidth=0, cursor="xterm")
        self.term_text.pack(fill="both", expand=True)
        self.term_text.bind("<Key>", self.on_console_key)
        self.term_text.bind("<Button-1>", lambda e: (self.term_text.focus_set(), "break")[1])
        self.term = Terminal(self.term_text)
        bottom.add(frame, stretch="always", width=600)

        frame, _ = self._panel(bottom, "DEBUG")
        self.out = self._text(frame, bg=COLORS["listener_bg"], wrap="char")
        self.out.tag_configure("cmd", foreground=COLORS["prompt"])
        self.out.tag_configure("info", foreground=COLORS["info"])
        self.out.bind("<Key>", lambda e: None if e.state & 4 else "break")   # read-only, Ctrl+C copies
        row = tk.Frame(frame, bg=COLORS["entry_bg"])
        row.pack(side="bottom", fill="x")
        tk.Label(row, text="-", bg=COLORS["entry_bg"], fg=COLORS["prompt"],
                 font=self.font_bold).pack(side="left", padx=(6, 0))
        self.cmd = tk.Entry(row, bg=COLORS["entry_bg"], fg=COLORS["entry_fg"], font=self.font,
                            insertbackground=COLORS["cursor"], relief="flat")
        self.cmd.pack(side="left", fill="x", expand=True, padx=4, pady=4)
        self.cmd.bind("<Return>", self.on_command)
        self.cmd.bind("<Up>", lambda e: self.recall(-1))
        self.cmd.bind("<Down>", lambda e: self.recall(1))
        self.out.pack(fill="both", expand=True)
        bottom.add(frame, stretch="always")

    def _bind_keys(self):
        for key, fn in (("<F7>", self.step), ("<F8>", self.step_over), ("<F5>", self.go),
                        ("<F6>", self.stop), ("<F4>", self.run_to_selected),
                        ("<F9>", self.toggle_selected), ("<Control-o>", self.open_file),
                        ("<Control-r>", self.restart)):
            self.bind_all(key, lambda e, fn=fn: (fn(), "break")[1])
        self.bind_all("<Control-l>", lambda e: (self.cmd.focus_set(), "break")[1])
        self.bind_all("<Escape>", lambda e: self.stop() if self.busy else None)

    # ------------------------------------------------------------- program
    def empty(self):
        cpm = self.cpm
        cpm._init_memory()
        cpm.exited = False
        cpm.cpu.reset()
        cpm.cpu.pc = TPA
        cpm.cpu.sp = BDOS_BASE
        self.program = None
        self.after_load("empty memory at 0100 - E enters bytes, H lists commands")

    def load(self, path):
        folder = os.path.dirname(os.path.abspath(path))
        self.cpm.drives = {"A": folder}
        self.cpm._users = {"A": os.path.isdir(os.path.join(folder, "0"))}
        self.cpm.drive = self.cpm.user = 0
        try:
            self.cpm.load_file(path, self.args_entry.get().split())
        except OSError as e:
            self.log("\n%s" % e, "info")
            return
        self.program = path
        self.after_load("loaded %s" % os.path.basename(path))

    def after_load(self, message):
        self.dbg.cpu = self.cpm.cpu
        self.steps = 0
        self.view_start = self.mem_addr = self.cpm.cpu.pc
        self.prev_regs, self.prev_mem = {}, None
        self.title("%s - Z80 debugger" % (os.path.basename(self.program) if self.program else "empty"))
        self.log("\n" + message, "info")
        self.refresh()

    def open_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title="Open a CP/M program", filetypes=(
            ("CP/M programs", "*.com *.COM"), ("All files", "*")))
        if path:
            self.term.clear()
            self.load(path)

    def restart(self):
        if self.busy:
            return
        if self.program:
            self.load(self.program)
        else:
            self.empty()

    def set_cpu(self, name):
        if self.busy:
            self.cpu_var.set("Z180" if self.cpm.cpu.z180 else "Z80")
            return
        cls = Z180 if name == "Z180" else Z80
        if type(self.cpm.cpu) is not cls:
            self.cpm.cpu = cls(self.cpm.cpu.mem)
            self.dbg.cpu = self.cpm.cpu
            self.log("\nCPU: %s - restart to begin again on it" % name, "info")
            self.refresh()

    # ------------------------------------------------------------- running
    def action(fn):
        """Run fn as the one thing going on; ignore F-keys pressed meanwhile."""
        def wrapper(self, *a):
            if self.busy:
                return
            self.busy, self.stop_req = True, False
            self.snapshot()
            try:
                fn(self, *a)
            except Stopped:
                self.log("\nStopped at %04X" % self.cpm.cpu.pc, "info")
            finally:
                self.busy = False
            if self.closing:
                self.destroy()
                return
            self.refresh()
        wrapper.__name__ = fn.__name__
        return wrapper

    def finished(self):
        if self.cpm.exited or self.cpm.cpu.halted:
            self.dbg.end_message()
            return True
        return False

    @action
    def step(self):
        if not self.finished():
            self.dbg.trace(1)
            self.steps += 1

    @action
    def step_over(self):
        if not self.finished():
            self.dbg.trace(1, over=True)
            self.steps += 1

    def go(self, stop=None):
        self._go(stop)

    @action
    def _go(self, stop=None):
        if self.finished():
            return
        cpm, cpu, bps = self.cpm, self.cpm.cpu, self.dbg.bps
        self.set_status("running")
        first = True
        while True:
            if bps or stop is not None:
                for _ in range(5000):
                    if not first and (cpu.pc in bps or cpu.pc == stop):
                        self.log("\n%s at %04X" % ("Breakpoint" if cpu.pc in bps else "Stopped",
                                                   cpu.pc), "info")
                        return
                    first = False
                    cpm.step_one()
                    self.steps += 1
                    if self.finished():
                        return
            else:
                try:
                    cpm.run(max_steps=100000)
                finally:
                    self.steps += cpm._steps_last
                if self.finished():
                    return
            self.pump()
            self.set_status("running")

    def run_to_selected(self):
        if self.selected is not None:
            self.go(self.selected)

    def stop(self):
        if self.busy:
            self.stop_req = True

    def pump(self):
        """Let the window handle its events while the program runs."""
        try:
            self.update()
        except tk.TclError:
            self.closing = True
        if self.stop_req or self.closing:
            raise Stopped

    def wait_key(self):
        self.set_status("waiting for a key in the CONSOLE pane")
        if self.focus_get() is not self.cmd:
            self.term_text.focus_set()
        while not self.console._buf:
            self.pump()
            time.sleep(0.01)
        self.set_status("running")

    def on_close(self):
        if self.busy:
            self.closing = self.stop_req = True
        else:
            self.destroy()

    # ------------------------------------------------------------ console keys
    def on_console_key(self, e):
        if e.state & 4 and e.keysym.lower() == "c" and self.term_text.tag_ranges("sel") \
                and not self.busy:
            return None                                  # copy a selection
        if e.keysym in ("BackSpace",):
            code = 0x7F if self.console.backspace == "del" else 0x08
        elif e.keysym in _KEYS:
            code = _KEYS[e.keysym]
        elif e.char and len(e.char) == 1:
            code = ord(e.char)
            if code > 0xFF:
                code = 0x3F
        else:
            return None
        if e.keysym.startswith("F") and e.keysym[1:].isdigit():
            return None
        self.console.key(code)
        return "break"

    # ------------------------------------------------------------ command line
    def on_command(self, _e=None):
        text = self.cmd.get().strip()
        self.cmd.delete(0, "end")
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.hist_pos = len(self.history)
        self.log("\n-" + text, "cmd")
        words = text.split()
        if not words:
            return
        cmd = words[0].upper()
        if cmd in ("Q", "QUIT", "EXIT"):
            self.on_close()
        elif cmd == "G":
            if len(words) > 1:
                try:
                    self.cpm.cpu.pc = self.dbg.num(words[1])
                except ValueError:
                    self.log("\n?")
                    return
            self.go()
        else:
            self.run_command(text)

    @action
    def run_command(self, text):
        self.dbg.command(text)

    def recall(self, step):
        if not self.history:
            return "break"
        self.hist_pos = max(0, min(len(self.history), self.hist_pos + step))
        self.cmd.delete(0, "end")
        if self.hist_pos < len(self.history):
            self.cmd.insert(0, self.history[self.hist_pos])
        return "break"

    def log(self, text, tag=None):
        self.out.insert("end", text, tag or ())
        self.out.see("end")

    # ---------------------------------------------------------------- views
    def snapshot(self):
        self.prev_regs = self.reg_values()
        self.prev_mem = bytes(self.cpm.cpu.mem)

    def refresh(self):
        cpu = self.cpm.cpu
        self.cpu_var.set("Z180" if cpu.z180 else "Z80")
        if cpu.z180:
            self.phys_check.pack(side="right", padx=4)
        else:
            self.phys_check.pack_forget()
            self.phys_var.set(False)
        self.show_code(follow=True)
        self.show_regs()
        self.show_stack()
        self.show_memory()
        if self.cpm.exited:
            state = "ended"
        elif cpu.halted:
            state = "halted"
        else:
            state = "stopped"
        self.set_status(state)

    def set_status(self, state):
        cpu = self.cpm.cpu
        self.status.configure(text="%s   %s   PC=%04X   %d instructions   %s" % (
            "Z180" if cpu.z180 else "Z80", state, cpu.pc, self.steps,
            os.path.basename(self.program) if self.program else "(no program)"))

    def code_rows(self):
        h = self.code.winfo_height()
        line = tkfont.Font(font=self.font).metrics("linespace")
        return max(10, h // max(line, 1) - 1) if h > 1 else 30

    def show_code(self, follow=False):
        pc = self.cpm.cpu.pc
        rows = self.code_rows()
        if follow and (pc not in self.view_addrs or self.view_addrs.index(pc) > rows - 4):
            self.view_start = pc
        addrs, lines, a = [], [], self.view_start
        for _ in range(rows):
            n, text = self.dbg.line(a)
            addrs.append(a)
            lines.append(text)
            a = (a + n) & 0xFFFF
        self.view_addrs = addrs
        c = self.code
        c.delete("1.0", "end")
        for i, (addr, text) in enumerate(zip(addrs, lines)):
            mark = ">" if addr == pc else " "
            bp = "*" if addr in self.dbg.bps else " "
            tags = []
            if addr == pc:
                tags.append("pc")
            if addr in self.dbg.bps:
                tags.append("bp")
            if addr == self.selected:
                tags.append("chosen")
            c.insert("end", "%s%s %s\n" % (bp, mark, text), tuple(tags))

    def scroll_code(self, rows):
        if rows < 0:
            self.view_start = (self.view_start - 3) & 0xFFFF     # bytes: no way to go back by lines
        else:
            self.view_start = self.view_addrs[min(3, len(self.view_addrs) - 1)]
        self.show_code()
        return "break"

    def code_addr(self, e):
        row = int(self.code.index("@%d,%d" % (e.x, e.y)).split(".")[0]) - 1
        return self.view_addrs[row] if 0 <= row < len(self.view_addrs) else None

    def on_code_click(self, e):
        self.selected = self.code_addr(e)
        self.show_code()
        return "break"

    def on_code_double(self, e):
        self.selected = self.code_addr(e)
        self.toggle_selected()
        return "break"

    def toggle_selected(self):
        a = self.selected if self.selected is not None else self.cpm.cpu.pc
        bps = self.dbg.bps
        bps.remove(a) if a in bps else bps.append(a)
        self.show_code()

    def on_goto(self, _e):
        try:
            self.view_start = self.dbg.num(self.goto.get())
        except ValueError:
            return
        self.show_code()

    def reg_values(self):
        c = self.cpm.cpu
        alt = c.alt
        v = {"PC": c.pc, "SP": c.sp, "AF": c.af, "BC": c.bc, "DE": c.de, "HL": c.hl,
             "IX": c.ix, "IY": c.iy, "AF'": (alt[7] << 8) | c.f_alt,
             "BC'": (alt[0] << 8) | alt[1], "DE'": (alt[2] << 8) | alt[3],
             "HL'": (alt[4] << 8) | alt[5], "I": c.i, "R": c.rr}
        if c.z180:
            v.update(CBAR=c.cbar, BBR=c.bbr, CBR=c.cbr)
        return v

    def show_regs(self):
        v = self.reg_values()
        t = self.regs
        t.delete("1.0", "end")
        rows = (("PC", "SP"), ("AF", "AF'"), ("BC", "BC'"), ("DE", "DE'"), ("HL", "HL'"),
                ("IX", "IY"))
        for row in rows:
            for name in row:
                t.insert("end", "%-3s " % name, "dim")
                t.insert("end", "%04X    " % v[name],
                         "chg" if self.prev_regs and self.prev_regs.get(name) != v[name] else ())
            t.insert("end", "\n")
        t.insert("end", "%-3s " % "F", "dim")
        f = self.cpm.cpu.f
        old = self.prev_regs.get("AF", v["AF"]) & 0xFF
        for ch, m in zip("SZ-H-PNC", (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01)):
            t.insert("end", ch if f & m else "-", "chg" if (f ^ old) & m else ())
        c = self.cpm.cpu
        t.insert("end", "\n%-3s " % "I", "dim")
        t.insert("end", "%02X  R %02X  IM%d  IFF %d%d\n" % (c.i, c.rr, c.im, c.iff1, c.iff2))
        if c.z180:
            for name in ("CBAR", "BBR", "CBR"):
                t.insert("end", name + " ", "dim")
                t.insert("end", "%02X  " % v[name],
                         "chg" if self.prev_regs and self.prev_regs.get(name) != v[name] else ())
            t.insert("end", "\n" + c.mmu_info().split("  ", 1)[1].strip().replace("  ", "\n"), "dim")

    def show_stack(self):
        cpu = self.cpm.cpu
        mem = cpu.mem
        t = self.stack
        t.delete("1.0", "end")
        for i in range(14):
            a = (cpu.sp + 2 * i) & 0xFFFF
            w = mem[a] | (mem[(a + 1) & 0xFFFF] << 8)
            t.insert("end", "%04X  " % a, "dim")
            t.insert("end", "%04X\n" % w)

    def show_memory(self):
        t = self.mem
        t.delete("1.0", "end")
        phys = self.phys_var.get() and self.cpm.cpu.z180
        if phys:
            data = self.cpm.cpu.physical()
            width, size, prev = 5, len(data), None
        else:
            data = self.cpm.cpu.mem
            width, size, prev = 4, 0x10000, self.prev_mem
        start = self.mem_addr & ~0xF
        rows = max(4, self.mem.winfo_height() // max(tkfont.Font(font=self.font)
                                                      .metrics("linespace"), 1)) \
            if self.mem.winfo_height() > 1 else 8
        for r in range(rows):
            a = (start + 16 * r) % size
            t.insert("end", "%0*X " % (width, a), "dim")
            for i in range(16):
                b = data[(a + i) % size]
                chg = prev is not None and prev[(a + i) % size] != b
                t.insert("end", (" " if i != 8 else "  ") + "%02X" % b, "chg" if chg else ())
            t.insert("end", "  " + "".join(chr(data[(a + i) % size])
                                            if 32 <= data[(a + i) % size] < 127 else "."
                                            for i in range(16)) + "\n")

    def on_mem_addr(self, _e):
        text = self.mem_entry.get().strip()
        try:
            if self.phys_var.get():
                self.mem_addr = int(text.rstrip("hH"), 16)
            else:
                self.mem_addr = self.dbg.num(text)
        except ValueError:
            return
        self.show_memory()

    def scroll_mem(self, rows):
        size = len(self.cpm.cpu.physical()) if self.phys_var.get() else 0x10000
        self.mem_addr = (self.mem_addr + 16 * rows) % size
        self.show_memory()
        return "break"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Debugger window for CP/M programs")
    ap.add_argument("--cpu", choices=["z80", "z180"], default="z80")
    ap.add_argument("program", nargs="?", help=".COM file to load")
    ap.add_argument("args", nargs="*", help="its command line")
    ns = ap.parse_args(argv)
    app = App(ns.cpu)
    if ns.program:
        app.args_entry.insert(0, " ".join(ns.args))
        app.load(ns.program)
    app.cmd.focus_set()
    app.mainloop()


if __name__ == "__main__":
    main()
