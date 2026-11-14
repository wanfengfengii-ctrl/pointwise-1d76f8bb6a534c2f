"""Core implementation of the dependency-free git pack v2 importer.

Only the Python standard library is used (``zlib``, ``hashlib``, ``tempfile``,
``shutil`` ...).  The ``git`` executable is never invoked.

Memory discipline
-----------------
* The pack input is a single-pass iterator of chunks (<= 64 KiB).  A bounded
  rolling buffer holds only the few bytes needed to parse the current header
  and the bytes currently fed to zlib.
* Inflated object bodies and inflated delta payloads are streamed straight into
  per-entry spool files; they are never materialised as a whole in memory.
* Delta reconstruction copies fixed-size blocks between spool files.
* The only structures that grow with the archive are the in-memory entry index
  (at most 4096 records) and the open spool-file table.
"""

import hashlib
import os
import shutil
import tempfile
import zlib

# --- Fixed limits mandated by the task -------------------------------------
MAX_ENTRIES = 4096
MAX_OBJECT_BYTES = 64 * 1024 * 1024          # 64 MiB per inflated body/result
MAX_CHUNK = 64 * 1024                        # 64 KiB input chunk
COPY_BUF = 64 * 1024                         # streaming copy block size

PACK_SIGNATURE = b"PACK"
PACK_VERSION = 2
TRAILER_BYTES = 20                           # SHA-1 of the whole pack
HEADER_BYTES = 12                            # "PACK" + version + count

# Pack entry type codes.
T_COMMIT, T_TREE, T_BLOB, T_TAG = 1, 2, 3, 4
T_OFS_DELTA, T_REF_DELTA = 6, 7
_TYPE_NAMES = {
    T_COMMIT: "commit",
    T_TREE: "tree",
    T_BLOB: "blob",
    T_TAG: "tag",
}
_DELTA_TYPES = frozenset((T_OFS_DELTA, T_REF_DELTA))
_VALID_TYPES = frozenset(
    (T_COMMIT, T_TREE, T_BLOB, T_TAG, T_OFS_DELTA, T_REF_DELTA))


class _Truncated(Exception):
    """Raised internally when the chunk stream ends before parsing completes."""


class _PackReader:
    """Single-pass reader over an iterator of fixed-size bytes chunks.

    ``read(n)`` returns exactly *n* bytes or raises :class:`_Truncated`.
    :meth:`inflate_stream` drives a streaming zlib decompressor while
    simultaneously updating the running whole-pack SHA-1; any bytes after the
    zlib end marker are retained so the next object's header can parse them.
    """

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buf = bytearray()
        self._exhausted = False
        self.pack_sha = hashlib.sha1()
        # Total bytes already folded into pack_sha == current read position.
        self.bytes_consumed = 0

    def _pull(self):
        if self._exhausted:
            return False
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._exhausted = True
            return False
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("pack chunks must be bytes-like")
        if len(chunk) > MAX_CHUNK:
            raise ValueError("pack chunk exceeds 64 KiB")
        self._buf.extend(chunk)
        return True

    def read(self, n):
        """Read exactly *n* bytes; raise :class:`_Truncated` on short input."""
        while len(self._buf) < n:
            if not self._pull():
                raise _Truncated("pack ended before %d bytes were available" % n)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def read_byte(self):
        return self.read(1)[0]

    def inflate_stream(self, sink, size_limit=MAX_OBJECT_BYTES):
        """Inflate one zlib stream from the current position into ``sink``.

        Every byte consumed (including the zlib wrapper) is folded into
        :attr:`pack_sha`.  Inflated output is written to the file-like
        ``sink`` and capped at ``size_limit``.  Returns the number of inflated
        bytes.  Bytes belonging to the next object are retained in the internal
        buffer.
        """
        dec = zlib.decompressobj()
        total = 0
        while True:
            if not self._buf:
                if not self._pull():
                    raise _Truncated("pack ended inside a zlib stream")
            # Feed whatever is buffered; zlib reports the untouched tail via
            # ``unused_data`` once the end of the stream has been reached.
            try:
                produced = dec.decompress(bytes(self._buf), COPY_BUF)
            except zlib.error as exc:
                raise ValueError("corrupt zlib stream in pack object: %s" % exc)
            used = len(self._buf) - len(dec.unused_data)
            self.pack_sha.update(self._buf[:used])
            self.bytes_consumed += used
            del self._buf[:used]
            if produced:
                total += len(produced)
                if total > size_limit:
                    raise ValueError("inflated object exceeds 64 MiB limit")
                sink.write(produced)
            if dec.eof:
                return total

    def hash_remaining_header(self, data):
        """Account for header bytes consumed outside :meth:`inflate_stream`."""
        self.pack_sha.update(data)
        self.bytes_consumed += len(data)

    def finish_trailer(self):
        """Read the 20-byte pack checksum and reject any trailing bytes."""
        trailer = self.read(TRAILER_BYTES)
        # The trailer itself is not part of the hashed region.
        # After the trailer the stream must be empty.
        if self._buf:
            raise ValueError("trailing bytes after pack checksum")
        while not self._exhausted:
            if not self._pull():
                break
            if self._buf:
                raise ValueError("trailing bytes after pack checksum")
        digest = self.pack_sha.digest()
        if digest != trailer:
            raise ValueError("pack checksum mismatch: expected %s, computed %s"
                             % (trailer.hex(), digest.hex()))


def _read_entry_header(reader):
    """Parse a pack entry header -> (type_code, inflated_size)."""
    first = reader.read_byte()
    reader.hash_remaining_header(bytes((first,)))
    type_code = (first >> 4) & 0x07
    size = first & 0x0F
    shift = 4
    while first & 0x80:
        nxt = reader.read_byte()
        reader.hash_remaining_header(bytes((nxt,)))
        size |= (nxt & 0x7F) << shift
        shift += 7
        first = nxt
    return type_code, size


def _read_ofs_offset(reader):
    """Parse the negative-offset varint of an OFS_DELTA entry."""
    first = reader.read_byte()
    reader.hash_remaining_header(bytes((first,)))
    offset = first & 0x7F
    while first & 0x80:
        nxt = reader.read_byte()
        reader.hash_remaining_header(bytes((nxt,)))
        offset = ((offset + 1) << 7) | (nxt & 0x7F)
        first = nxt
    return offset


def _git_object_hash(type_name, spool_path, length):
    """Compute the git SHA-1 of a spooled object body, streamed from disk."""
    header = ("%s %d\x00" % (type_name, length)).encode()
    h = hashlib.sha1(header)
    remaining = length
    with open(spool_path, "rb") as fh:
        while remaining > 0:
            block = fh.read(min(COPY_BUF, remaining))
            if not block:
                raise ValueError("spool truncated while hashing object")
            h.update(block)
            remaining -= len(block)
    return h.hexdigest()


def _read_delta_size(fh):
    """Read one LEB128 size from the head of a spooled delta payload."""
    size = 0
    shift = 0
    while True:
        b = fh.read(1)
        if not b:
            raise ValueError("truncated delta size header")
        c = b[0]
        size |= (c & 0x7F) << shift
        if not (c & 0x80):
            return size
        shift += 7


def _apply_delta(base_path, base_len, delta_path, delta_len, out_path,
                 result_limit=MAX_OBJECT_BYTES):
    """Reconstruct an object from a spooled base and spooled delta.

    Implements the exact ``patch_delta`` semantics from git's ``patch-delta.c``
    and streams both inputs and the output through fixed-size buffers; nothing
    is held whole in memory.
    """
    with open(base_path, "rb") as base_fh, \
            open(delta_path, "rb") as d_fh, \
            open(out_path, "wb") as out_fh:
        base_size = _read_delta_size(d_fh)
        result_size = _read_delta_size(d_fh)
        if base_size != base_len:
            raise ValueError("delta base length mismatch: header %d, actual %d"
                             % (base_size, base_len))
        if result_size > result_limit:
            raise ValueError("delta result exceeds 64 MiB limit")
        produced = 0
        # Number of delta instruction bytes remaining (excludes the two sizes).
        instr_left = delta_len - d_fh.tell()

        while instr_left > 0:
            b = d_fh.read(1)
            if not b:
                raise ValueError("truncated delta instruction stream")
            instr_left -= 1
            cmd = b[0]

            if cmd & 0x80:
                # COPY: low nibble (0x01/02/04/08) selects offset bytes,
                # 0x10/0x20/0x40 select size bytes -- mirrors patch-delta.c.
                cp_off = 0
                cp_size = 0
                for bit, shift in ((0x01, 0), (0x02, 8), (0x04, 16), (0x08, 24)):
                    if cmd & bit:
                        rb = d_fh.read(1)
                        if not rb:
                            raise ValueError("truncated copy offset in delta")
                        instr_left -= 1
                        cp_off |= rb[0] << shift
                for bit, shift in ((0x10, 0), (0x20, 8), (0x40, 16)):
                    if cmd & bit:
                        rb = d_fh.read(1)
                        if not rb:
                            raise ValueError("truncated copy size in delta")
                        instr_left -= 1
                        cp_size |= rb[0] << shift
                if cp_size == 0:
                    cp_size = 0x10000
                # Range validation against both the base and the result.
                if cp_off > base_len or cp_size > base_len - cp_off:
                    raise ValueError("delta copy range exceeds base object")
                if cp_size > result_size - produced:
                    raise ValueError("delta copy exceeds declared result length")
                base_fh.seek(cp_off)
                remaining = cp_size
                while remaining > 0:
                    block = base_fh.read(min(COPY_BUF, remaining))
                    if not block:
                        raise ValueError("base object truncated during copy")
                    out_fh.write(block)
                    remaining -= len(block)
                produced += cp_size

            elif cmd:
                # INSERT: the next ``cmd`` bytes are literal output.
                insert_len = cmd
                if insert_len > instr_left:
                    raise ValueError("delta insert overruns instruction stream")
                if insert_len > result_size - produced:
                    raise ValueError("delta insert exceeds declared result length")
                remaining = insert_len
                while remaining > 0:
                    block = d_fh.read(min(COPY_BUF, remaining))
                    if not block:
                        raise ValueError("truncated delta insert payload")
                    out_fh.write(block)
                    remaining -= len(block)
                instr_left -= insert_len
                produced += insert_len

            else:
                # Opcode zero is reserved and must be rejected.
                raise ValueError("unexpected delta opcode 0")

        if instr_left != 0:
            raise ValueError("trailing bytes inside delta instruction stream")
        if produced != result_size:
            raise ValueError("delta result length mismatch: header %d, built %d"
                             % (result_size, produced))
    return result_size


# --- Entry records ----------------------------------------------------------
class _Entry:
    __slots__ = (
        "index", "pack_offset", "type_code", "inflated_size",
        "spool", "base_offset", "base_oid",
        "resolved_type", "oid", "result_len", "result_spool",
    )

    def __init__(self, index, pack_offset, type_code, inflated_size, spool):
        self.index = index
        self.pack_offset = pack_offset
        self.type_code = type_code
        self.inflated_size = inflated_size
        self.spool = spool
        self.base_offset = None       # OFS_DELTA: absolute pack offset of base
        self.base_oid = None          # REF_DELTA: 20-byte binary base id
        self.resolved_type = None     # inherited from the chain's base
        self.oid = None
        self.result_len = None
        self.result_spool = None


def import_pack(chunks, target_dir):
    """Import a non-thin git pack v2 stream into ``target_dir``.

    Parameters
    ----------
    chunks:
        Single-pass iterable of ``bytes`` chunks, each at most 64 KiB.
    target_dir:
        Directory to create; it must not exist, while its parent must.

    Returns
    -------
    list of ``[oid_hex, type_name, byte_length]`` sorted by lowercase
    hexadecimal object id.  Each distinct object body is stored as a file named
    after its id inside ``target_dir``; duplicate objects are coalesced.

    Raises
    ------
    ValueError
        For any corrupt, truncated, malformed or unresolvable pack.
    Other exceptions
        Propagated unchanged from the underlying chunk iterator or the
        operating system (e.g. :class:`OSError`).
    """
    target_dir = os.fspath(target_dir)
    parent = os.path.dirname(os.path.abspath(target_dir))
    # Preconditions: parent exists, target does not.
    if not os.path.isdir(parent):
        raise FileNotFoundError("parent directory does not exist: %s" % parent)
    if os.path.lexists(target_dir):
        raise FileExistsError("target already exists: %s" % target_dir)

    work = tempfile.mkdtemp(prefix=".pack_import_", dir=parent)
    spool_dir = os.path.join(work, "spool")
    os.mkdir(spool_dir)
    try:
        entries = _parse_pack(chunks, spool_dir)
        records, resolved = _resolve_all(entries, work, spool_dir)
        _publish(work, target_dir, resolved)
    except BaseException:
        # Remove only the temporary content we created; target_dir is only
        # ever put in place by the atomic rename in _publish.
        shutil.rmtree(work, ignore_errors=True)
        raise
    return records


def _parse_pack(chunks, spool_dir):
    reader = _PackReader(chunks)
    try:
        signature = reader.read(4)
        version = reader.read(4)
        count_raw = reader.read(4)
    except _Truncated as exc:
        raise ValueError("truncated pack header") from exc
    if signature != PACK_SIGNATURE:
        raise ValueError("bad pack signature")
    version_num = int.from_bytes(version, "big")
    if version_num != PACK_VERSION:
        raise ValueError("unsupported pack version %d (only v2 supported)"
                         % version_num)
    count = int.from_bytes(count_raw, "big")
    if count > MAX_ENTRIES:
        raise ValueError("pack contains %d objects, limit is %d"
                         % (count, MAX_ENTRIES))
    reader.hash_remaining_header(signature + version + count_raw)

    entries = []
    try:
        for index in range(count):
            pack_offset = reader.bytes_consumed
            type_code, inflated_size = _read_entry_header(reader)
            if type_code not in _VALID_TYPES:
                raise ValueError("unsupported pack object type %d at offset %d"
                                 % (type_code, pack_offset))
            if inflated_size > MAX_OBJECT_BYTES:
                raise ValueError("declared object size exceeds 64 MiB limit")

            spool_path = os.path.join(spool_dir, "obj%04d" % index)
            entry = _Entry(index, pack_offset, type_code, inflated_size,
                           spool_path)

            if type_code in _DELTA_TYPES:
                if type_code == T_OFS_DELTA:
                    neg = _read_ofs_offset(reader)
                    base_pack_offset = pack_offset - neg
                    entry.base_offset = base_pack_offset
                else:
                    entry.base_oid = reader.read(20)
                    reader.hash_remaining_header(entry.base_oid)

            with open(spool_path, "wb") as sink:
                got = reader.inflate_stream(sink)
            if got != inflated_size:
                raise ValueError(
                    "object %d size mismatch: header %d, inflated %d"
                    % (index, inflated_size, got))
            entries.append(entry)

        reader.finish_trailer()
    except _Truncated as exc:
        raise ValueError("truncated pack: %s" % exc) from exc

    if len(entries) != count:
        raise ValueError("object count mismatch: header %d, found %d"
                         % (count, len(entries)))
    return entries


def _resolve_all(entries, work, spool_dir):
    """Resolve delta chains, hash objects and build the result records.

    Non-delta entries are resolved first so REF_DELTA bases that appear later
    in the stream can still be found.  Remaining deltas are drained through a
    worklist, re-queuing entries whose base is not yet available, which makes
    forward REF references resolve while cyclic chains and missing bases fail.
    """
    by_offset = {e.pack_offset: e for e in entries}
    by_oid = {}                       # hex oid -> resolved entry
    result_dir = os.path.join(work, "result")
    os.mkdir(result_dir)

    def finalize_non_delta(entry):
        entry.resolved_type = _TYPE_NAMES[entry.type_code]
        entry.result_spool = entry.spool
        entry.result_len = entry.inflated_size
        entry.oid = _git_object_hash(
            entry.resolved_type, entry.result_spool, entry.result_len)
        by_oid.setdefault(entry.oid, entry)

    def apply_against_base(entry, base):
        out_path = os.path.join(result_dir, "res%04d" % entry.index)
        length = _apply_delta(
            base.result_spool, base.result_len,
            entry.spool, entry.inflated_size, out_path)
        entry.result_spool = out_path
        entry.result_len = length
        entry.resolved_type = base.resolved_type
        entry.oid = _git_object_hash(
            entry.resolved_type, entry.result_spool, entry.result_len)
        by_oid.setdefault(entry.oid, entry)

    pending = []
    for entry in entries:
        if entry.type_code in _DELTA_TYPES:
            pending.append(entry)
        else:
            finalize_non_delta(entry)

    # Drain pending deltas; each pass must finalise at least one entry.
    while pending:
        progressed = False
        still = []
        for entry in pending:
            base = _find_base(entry, by_offset, by_oid)
            if base is None:
                still.append(entry)
                continue
            apply_against_base(entry, base)
            progressed = True
        if not progressed:
            # No base became available: a cycle or a genuinely missing base
            # (thin pack / dangling reference).
            raise ValueError(
                "unresolvable delta chain (missing or cyclic base): %s"
                % ", ".join(_describe_base(e) for e in still))
        pending = still

    # Coalesce duplicate objects and produce the sorted listing.
    seen = {}
    for entry in entries:
        if entry.oid not in seen:
            seen[entry.oid] = entry
    records = []
    for oid in sorted(seen):
        e = seen[oid]
        records.append([oid, e.resolved_type, e.result_len])
    return records, seen


def _describe_base(entry):
    if entry.type_code == T_REF_DELTA:
        return "REF->" + entry.base_oid.hex()
    return "OFS@%d" % entry.base_offset


def _find_base(entry, by_offset, by_oid):
    """Return the resolved base entry or ``None`` if it is not ready."""
    if entry.type_code == T_OFS_DELTA:
        base = by_offset.get(entry.base_offset)
        # OFS_DELTA always points to an earlier object in the stream.
        if base is None:
            raise ValueError("OFS_DELTA base offset %d not found in pack"
                             % entry.base_offset)
        # Earlier entry but itself unresolved can only happen through a cycle.
        return base if base.oid is not None else None
    return by_oid.get(entry.base_oid.hex())


def _publish(work, target_dir, resolved):
    """Materialise object files and atomically publish ``target_dir``."""
    out_dir = os.path.join(work, "objects")
    os.mkdir(out_dir)
    for oid, entry in resolved.items():
        dst = os.path.join(out_dir, oid)
        with open(entry.result_spool, "rb") as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, COPY_BUF)
    # Atomic single-step publication; target_dir was proven absent up front.
    os.rename(out_dir, target_dir)
