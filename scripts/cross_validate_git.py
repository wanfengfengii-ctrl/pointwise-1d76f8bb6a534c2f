"""Cross-validate the importer against the real git executable.

Builds a repository with commit/tree/blob/tag objects and forced deltas,
exports native packs (both OFS and REF delta variants), feeds each pack through
import_pack in small chunks, and compares every object id/type/size/body
against git's own view.  The importer itself never invokes git; git is used
only as an independent oracle in this test harness.
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/scripts")
from pack_import import import_pack
from packbuilder import chunkify


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


ROOT = tempfile.mkdtemp(prefix="xval_")
repo = os.path.join(ROOT, "repo")
os.makedirs(repo)
run(["git", "init", "-q", repo])
git = ["git", "-C", repo]

# Create content: several similar large blobs (to force deltas), plus commits,
# a tree and an annotated tag.
blobs = {}
for i in range(6):
    body = (("line %04d common prefix\n" % i) + ("xyz" * 400) +
            ("\ntail-%d" % i)).encode()
    p = os.path.join(ROOT, "f%d" % i)
    open(p, "wb").write(body)
    oid = run(git + ["hash-object", "-w", p]).stdout.strip()
    blobs[oid] = body

# Build a real history so trees/commits exist.
run(git + ["config", "user.email", "t@e.st"])
run(git + ["config", "user.name", "T"])
for i in range(3):
    name = "f%d" % i
    run(["cp", os.path.join(ROOT, name), os.path.join(repo, name)])
    run(git + ["add", name])
    run(git + ["commit", "-q", "-m", "commit %d" % i])
tag_oid = run(git + ["tag", "-a", "v1", "-m", "release"]).stdout
# enumerate every object git has
out = run(git + ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)", ],
          input="\n".join(blobs) + "\n").stdout
all_objs = {}
for line in run(git + ["rev-list", "--objects", "--all"]).stdout.splitlines():
    pass
# Full object enumeration via cat-file --batch-all-objects
batch = run(git + ["cat-file", "--batch-all-objects",
                   "--batch-check=%(objectname) %(objecttype) %(objectsize)"]).stdout
for line in batch.splitlines():
    oid, typ, size = line.split()
    all_objs[oid] = (typ, int(size))
print("git objects:", len(all_objs))

# Export native packs with and without delta-base-offset, forcing deep deltas.
oids_blob = "\n".join(blobs) + "\n"
pack_paths = {}
for label, extra in (("ofs", ["--delta-base-offset"]), ("ref", [])):
    pp = os.path.join(ROOT, label + ".pack")
    with open(pp, "wb") as fh:
        subprocess.run(git + ["pack-objects", "--stdout", "--window=100",
                              "--depth=10"] + extra,
                       input=oids_blob, stdout=fh, text=True)
    pack_paths[label] = pp

# Also a pack containing ALL objects (via rev-list)
full_pack = os.path.join(ROOT, "full.pack")
with open(full_pack, "wb") as fh:
    subprocess.run(git + ["pack-objects", "--stdout", "--window=100",
                          "--depth=10", "--delta-base-offset"],
                   input=run(git + ["rev-list", "--objects", "--all"]).stdout,
                   stdout=fh, text=True)
pack_paths["full"] = full_pack

failures = 0
for label, pp in pack_paths.items():
    data = open(pp, "rb").read()
    # git oracle: verify-pack listing
    idxdir = os.path.join(ROOT, "idx_" + label)
    os.makedirs(idxdir)
    r = subprocess.run(["git", "index-pack", os.path.join(idxdir, "p.pack")],
                       input=data, capture_output=True)
    # index-pack needs the pack file on disk; write it first
    open(os.path.join(idxdir, "p.pack"), "wb").write(data)
    r = subprocess.run(["git", "index-pack", os.path.join(idxdir, "p.pack")],
                       capture_output=True, text=True, cwd=idxdir)
    if r.returncode != 0:
        print(label, "index-pack failed:", r.stderr)
        failures += 1
        continue
    vp = subprocess.run(["git", "verify-pack", "-v",
                         os.path.join(idxdir, "p.idx")],
                        capture_output=True, text=True, cwd=idxdir).stdout
    git_ids = set()
    for line in vp.splitlines():
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) == 40:
            git_ids.add(parts[0])

    tgt = os.path.join(ROOT, "out_" + label)
    records = import_pack(chunkify(data, 4096), tgt)
    imp_ids = {r[0] for r in records}

    ok_ids = imp_ids == git_ids
    print("[%s] pack=%d bytes, git objs=%d, imported=%d, id-set match=%s"
          % (label, len(data), len(git_ids), len(records), ok_ids))
    if not ok_ids:
        failures += 1
        print("  missing:", git_ids - imp_ids, "extra:", imp_ids - git_ids)

    # Verify type/size/body for every object against git.
    for oid, typ, size in records:
        exp = all_objs.get(oid)
        if exp is None:
            print("  unknown oid", oid); failures += 1; continue
        if (typ, size) != exp:
            print("  metadata mismatch", oid, (typ, size), exp); failures += 1
        body = open(os.path.join(tgt, oid), "rb").read()
        git_body = subprocess.run(git + ["cat-file", typ, oid],
                                  capture_output=True).stdout
        if body != git_body:
            print("  body mismatch", oid); failures += 1
    # Files on disk exactly equal object count.
    if set(os.listdir(tgt)) != imp_ids:
        print("  on-disk file set mismatch"); failures += 1

print()
print("CROSS-VALIDATION", "PASS" if failures == 0 else "FAIL (%d)" % failures)
shutil.rmtree(ROOT, ignore_errors=True)
sys.exit(1 if failures else 0)
