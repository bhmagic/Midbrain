#include "SharedMemoryPublisher.hpp"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <stdexcept>

namespace fbp {

SharedMemoryPublisher::~SharedMemoryPublisher() {
    if(view_) {
        UnmapViewOfFile(view_);
        view_ = nullptr;
    }
    if(mappingHandle_) {
        CloseHandle(mappingHandle_);
        mappingHandle_ = nullptr;
    }
}

uint64_t SharedMemoryPublisher::alignUp(uint64_t value, uint64_t alignment) {
    return (value + alignment - 1) / alignment * alignment;
}

void SharedMemoryPublisher::copyFixed(char* dst, size_t dstSize, const std::string& src) {
    if(dstSize == 0) {
        return;
    }
    std::memset(dst, 0, dstSize);
    const auto n = std::min(dstSize - 1, src.size());
    std::memcpy(dst, src.data(), n);
}


std::string SharedMemoryPublisher::wideToUtf8(const std::wstring& src) {
    if(src.empty()) {
        return {};
    }

    const int requiredBytes = WideCharToMultiByte(CP_UTF8, 0, src.data(), static_cast<int>(src.size()), nullptr, 0, nullptr, nullptr);
    if(requiredBytes <= 0) {
        return {};
    }

    std::string dst(static_cast<size_t>(requiredBytes), '\0');
    const int writtenBytes = WideCharToMultiByte(CP_UTF8, 0, src.data(), static_cast<int>(src.size()), dst.data(), requiredBytes, nullptr, nullptr);
    if(writtenBytes <= 0) {
        return {};
    }
    return dst;
}

uint64_t SharedMemoryPublisher::unixTimeUs() {
    const auto now = std::chrono::system_clock::now().time_since_epoch();
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(now).count());
}

uint64_t SharedMemoryPublisher::currentQpc() {
    LARGE_INTEGER qpc{};
    QueryPerformanceCounter(&qpc);
    return static_cast<uint64_t>(qpc.QuadPart);
}

double SharedMemoryPublisher::qpcTicksPerSecond() {
    LARGE_INTEGER freq{};
    QueryPerformanceFrequency(&freq);
    return static_cast<double>(freq.QuadPart);
}

void SharedMemoryPublisher::create(const std::wstring& mappingName, const std::vector<StreamAllocation>& allocations) {
    if(allocations.empty() || allocations.size() > kMaxStreams) {
        throw std::runtime_error("invalid stream allocation count");
    }
    if(mappingHandle_ || view_) {
        throw std::runtime_error("shared memory already created");
    }

    mappingName_ = mappingName;
    uint64_t offset = kSharedHeaderBytes;
    std::vector<StreamDescriptor> descriptors(allocations.size());

    for(size_t i = 0; i < allocations.size(); ++i) {
        const auto& allocation = allocations[i];
        if(allocation.slotCount == 0 || allocation.payloadCapacityBytes == 0) {
            throw std::runtime_error("invalid stream slot allocation");
        }

        StreamDescriptor desc{};
        copyFixed(desc.name, sizeof(desc.name), allocation.name);
        desc.stream_kind = static_cast<uint32_t>(allocation.kind);
        desc.payload_kind = static_cast<uint32_t>(allocation.payloadKind);
        desc.slot_count = allocation.slotCount;
        desc.slot_payload_capacity_bytes = allocation.payloadCapacityBytes;
        desc.slot_stride_bytes = alignUp(kSlotHeaderBytes + allocation.payloadCapacityBytes, 4096);
        desc.base_offset_bytes = alignUp(offset, 4096);
        desc.latest_slot = -1;
        descriptors[i] = desc;
        offset = desc.base_offset_bytes + desc.slot_stride_bytes * desc.slot_count;
    }

    totalBytes_ = alignUp(offset, 4096);
    SetLastError(ERROR_SUCCESS);
    mappingHandle_ = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
                                        static_cast<DWORD>(totalBytes_ >> 32),
                                        static_cast<DWORD>(totalBytes_ & 0xffffffff),
                                        mappingName.c_str());
    const DWORD createErrorCode = GetLastError();
    if(!mappingHandle_) {
        const DWORD errorCode = createErrorCode;
        throw std::runtime_error(
            "CreateFileMappingW failed with Windows error " + std::to_string(errorCode)
        );
    }
    if(createErrorCode == ERROR_ALREADY_EXISTS) {
        CloseHandle(mappingHandle_);
        mappingHandle_ = nullptr;
        throw std::runtime_error(
            "shared-memory mapping already exists; stop the previous CameraHost or consumer before restarting"
        );
    }

    view_ = static_cast<uint8_t*>(MapViewOfFile(mappingHandle_, FILE_MAP_ALL_ACCESS, 0, 0, 0));
    if(!view_) {
        const DWORD errorCode = GetLastError();
        CloseHandle(mappingHandle_);
        mappingHandle_ = nullptr;
        throw std::runtime_error(
            "MapViewOfFile failed with Windows error " + std::to_string(errorCode)
        );
    }

    std::memset(view_, 0, static_cast<size_t>(totalBytes_));
    header_ = reinterpret_cast<SharedMemoryHeader*>(view_);
    header_->magic = kMagic;
    header_->layout_version = kLayoutVersion;
    header_->stream_count = static_cast<uint32_t>(allocations.size());
    header_->total_bytes = totalBytes_;
    header_->header_bytes = kSharedHeaderBytes;
    header_->host_start_qpc = currentQpc();
    header_->qpc_ticks_per_second = qpcTicksPerSecond();
    header_->host_start_unix_us = unixTimeUs();
    header_->process_id = GetCurrentProcessId();

    const std::string mappingNameUtf8 = wideToUtf8(mappingName);
    copyFixed(header_->mapping_name, sizeof(header_->mapping_name), mappingNameUtf8);

    for(size_t i = 0; i < descriptors.size(); ++i) {
        header_->streams[i] = descriptors[i];
        streams_.emplace(descriptors[i].stream_kind, RuntimeStream{static_cast<uint32_t>(i), &header_->streams[i]});
    }
}

void SharedMemoryPublisher::setDeviceInfo(
    const std::string& deviceName,
    const std::string& serial,
    const std::string& sdkVersion,
    const std::string& firmwareVersion,
    const std::string& connectionType,
    const std::string& deviceUid,
    uint32_t usbPid,
    uint32_t usbVid,
    bool globalTimestampSupported,
    bool globalTimestampEnabled
) {
    if(!header_) {
        return;
    }
    copyFixed(header_->device_name, sizeof(header_->device_name), deviceName);
    copyFixed(header_->device_serial, sizeof(header_->device_serial), serial);
    copyFixed(header_->sdk_version, sizeof(header_->sdk_version), sdkVersion);
    copyFixed(header_->firmware_version, sizeof(header_->firmware_version), firmwareVersion);
    copyFixed(header_->connection_type, sizeof(header_->connection_type), connectionType);
    copyFixed(header_->device_uid, sizeof(header_->device_uid), deviceUid);
    header_->usb_pid = usbPid;
    header_->usb_vid = usbVid;
    header_->global_timestamp_supported = globalTimestampSupported ? 1u : 0u;
    header_->global_timestamp_enabled = globalTimestampEnabled ? 1u : 0u;
}

FrameSlotHeader* SharedMemoryPublisher::slotHeader(const StreamDescriptor& descriptor, uint32_t slotIndex) const {
    auto* ptr = view_ + descriptor.base_offset_bytes + descriptor.slot_stride_bytes * slotIndex;
    return reinterpret_cast<FrameSlotHeader*>(ptr);
}

uint8_t* SharedMemoryPublisher::slotPayload(const StreamDescriptor& descriptor, uint32_t slotIndex) const {
    auto* ptr = view_ + descriptor.base_offset_bytes + descriptor.slot_stride_bytes * slotIndex + kSlotHeaderBytes;
    return ptr;
}

bool SharedMemoryPublisher::publish(const PublishMetadata& metadata, const void* payload, uint64_t payloadBytes) {
    if(!header_ || !payload) {
        return false;
    }

    auto it = streams_.find(static_cast<uint32_t>(metadata.streamKind));
    if(it == streams_.end()) {
        return false;
    }

    auto* desc = it->second.descriptor;
    if(payloadBytes > desc->slot_payload_capacity_bytes) {
        InterlockedIncrement64(reinterpret_cast<volatile LONG64*>(&desc->dropped_frame_count));
        return false;
    }

    // Multiple slots form a latest-frame double/ring buffer. The writer always advances
    // to the next slot instead of overwriting the currently published slot. A slow reader
    // may still be reading an old slot when it wraps around, so every slot has its own
    // seqlock counter. Readers accept a payload only when this counter is even and
    // unchanged before and after copying the payload.
    const int64_t previousSlot = desc->latest_slot;
    const auto slot = static_cast<uint32_t>((previousSlot + 1) % static_cast<int64_t>(desc->slot_count));
    auto* slotHdr = slotHeader(*desc, slot);
    auto* data = slotPayload(*desc, slot);

    const int64_t currentSequence = slotHdr->sequence;
    const int64_t oddSequence = (currentSequence % 2 == 0) ? currentSequence + 1 : currentSequence + 2;
    InterlockedExchange64(reinterpret_cast<volatile LONG64*>(&slotHdr->sequence), oddSequence);
    MemoryBarrier();

    slotHdr->frame_number = metadata.frameNumber;
    slotHdr->host_qpc = currentQpc();
    slotHdr->device_timestamp_us = metadata.deviceTimestampUs;
    slotHdr->system_timestamp_us = metadata.systemTimestampUs;
    slotHdr->global_timestamp_us = metadata.globalTimestampUs;
    slotHdr->stream_kind = static_cast<uint32_t>(metadata.streamKind);
    slotHdr->payload_kind = static_cast<uint32_t>(metadata.payloadKind);
    slotHdr->frame_type = metadata.frameType;
    slotHdr->format = metadata.format;
    slotHdr->width = metadata.width;
    slotHdr->height = metadata.height;
    slotHdr->stride_bytes = metadata.strideBytes;
    slotHdr->bytes_per_pixel = metadata.bytesPerPixel;
    slotHdr->payload_bytes = payloadBytes;
    slotHdr->depth_value_scale_mm = metadata.depthValueScaleMm;
    slotHdr->flags = metadata.flags;
    slotHdr->metadata_mask = metadata.metadataMask;
    for(size_t i = 0; i < metadata.metadataValues.size(); ++i) {
        slotHdr->metadata_values[i] = metadata.metadataValues[i];
    }
    copyFixed(slotHdr->format_name, sizeof(slotHdr->format_name), metadata.formatName);
    copyFixed(slotHdr->note, sizeof(slotHdr->note), metadata.note);

    std::memcpy(data, payload, static_cast<size_t>(payloadBytes));
    MemoryBarrier();

    // Publish the completed slot by making its sequence even. Only after that do we
    // update the stream descriptor to point readers at the newest stable slot.
    InterlockedExchange64(reinterpret_cast<volatile LONG64*>(&slotHdr->sequence), oddSequence + 1);
    MemoryBarrier();
    InterlockedExchange64(reinterpret_cast<volatile LONG64*>(&desc->latest_slot), slot);
    InterlockedExchange64(reinterpret_cast<volatile LONG64*>(&desc->latest_frame_number), metadata.frameNumber);
    return true;
}

bool SharedMemoryPublisher::publishText(StreamKind kind, const std::string& text, uint64_t frameNumber, const std::string& note) {
    PublishMetadata meta{};
    meta.streamKind = kind;
    meta.payloadKind = PayloadKind::Utf8Text;
    meta.frameNumber = frameNumber;
    meta.formatName = "utf8";
    meta.note = note;
    return publish(meta, text.data(), static_cast<uint64_t>(text.size()));
}

} // namespace fbp
