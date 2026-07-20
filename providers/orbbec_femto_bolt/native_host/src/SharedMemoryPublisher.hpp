#pragma once

#include "FemtoBoltPipeline/FemtoSharedMemoryLayout.hpp"

#include <Windows.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace fbp {

struct StreamAllocation {
    StreamKind kind;
    PayloadKind payloadKind;
    std::string name;
    uint32_t slotCount;
    uint64_t payloadCapacityBytes;
};

struct PublishMetadata {
    StreamKind streamKind;
    PayloadKind payloadKind;
    uint64_t frameNumber = 0;
    uint64_t deviceTimestampUs = 0;
    uint64_t systemTimestampUs = 0;
    uint64_t globalTimestampUs = 0;
    uint32_t frameType = 0;
    uint32_t format = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t strideBytes = 0;
    uint32_t bytesPerPixel = 0;
    float depthValueScaleMm = 0.0f;
    uint32_t flags = 0;
    uint64_t metadataMask = 0;
    std::array<int64_t, kFrameMetadataTypeCount> metadataValues{};
    std::string formatName;
    std::string note;
};

class SharedMemoryPublisher {
public:
    SharedMemoryPublisher() = default;
    ~SharedMemoryPublisher();

    SharedMemoryPublisher(const SharedMemoryPublisher&) = delete;
    SharedMemoryPublisher& operator=(const SharedMemoryPublisher&) = delete;

    void create(const std::wstring& mappingName, const std::vector<StreamAllocation>& allocations);
    void setDeviceInfo(
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
    );
    bool publish(const PublishMetadata& metadata, const void* payload, uint64_t payloadBytes);
    bool publishText(StreamKind kind, const std::string& text, uint64_t frameNumber, const std::string& note = {});

    const std::wstring& mappingName() const { return mappingName_; }
    uint64_t totalBytes() const { return totalBytes_; }

private:
    struct RuntimeStream {
        uint32_t index = 0;
        StreamDescriptor* descriptor = nullptr;
    };

    static uint64_t alignUp(uint64_t value, uint64_t alignment);
    static void copyFixed(char* dst, size_t dstSize, const std::string& src);
    static std::string wideToUtf8(const std::wstring& src);
    static uint64_t unixTimeUs();
    static uint64_t currentQpc();
    static double qpcTicksPerSecond();

    FrameSlotHeader* slotHeader(const StreamDescriptor& descriptor, uint32_t slotIndex) const;
    uint8_t* slotPayload(const StreamDescriptor& descriptor, uint32_t slotIndex) const;

    HANDLE mappingHandle_ = nullptr;
    uint8_t* view_ = nullptr;
    SharedMemoryHeader* header_ = nullptr;
    uint64_t totalBytes_ = 0;
    std::wstring mappingName_;
    std::unordered_map<uint32_t, RuntimeStream> streams_;
};

} // namespace fbp
