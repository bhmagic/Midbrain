#pragma once

#include <cstdint>

// Shared-memory layout for FemtoBoltPipeline CameraHost v2.
//
// Each stream uses a small latest-frame ring. The writer marks a slot sequence
// odd while updating it and even after metadata and payload are complete.
// Readers accept a copy only when the sequence is even and unchanged before and
// after the copy. This keeps the producer non-blocking and prevents torn frames.

namespace fbp {

static constexpr uint32_t kLayoutVersion = 2;
static constexpr uint32_t kMaxStreams = 9;
static constexpr uint32_t kStreamNameBytes = 32;
static constexpr uint32_t kDeviceTextBytes = 256;
static constexpr uint32_t kFrameMetadataTypeCount = 34;
static constexpr uint32_t kSlotHeaderBytes = 512;
static constexpr uint32_t kSharedHeaderBytes = 4096;
static constexpr uint64_t kMagic = 0x31504d4850434246ull;

enum class StreamKind : uint32_t {
    Color = 0,
    Depth = 1,
    Infrared = 2,
    PointCloud = 3,
    Accel = 4,
    Gyro = 5,
    Calibration = 6,
    Status = 7,
    AlignedDepth = 8
};

enum class PayloadKind : uint32_t {
    RawFrame = 0,
    ImuSample = 1,
    Utf8Text = 2,
    PointCloud = 3
};

#pragma pack(push, 1)

struct StreamDescriptor {
    char name[kStreamNameBytes];
    uint32_t stream_kind;
    uint32_t payload_kind;
    uint32_t slot_count;
    uint32_t reserved0;
    uint64_t slot_stride_bytes;
    uint64_t slot_payload_capacity_bytes;
    uint64_t base_offset_bytes;
    volatile int64_t latest_slot;
    volatile uint64_t latest_frame_number;
    volatile uint64_t dropped_frame_count;
};

struct SharedMemoryHeader {
    uint64_t magic;
    uint32_t layout_version;
    uint32_t stream_count;
    uint64_t total_bytes;
    uint64_t header_bytes;
    uint64_t host_start_qpc;
    double qpc_ticks_per_second;
    uint64_t host_start_unix_us;
    uint64_t process_id;
    char mapping_name[128];
    char device_name[kDeviceTextBytes];
    char device_serial[kDeviceTextBytes];
    char sdk_version[64];
    char firmware_version[64];
    char connection_type[32];
    char device_uid[128];
    uint32_t usb_pid;
    uint32_t usb_vid;
    uint32_t global_timestamp_supported;
    uint32_t global_timestamp_enabled;
    StreamDescriptor streams[kMaxStreams];
    uint8_t reserved[4096 - 8 - 4 - 4 - 8 - 8 - 8 - 8 - 8 - 8 - 128 - kDeviceTextBytes - kDeviceTextBytes - 64 - 64 - 32 - 128 - 4 - 4 - 4 - 4 - sizeof(StreamDescriptor) * kMaxStreams];
};

struct FrameSlotHeader {
    volatile int64_t sequence;
    uint64_t frame_number;
    uint64_t host_qpc;
    uint64_t device_timestamp_us;
    uint64_t system_timestamp_us;
    uint64_t global_timestamp_us;
    uint32_t stream_kind;
    uint32_t payload_kind;
    uint32_t frame_type;
    uint32_t format;
    uint32_t width;
    uint32_t height;
    uint32_t stride_bytes;
    uint32_t bytes_per_pixel;
    uint64_t payload_bytes;
    float depth_value_scale_mm;
    uint32_t flags;
    uint64_t metadata_mask;
    int64_t metadata_values[kFrameMetadataTypeCount];
    char format_name[32];
    char note[64];
    uint8_t reserved[512 - 8 - 8 - 8 - 8 - 8 - 8 - 4 - 4 - 4 - 4 - 4 - 4 - 4 - 4 - 8 - 4 - 4 - 8 - 8 * kFrameMetadataTypeCount - 32 - 64];
};

struct ImuSamplePayload {
    uint64_t frame_number;
    uint64_t device_timestamp_us;
    uint64_t system_timestamp_us;
    uint64_t global_timestamp_us;
    float x;
    float y;
    float z;
    float temperature_c;
    uint32_t stream_kind;
    uint32_t reserved0;
};

#pragma pack(pop)

static_assert(sizeof(SharedMemoryHeader) == kSharedHeaderBytes, "SharedMemoryHeader must stay 4096 bytes");
static_assert(sizeof(FrameSlotHeader) == kSlotHeaderBytes, "FrameSlotHeader must stay 512 bytes");

} // namespace fbp
