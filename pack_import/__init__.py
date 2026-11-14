"""Git pack v2 archive importer (pure standard library, no git invocation).

Public entry point: :func:`import_pack`.

The implementation streams a single-pass sequence of ``bytes`` chunks (each no
larger than 64 KiB) and never keeps a whole compressed object, a whole inflated
body or a whole delta resident in memory.  Every object body is spooled to a
temporary file and processed through fixed-size buffers; only the object index
(O(entries)) grows with the input.
"""

from .pack_import import import_pack

__all__ = ["import_pack"]
