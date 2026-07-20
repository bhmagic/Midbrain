"""Windows shared-memory access for the Femto Bolt CameraHost.

Large camera payloads remain in the native Windows named mapping. This module
opens an existing mapping without creating it, exposes stable BufferRef
metadata, and validates seqlock generations before copying a payload.
"""

from __future__ import annotations

import ctypes
import mmap
import struct
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from typing import Any, Optional

MAGIC = 0x31504D4850434246
HEADER_BYTES = 4096
SLOT_HEADER_BYTES_V1 = 256
SLOT_HEADER_BYTES_V2 = 512

STREAM_COLOR = 0
STREAM_DEPTH = 1
STREAM_IR = 2
STREAM_POINT_CLOUD = 3
STREAM_ACCEL = 4
STREAM_GYRO = 5
STREAM_CALIBRATION = 6
STREAM_STATUS = 7
STREAM_ALIGNED_DEPTH = 8

STREAM_KIND_NAMES = {
    STREAM_COLOR: "color",
    STREAM_DEPTH: "depth",
    STREAM_IR: "ir",
    STREAM_POINT_CLOUD: "point_cloud",
    STREAM_ACCEL: "accel",
    STREAM_GYRO: "gyro",
    STREAM_CALIBRATION: "calibration",
    STREAM_STATUS: "status",
    STREAM_ALIGNED_DEPTH: "depth_aligned_to_color",
}

# The enum order is fixed by Orbbec SDK 2.8.x OBFrameMetadataType.
FRAME_METADATA_NAMES = (
    "timestamp",
    "sensor_timestamp",
    "frame_number",
    "auto_exposure",
    "exposure",
    "gain",
    "auto_white_balance",
    "white_balance",
    "brightness",
    "contrast",
    "saturation",
    "sharpness",
    "backlight_compensation",
    "hue",
    "gamma",
    "power_line_frequency",
    "low_light_compensation",
    "manual_white_balance",
    "actual_frame_rate",
    "frame_rate",
    "ae_roi_left",
    "ae_roi_top",
    "ae_roi_right",
    "ae_roi_bottom",
    "exposure_priority",
    "hdr_sequence_name",
    "hdr_sequence_size",
    "hdr_sequence_index",
    "laser_power",
    "laser_power_level",
    "laser_status",
    "gpio_input_data",
    "disparity_search_offset",
    "disparity_search_range",
)

STREAM_DESCRIPTOR = struct.Struct("<32sIIIIQQQqQQ")
FRAME_PREFIX_V1 = struct.Struct("<qQQQQQIIIIIIIIQfII")
FRAME_PREFIX_V2 = struct.Struct("<qQQQQQIIIIIIIIQfIQ")
FRAME_METADATA_VALUES_V2 = struct.Struct("<34q")
IMU_PAYLOAD = struct.Struct("<QQQQffffII")
_HEADER_PREFIX = struct.Struct("<QIIQQQdQQ")

_HEADER_V1_STREAM_TABLE_OFFSET = _HEADER_PREFIX.size + 128 + 256 + 256 + 64
_HEADER_V2_STREAM_TABLE_OFFSET = (
    _HEADER_V1_STREAM_TABLE_OFFSET
    + 64  # firmware_version
    + 32  # connection_type
    + 128  # device_uid
    + 16  # USB PID/VID and global timestamp flags
)

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
    _kernel32 = None
    _open_file_mapping = None
    _close_handle = None


def _open_existing_mapping_handle(mapping_name: str) -> int:
    """Open an existing Windows named mapping without creating a new one."""
    if _open_file_mapping is None:
        raise OSError("Windows named shared memory is only available on Windows")
    handle = _open_file_mapping(_FILE_MAP_READ, False, mapping_name)
    if handle:
        return int(handle)

    error_code = ctypes.get_last_error()
    if error_code == _ERROR_FILE_NOT_FOUND:
        raise FileNotFoundError(
            error_code,
            f"shared-memory mapping does not exist yet: {mapping_name}",
        )
    raise OSError(error_code, f"OpenFileMappingW failed for {mapping_name}")


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _metadata_dict(mask: int, values: tuple[int, ...]) -> dict[str, int]:
    return {
        name: int(values[index])
        for index, name in enumerate(FRAME_METADATA_NAMES)
        if mask & (1 << index)
    }


@dataclass(frozen=True)
class StreamDescriptorInfo:
    name: str
    stream_kind: int
    payload_kind: int
    slot_count: int
    slot_stride_bytes: int
    slot_payload_capacity_bytes: int
    base_offset_bytes: int
    latest_slot: int
    latest_frame_number: int
    dropped_frame_count: int


@dataclass(frozen=True)
class MappingHeader:
    layout_version: int
    total_bytes: int
    mapping_name: str
    device_name: str
    device_serial: str
    sdk_version: str
    firmware_version: str
    connection_type: str
    device_uid: str
    usb_pid: int
    usb_vid: int
    global_timestamp_supported: bool
    global_timestamp_enabled: bool
    process_id: int
    slot_header_bytes: int
    streams: tuple[StreamDescriptorInfo, ...]


@dataclass(frozen=True)
class BufferRef:
    transport: str
    mapping_name: str
    stream_kind: int
    stream_name: str
    pool_id: str
    slot_id: int
    generation: int
    slot_offset: int
    payload_offset: int
    payload_bytes: int
    payload_capacity_bytes: int
    frame_number: int
    host_qpc: int
    device_timestamp_us: int
    system_timestamp_us: int
    global_timestamp_us: int
    frame_type: int
    format: int
    format_name: str
    width: int
    height: int
    stride_bytes: int
    bytes_per_pixel: int
    depth_value_scale_mm: float
    flags: int
    metadata_mask: int
    frame_metadata: dict[str, int]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImuSample:
    frame_number: int
    device_timestamp_us: int
    system_timestamp_us: int
    global_timestamp_us: int
    x: float
    y: float
    z: float
    temperature_c: float
    stream_kind: int

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stream_name"] = STREAM_KIND_NAMES.get(
            self.stream_kind,
            str(self.stream_kind),
        )
        return data


class CameraSharedMemory:
    def __init__(self, mapping_name: str):
        self.mapping_name = mapping_name
        self._mm: Optional[mmap.mmap] = None
        self.header: Optional[MappingHeader] = None

    def open(self) -> "CameraSharedMemory":
        # Python's Windows named mmap creates a mapping when one does not exist.
        # OpenFileMappingW first ensures CameraHost remains the sole creator.
        existing_handle = _open_existing_mapping_handle(self.mapping_name)
        try:
            probe = mmap.mmap(
                -1,
                HEADER_BYTES,
                tagname=self.mapping_name,
                access=mmap.ACCESS_READ,
            )
            try:
                header = self._read_header(probe)
            finally:
                probe.close()

            mapped = mmap.mmap(
                -1,
                header.total_bytes,
                tagname=self.mapping_name,
                access=mmap.ACCESS_READ,
            )
            try:
                validated_header = self._read_header(mapped)
            except Exception:
                mapped.close()
                raise

            self._mm = mapped
            self.header = validated_header
            return self
        finally:
            if _close_handle is not None:
                _close_handle(existing_handle)

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        self.header = None

    def refresh(self) -> MappingHeader:
        if self._mm is None:
            raise RuntimeError("shared memory is not open")
        self.header = self._read_header(self._mm)
        return self.header

    def latest_ref(self, stream_kind: int, attempts: int = 8) -> Optional[BufferRef]:
        stream = self._find_stream(stream_kind)
        if (
            stream is None
            or stream.latest_slot < 0
            or stream.latest_slot >= stream.slot_count
        ):
            return None
        return self._ref_at_slot(stream, stream.latest_slot, attempts=attempts)

    def recent_refs(
        self,
        stream_kind: int,
        *,
        after_frame_number: int = -1,
        attempts: int = 4,
    ) -> list[BufferRef]:
        """Return currently retained stream slots in frame order.

        The caller must detect gaps because a producer may overwrite the finite ring
        before a slow consumer reads it. Duplicate frame numbers are collapsed by
        keeping the newest generation.
        """
        stream = self._find_stream(stream_kind)
        if stream is None:
            return []
        by_frame: dict[int, BufferRef] = {}
        for slot_id in range(stream.slot_count):
            reference = self._ref_at_slot(stream, slot_id, attempts=attempts)
            if reference is None or reference.frame_number <= after_frame_number:
                continue
            current = by_frame.get(reference.frame_number)
            if current is None or reference.generation > current.generation:
                by_frame[reference.frame_number] = reference
        return [by_frame[number] for number in sorted(by_frame)]

    def recent_imu_samples(
        self,
        stream_kind: int,
        *,
        after_frame_number: int = -1,
    ) -> list[ImuSample]:
        samples: list[ImuSample] = []
        for reference in self.recent_refs(
            stream_kind,
            after_frame_number=after_frame_number,
        ):
            try:
                payload = self.read_ref(reference)
            except RuntimeError:
                continue
            if len(payload) < IMU_PAYLOAD.size:
                continue
            values = IMU_PAYLOAD.unpack(payload[: IMU_PAYLOAD.size])
            samples.append(
                ImuSample(
                    frame_number=int(values[0]),
                    device_timestamp_us=int(values[1]),
                    system_timestamp_us=int(values[2]),
                    global_timestamp_us=int(values[3]),
                    x=float(values[4]),
                    y=float(values[5]),
                    z=float(values[6]),
                    temperature_c=float(values[7]),
                    stream_kind=int(values[8]),
                )
            )
        return samples

    def _ref_at_slot(
        self,
        stream: StreamDescriptorInfo,
        slot_id: int,
        *,
        attempts: int,
    ) -> Optional[BufferRef]:
        mm = self._require_open()
        header = self.header
        if header is None or slot_id < 0 or slot_id >= stream.slot_count:
            return None

        slot_offset = stream.base_offset_bytes + stream.slot_stride_bytes * slot_id
        payload_offset = slot_offset + header.slot_header_bytes

        for _ in range(attempts):
            sequence_before = self._read_sequence(slot_offset)
            if sequence_before <= 0 or sequence_before & 1:
                time.sleep(0.0005)
                continue

            mm.seek(slot_offset)
            raw_header = mm.read(header.slot_header_bytes)
            if len(raw_header) < header.slot_header_bytes:
                return None

            if header.layout_version >= 2:
                values = FRAME_PREFIX_V2.unpack_from(raw_header, 0)
                metadata_mask = int(values[17])
                metadata_values = FRAME_METADATA_VALUES_V2.unpack_from(
                    raw_header,
                    FRAME_PREFIX_V2.size,
                )
                format_name = _cstr(raw_header[376:408])
                note = _cstr(raw_header[408:472])
            else:
                values = FRAME_PREFIX_V1.unpack_from(raw_header, 0)
                metadata_mask = 0
                metadata_values = tuple(0 for _ in FRAME_METADATA_NAMES)
                format_name = _cstr(raw_header[100:132])
                note = _cstr(raw_header[132:196])

            sequence_after = self._read_sequence(slot_offset)
            if (
                sequence_before != sequence_after
                or sequence_after & 1
                or values[0] != sequence_before
            ):
                time.sleep(0.0005)
                continue

            payload_bytes = int(values[14])
            if payload_bytes < 0 or payload_bytes > stream.slot_payload_capacity_bytes:
                return None

            return BufferRef(
                transport="windows_named_shared_memory",
                mapping_name=self.mapping_name,
                stream_kind=int(values[6]),
                stream_name=STREAM_KIND_NAMES.get(int(values[6]), str(values[6])),
                pool_id=f"{self.mapping_name}:{stream.name}",
                slot_id=slot_id,
                generation=sequence_after,
                slot_offset=slot_offset,
                payload_offset=payload_offset,
                payload_bytes=payload_bytes,
                payload_capacity_bytes=stream.slot_payload_capacity_bytes,
                frame_number=int(values[1]),
                host_qpc=int(values[2]),
                device_timestamp_us=int(values[3]),
                system_timestamp_us=int(values[4]),
                global_timestamp_us=int(values[5]),
                frame_type=int(values[8]),
                format=int(values[9]),
                width=int(values[10]),
                height=int(values[11]),
                stride_bytes=int(values[12]),
                bytes_per_pixel=int(values[13]),
                depth_value_scale_mm=float(values[15]),
                flags=int(values[16]),
                metadata_mask=metadata_mask,
                frame_metadata=_metadata_dict(metadata_mask, metadata_values),
                format_name=format_name,
                note=note,
            )
        return None

    def read_ref(self, reference: dict[str, Any] | BufferRef, attempts: int = 4) -> bytes:
        mm = self._require_open()
        ref = reference.to_dict() if isinstance(reference, BufferRef) else reference
        if ref["mapping_name"] != self.mapping_name:
            raise ValueError("BufferRef belongs to a different mapping")
        slot_offset = int(ref["slot_offset"])
        payload_offset = int(ref["payload_offset"])
        payload_bytes = int(ref["payload_bytes"])
        expected_generation = int(ref["generation"])

        for _ in range(attempts):
            before = self._read_sequence(slot_offset)
            if before != expected_generation or before & 1:
                raise RuntimeError("BufferRef has expired or the slot was recycled")
            mm.seek(payload_offset)
            payload = mm.read(payload_bytes)
            after = self._read_sequence(slot_offset)
            if before == after == expected_generation and not (after & 1):
                return payload
            time.sleep(0.0005)
        raise RuntimeError("could not obtain a consistent shared-memory payload")

    def read_text(self, stream_kind: int) -> Optional[str]:
        ref = self.latest_ref(stream_kind)
        if ref is None:
            return None
        return self.read_ref(ref).decode("utf-8", errors="replace")

    def read_imu(self, stream_kind: int) -> Optional[ImuSample]:
        ref = self.latest_ref(stream_kind)
        if ref is None:
            return None
        payload = self.read_ref(ref)
        if len(payload) < IMU_PAYLOAD.size:
            return None
        values = IMU_PAYLOAD.unpack(payload[: IMU_PAYLOAD.size])
        return ImuSample(
            frame_number=int(values[0]),
            device_timestamp_us=int(values[1]),
            system_timestamp_us=int(values[2]),
            global_timestamp_us=int(values[3]),
            x=float(values[4]),
            y=float(values[5]),
            z=float(values[6]),
            temperature_c=float(values[7]),
            stream_kind=int(values[8]),
        )

    def _find_stream(self, stream_kind: int) -> Optional[StreamDescriptorInfo]:
        header = self.refresh()
        return next(
            (stream for stream in header.streams if stream.stream_kind == stream_kind),
            None,
        )

    def _read_sequence(self, slot_offset: int) -> int:
        mm = self._require_open()
        mm.seek(slot_offset)
        return struct.unpack("<q", mm.read(8))[0]

    def _require_open(self) -> mmap.mmap:
        if self._mm is None:
            raise RuntimeError("shared memory is not open")
        return self._mm

    @staticmethod
    def _read_header(mm: mmap.mmap) -> MappingHeader:
        mm.seek(0)
        values = _HEADER_PREFIX.unpack(mm.read(_HEADER_PREFIX.size))
        (
            magic,
            version,
            stream_count,
            total_bytes,
            _header_bytes,
            _host_start_qpc,
            _qpc_ticks_per_second,
            _host_start_unix_us,
            process_id,
        ) = values
        if magic != MAGIC:
            raise RuntimeError(f"unexpected shared-memory magic 0x{magic:x}")
        if version not in (1, 2):
            raise RuntimeError(f"unsupported shared-memory version {version}")

        mapping_name = _cstr(mm.read(128))
        device_name = _cstr(mm.read(256))
        device_serial = _cstr(mm.read(256))
        sdk_version = _cstr(mm.read(64))

        firmware_version = ""
        connection_type = ""
        device_uid = ""
        usb_pid = 0
        usb_vid = 0
        global_timestamp_supported = False
        global_timestamp_enabled = False
        slot_header_bytes = SLOT_HEADER_BYTES_V1

        if version >= 2:
            firmware_version = _cstr(mm.read(64))
            connection_type = _cstr(mm.read(32))
            device_uid = _cstr(mm.read(128))
            (
                usb_pid,
                usb_vid,
                global_timestamp_supported_raw,
                global_timestamp_enabled_raw,
            ) = struct.unpack("<IIII", mm.read(16))
            global_timestamp_supported = bool(global_timestamp_supported_raw)
            global_timestamp_enabled = bool(global_timestamp_enabled_raw)
            slot_header_bytes = SLOT_HEADER_BYTES_V2
            mm.seek(_HEADER_V2_STREAM_TABLE_OFFSET)
        else:
            mm.seek(_HEADER_V1_STREAM_TABLE_OFFSET)

        streams: list[StreamDescriptorInfo] = []
        for _ in range(stream_count):
            row = STREAM_DESCRIPTOR.unpack(mm.read(STREAM_DESCRIPTOR.size))
            streams.append(
                StreamDescriptorInfo(
                    name=_cstr(row[0]),
                    stream_kind=int(row[1]),
                    payload_kind=int(row[2]),
                    slot_count=int(row[3]),
                    slot_stride_bytes=int(row[5]),
                    slot_payload_capacity_bytes=int(row[6]),
                    base_offset_bytes=int(row[7]),
                    latest_slot=int(row[8]),
                    latest_frame_number=int(row[9]),
                    dropped_frame_count=int(row[10]),
                )
            )

        return MappingHeader(
            layout_version=int(version),
            total_bytes=int(total_bytes),
            mapping_name=mapping_name,
            device_name=device_name,
            device_serial=device_serial,
            sdk_version=sdk_version,
            firmware_version=firmware_version,
            connection_type=connection_type,
            device_uid=device_uid,
            usb_pid=int(usb_pid),
            usb_vid=int(usb_vid),
            global_timestamp_supported=global_timestamp_supported,
            global_timestamp_enabled=global_timestamp_enabled,
            process_id=int(process_id),
            slot_header_bytes=slot_header_bytes,
            streams=tuple(streams),
        )
