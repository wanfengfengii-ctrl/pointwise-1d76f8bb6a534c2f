"""End-to-end demonstration of a real pack import.

Builds the exact scenario from the task: a REF_DELTA object whose reference
base (blob ``abc``) is present in the same non-thin pack, with the delta
``030490030121`` reconstructing ``abc!``.  The pack is fed through the public
``import_pack`` API in 64 KiB chunks and the result is printed together with
the stored object ids and bodies.
"""

import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/scripts")

from pack_import import import_pack
from packbuilder import PackBuilder, git_oid, chunkify


def main():
    base_body = b"abc"
    base_oid = git_oid("blob", base_body)
    delta = bytes.fromhex("030490030121")

    pb = PackBuilder()
    # Emit the delta first, then the base it references, proving that a REF
    # base is allowed to arrive later in the single-pass stream.
    pb.add_ref_delta(base_oid, delta)
    pb.add_object(3, base_body)
    pack = pb.build()

    work = tempfile.mkdtemp(prefix="demo_pack_")
    try:
        target = os.path.join(work, "objects")
        records = import_pack(chunkify(pack), target)

        print("pack bytes (%d): %s" % (len(pack), pack.hex()))
        print("imported records (sorted by object id):")
        for oid, typ, size in records:
            stored = open(os.path.join(target, oid), "rb").read()
            print("  %s  %-6s  %3d bytes  body=%r" % (oid, typ, size, stored))

        want = "9349f5b140d0cce46e41da5ae0cf40ad5f596853"
        assert records[-1][0] == want or any(r[0] == want for r in records)
        assert open(os.path.join(target, want), "rb").read() == b"abc!"
        print("\nOK: reconstructed object id %s -> b'abc!'" % want)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
