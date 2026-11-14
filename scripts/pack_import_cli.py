"""Command line entry point for the pack importer.

Subcommands
-----------
import   Read a pack file (or stdin when ``-`` is given), split it into the
         mandated <= 64 KiB single-pass chunks and call ``import_pack``.
         Prints the returned ``[id, type, bytes]`` records, one per line.

The module imports only from the Python standard library and never shells out
to git.
"""

import argparse
import json
import sys

sys.path.insert(0, "/workspace")

from pack_import import import_pack

CHUNK = 64 * 1024


def _chunks_from(path):
    if path == "-":
        stream = sys.stdin.buffer
        while True:
            block = stream.read(CHUNK)
            if not block:
                return
            yield block
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                return
            yield block


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pack_import_cli",
        description="Import a non-thin git pack v2 archive without git.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser("import", help="import a pack into a target dir")
    p_imp.add_argument("pack", help="pack file path, or - for stdin")
    p_imp.add_argument("target", help="target directory (must not exist)")
    p_imp.add_argument("--json", action="store_true",
                       help="emit the result list as a JSON array")

    args = parser.parse_args(argv)

    if args.cmd == "import":
        records = import_pack(_chunks_from(args.pack), args.target)
        if args.json:
            print(json.dumps(records))
        else:
            for oid, typ, size in records:
                print("%s  %-6s  %d" % (oid, typ, size))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
