#include "OrbbecNativeCamera.hpp"

#include <chrono>
#include <csignal>
#include <atomic>
#include <iostream>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#endif

namespace {

std::atomic<bool> g_stopRequested{false};

std::string obErrorText(const ob::Error& e) {
    std::ostringstream oss;
    oss << e.getName() << "(" << e.getArgs() << "): " << e.getMessage()
        << " [" << e.getExceptionType() << "]";
    return oss.str();
}

#ifdef _WIN32
BOOL WINAPI cameraHostConsoleHandler(DWORD ctrlType) {
    switch(ctrlType) {
    case CTRL_C_EVENT:
    case CTRL_BREAK_EVENT:
    case CTRL_CLOSE_EVENT:
    case CTRL_LOGOFF_EVENT:
    case CTRL_SHUTDOWN_EVENT:
        g_stopRequested.store(true);
        return TRUE;
    default:
        return FALSE;
    }
}
#else
void cameraHostSignalHandler(int) {
    g_stopRequested.store(true);
}
#endif

} // namespace

namespace fbp {

OrbbecNativeCamera::OrbbecNativeCamera(SharedMemoryPublisher& publisher) : publisher_(publisher) {}

OrbbecNativeCamera::~OrbbecNativeCamera() {
    stop();
}

std::string OrbbecNativeCamera::sdkVersionString() {
    std::ostringstream oss;
    oss << ob::Version::getMajor() << "." << ob::Version::getMinor() << "." << ob::Version::getPatch();
    const auto* stage = ob::Version::getStageVersion();
    if(stage && *stage) {
        oss << "-" << stage;
    }
    return oss.str();
}

std::string OrbbecNativeCamera::formatName(OBFormat format) {
    switch(format) {
    case OB_FORMAT_Y16: return "Y16";
    case OB_FORMAT_Y8: return "Y8";
    case OB_FORMAT_RGB: return "RGB";
    case OB_FORMAT_BGR: return "BGR";
    case OB_FORMAT_BGRA: return "BGRA";
    case OB_FORMAT_RGBA: return "RGBA";
    case OB_FORMAT_MJPG: return "MJPG";
    case OB_FORMAT_NV12: return "NV12";
    case OB_FORMAT_POINT: return "POINT";
    case OB_FORMAT_RGB_POINT: return "RGB_POINT";
    default: return "OB_FORMAT_" + std::to_string(static_cast<int>(format));
    }
}

uint32_t OrbbecNativeCamera::bytesPerPixelFromFormat(OBFormat format) {
    switch(format) {
    case OB_FORMAT_Y8: return 1;
    case OB_FORMAT_Y16: return 2;
    case OB_FORMAT_RGB: return 3;
    case OB_FORMAT_BGR: return 3;
    case OB_FORMAT_RGBA: return 4;
    case OB_FORMAT_BGRA: return 4;
    default: return 0;
    }
}

uint32_t OrbbecNativeCamera::strideFromFrame(const std::shared_ptr<ob::VideoFrame>& videoFrame) {
    if(!videoFrame || videoFrame->height() == 0) {
        return 0;
    }
    const auto bpp = bytesPerPixelFromFormat(videoFrame->format());
    if(bpp != 0) {
        return videoFrame->width() * bpp;
    }
    return videoFrame->dataSize() / videoFrame->height();
}

std::string OrbbecNativeCamera::sensorListText(const std::shared_ptr<ob::Device>& device) {
    std::ostringstream oss;
    auto sensorList = device->getSensorList();
    oss << "Sensors:";
    for(uint32_t i = 0; i < sensorList->count(); ++i) {
        const auto type = sensorList->type(i);
        oss << " " << static_cast<int>(type);
    }
    return oss.str();
}

bool OrbbecNativeCamera::tryEnableDefaultVideoStream(
    const std::shared_ptr<ob::Config>& config,
    OBSensorType sensorType,
    const char* label,
    std::shared_ptr<ob::VideoStreamProfile>* selectedProfile
) {
    try {
        auto profiles = videoPipeline_->getStreamProfileList(sensorType);
        if(!profiles || profiles->count() == 0) {
            std::cout << "[CameraHost] " << label << " profile list is empty; skipping.\n";
            return false;
        }
        auto profile = profiles->getProfile(OB_PROFILE_DEFAULT);
        config->enableStream(profile);
        auto videoProfile = profile->as<ob::VideoStreamProfile>();
        if(selectedProfile) {
            *selectedProfile = videoProfile;
        }
        std::cout << "[CameraHost] Enabled " << label << " " << videoProfile->width() << "x" << videoProfile->height()
                  << " @ " << videoProfile->fps() << " fps, format " << formatName(videoProfile->format()) << "\n";
        return true;
    }
    catch(const std::exception& e) {
        std::cout << "[CameraHost] " << label << " not enabled: " << e.what() << "\n";
        return false;
    }
}

void OrbbecNativeCamera::start(const CameraOptions& options) {
    if(running_) {
        return;
    }

    options_ = options;
    calibrationReady_.store(false);
    depthFrameCounter_.store(0);
    colorProfile_.reset();
    depthProfile_.reset();
    irProfile_.reset();
    accelProfile_.reset();
    gyroProfile_.reset();
    ob::Context::setLoggerSeverity(OB_LOG_SEVERITY_WARN);

    videoPipeline_ = std::make_shared<ob::Pipeline>();
    auto device = videoPipeline_->getDevice();
    auto info = device->getDeviceInfo();

    bool globalTimestampSupported = false;
    bool globalTimestampEnabled = false;
    try {
        globalTimestampSupported = device->isGlobalTimestampSupported();
        if(globalTimestampSupported) {
            device->enableGlobalTimestamp(true);
            globalTimestampEnabled = true;
            std::cout << "[CameraHost] Host-domain global timestamps enabled.\n";
        }
    }
    catch(const std::exception& e) {
        std::cout << "[CameraHost] Global timestamp unavailable; continuing: " << e.what() << "\n";
    }

    publisher_.setDeviceInfo(
        info->getName(),
        info->getSerialNumber(),
        sdkVersionString(),
        info->getFirmwareVersion(),
        info->getConnectionType(),
        info->getUid(),
        static_cast<uint32_t>(info->getPid()),
        static_cast<uint32_t>(info->getVid()),
        globalTimestampSupported,
        globalTimestampEnabled
    );

    std::cout << "[CameraHost] SDK version: " << sdkVersionString() << "\n";
    std::cout << "[CameraHost] Device: " << info->getName() << "\n";
    std::cout << "[CameraHost] Serial: " << info->getSerialNumber() << "\n";
    std::cout << "[CameraHost] Firmware: " << info->getFirmwareVersion() << "\n";
    std::cout << "[CameraHost] Connection: " << info->getConnectionType() << "\n";
    std::cout << "[CameraHost] " << sensorListText(device) << "\n";

    auto config = std::make_shared<ob::Config>();
    std::shared_ptr<ob::VideoStreamProfile> colorProfile = nullptr;

    if(options_.enableColor) {
        try {
            auto colorProfiles = videoPipeline_->getStreamProfileList(OB_SENSOR_COLOR);
            if(colorProfiles && colorProfiles->count() > 0) {
                auto profile = colorProfiles->getProfile(OB_PROFILE_DEFAULT);
                colorProfile = profile->as<ob::VideoStreamProfile>();
                colorProfile_ = colorProfile;
                config->enableStream(profile);
                std::cout << "[CameraHost] Enabled color " << colorProfile->width() << "x" << colorProfile->height()
                          << " @ " << colorProfile->fps() << " fps, format " << formatName(colorProfile->format()) << "\n";
            }
        }
        catch(const std::exception& e) {
            std::cout << "[CameraHost] Color not enabled: " << e.what() << "\n";
        }
    }

    if(options_.enableDepth) {
        try {
            bool depthEnabled = false;
            if(options_.enableHardwareD2CAlignment && colorProfile) {
                std::shared_ptr<ob::StreamProfileList> depthProfiles;
                try {
                    depthProfiles = videoPipeline_->getD2CDepthProfileList(colorProfile, ALIGN_D2C_HW_MODE);
                    if(depthProfiles && depthProfiles->count() > 0) {
                        auto depthProfile = depthProfiles->getProfile(OB_PROFILE_DEFAULT);
                        depthProfile_ = depthProfile->as<ob::VideoStreamProfile>();
                        config->enableStream(depthProfile);
                        config->setAlignMode(ALIGN_D2C_HW_MODE);
                        std::cout << "[CameraHost] Enabled depth with hardware D2C alignment.\n";
                        depthEnabled = true;
                    }
                }
                catch(const std::exception& e) {
                    std::cout << "[CameraHost] Hardware D2C profile unavailable: " << e.what() << "\n";
                }
            }
            if(!depthEnabled) {
                depthEnabled = tryEnableDefaultVideoStream(config, OB_SENSOR_DEPTH, "depth", &depthProfile_);
            }
            (void)depthEnabled;
        }
        catch(const std::exception& e) {
            std::cout << "[CameraHost] Depth not enabled: " << e.what() << "\n";
        }
    }

    if(options_.enableIr) {
        bool enabledIr = tryEnableDefaultVideoStream(config, OB_SENSOR_IR, "IR", &irProfile_);
        if(!enabledIr) {
            enabledIr = tryEnableDefaultVideoStream(config, OB_SENSOR_IR_LEFT, "IR_LEFT", &irProfile_);
        }
        if(!enabledIr) {
            (void)tryEnableDefaultVideoStream(config, OB_SENSOR_IR_RIGHT, "IR_RIGHT", &irProfile_);
        }
    }

    if(options_.enableFrameSync) {
        try {
            videoPipeline_->enableFrameSync();
            std::cout << "[CameraHost] Frame sync enabled.\n";
        }
        catch(const std::exception& e) {
            std::cout << "[CameraHost] Frame sync enable failed; continuing: " << e.what() << "\n";
        }
    }

    if(options_.enableSoftwareD2CAlignment) {
        try {
            d2cAlignFilter_ = std::make_shared<ob::Align>(OB_STREAM_COLOR);
            d2cAlignFilter_->setMatchTargetResolution(true);
            std::cout << "[CameraHost] Software D2C aligned-depth publishing enabled.\n";
        }
        catch(const std::exception& e) {
            std::cout << "[CameraHost] Software D2C align unavailable: " << e.what() << "\n";
            d2cAlignFilter_.reset();
        }
    }

    if(options_.enablePointCloud) {
        try {
            pointCloudFilter_ = std::make_unique<ob::PointCloudFilter>();
            pointCloudFilter_->setCreatePointFormat(
                options_.enableRgbPointCloudExperimental ? OB_FORMAT_RGB_POINT : OB_FORMAT_POINT
            );
            std::cout << "[CameraHost] Point cloud publishing enabled: "
                      << (options_.enableRgbPointCloudExperimental ? "RGB_POINT (experimental)" : "POINT")
                      << "\n";
        }
        catch(const std::exception& e) {
            std::cout << "[CameraHost] Point cloud filter unavailable: " << e.what() << "\n";
            pointCloudFilter_.reset();
        }
    }

    running_ = true;
    videoPipeline_->start(config, [this](std::shared_ptr<ob::FrameSet> frameset) {
        try {
            publishFrameSet(frameset);
        }
        catch(const ob::Error& e) {
            publishStatusText(std::string("Video callback Orbbec SDK error: ") + obErrorText(e));
        }
        catch(const std::exception& e) {
            publishStatusText(std::string("Video callback failed: ") + e.what());
        }
        catch(...) {
            publishStatusText("Video callback failed: unknown exception");
        }
    });

    if(options_.enableImu) {
        startImuPipeline(device);
    }

    publishCalibrationText();
    publishStatusText("CameraHost started");
}

void OrbbecNativeCamera::startImuPipeline(const std::shared_ptr<ob::Device>& device) {
    try {
        imuPipeline_ = std::make_shared<ob::Pipeline>(device);
        auto imuConfig = std::make_shared<ob::Config>();
        auto accelProfiles = imuPipeline_->getStreamProfileList(OB_SENSOR_ACCEL);
        auto gyroProfiles = imuPipeline_->getStreamProfileList(OB_SENSOR_GYRO);
        if(!accelProfiles || accelProfiles->count() == 0 || !gyroProfiles || gyroProfiles->count() == 0) {
            throw std::runtime_error("IMU profile list is empty");
        }
        accelProfile_ = accelProfiles->getProfile(OB_PROFILE_DEFAULT)->as<ob::AccelStreamProfile>();
        gyroProfile_ = gyroProfiles->getProfile(OB_PROFILE_DEFAULT)->as<ob::GyroStreamProfile>();
        imuConfig->enableStream(accelProfile_);
        imuConfig->enableStream(gyroProfile_);
        imuPipeline_->start(imuConfig, [this](std::shared_ptr<ob::FrameSet> frameset) {
            if(!frameset) {
                return;
            }
            for(uint32_t i = 0; i < frameset->frameCount(); ++i) {
                publishImuFrame(frameset->getFrame(static_cast<int>(i)));
            }
        });
        std::cout << "[CameraHost] IMU accel/gyro publishing enabled.\n";
    }
    catch(const ob::Error& e) {
        imuPipeline_.reset();
        accelProfile_.reset();
        gyroProfile_.reset();
        std::cout << "[CameraHost] IMU pipeline not enabled: " << obErrorText(e) << "\n";
    }
    catch(const std::exception& e) {
        imuPipeline_.reset();
        accelProfile_.reset();
        gyroProfile_.reset();
        std::cout << "[CameraHost] IMU pipeline not enabled: " << e.what() << "\n";
    }
}

bool OrbbecNativeCamera::publishCalibrationText() {
    if(!videoPipeline_) {
        return false;
    }

    try {
        const auto cameraParam = videoPipeline_->getCameraParam();
        auto writeFloatArray = [](std::ostringstream& out, const auto* values, size_t count) {
            out << "[";
            for(size_t i = 0; i < count; ++i) {
                if(i) {
                    out << ", ";
                }
                out << values[i];
            }
            out << "]";
        };
        auto writeCameraIntrinsic = [](std::ostringstream& out, const OBCameraIntrinsic& value) {
            out << "{\"fx\": " << value.fx << ", \"fy\": " << value.fy
                << ", \"cx\": " << value.cx << ", \"cy\": " << value.cy
                << ", \"width\": " << value.width << ", \"height\": " << value.height << "}";
        };
        auto writeDistortion = [](std::ostringstream& out, const OBCameraDistortion& value) {
            out << "{\"k1\": " << value.k1 << ", \"k2\": " << value.k2
                << ", \"k3\": " << value.k3 << ", \"k4\": " << value.k4
                << ", \"k5\": " << value.k5 << ", \"k6\": " << value.k6
                << ", \"p1\": " << value.p1 << ", \"p2\": " << value.p2
                << ", \"model\": " << static_cast<int>(value.model) << "}";
        };
        auto writeExtrinsic = [&writeFloatArray](std::ostringstream& out, const OBExtrinsic& value) {
            out << "{\"rot\": ";
            writeFloatArray(out, value.rot, 9);
            out << ", \"trans\": ";
            writeFloatArray(out, value.trans, 3);
            out << ", \"translation_units\": \"millimeters\"}";
        };
        auto extrinsicField = [&writeExtrinsic](
            const char* name,
            const std::shared_ptr<ob::StreamProfile>& source,
            const std::shared_ptr<ob::StreamProfile>& target
        ) -> std::string {
            if(!source || !target) {
                return {};
            }
            try {
                const auto extrinsic = source->getExtrinsicTo(target);
                std::ostringstream field;
                field << "\"" << name << "\": ";
                writeExtrinsic(field, extrinsic);
                return field.str();
            }
            catch(const std::exception&) {
                // Extrinsics are capability-specific. Omit unsupported pairs.
                return {};
            }
        };
        auto writeFields = [](std::ostringstream& out, const std::vector<std::string>& fields) {
            for(size_t i = 0; i < fields.size(); ++i) {
                if(i) {
                    out << ", ";
                }
                out << fields[i];
            }
        };

        std::ostringstream oss;
        oss << "{";
        oss << "\"rgb_intrinsic\": ";
        writeCameraIntrinsic(oss, cameraParam.rgbIntrinsic);
        oss << ", \"depth_intrinsic\": ";
        writeCameraIntrinsic(oss, cameraParam.depthIntrinsic);
        oss << ", \"rgb_distortion\": ";
        writeDistortion(oss, cameraParam.rgbDistortion);
        oss << ", \"depth_distortion\": ";
        writeDistortion(oss, cameraParam.depthDistortion);
        oss << ", \"transform\": ";
        writeExtrinsic(oss, cameraParam.transform);
        oss << ", \"depth_to_color\": ";
        writeExtrinsic(oss, cameraParam.transform);
        oss << ", \"is_mirrored\": " << (cameraParam.isMirrored ? "true" : "false");

        if(irProfile_) {
            try {
                std::ostringstream ir;
                ir << "{\"intrinsic\": ";
                writeCameraIntrinsic(ir, irProfile_->getIntrinsic());
                ir << ", \"distortion\": ";
                writeDistortion(ir, irProfile_->getDistortion());
                const auto toColor = extrinsicField("to_color", irProfile_, colorProfile_);
                const auto toDepth = extrinsicField("to_depth", irProfile_, depthProfile_);
                if(!toColor.empty()) {
                    ir << ", " << toColor;
                }
                if(!toDepth.empty()) {
                    ir << ", " << toDepth;
                }
                ir << "}";
                oss << ", \"infrared\": " << ir.str();
            }
            catch(const std::exception&) {
                // Some firmware exposes IR frames without calibration profiles.
            }
        }

        std::vector<std::string> imuFields;
        if(accelProfile_) {
            try {
                const auto intrinsic = accelProfile_->getIntrinsic();
                std::ostringstream field;
                field << "\"accelerometer\": {"
                    << "\"sample_rate\": " << static_cast<int>(accelProfile_->getSampleRate())
                    << ", \"full_scale_range\": " << static_cast<int>(accelProfile_->getFullScaleRange())
                    << ", \"intrinsic\": {\"noise_density\": " << intrinsic.noiseDensity
                    << ", \"random_walk\": " << intrinsic.randomWalk
                    << ", \"reference_temperature_c\": " << intrinsic.referenceTemp
                    << ", \"bias\": ";
                writeFloatArray(field, intrinsic.bias, 3);
                field << ", \"gravity\": ";
                writeFloatArray(field, intrinsic.gravity, 3);
                field << ", \"scale_misalignment\": ";
                writeFloatArray(field, intrinsic.scaleMisalignment, 9);
                field << ", \"temperature_slope\": ";
                writeFloatArray(field, intrinsic.tempSlope, 9);
                field << "}}";
                imuFields.push_back(field.str());
            }
            catch(const std::exception&) {
                // Keep RGB-D calibration valid if IMU intrinsic retrieval fails.
            }
        }
        if(gyroProfile_) {
            try {
                const auto intrinsic = gyroProfile_->getIntrinsic();
                std::ostringstream field;
                field << "\"gyroscope\": {"
                    << "\"sample_rate\": " << static_cast<int>(gyroProfile_->getSampleRate())
                    << ", \"full_scale_range\": " << static_cast<int>(gyroProfile_->getFullScaleRange())
                    << ", \"intrinsic\": {\"noise_density\": " << intrinsic.noiseDensity
                    << ", \"random_walk\": " << intrinsic.randomWalk
                    << ", \"reference_temperature_c\": " << intrinsic.referenceTemp
                    << ", \"bias\": ";
                writeFloatArray(field, intrinsic.bias, 3);
                field << ", \"scale_misalignment\": ";
                writeFloatArray(field, intrinsic.scaleMisalignment, 9);
                field << ", \"temperature_slope\": ";
                writeFloatArray(field, intrinsic.tempSlope, 9);
                field << "}}";
                imuFields.push_back(field.str());
            }
            catch(const std::exception&) {
                // Keep RGB-D calibration valid if IMU intrinsic retrieval fails.
            }
        }

        for(const auto& field : {
                extrinsicField("accelerometer_to_color", accelProfile_, colorProfile_),
                extrinsicField("accelerometer_to_depth", accelProfile_, depthProfile_),
                extrinsicField("gyroscope_to_color", gyroProfile_, colorProfile_),
                extrinsicField("gyroscope_to_depth", gyroProfile_, depthProfile_)}) {
            if(!field.empty()) {
                imuFields.push_back(field);
            }
        }
        if(!imuFields.empty()) {
            oss << ", \"imu\": {";
            writeFields(oss, imuFields);
            oss << "}";
        }

        oss << "}";
        const bool valid = cameraParam.rgbIntrinsic.fx > 0.0f && cameraParam.rgbIntrinsic.fy > 0.0f
            && cameraParam.rgbIntrinsic.width > 0 && cameraParam.rgbIntrinsic.height > 0
            && cameraParam.depthIntrinsic.fx > 0.0f && cameraParam.depthIntrinsic.fy > 0.0f
            && cameraParam.depthIntrinsic.width > 0 && cameraParam.depthIntrinsic.height > 0;
        publisher_.publishText(
            StreamKind::Calibration,
            oss.str(),
            1,
            valid ? "RGB-D, IR, and IMU calibration JSON" : "provisional calibration"
        );
        calibrationReady_.store(valid);
        if(valid) {
            std::cout << "[CameraHost] Published valid sensor calibration JSON.\n";
        }
        else {
            std::cout << "[CameraHost] Published provisional calibration; waiting for depth frames before retry.\n";
        }
        return valid;
    }
    catch(const ob::Error& e) {
        const std::string msg = std::string("Calibration unavailable: ") + obErrorText(e);
        publisher_.publishText(StreamKind::Calibration, msg, 1, "calibration error");
        std::cout << "[CameraHost] " << msg << "\n";
        return false;
    }
    catch(const std::exception& e) {
        const std::string msg = std::string("Calibration unavailable: ") + e.what();
        publisher_.publishText(StreamKind::Calibration, msg, 1, "calibration error");
        std::cout << "[CameraHost] " << msg << "\n";
        return false;
    }
}

void OrbbecNativeCamera::publishStatusText(const std::string& status) {
    const auto frame = ++statusFrameCounter_;
    publisher_.publishText(StreamKind::Status, status, frame, "status");
}

void OrbbecNativeCamera::publishFrameSet(const std::shared_ptr<ob::FrameSet>& frameset) {
    if(!frameset || !running_) {
        return;
    }

    for(uint32_t i = 0; i < frameset->frameCount(); ++i) {
        auto frame = frameset->getFrame(static_cast<int>(i));
        if(!frame) {
            continue;
        }
        switch(frame->type()) {
        case OB_FRAME_COLOR:
            publishVideoFrame(frame, StreamKind::Color, "native color frame");
            break;
        case OB_FRAME_DEPTH:
            publishVideoFrame(frame, StreamKind::Depth, "native depth frame");
            break;
        case OB_FRAME_IR:
        case OB_FRAME_IR_LEFT:
        case OB_FRAME_IR_RIGHT:
            publishVideoFrame(frame, StreamKind::Infrared, "native infrared frame");
            break;
        default:
            break;
        }
    }

    std::shared_ptr<ob::Frame> alignedFrame;
    std::shared_ptr<ob::FrameSet> alignedFrameset;
    if(d2cAlignFilter_ && frameset->depthFrame() && frameset->colorFrame()) {
        try {
            alignedFrame = d2cAlignFilter_->process(frameset);
            if(alignedFrame) {
                alignedFrameset = alignedFrame->as<ob::FrameSet>();
                if(alignedFrameset && alignedFrameset->depthFrame()) {
                    publishVideoFrame(
                        alignedFrameset->depthFrame(),
                        StreamKind::AlignedDepth,
                        "software D2C; depth resampled into color coordinates"
                    );
                }
            }
        }
        catch(const ob::Error& e) {
            publishStatusText(std::string("Aligned depth publish failed: ") + obErrorText(e));
        }
        catch(const std::exception& e) {
            publishStatusText(std::string("Aligned depth publish failed: ") + e.what());
        }
    }

    if(pointCloudFilter_) {
        // Native XYZ stays in the depth optical frame. RGB_POINT requires an
        // aligned RGB-D frameset so color samples correspond to depth points.
        std::shared_ptr<ob::Frame> pointInput = frameset;
        std::shared_ptr<ob::FrameSet> pointSource = frameset;
        if(options_.enableRgbPointCloudExperimental && alignedFrame && alignedFrameset) {
            pointInput = alignedFrame;
            pointSource = alignedFrameset;
        }
        publishPointCloud(pointInput, pointSource);
    }
}

void OrbbecNativeCamera::publishPointCloud(
    const std::shared_ptr<ob::Frame>& input,
    const std::shared_ptr<ob::FrameSet>& sourceFrameset
) {
    if(!input || !sourceFrameset || !sourceFrameset->depthFrame() || !pointCloudFilter_) {
        return;
    }

    try {
        if(options_.enableRgbPointCloudExperimental && !sourceFrameset->colorFrame()) {
            return;
        }

        // The frameset carries the active calibration. Apply the depth value
        // scale so XYZ coordinates are emitted in millimeters.
        pointCloudFilter_->setPositionDataScaled(
            sourceFrameset->depthFrame()->getValueScale()
        );
        auto pc = pointCloudFilter_->process(input);
        if(!pc || pc->dataSize() == 0) {
            return;
        }

        PublishMetadata meta{};
        meta.streamKind = StreamKind::PointCloud;
        meta.payloadKind = PayloadKind::PointCloud;
        meta.frameNumber = pc->index();
        meta.deviceTimestampUs = pc->timeStampUs();
        meta.systemTimestampUs = pc->systemTimeStampUs();
        meta.globalTimestampUs = pc->globalTimeStampUs();
        meta.frameType = static_cast<uint32_t>(pc->type());
        meta.format = static_cast<uint32_t>(pc->format());
        meta.formatName = formatName(pc->format());

        auto depthVideo = sourceFrameset->depthFrame()->as<ob::VideoFrame>();
        const uint32_t bytesPerPoint = options_.enableRgbPointCloudExperimental
            ? static_cast<uint32_t>(sizeof(OBColorPoint))
            : static_cast<uint32_t>(sizeof(OBPoint));
        const uint32_t pointCount = bytesPerPoint == 0
            ? 0
            : static_cast<uint32_t>(pc->dataSize() / bytesPerPoint);
        if(depthVideo && depthVideo->width() * depthVideo->height() <= pointCount) {
            meta.width = depthVideo->width();
            meta.height = depthVideo->height();
        }
        else {
            meta.width = pointCount;
            meta.height = 1;
        }
        meta.bytesPerPixel = bytesPerPoint;
        meta.strideBytes = meta.width * bytesPerPoint;
        meta.note = options_.enableRgbPointCloudExperimental
            ? "OBColorPoint[] XYZRGB in color optical coordinates; experimental"
            : "OBPoint[] XYZ millimeters in depth optical coordinates";
        if(meta.globalTimestampUs != 0) {
            meta.flags |= 0x2u;
        }
        if(options_.enableRgbPointCloudExperimental) {
            meta.flags |= 0x8u;
        }
        publisher_.publish(meta, pc->data(), pc->dataSize());
    }
    catch(const ob::Error& e) {
        publishStatusText(std::string("Point cloud publish failed: ") + obErrorText(e));
    }
    catch(const std::exception& e) {
        publishStatusText(std::string("Point cloud publish failed: ") + e.what());
    }
}

void OrbbecNativeCamera::collectFrameMetadata(
    const std::shared_ptr<ob::Frame>& frame,
    PublishMetadata& metadata
) {
    if(!frame) {
        return;
    }
    for(uint32_t index = 0; index < kFrameMetadataTypeCount; ++index) {
        const auto type = static_cast<OBFrameMetadataType>(index);
        try {
            if(frame->hasMetadata(type)) {
                metadata.metadataMask |= (uint64_t{1} << index);
                metadata.metadataValues[index] = frame->getMetadataValue(type);
            }
        }
        catch(const std::exception&) {
            // Metadata availability varies by stream, device, driver, and Windows registration.
        }
    }
}

void OrbbecNativeCamera::publishVideoFrame(const std::shared_ptr<ob::Frame>& frame, StreamKind kind, const std::string& note) {
    if(!frame) {
        return;
    }

    try {
        auto video = frame->as<ob::VideoFrame>();
        PublishMetadata meta{};
        meta.streamKind = kind;
        meta.payloadKind = PayloadKind::RawFrame;
        meta.frameNumber = video->index();
        meta.deviceTimestampUs = video->timeStampUs();
        meta.systemTimestampUs = video->systemTimeStampUs();
        meta.globalTimestampUs = video->globalTimeStampUs();
        meta.frameType = static_cast<uint32_t>(video->type());
        meta.format = static_cast<uint32_t>(video->format());
        meta.formatName = formatName(video->format());
        meta.width = video->width();
        meta.height = video->height();
        meta.bytesPerPixel = bytesPerPixelFromFormat(video->format());
        meta.strideBytes = strideFromFrame(video);
        meta.note = note;
        collectFrameMetadata(frame, meta);
        if(meta.metadataMask != 0) {
            meta.flags |= 0x1u;
        }
        if(meta.globalTimestampUs != 0) {
            meta.flags |= 0x2u;
        }
        if(kind == StreamKind::AlignedDepth) {
            meta.flags |= 0x4u;
        }

        if(kind == StreamKind::Depth || kind == StreamKind::AlignedDepth) {
            try {
                auto depth = frame->as<ob::DepthFrame>();
                meta.depthValueScaleMm = depth->getValueScale();
            }
            catch(...) {
                meta.depthValueScaleMm = 0.0f;
            }
        }

        publisher_.publish(meta, video->data(), video->dataSize());
        if(kind == StreamKind::Depth) {
            const auto depthCount = ++depthFrameCounter_;
            if(!calibrationReady_.load() && (depthCount == 1 || depthCount % 30 == 0)) {
                (void)publishCalibrationText();
            }
        }
    }
    catch(const ob::Error& e) {
        publishStatusText(std::string("Video publish failed: ") + obErrorText(e));
    }
    catch(const std::exception& e) {
        publishStatusText(std::string("Video publish failed: ") + e.what());
    }
}

void OrbbecNativeCamera::publishImuFrame(const std::shared_ptr<ob::Frame>& frame) {
    if(!frame) {
        return;
    }

    try {
        ImuSamplePayload sample{};
        sample.frame_number = frame->index();
        sample.device_timestamp_us = frame->timeStampUs();
        sample.system_timestamp_us = frame->systemTimeStampUs();
        sample.global_timestamp_us = frame->globalTimeStampUs();

        StreamKind kind;
        std::string note;
        if(frame->type() == OB_FRAME_ACCEL) {
            auto accel = frame->as<ob::AccelFrame>();
            const auto value = accel->value();
            sample.x = value.x;
            sample.y = value.y;
            sample.z = value.z;
            sample.temperature_c = accel->temperature();
            kind = StreamKind::Accel;
            note = "m/s^2";
        }
        else if(frame->type() == OB_FRAME_GYRO) {
            auto gyro = frame->as<ob::GyroFrame>();
            const auto value = gyro->value();
            sample.x = value.x;
            sample.y = value.y;
            sample.z = value.z;
            sample.temperature_c = gyro->temperature();
            kind = StreamKind::Gyro;
            note = "rad/s";
        }
        else {
            return;
        }
        sample.stream_kind = static_cast<uint32_t>(kind);

        PublishMetadata meta{};
        meta.streamKind = kind;
        meta.payloadKind = PayloadKind::ImuSample;
        meta.frameNumber = sample.frame_number;
        meta.deviceTimestampUs = sample.device_timestamp_us;
        meta.systemTimestampUs = sample.system_timestamp_us;
        meta.globalTimestampUs = sample.global_timestamp_us;
        meta.frameType = static_cast<uint32_t>(frame->type());
        meta.format = static_cast<uint32_t>(frame->format());
        meta.formatName = formatName(frame->format());
        meta.width = 1;
        meta.height = 1;
        meta.note = note;
        if(meta.globalTimestampUs != 0) {
            meta.flags |= 0x2u;
        }
        publisher_.publish(meta, &sample, sizeof(sample));
    }
    catch(const ob::Error& e) {
        publishStatusText(std::string("IMU publish failed: ") + obErrorText(e));
    }
    catch(const std::exception& e) {
        publishStatusText(std::string("IMU publish failed: ") + e.what());
    }
}

void OrbbecNativeCamera::runUntilEnter() {
    g_stopRequested.store(false);

#ifdef _WIN32
    SetConsoleCtrlHandler(cameraHostConsoleHandler, TRUE);
#else
    std::signal(SIGINT, cameraHostSignalHandler);
    std::signal(SIGTERM, cameraHostSignalHandler);
#endif

    std::cout << "[CameraHost] Running. Press CTRL+C to stop.\n";

    while(running_ && !g_stopRequested.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

#ifdef _WIN32
    SetConsoleCtrlHandler(cameraHostConsoleHandler, FALSE);
#endif
}

void OrbbecNativeCamera::stop() {
    if(!running_) {
        return;
    }
    running_ = false;

    if(imuPipeline_) {
        try {
            imuPipeline_->stop();
        }
        catch(const std::exception& e) {
            std::cout << "[CameraHost] IMU stop warning: " << e.what() << "\n";
        }
        imuPipeline_.reset();
    }

    if(videoPipeline_) {
        try {
            videoPipeline_->stop();
        }
        catch(const std::exception& e) {
            std::cout << "[CameraHost] Video stop warning: " << e.what() << "\n";
        }
        videoPipeline_.reset();
    }

    pointCloudFilter_.reset();
    d2cAlignFilter_.reset();
    publishStatusText("CameraHost stopped");
    std::cout << "[CameraHost] Stopped.\n";
}

} // namespace fbp
