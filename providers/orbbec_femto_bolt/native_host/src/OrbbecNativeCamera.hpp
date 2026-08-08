#pragma once

#include "SharedMemoryPublisher.hpp"

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>

#include "libobsensor/ObSensor.hpp"
#include "libobsensor/hpp/Error.hpp"
#include "libobsensor/hpp/Filter.hpp"
#include "libobsensor/hpp/Pipeline.hpp"

namespace fbp {

struct CameraOptions {
    bool enableColor = true;
    bool enableDepth = true;
    bool enableIr = true;
    bool enableImu = true;
    bool enableFrameSync = true;
    bool enableHardwareD2CAlignment = true;
    bool enableSoftwareD2CAlignment = true;
    bool enablePointCloud = true;
    bool enableRgbPointCloudExperimental = false;
    uint32_t statusPrintIntervalMs = 1000;
};

class OrbbecNativeCamera {
public:
    explicit OrbbecNativeCamera(SharedMemoryPublisher& publisher);
    ~OrbbecNativeCamera();

    OrbbecNativeCamera(const OrbbecNativeCamera&) = delete;
    OrbbecNativeCamera& operator=(const OrbbecNativeCamera&) = delete;

    void start(const CameraOptions& options);
    void stop();
    void runUntilEnter();

private:
    void publishFrameSet(const std::shared_ptr<ob::FrameSet>& frameset);
    void publishVideoFrame(
        const std::shared_ptr<ob::Frame>& frame,
        StreamKind kind,
        const std::string& note = {},
        const std::shared_ptr<ob::Frame>& captureTimestampSource = nullptr
    );
    void publishPointCloud(const std::shared_ptr<ob::Frame>& input, const std::shared_ptr<ob::FrameSet>& sourceFrameset);
    void publishImuFrame(const std::shared_ptr<ob::Frame>& frame);
    bool publishCalibrationText();
    void publishStatusText(const std::string& status);
    void startImuPipeline(const std::shared_ptr<ob::Device>& device);
    bool tryEnableDefaultVideoStream(
        const std::shared_ptr<ob::Config>& config,
        OBSensorType sensorType,
        const char* label,
        std::shared_ptr<ob::VideoStreamProfile>* selectedProfile = nullptr
    );
    static void collectFrameMetadata(const std::shared_ptr<ob::Frame>& frame, PublishMetadata& metadata);
    std::string sensorListText(const std::shared_ptr<ob::Device>& device);

    static std::string sdkVersionString();
    static std::string formatName(OBFormat format);
    static uint32_t bytesPerPixelFromFormat(OBFormat format);
    static uint32_t strideFromFrame(const std::shared_ptr<ob::VideoFrame>& videoFrame);

    SharedMemoryPublisher& publisher_;
    CameraOptions options_{};
    std::shared_ptr<ob::Pipeline> videoPipeline_;
    std::shared_ptr<ob::Pipeline> imuPipeline_;
    std::shared_ptr<ob::VideoStreamProfile> colorProfile_;
    std::shared_ptr<ob::VideoStreamProfile> depthProfile_;
    std::shared_ptr<ob::VideoStreamProfile> irProfile_;
    std::shared_ptr<ob::AccelStreamProfile> accelProfile_;
    std::shared_ptr<ob::GyroStreamProfile> gyroProfile_;
    std::shared_ptr<ob::Align> d2cAlignFilter_;
    std::unique_ptr<ob::PointCloudFilter> pointCloudFilter_;
    std::atomic<bool> running_{false};
    std::atomic<uint64_t> statusFrameCounter_{0};
    std::atomic<uint64_t> depthFrameCounter_{0};
    std::atomic<bool> calibrationReady_{false};
};

} // namespace fbp
