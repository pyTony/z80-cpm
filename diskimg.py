#!/usr/bin/env python3
"""
diskimg.py - read CP/M 2.2 disk images (raw sector dumps) described cpmtools-style:
sector size, tracks, sectors per track, block size, directory entries, skew, boot tracks
and a byte offset (a slice inside a bigger image, as in RomWBW hard disk images).

    from diskimg import Image, BUILTIN, detect
    img = Image(open("disk.img", "rb").read(), BUILTIN["ibm-3740"])
    for f in img.files():
        print(f.user, f.name, f.size)
        data = img.read(f)

Read only.  A cpmtools "diskdefs" file can be loaded with parse_diskdefs().
"""

import re
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Format:
    name: str
    seclen: int
    tracks: int
    sectrk: int
    blocksize: int
    maxdir: int
    skew: int = 0
    boottrk: int = 0
    offset: int = 0                  # bytes before the first track in the image
    slice_prefix: int = 0            # bytes before slice 0 when a slice number is used

    @property
    def size(self):
        return self.tracks * self.sectrk * self.seclen

    @property
    def dsm(self):
        return (self.tracks - self.boottrk) * self.sectrk * self.seclen // self.blocksize - 1


def _fmt(name, seclen, tracks, sectrk, blocksize, maxdir, skew=0, boottrk=0, prefix=0):
    return Format(name, seclen, tracks, sectrk, blocksize, maxdir, skew, boottrk, 0, prefix)


# The usual geometries (the numbers are those of the cpmtools/RomWBW disk definitions).
BUILTIN = {f.name: f for f in (
    _fmt("ibm-3740", 128, 77, 26, 1024, 64, skew=6, boottrk=2),          # 8" single density
    _fmt("wbw_fd360", 512, 80, 9, 2048, 128, boottrk=4),
    _fmt("wbw_fd720", 512, 160, 9, 2048, 128, boottrk=4),
    _fmt("wbw_fd120", 512, 160, 15, 2048, 256, boottrk=2),
    _fmt("wbw_fd144", 512, 160, 18, 4096, 256, boottrk=2),
    _fmt("wbw_hd512", 512, 1040, 16, 4096, 512, boottrk=16),             # slice 8320 KB
    _fmt("wbw_hd1k", 512, 1024, 16, 4096, 1024, boottrk=2, prefix=1 << 20),   # slice 8 MB
)}
ALIASES = {"hd512": "wbw_hd512", "hd1k": "wbw_hd1k", "fd720": "wbw_fd720",
           "fd144": "wbw_fd144", "fd360": "wbw_fd360", "fd120": "wbw_fd120",
           "ibm": "ibm-3740", "8": "ibm-3740"}


def parse_diskdefs(text):
    """{name: Format} from the text of a cpmtools diskdefs file."""
    defs, cur = {}, None
    for line in text.splitlines():
        words = line.split("#")[0].split()
        if not words:
            continue
        if words[0] == "diskdef" and len(words) > 1:
            cur = {"name": words[1]}
        elif words[0] == "end" and cur is not None:
            try:
                defs[cur["name"]] = Format(
                    cur["name"], cur["seclen"], cur["tracks"], cur["sectrk"], cur["blocksize"],
                    cur["maxdir"], cur.get("skew", 0), cur.get("boottrk", 0), cur.get("offset", 0))
            except KeyError:
                pass
            cur = None
        elif cur is not None and len(words) > 1:
            key, val = words[0], words[1]
            if key == "offset":
                m = re.fullmatch(r"(\d+)([KMTS]?)", val.upper())
                if m:
                    n, u = int(m.group(1)), m.group(2)
                    cur["offset"] = {"": n, "K": n << 10, "M": n << 20,
                                     "T": n * cur.get("sectrk", 0) * cur.get("seclen", 0),
                                     "S": n * cur.get("seclen", 0)}[u]
            elif key in ("seclen", "tracks", "sectrk", "blocksize", "maxdir", "skew", "boottrk"):
                try:
                    cur[key] = int(val, 0)
                except ValueError:
                    pass
    return defs


@dataclass
class File:
    user: int
    name: str                 # "NAME.EXT" (upper case, attribute bits removed)
    attrs: str                # "R" read-only, "S" system, "A" archived
    records: int              # 128-byte records
    blocks: list

    @property
    def size(self):
        return self.records * 128


class Image:
    def __init__(self, data, fmt, slice_no=0):
        self.data = data
        off = fmt.offset
        if slice_no:
            off += fmt.slice_prefix + slice_no * fmt.size
        elif fmt.slice_prefix and not fmt.offset:
            off = 0                                  # slice 0 of a bare slice image
        self.fmt = replace(fmt, offset=off)
        f = self.fmt
        self.ptr16 = f.dsm >= 256                    # 16-bit block numbers in the entries
        per_entry = 8 if self.ptr16 else 16
        self.exm = max(1, per_entry * f.blocksize // 16384) - 1
        tab, gap = [], 0
        for i in range(f.sectrk):                    # cpmtools' skew table
            while gap in tab:
                gap = (gap + 1) % f.sectrk
            tab.append(gap)
            gap = (gap + f.skew) % f.sectrk
        self.skewtab = tab

    # ------------------------------------------------------------ low level
    def _sector(self, logical):
        f = self.fmt
        track, sec = divmod(logical, f.sectrk)
        pos = f.offset + (track * f.sectrk + self.skewtab[sec]) * f.seclen
        return self.data[pos:pos + f.seclen]

    def block(self, n):
        f = self.fmt
        per = f.blocksize // f.seclen
        first = f.boottrk * f.sectrk + n * per
        return b"".join(self._sector(first + i) for i in range(per))

    def fits(self):
        """True if the image is big enough for this geometry."""
        return len(self.data) >= self.fmt.offset + self.fmt.size

    # ------------------------------------------------------------ directory
    def raw_directory(self):
        f = self.fmt
        nblocks = -(-f.maxdir * 32 // f.blocksize)
        d = b"".join(self.block(i) for i in range(nblocks))
        return d[:f.maxdir * 32]

    def sane(self):
        """(used entries, bad entries) of the directory: a wrong geometry gives many bad ones."""
        used = bad = 0
        d = self.raw_directory()
        if len(d) < self.fmt.maxdir * 32:
            return 0, 1
        for i in range(0, len(d), 32):
            e = d[i:i + 32]
            st = e[0]
            if st == 0xE5:
                continue
            if st <= 15:
                name = bytes(c & 0x7F for c in e[1:12])
                if all(0x20 <= c < 0x7F for c in name) and name[:8].strip():
                    used += 1
                else:
                    bad += 1
            elif st not in (0x20, 0x21):
                bad += 1
        return used, bad

    def files(self):
        """The files of all user areas 0-15, in directory order of their first extent."""
        d = self.raw_directory()
        found = {}
        order = []
        for i in range(0, len(d), 32):
            e = d[i:i + 32]
            if e[0] > 15:
                continue
            name = bytes(c & 0x7F for c in e[1:9]).decode("latin1").rstrip()
            ext = bytes(c & 0x7F for c in e[9:12]).decode("latin1").rstrip()
            key = (e[0], name, ext)
            ex = (e[12] & 0x1F) | ((e[14] & 0x3F) << 5)
            found.setdefault(key, []).append((ex, e))
            if key not in order:
                order.append(key)
        out = []
        for key in order:
            ents = sorted(found[key], key=lambda t: t[0])
            blocks, records = [], 0
            for n, (ex, e) in enumerate(ents):
                if self.ptr16:
                    ptrs = [e[16 + j] | (e[17 + j] << 8) for j in range(0, 16, 2)]
                else:
                    ptrs = list(e[16:32])
                blocks += [p for p in ptrs if p]
                if n < len(ents) - 1:
                    records += (self.exm + 1) * 128
                else:
                    records += (ex & self.exm) * 128 + e[15]
            # (the records of the earlier entries are full: this also fixes sparse extents)
            if ents:
                records = (ents[-1][0] // (self.exm + 1)) * (self.exm + 1) * 128 + \
                    (ents[-1][0] & self.exm) * 128 + ents[-1][1][15]
            attrs = "".join(ch for ch, k in (("R", 9), ("S", 10), ("A", 11))
                            if ents[0][1][k] & 0x80)
            nm = key[1] + ("." + key[2] if key[2] else "")
            out.append(File(key[0], nm, attrs, records, blocks))
        return out

    def read(self, f):
        """The contents of a File (a whole number of 128-byte records)."""
        data = b"".join(self.block(b) for b in f.blocks if b <= self.fmt.dsm)
        return data[:f.size]


# --------------------------------------------------------------------------- detection
def resolve(spec, defs=None):
    """'hd1k', 'wbw_hd1k:3' -> (Format, slice number)."""
    defs = dict(BUILTIN, **(defs or {}))
    name, _, sl = spec.partition(":")
    name = ALIASES.get(name.lower(), name)
    for k, v in defs.items():
        if k.lower() == name.lower():
            return v, int(sl) if sl.isdigit() else 0
    return None, 0


def detect(data, defs=None):
    """The (Format, slice, offset variant) that gives a sane directory, or None."""
    cands = []
    for fmt in dict(BUILTIN, **(defs or {})).values():
        cands.append((fmt, fmt.offset))
        if fmt.slice_prefix:
            cands.append((fmt, fmt.slice_prefix))        # first slice of a combined image
    cands.sort(key=lambda c: (len(data) != c[1] + c[0].size, abs(len(data) - c[1] - c[0].size)))
    for fmt, off in cands:
        img = Image(data, replace(fmt, offset=off, slice_prefix=0))
        if not img.fits():
            continue
        used, bad = img.sane()
        if used and not bad:
            return img
    return None
