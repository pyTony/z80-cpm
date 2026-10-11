#!/usr/bin/env python3
"""
fix_turbo_ansi.py - repair the "ANSI" terminal definition for Turbo Pascal 3.

The ANSI entry in TINST.DTA (and therefore in any TURBO.COM installed with it)
has a damaged cursor-addressing string: the two bytes ESC [ were replaced by a
single 0x03, so the editor prints  <^C>02;01H  instead of moving the cursor.
Other entries (DEC Rainbow, Texas Instruments, ...) have it correct.

This script rewrites the string to  ESC [ 0 0 ; 0 0 H  and updates the two
position offsets that follow it.  Run it on TURBO.COM and/or TINST.DTA:

    python fix_turbo_ansi.py I/0/TURBO.COM I/0/TINST.DTA

A backup is saved next to each file as NAME.bak (only if none exists yet).
Files that are already fixed or don't contain the bad entry are left alone.
"""
import shutil
import sys

BAD_GOTO = bytes([0x07, 0x03]) + b"00;00H"      # length 7, ^C, "00;00H"
GOOD_GOTO = bytes([0x08, 0x1B, 0x5B]) + b"00;00H"


def patch(data):
    """Return the patched bytes, or None if there is nothing to fix."""
    i = data.find(BAD_GOTO)
    if i < 0:
        return None
    out = bytearray(data)
    # The new string is one byte longer; in TURBO.COM the next byte is padding
    # and in TINST.DTA it is a stray extra "H" - both are safe to overwrite.
    if out[i + len(BAD_GOTO)] not in (0x00, ord("H")):
        raise ValueError("unexpected bytes after the cursor string; not patching")
    out[i:i + len(GOOD_GOTO)] = GOOD_GOTO
    # Offsets of the ';' and 'H' inside the string: 03,06 -> 04,07
    window = bytes(out[i + len(GOOD_GOTO):i + len(GOOD_GOTO) + 48])
    for pattern in (b"\x01\x01\x06\x03", b"\x06\x03\x01\x01"):
        j = window.find(pattern)
        if j >= 0:
            k = i + len(GOOD_GOTO) + j + pattern.find(b"\x06")
            out[k:k + 2] = b"\x07\x04"
            return bytes(out)
    raise ValueError("could not find the position offsets; not patching")


def main(paths):
    if not paths:
        print(__doc__)
        return 1
    status = 0
    for path in paths:
        try:
            with open(path, "rb") as f:
                data = f.read()
            new = patch(data)
        except (OSError, ValueError) as e:
            print("%s: %s" % (path, e))
            status = 1
            continue
        if new is None:
            print("%s: nothing to fix (already OK?)" % path)
            continue
        try:
            with open(path + ".bak", "xb") as f:
                f.write(data)
        except FileExistsError:
            pass
        with open(path, "wb") as f:
            f.write(new)
        print("%s: fixed" % path)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
