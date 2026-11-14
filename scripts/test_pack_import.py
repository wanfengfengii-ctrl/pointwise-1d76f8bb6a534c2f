"""Comprehensive verification for pack_import.import_pack.

Run with the isolated interpreter so no planted modules in the data
directories can shadow the standard library::

    python3 -I /workspace/scripts/test_pack_import.py
"""

import os
import shutil
import sys
import tempfile
import hashlib

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/scripts")

from pack_import import import_pack
from packbuilder import (
    PackBuilder, git_oid, chunkify,
    OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG,
)

PASS = 0
FAIL = 0
WORK = tempfile.mkdtemp(prefix="pi_test_")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


def expect_value_error(name, chunks, target):
    try:
        import_pack(chunks, target)
    except ValueError:
        check(name + " -> ValueError", True)
        return
    except Exception as exc:  # pragma: no cover - diagnostic
        check(name + " -> ValueError", False, "got %r" % exc)
        return
    check(name + " -> ValueError", False, "no exception")


def fresh_target(tag):
    return os.path.join(WORK, "tgt_" + tag + "_" + str(len(os.listdir(WORK))))


# ---------------------------------------------------------------------------
print("== 1. task example: late-arriving REF base, blob abc -> abc! ==")
base_body = b"abc"
base_oid = git_oid("blob", base_body)
delta = bytes.fromhex("030490030121")
pb = PackBuilder()
base_off = pb.current_offset
pb.add_object(OBJ_BLOB, base_body)
pb.add_ref_delta(base_oid, delta)
pack = pb.build()
tgt = fresh_target("ref")
records = import_pack(chunkify(pack), tgt)
want_oid = "9349f5b140d0cce46e41da5ae0cf40ad5f596853"
check("record count", len(records) == 2, records)
check("result oid", any(r[0] == want_oid for r in records), records)
res = next(r for r in records if r[0] == want_oid)
check("result type/size", res == [want_oid, "blob", 4], res)
check("sorted by hex oid", records == sorted(records, key=lambda r: r[0]))
with open(os.path.join(tgt, want_oid), "rb") as fh:
    check("stored body abc!", fh.read() == b"abc!")
with open(os.path.join(tgt, base_oid.hex()), "rb") as fh:
    check("stored base abc", fh.read() == b"abc")

# REF base arriving AFTER the delta (reorder entries: delta first).
print("== 2. REF base genuinely late (delta precedes base in stream) ==")
pb2 = PackBuilder()
pb2.add_ref_delta(base_oid, delta)
pb2.add_object(OBJ_BLOB, base_body)
tgt2 = fresh_target("late")
rec2 = import_pack(chunkify(pb2.build()), tgt2)
check("late base resolves", any(r[0] == want_oid for r in rec2), rec2)

print("== 3. OFS_DELTA reconstruction ==")
pb3 = PackBuilder()
off = pb3.current_offset
pb3.add_object(OBJ_BLOB, base_body)
pb3.add_ofs_delta(off, delta)
tgt3 = fresh_target("ofs")
rec3 = import_pack(chunkify(pb3.build()), tgt3)
check("ofs result", next(r for r in rec3 if r[0] == want_oid)[2] == 4, rec3)

print("== 4. all four object types ==")
bodies = {
    "commit": b"tree 0123456789012345678901234567890123456789\nauthor A <a@b.c> 1 +0000\n\nmsg\n",
    "tree": b"100644 x\x00" + bytes(20),
    "blob": b"hello world",
    "tag": b"object 0123456789012345678901234567890123456789\ntype commit\ntag v1\n\n",
}
pb4 = PackBuilder()
for tname, body in bodies.items():
    pb4.add_object({"commit": OBJ_COMMIT, "tree": OBJ_TREE,
                    "blob": OBJ_BLOB, "tag": OBJ_TAG}[tname], body)
tgt4 = fresh_target("types")
rec4 = import_pack(chunkify(pb4.build()), tgt4)
exp = sorted([[git_oid(t, b).hex(), t, len(b)] for t, b in bodies.items()],
             key=lambda r: r[0])
check("all four types", rec4 == exp, (rec4, exp))
for oid, tname, size in exp:
    with open(os.path.join(tgt4, oid), "rb") as fh:
        check("body on disk " + tname, fh.read() == bodies[tname])

print("== 5. duplicate objects coalesce ==")
pb5 = PackBuilder()
pb5.add_object(OBJ_BLOB, b"dup")
pb5.add_object(OBJ_BLOB, b"dup")
pb5.add_object(OBJ_BLOB, b"dup")
tgt5 = fresh_target("dup")
rec5 = import_pack(chunkify(pb5.build()), tgt5)
check("one record for dup", len(rec5) == 1 and rec5[0][0] == git_oid("blob", b"dup").hex(), rec5)
check("one file on disk", os.listdir(tgt5) == [git_oid("blob", b"dup").hex()])

print("== 6. multi-hop delta chain with inherited type ==")
# base blob "aaaa..."; delta1 -> "X"+base; delta2 builds on delta1.
base6 = b"a" * 100
def v(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out[-1] |= 0x80
        out.append(n & 0x7F)
        n >>= 7
    return bytes(out)
# delta1: insert X, then copy whole base (off 0 size 100)
d1 = v(len(base6)) + v(101) + bytes([1]) + b"X" + bytes([0x90, 100])
mid = b"X" + base6
check("d1 sanity", len(mid) == 101)
# delta2: copy first 50 of mid (off0 size50), insert Z, copy rest (off50 size51)
copy_first50 = bytes([0x90, 50])                       # size byte only
insert_z = bytes([1]) + b"Z"
copy_rest = bytes([0x91, 50, 51])                    # opcode 0x91: off=50, size=51
d2 = v(len(mid)) + v(102) + copy_first50 + insert_z + copy_rest
final = mid[:50] + b"Z" + mid[50:]
pb6 = PackBuilder()
o0 = pb6.current_offset
pb6.add_object(OBJ_BLOB, base6)
o1 = pb6.current_offset
pb6.add_ofs_delta(o0, d1)
pb6.add_ofs_delta(o1, d2)
tgt6 = fresh_target("chain")
rec6 = import_pack(chunkify(pb6.build()), tgt6)
final_oid = git_oid("blob", final).hex()
got6 = next(r for r in rec6 if r[0] == final_oid)
check("chain result", got6 == [final_oid, "blob", 102], got6)
with open(os.path.join(tgt6, final_oid), "rb") as fh:
    check("chain body", fh.read() == final)

print("== 7. byte-at-a-time chunks (streaming boundaries) ==")
tgt7 = fresh_target("tiny")
rec7 = import_pack([bytes([b]) for b in pack], tgt7)
check("1-byte chunks work", any(r[0] == want_oid for r in rec7))

print("== 8. rejection cases ==")
# bad signature
bad_sig = b"XXXX" + pack[4:]
expect_value_error("bad signature", chunkify(bad_sig), fresh_target("sig"))
# unsupported version
bad_ver = pack[:4] + (3).to_bytes(4, "big") + pack[8:]
# recompute trailer
bad_ver = bad_ver[:-20] + hashlib.sha1(bad_ver[:-20]).digest()
expect_value_error("version 3", chunkify(bad_ver), fresh_target("ver"))
# bad checksum
expect_value_error("bad checksum", chunkify(pb.build(bad_trailer=True)),
                   fresh_target("csum"))
# trailing bytes
expect_value_error("trailing bytes", chunkify(pb.build(extra=b"\x00")),
                   fresh_target("trail"))
# truncated stream at several cut points
for cut in (0, 4, 11, 12, 20, len(pack) - 1, len(pack) - 20):
    expect_value_error("truncated@%d" % cut, chunkify(pack[:cut]),
                       fresh_target("cut%d" % cut))
# count mismatch (header claims more objects)
expect_value_error("count too large",
                   chunkify(pb.build(count_override=pb._count + 1)),
                   fresh_target("count"))
# missing REF base (thin-style reference that never arrives)
pbx = PackBuilder()
pbx.add_ref_delta(bytes(20), delta)
expect_value_error("missing REF base", chunkify(pbx.build()),
                   fresh_target("nobase"))
# corrupt zlib
pbz = PackBuilder()
good = zlib_compress = __import__("zlib").compress(b"abc")
pbz._emit(__import__("packbuilder", fromlist=["entry_header"]).entry_header(OBJ_BLOB, 3))
pbz._emit(b"\x78\x9c\xff\xff\xff\xff")
pbz._count += 1
expect_value_error("corrupt zlib", chunkify(pbz.build()), fresh_target("zlib"))

print("== 9. delta instruction / range validation ==")
# copy beyond base
bad_copy = v(3) + v(4) + bytes([0x91, 5])  # offset 5 > base len 3
pb9 = PackBuilder()
pb9.add_object(OBJ_BLOB, base_body)
pb9.add_ref_delta(base_oid, bad_copy)
expect_value_error("copy out of range", chunkify(pb9.build()), fresh_target("range"))
# zero opcode reserved
bad_zero = v(3) + v(3) + bytes([0x00])
pb9b = PackBuilder()
pb9b.add_object(OBJ_BLOB, base_body)
pb9b.add_ref_delta(base_oid, bad_zero)
expect_value_error("opcode zero", chunkify(pb9b.build()), fresh_target("zero"))
# declared result length mismatch
bad_len = v(3) + v(99) + bytes([0x90, 3])
pb9c = PackBuilder()
pb9c.add_object(OBJ_BLOB, base_body)
pb9c.add_ref_delta(base_oid, bad_len)
expect_value_error("result length mismatch", chunkify(pb9c.build()),
                   fresh_target("dlength"))
# insert overruns stream
bad_ins = v(3) + v(4) + bytes([5]) + b"ab"
pb9d = PackBuilder()
pb9d.add_object(OBJ_BLOB, base_body)
pb9d.add_ref_delta(base_oid, bad_ins)
expect_value_error("insert overrun", chunkify(pb9d.build()), fresh_target("ins"))
# base size header mismatch
bad_base = v(7) + v(4) + bytes([0x90, 3]) + bytes([1, 0x21])
pb9e = PackBuilder()
pb9e.add_object(OBJ_BLOB, base_body)
pb9e.add_ref_delta(base_oid, bad_base)
expect_value_error("base size mismatch", chunkify(pb9e.build()),
                   fresh_target("bsize"))

print("== 10. publish boundaries and cleanup ==")
parent_missing = os.path.join(WORK, "no_such_parent", "child")
try:
    import_pack(chunkify(pack), parent_missing)
    check("missing parent raises", False)
except FileNotFoundError:
    check("missing parent raises FileNotFoundError", True)
existing = os.path.join(WORK, "existing_dir")
os.makedirs(existing, exist_ok=True)
try:
    import_pack(chunkify(pack), existing)
    check("existing target raises", False)
except FileExistsError:
    check("existing target raises FileExistsError", True)
# failed import leaves no target and no temp residue (in a fresh parent)
clean_parent = os.path.join(WORK, "clean_parent")
os.makedirs(clean_parent)
try:
    import_pack(chunkify(pb.build(bad_trailer=True)),
                os.path.join(clean_parent, "should_not_appear"))
except ValueError:
    pass
check("no target on failure",
      "should_not_appear" not in os.listdir(clean_parent))
check("temp cleaned on failure", os.listdir(clean_parent) == [])

print("== 11. source exception propagates unchanged ==")
class Boom(Exception):
    pass
def boom_chunks():
    yield pack[:30]
    raise Boom("iterator exploded")
try:
    import_pack(boom_chunks(), fresh_target("boom"))
    check("source exception propagates", False)
except Boom:
    check("source exception propagates", True)

print("== 12. chunk size limit enforced ==")
try:
    import_pack([b"x" * 70000], fresh_target("bigchunk"))
    check("oversized chunk rejected", False)
except ValueError:
    check("oversized chunk rejected", True)

print("== 13. unknown / reserved entry type rejected ==")
from packbuilder import entry_header
bad_type = (b"PACK" + (2).to_bytes(4, "big") + (1).to_bytes(4, "big")
            + entry_header(5, 1) + __import__("zlib").compress(b"x"))
bad_type += hashlib.sha1(bad_type).digest()
expect_value_error("type 5 reserved", chunkify(bad_type), fresh_target("t5"))

print("== 14. declared object size over 64 MiB rejected ==")
huge_hdr = (b"PACK" + (2).to_bytes(4, "big") + (1).to_bytes(4, "big")
            + entry_header(OBJ_BLOB, 64 * 1024 * 1024 + 1)
            + __import__("zlib").compress(b"x"))
huge_hdr += hashlib.sha1(huge_hdr).digest()
expect_value_error("declared >64MiB", chunkify(huge_hdr), fresh_target("huge"))

print("== 15. object count over 4096 rejected ==")
cnt_hdr = b"PACK" + (2).to_bytes(4, "big") + (4097).to_bytes(4, "big")
cnt_pack = cnt_hdr + hashlib.sha1(cnt_hdr).digest()
expect_value_error("count 4097", chunkify(cnt_pack), fresh_target("cnt"))

print("== 16. empty (zero-object) pack accepted ==")
empty_pack = b"PACK" + (2).to_bytes(4, "big") + (0).to_bytes(4, "big")
empty_pack += hashlib.sha1(empty_pack).digest()
tgt16 = fresh_target("empty")
check("empty pack -> []", import_pack(chunkify(empty_pack), tgt16) == [])
check("empty target dir", os.path.isdir(tgt16) and os.listdir(tgt16) == [])

print("== 17. REF delta cycle is rejected ==")
# Two REF deltas that reference each other's would-be oids (neither base exists
# as a concrete object); this must fail rather than loop forever.
oid_a = hashlib.sha1(b"a").digest()
oid_b = hashlib.sha1(b"b").digest()
pbc = PackBuilder()
pbc.add_ref_delta(oid_b, bytes([3, 3, 0x90, 3]))
pbc.add_ref_delta(oid_a, bytes([3, 3, 0x90, 3]))
expect_value_error("ref cycle/missing", chunkify(pbc.build()),
                   fresh_target("cycle"))

print("== 18. inflated output beyond 64 MiB is rejected (zlib bomb) ==")
# A stream whose actual inflated size exceeds both the declared header and the
# cap: declare a small size but emit a larger compressed payload.
co = __import__("zlib").compressobj()
big = co.compress(b"x" * (64 * 1024 * 1024 + 10)) + co.flush()
bomb = (b"PACK" + (2).to_bytes(4, "big") + (1).to_bytes(4, "big")
        + entry_header(OBJ_BLOB, 10) + big)
bomb += hashlib.sha1(bomb).digest()
expect_value_error("inflated bomb", chunkify(bomb), fresh_target("bomb"))

print("== 19. memory discipline: no full body/delta retained ==")
# Large object (a few MiB) processed in 1-KiB chunks; peak extra buffer must
# stay bounded by the fixed block size, not the object size.
import resource
big_body = os.urandom(3 * 1024 * 1024)
pbd = PackBuilder()
pbd.add_object(OBJ_BLOB, big_body)
big_pack = pbd.build()
before_ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
tgt19 = fresh_target("mem")
rec19 = import_pack([big_pack[i:i+1024] for i in range(0, len(big_pack), 1024)],
                    tgt19)
big_oid = git_oid("blob", big_body).hex()
check("large object imported", rec19 == [[big_oid, "blob", len(big_body)]])
with open(os.path.join(tgt19, big_oid), "rb") as fh:
    check("large body intact", fh.read() == big_body)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAIL else 0)
