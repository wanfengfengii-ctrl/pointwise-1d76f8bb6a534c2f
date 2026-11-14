"""Helpers to build git-compatible pack v2 byte streams for testing.

This module is test-only and deliberately independent of the importer.
Layout facts were cross-checked against the real ``git`` executable:

* entry header: MSB-continuation, 3 type bits in the first byte, low 4 bits
  hold size bits 0..3, later bytes hold size bits 4.. in groups of 7;
* OFS_DELTA (type 6) and REF_DELTA (type 7) header sizes count only the
  inflated delta bytes -- the offset varint / 20-byte base id sit outside the
  zlib stream;
* REF_DELTA stores the 20-byte base id as plaintext between the entry header
  and the zlib-compressed delta.
"""

import hashlib
import zlib

OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG = 1, 2, 3, 4
OFS_DELTA, REF_DELTA = 6, 7


def entry_header(type_code, size):
    out = bytearray([(type_code << 4) | (size & 0x0F)])
    rest = size >> 4
    while rest:
        out[-1] |= 0x80
        out.append(rest & 0x7F)
        rest >>= 7
    return bytes(out)


def ofs_offset(negative):
    out = bytearray([negative & 0x7F])
    rest = negative >> 7
    while rest:
        out[-1] |= 0x80
        out.append(rest & 0x7F)
        rest >>= 7
    out.reverse()
    for i in range(len(out) - 1):
        out[i] |= 0x80
    return bytes(out)


def git_oid(type_name, body):
    store = ("%s %d\x00" % (type_name, len(body))).encode() + body
    return hashlib.sha1(store).digest()


class PackBuilder:
    def __init__(self):
        self._parts = []
        self._count = 0
        self._offset = 12

    def _emit(self, blob):
        self._parts.append(blob)
        self._offset += len(blob)

    def add_object(self, type_code, body):
        self._emit(entry_header(type_code, len(body)))
        self._emit(zlib.compress(body))
        self._count += 1

    def add_ofs_delta(self, base_pack_offset, delta):
        neg = self._offset - base_pack_offset
        self._emit(entry_header(OFS_DELTA, len(delta)))
        self._emit(ofs_offset(neg))
        self._emit(zlib.compress(delta))
        self._count += 1

    def add_ref_delta(self, base_oid_bytes, delta):
        self._emit(entry_header(REF_DELTA, len(delta)))
        self._emit(base_oid_bytes)
        self._emit(zlib.compress(delta))
        self._count += 1

    def build(self, count_override=None, bad_trailer=False, extra=b""):
        head = b"PACK" + (2).to_bytes(4, "big")
        n = self._count if count_override is None else count_override
        head += n.to_bytes(4, "big")
        data = head + b"".join(self._parts)
        trailer = b"\x00" * 20 if bad_trailer else hashlib.sha1(data).digest()
        return data + trailer + extra

    @property
    def current_offset(self):
        return self._offset


def chunkify(data, size=64 * 1024):
    """Split a pack into <=64 KiB single-pass chunks (1-byte chunks optional)."""
    return [data[i:i + size] for i in range(0, len(data), size)]
