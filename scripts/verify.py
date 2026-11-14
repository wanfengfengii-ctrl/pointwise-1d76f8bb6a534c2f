"""Acceptance checks: positive body verification plus rejection behaviour.

Run as a CLI; exits non-zero if any expectation fails.  It exercises the real
public API and covers both the happy path (object ids, types, sizes and raw
bodies on disk) and the mandated failures (truncation, bad checksum, trailing
bytes, missing base, corrupt delta), each of which must raise ``ValueError``.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import zlib

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/scripts")

from pack_import import import_pack
from packbuilder import PackBuilder, git_oid, chunkify, entry_header

failures = []


def expect_ok(label, cond):
    print(("  ok  " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def expect_value_error(label, chunks, target):
    try:
        import_pack(chunks, target)
    except ValueError:
        expect_ok(label + " raises ValueError", True)
        return
    expect_ok(label + " raises ValueError", False)


def main():
    work = tempfile.mkdtemp(prefix="verify_pack_")
    try:
        print("positive checks:")
        base = b"abc"
        base_oid = git_oid("blob", base)
        delta = bytes.fromhex("030490030121")
        pb = PackBuilder()
        pb.add_ref_delta(base_oid, delta)
        pb.add_object(3, base)
        pack = pb.build()

        target = os.path.join(work, "objects")
        records = import_pack(chunkify(pack), target)

        want = "9349f5b140d0cce46e41da5ae0cf40ad5f596853"
        expect_ok("result id 9349f5b1... present",
                  any(r[0] == want for r in records))
        expect_ok("result type/size blob/4",
                  [want, "blob", 4] in records)
        expect_ok("base object present",
                  any(r[0] == base_oid.hex() for r in records))
        expect_ok("stored reconstructed body == b'abc!'",
                  open(os.path.join(target, want), "rb").read() == b"abc!")
        expect_ok("stored base body == b'abc'",
                  open(os.path.join(target, base_oid.hex()), "rb").read()
                  == b"abc")
        expect_ok("records sorted by hex id",
                  records == sorted(records, key=lambda r: r[0]))
        expect_ok("one file per distinct object",
                  sorted(os.listdir(target)) == sorted(r[0] for r in records))

        print("rejection checks:")
        expect_value_error("truncated stream",
                           chunkify(pack[:len(pack) // 2]),
                           os.path.join(work, "t1"))
        expect_value_error("bad pack checksum",
                           chunkify(pb.build(bad_trailer=True)),
                           os.path.join(work, "t2"))
        expect_value_error("trailing byte after trailer",
                           chunkify(pb.build(extra=b"\x00")),
                           os.path.join(work, "t3"))
        missing = PackBuilder()
        missing.add_ref_delta(b"\x11" * 20, delta)
        expect_value_error("missing REF base",
                           chunkify(missing.build()),
                           os.path.join(work, "t4"))
        bad_delta = bytes([3, 4, 0x91, 9])      # copy offset 9 > base length 3
        pbd = PackBuilder()
        pbd.add_object(3, base)
        pbd.add_ref_delta(base_oid, bad_delta)
        expect_value_error("delta copy out of range",
                           chunkify(pbd.build()),
                           os.path.join(work, "t5"))
        corrupt = PackBuilder()
        corrupt._emit(entry_header(3, 3))
        corrupt._emit(b"\x78\x9c\x00\x00\x00\xff\xff")
        corrupt._count += 1
        expect_value_error("corrupt zlib data",
                           chunkify(corrupt.build()),
                           os.path.join(work, "t6"))

        print("\nverify %s (%d failure(s))"
              % ("PASSED" if not failures else "FAILED", len(failures)))
        return 1 if failures else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
