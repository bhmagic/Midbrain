"""Read generation-guarded BufferRefs from an existing Windows named mapping."""

from __future__ import annotations

import ctypes
import mmap
import struct
import time
from ctypes import wintypes
from typing import Any, Iterable, Mapping, Sequence


_FILE_MAP_READ = 0x0004
_ERROR_FILE_NOT_FOUND = 2

if hasattr(ctypes, "WinDLL"):
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _open_file_mapping = _kernel32.OpenFileMappingW
    _open_file_mapping.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _open_file_mapping.restype = wintypes.HANDLE
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL
else:
    _open_file_mapping = None
    _close_handle = None


def _open_existing_mapping_handle(mapping_name: str) -> int:
    if _open_file_mapping is None:
        raise OSError("Windows named shared memory is only available on Windows")
    handle = _open_file_mapping(_FILE_MAP_READ, False, mapping_name)
    if handle:
        return int(handle)
    error_code = ctypes.get_last_error()
    if error_code == _ERROR_FILE_NOT_FOUND:
        raise FileNotFoundError(
            error_code,
            f"shared-memory mapping does not exist: {mapping_name}",
        )
    raise OSError(error_code, f"OpenFileMappingW failed for {mapping_name}")


def _integer(reference: Mapping[str, Any], name: str, *, minimum: int = 0) -> int:
    try:
        value = int(reference[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"BufferRef requires integer {name}") from exc
    if value < minimum:
        raise ValueError(f"BufferRef {name} must be at least {minimum}")
    return value


class WindowsBufferRefReader:
    """Copy only caller-supplied Fabric BufferRefs; never select producer slots."""

    def __init__(self, mapping_name: str) -> None:
        self.mapping_name = str(mapping_name).strip()
        if not self.mapping_name:
            raise ValueError("BufferRef mapping_name is required")
        self._mapping: mmap.mmap | None = None
        self._mapped_bytes = 0

    def open(
        self,
        references: Iterable[Mapping[str, Any]],
    ) -> "WindowsBufferRefReader":
        if self._mapping is not None:
            raise RuntimeError("BufferRef reader is already open")
        refs = list(references)
        if not refs:
            raise ValueError("at least one BufferRef is required")
        required_bytes = max(self._required_mapping_bytes(reference) for reference in refs)
        existing_handle = _open_existing_mapping_handle(self.mapping_name)
        try:
            self._mapping = mmap.mmap(
                -1,
                required_bytes,
                tagname=self.mapping_name,
                access=mmap.ACCESS_READ,
            )
            self._mapped_bytes = required_bytes
        finally:
            if _close_handle is not None:
                _close_handle(existing_handle)
        return self

    def close(self) -> None:
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None
        self._mapped_bytes = 0

    def read_ref(self, reference: Mapping[str, Any], *, attempts: int = 4) -> bytes:
        mapping = self._mapping
        if mapping is None:
            raise RuntimeError("BufferRef reader is not open")
        values = self._validated_offsets(reference)
        if values[1] + values[2] > self._mapped_bytes:
            raise ValueError("BufferRef exceeds the opened mapping view")
        if attempts < 1:
            raise ValueError("BufferRef read attempts must be positive")
        slot_offset, payload_offset, payload_bytes, generation = values
        for _ in range(attempts):
            before = self._read_generation(mapping, slot_offset)
            if before != generation or before & 1:
                raise RuntimeError("BufferRef has expired or the slot was recycled")
            mapping.seek(payload_offset)
            payload = mapping.read(payload_bytes)
            after = self._read_generation(mapping, slot_offset)
            if before == after == generation and not (after & 1):
                if len(payload) != payload_bytes:
                    raise RuntimeError("BufferRef payload is shorter than declared")
                return payload
            time.sleep(0.0005)
        raise RuntimeError("could not obtain a consistent shared-memory payload")

    def _required_mapping_bytes(self, reference: Mapping[str, Any]) -> int:
        slot_offset, payload_offset, payload_bytes, _ = self._validated_offsets(reference)
        return max(slot_offset + 8, payload_offset + payload_bytes)

    def _validated_offsets(
        self,
        reference: Mapping[str, Any],
    ) -> tuple[int, int, int, int]:
        mapping_name = str(reference.get("mapping_name") or "").strip()
        if mapping_name != self.mapping_name:
            raise ValueError("BufferRef belongs to a different mapping")
        transport = str(reference.get("transport") or "").strip()
        if transport and transport != "windows_named_shared_memory":
            raise ValueError(f"unsupported BufferRef transport {transport!r}")
        slot_offset = _integer(reference, "slot_offset")
        payload_offset = _integer(reference, "payload_offset")
        payload_bytes = _integer(reference, "payload_bytes")
        generation = _integer(reference, "generation", minimum=1)
        if generation & 1:
            raise RuntimeError("BufferRef generation is not committed")
        if payload_offset < slot_offset + 8:
            raise ValueError("BufferRef payload overlaps its generation word")
        return slot_offset, payload_offset, payload_bytes, generation

    @staticmethod
    def _read_generation(mapping: mmap.mmap, slot_offset: int) -> int:
        mapping.seek(slot_offset)
        raw = mapping.read(8)
        if len(raw) != 8:
            raise RuntimeError("BufferRef generation word is unavailable")
        return int(struct.unpack("<q", raw)[0])


def copy_buffer_refs(
    references: Sequence[Mapping[str, Any]],
) -> tuple[bytes, ...]:
    """Copy one or more references from the same mapping under generation checks."""
    refs = tuple(references)
    if not refs:
        raise ValueError("at least one BufferRef is required")
    mapping_name = str(refs[0].get("mapping_name") or "").strip()
    reader = WindowsBufferRefReader(mapping_name).open(refs)
    try:
        return tuple(reader.read_ref(reference) for reference in refs)
    finally:
        reader.close()
