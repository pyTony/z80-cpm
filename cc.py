"""Compile a C file with uc80 + um80 + ul80 and run the result in the CP/M emulator.

    python cc.py hello                 hello.c -> hello.com, run it
    python cc.py hello --disk B3       hello.com -> disks/B/3 (drive B, user 3), run on that disk
    python cc.py hello.c --cpu z80 --no-run --keep
    python cc.py whetstone --full      every compiler, assembler and linker message
    python cc.py whetstone --cpu z80 -o whet80.com    a Z80 build next to the Z180 one

--cpu z180 (the default) links the Z180 runtime from the z180 folder: multiplies with MLT and
the z180.h helpers.  Such a program needs a Z180; --cpu z80 builds one for any Z80.  The
uc80 and um80 pip packages are installed on first use.  The screen shows a summary: the build
line, any warnings that matter, then the program's output and its run time (cpm.py --time).
Everything the tools print goes to BASENAME.log (--full shows it on the screen too).
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = '#include <stdio.h>\n\nint main(void) {\n    printf("Hello from uc80\\n");\n    return 0;\n}\n'

# uc80 embeds the runtime routines the program calls; libc needs a few more, so the linker adds
# whole runtime modules and each embedded routine is then defined twice (the embedded one is
# used).  Library names the program never calls being undefined is harmless too.
TWICE = re.compile(r"Mult\. Def\. Global")
UNUSED = re.compile(r"never used")
NOTE = re.compile(r"^(Warning|Error|ERROR)", re.I)

_color = sys.stdout.isatty()
if _color and os.name == "nt":
    os.system("")                       # turn on ANSI colours in the Windows console


def say(text, color=None):
    codes = {"red": "31", "green": "32", "yellow": "33", "cyan": "36", "gray": "90"}
    print("\x1b[%sm%s\x1b[0m" % (codes[color], text) if color and _color else text, flush=True)


def fail(message):
    say("ERROR: " + message, "red")
    sys.exit(1)


class Tools:
    def __init__(self, full, log_path):
        self.full = full
        self.log = []
        self.log_path = log_path
        open(log_path, "w").close()

    def run(self, what, *args):
        """python -m/args...; messages are kept, shown with --full or when the tool fails."""
        p = subprocess.run([sys.executable, *args], capture_output=True, text=True)
        out = (p.stdout + p.stderr).splitlines()
        self.log += out
        with open(self.log_path, "a") as f:
            f.write("$ %s\n%s" % (" ".join(args), "".join(line + "\n" for line in out)))
        if self.full or p.returncode:
            for line in out:
                say("  " + line, "gray" if self.full else None)
        if p.returncode:
            fail("%s failed (messages in %s)" % (what, self.log_path))
        return out


def ensure_packages():
    for package in ("uc80", "um80"):
        if subprocess.run([sys.executable, "-m", "pip", "show", package],
                          capture_output=True).returncode:
            say("Package %s is missing, installing with pip..." % package, "yellow")
            if subprocess.run([sys.executable, "-m", "pip", "install", package]).returncode:
                fail("pip could not install " + package)


def disk_target(spec, disks_dir, name):
    m = re.fullmatch(r"([A-Pa-p])(\d{0,2})", spec)
    if not m:
        fail("--disk should look like B or B3 (drive, optional user 0-15)")
    drive, user = m.group(1).upper(), int(m.group(2) or 0)
    if user > 15:
        fail("CP/M user numbers go from 0 to 15")
    if len(name) > 8:
        fail("CP/M file names have at most 8 characters before the dot: '%s'" % name)
    target = os.path.join(disks_dir, drive)
    if os.path.isdir(os.path.join(target, "0")):        # drive has user areas
        target = os.path.join(target, str(user))
    elif user:
        fail("Drive %s has no user areas (no folder named 0)" % drive)
    os.makedirs(target, exist_ok=True)
    return os.path.join(target, name + ".com"), drive, user


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file", help="hello, hello.c or hello.com")
    ap.add_argument("-o", "--output", help="name of the .com (default: the source's name)")
    ap.add_argument("--cpu", choices=["z80", "z180"], default="z180")
    ap.add_argument("--disk", default="", help="drive (and user) to put the .com on: B or B3")
    ap.add_argument("--disks-dir", default="disks")
    ap.add_argument("--no-run", action="store_true", help="build only")
    ap.add_argument("--keep", action="store_true", help="keep the .mac and .rel files")
    ap.add_argument("--full", action="store_true", help="show all tool messages")
    ns = ap.parse_args(argv)

    base = re.sub(r"\.(c|com)$", "", ns.file, flags=re.I)
    out = re.sub(r"\.com$", "", ns.output, flags=re.I) if ns.output else base
    src, asm, rel, com = base + ".c", out + ".mac", out + ".rel", out + ".com"
    ensure_packages()
    if not os.path.exists(src):
        say("%s not found, creating a small example." % src, "yellow")
        with open(src, "w") as f:
            f.write(EXAMPLE)
    for f in (asm, rel, com):
        if os.path.exists(f):
            os.remove(f)

    start = time.perf_counter()
    tools = Tools(ns.full, out + ".log")
    lib = tools.run("uc80", "-m", "uc80.main", "--print-lib-dir")[-1].strip()
    uc80_args, extra = [], []
    if ns.cpu == "z180":
        z180 = os.path.join(HERE, "z180")
        rt, z180rel = os.path.join(z180, "z180rt.mac"), os.path.join(z180, "z180.rel")
        tools.run("z180/mkruntime.py", os.path.join(z180, "mkruntime.py"), rt)
        tools.run("um80 on z180/z180.mac", "-m", "um80.um80", os.path.join(z180, "z180.mac"), "-o", z180rel)
        uc80_args, extra = ["--runtime-lib", rt, "-I", z180], [z180rel]
    tools.run("uc80 (C compiler)", "-m", "uc80.main", src, "-o", asm, *uc80_args)
    tools.run("um80 (assembler)", "-m", "um80.um80", asm, "-o", rel)
    link = tools.run("ul80 (linker)", "-m", "um80.ul80", rel, *extra,
                     os.path.join(lib, "libc.lib"), os.path.join(lib, "runtime.lib"), "-o", com)
    if not os.path.exists(com):
        fail("No %s was produced." % com)
    if not ns.keep:
        for f in (asm, rel):
            if os.path.exists(f):
                os.remove(f)
    took = time.perf_counter() - start

    say("%s -> %s  (%s, %s bytes, built in %.1f s)" % (
        src, com, ns.cpu.upper(), format(os.path.getsize(com), ","), took), "green")
    if not ns.full:
        twice = sum(1 for line in link if TWICE.search(line))
        unused = sum(1 for line in link if UNUSED.search(line))
        if twice or unused:
            say("  linker: %d runtime routines linked twice, %d unused library names undefined "
                "(harmless; see %s)" % (twice, unused, tools.log_path), "gray")
        for line in tools.log:
            if NOTE.match(line.strip()) and not TWICE.search(line) and not UNUSED.search(line):
                say("  " + line.strip(), "yellow")

    cmd = [sys.executable, os.path.join(HERE, "cpm.py"), "--time", "--cpu", ns.cpu]
    if ns.disk:
        target, drive, user = disk_target(ns.disk, ns.disks_dir, os.path.basename(out))
        os.replace(com, target)
        com = target
        say("Moved to %s (drive %s:, user %d)" % (target, drive, user), "green")
        cmd += ["--disk", ns.disks_dir]
    if not ns.no_run:
        say("--- running (Ctrl+] leaves) ---", "cyan")
        sys.exit(subprocess.run(cmd + [com]).returncode)


if __name__ == "__main__":
    main()
