"""Provider-neutral clients for dereferencing Fabric BufferRefs."""

from .windows import WindowsBufferRefReader, copy_buffer_refs

__all__ = ["WindowsBufferRefReader", "copy_buffer_refs"]
