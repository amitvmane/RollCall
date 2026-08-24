#!/usr/bin/env python3
"""Rebuild a SQLite database from an orphaned -wal file.

Why this exists: in WAL mode the recent writes live in `rollcall.db-wal`, not in
`rollcall.db`. If the main db file is replaced or lost while a large WAL is
sitting next to it, SQLite can no longer recover it (the WAL's page images no
longer belong to the db they're beside) — but the pages themselves are still
all there, in plain form, inside the WAL. This reads those frames directly and
reassembles a database image out of them.

Usage:
    python3 scripts/wal_recover.py <wal-file> -o recovered.db [--base <db>]

    <wal-file>   the orphaned -wal (or a copy of it, under any name)
    --base       optional main db to use for pages the WAL never touched;
                 omit it and untouched pages are written as zeros
    --strict     stop at the first frame whose checksum doesn't verify
                 (default: keep going, and report where verification broke)

The WAL format (https://sqlite.org/fileformat2.html#walformat):
    32-byte header, then repeating [24-byte frame header | one page of data].
Each frame header names the page number it carries, so replaying the frames in
order — last writer wins — reconstructs the db as of the final commit.

Afterwards ALWAYS check the result before trusting it:
    sqlite3 recovered.db 'PRAGMA integrity_check;'
"""
import argparse
import struct
import sys

WAL_MAGIC_LE = 0x377F0682  # checksums are little-endian
WAL_MAGIC_BE = 0x377F0683  # checksums are big-endian

WAL_HEADER_SIZE = 32
FRAME_HEADER_SIZE = 24


def _checksum(data, s0, s1, big_endian):
    """SQLite's WAL checksum: a running pair of 32-bit sums over 8-byte blocks."""
    fmt = ">II" if big_endian else "<II"
    mask = 0xFFFFFFFF
    for off in range(0, len(data) - 7, 8):
        x0, x1 = struct.unpack_from(fmt, data, off)
        s0 = (s0 + x0 + s1) & mask
        s1 = (s1 + x1 + s0) & mask
    return s0, s1


def recover(wal_path, out_path, base_path=None, strict=False):
    with open(wal_path, "rb") as fh:
        wal = fh.read()

    if len(wal) < WAL_HEADER_SIZE:
        sys.exit(f"{wal_path}: too small to be a WAL ({len(wal)} bytes)")

    magic, fmt_version, page_size, ckpt_seq, salt1, salt2, hchk1, hchk2 = struct.unpack(
        ">IIIIIIII", wal[:WAL_HEADER_SIZE]
    )

    if magic not in (WAL_MAGIC_LE, WAL_MAGIC_BE):
        sys.exit(
            f"{wal_path}: not a WAL file (magic {magic:#x}). "
            "If this is a plain SQLite db, you don't need this script."
        )
    big_endian = magic == WAL_MAGIC_BE

    # A page_size of 1 is SQLite's encoding for 65536.
    if page_size == 1:
        page_size = 65536
    if page_size < 512 or page_size & (page_size - 1):
        sys.exit(f"{wal_path}: implausible page size {page_size}")

    frame_size = FRAME_HEADER_SIZE + page_size
    n_frames = (len(wal) - WAL_HEADER_SIZE) // frame_size

    print(f"[wal] {wal_path}")
    print(f"[wal]   page size     : {page_size}")
    print(f"[wal]   frames present: {n_frames}")
    print(f"[wal]   salt          : {salt1:#x} {salt2:#x}  (checkpoint seq {ckpt_seq})")

    # The running checksum is seeded from the first 24 bytes of the WAL header.
    s0, s1 = _checksum(wal[:24], 0, 0, big_endian)
    header_ok = (s0, s1) == (hchk1, hchk2)
    print(f"[wal]   header cksum  : {'ok' if header_ok else 'MISMATCH (continuing)'}")

    pages = {}           # pgno -> page bytes, last write wins
    committed = {}       # snapshot of `pages` as of the last commit frame
    commit_dbsize = 0    # db size in pages recorded by that commit frame
    last_good = 0        # frames verified so far
    salt_breaks_at = None

    for i in range(n_frames):
        off = WAL_HEADER_SIZE + i * frame_size
        pgno, dbsize, fsalt1, fsalt2, fchk1, fchk2 = struct.unpack_from(
            ">IIIIII", wal, off
        )
        page = wal[off + FRAME_HEADER_SIZE:off + frame_size]

        # A salt change means the WAL was reset and restarted; frames past that
        # point belong to a different generation of the file.
        if (fsalt1, fsalt2) != (salt1, salt2):
            salt_breaks_at = i
            break

        s0, s1 = _checksum(wal[off:off + 8], s0, s1, big_endian)
        s0, s1 = _checksum(page, s0, s1, big_endian)
        if (s0, s1) != (fchk1, fchk2):
            print(f"[wal]   checksum broke at frame {i} (page {pgno})")
            if strict:
                break
            # Past a bad frame the running checksum is meaningless, so stop
            # verifying but keep replaying — the page data is usually intact.
            s0, s1 = fchk1, fchk2
        else:
            last_good = i + 1

        if pgno > 0:
            pages[pgno] = page
        if dbsize:  # non-zero marks a commit frame
            committed = dict(pages)
            commit_dbsize = dbsize

    if salt_breaks_at is not None:
        print(f"[wal]   salt changed at frame {salt_breaks_at} — stopped there")
    print(f"[wal]   frames checksum-verified: {last_good}/{n_frames}")

    if committed:
        pages, dbsize = committed, commit_dbsize
        print(f"[wal]   last commit: db of {dbsize} pages")
    else:
        dbsize = max(pages) if pages else 0
        print(
            f"[wal]   no commit frame found — replaying all {len(pages)} pages "
            f"(db assumed {dbsize} pages)"
        )

    if not pages:
        sys.exit("[wal] no usable frames — nothing to recover")

    base = b""
    if base_path:
        with open(base_path, "rb") as fh:
            base = fh.read()
        print(f"[base] {base_path}: {len(base) // page_size} pages, used for gaps")

    if 1 not in pages and len(base) < page_size:
        print(
            "[!] page 1 (the file header) is not in the WAL and no --base was "
            "given — the output will not be a valid SQLite file.",
            file=sys.stderr,
        )

    missing = 0
    with open(out_path, "wb") as out:
        for pgno in range(1, dbsize + 1):
            page = pages.get(pgno)
            if page is None:
                start = (pgno - 1) * page_size
                page = base[start:start + page_size]
                if len(page) != page_size:
                    page = b"\x00" * page_size
                    missing += 1
            out.write(page)

    print(f"[out] wrote {out_path}: {dbsize} pages ({dbsize * page_size} bytes)")
    if missing:
        print(f"[out] {missing} page(s) had no source and were zero-filled")
    print(f"[out] now verify:  sqlite3 {out_path} 'PRAGMA integrity_check;'")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wal", help="the orphaned -wal file (or a copy of it)")
    ap.add_argument("-o", "--out", required=True, help="path to write the rebuilt db")
    ap.add_argument("--base", help="optional main db supplying pages the WAL lacks")
    ap.add_argument("--strict", action="store_true",
                    help="stop at the first checksum mismatch")
    args = ap.parse_args()
    return recover(args.wal, args.out, args.base, args.strict)


if __name__ == "__main__":
    sys.exit(main())
