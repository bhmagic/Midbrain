from __future__ import annotations

import mmap
import struct
import uuid

import pytest

from midbrain_bufferref import WindowsBufferRefReader, copy_buffer_refs


def _reference(mapping_name: str, generation: int = 2) -> dict[str, object]:
    return {
        "transport": "windows_named_shared_memory",
        "mapping_name": mapping_name,
        "slot_offset": 128,
        "payload_offset": 256,
        "payload_bytes": 4,
        "generation": generation,
    }


def test_copy_buffer_refs_reads_a_committed_generation() -> None:
    mapping_name = f"Local\\midbrain_bufferref_test_{uuid.uuid4().hex}"
    producer = mmap.mmap(-1, 1024, tagname=mapping_name)
    try:
        producer.seek(128)
        producer.write(struct.pack("<q", 2))
        producer.seek(256)
        producer.write(b"test")
        assert copy_buffer_refs([_reference(mapping_name)]) == (b"test",)
    finally:
        producer.close()


def test_reader_rejects_a_recycled_reference() -> None:
    mapping_name = f"Local\\midbrain_bufferref_test_{uuid.uuid4().hex}"
    producer = mmap.mmap(-1, 1024, tagname=mapping_name)
    try:
        producer.seek(128)
        producer.write(struct.pack("<q", 4))
        reference = _reference(mapping_name, generation=2)
        reader = WindowsBufferRefReader(mapping_name).open([reference])
        try:
            with pytest.raises(RuntimeError, match="expired"):
                reader.read_ref(reference)
        finally:
            reader.close()
    finally:
        producer.close()


def test_reader_rejects_cross_mapping_references() -> None:
    first_name = f"Local\\midbrain_bufferref_test_{uuid.uuid4().hex}"
    second_name = f"Local\\midbrain_bufferref_test_{uuid.uuid4().hex}"
    with pytest.raises(ValueError, match="different mapping"):
        WindowsBufferRefReader(first_name)._required_mapping_bytes(
            _reference(second_name)
        )
