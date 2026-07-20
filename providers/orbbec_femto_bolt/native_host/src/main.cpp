#include "OrbbecNativeCamera.hpp"
#include "SharedMemoryPublisher.hpp"

#include <iostream>
#include <string>
#include <vector>

#ifdef _WIN32
#include <filesystem>
#include <windows.h>
#endif

namespace {

bool hasArg(int argc, char** argv, const std::string& arg) {
    for(int i = 1; i < argc; ++i) {
        if(argv[i] == arg) {
            return true;
        }
    }
    return false;
}

std::string argValue(int argc, char** argv, const std::string& arg, const std::string& fallback) {
    for(int i = 1; i + 1 < argc; ++i) {
        if(argv[i] == arg) {
            return argv[i + 1];
        }
    }
    return fallback;
}

#ifdef _WIN32
void prepareProcessWorkingDirectory() {
    wchar_t exePathBuffer[MAX_PATH]{};
    const DWORD length = GetModuleFileNameW(nullptr, exePathBuffer, MAX_PATH);
    if(length == 0 || length >= MAX_PATH) {
        return;
    }
    const std::filesystem::path exePath(exePathBuffer);
    const std::filesystem::path exeDir = exePath.parent_path();
    if(exeDir.empty()) {
        return;
    }
    SetCurrentDirectoryW(exeDir.c_str());
    std::error_code ec;
    std::filesystem::create_directories(exeDir / L"Log", ec);
}
#else
void prepareProcessWorkingDirectory() {}
#endif

void printUsage() {
    std::cout << "CameraHost.exe [options]\n\n"
              << "  --mapping-name NAME              Windows named mapping.\n"
              << "  --no-color                       Disable color.\n"
              << "  --no-depth                       Disable native depth.\n"
              << "  --no-ir                          Disable infrared.\n"
              << "  --no-imu                         Disable accelerometer and gyroscope.\n"
              << "  --no-frame-sync                  Disable SDK frame synchronization.\n"
              << "  --no-hardware-d2c                Do not request hardware D2C.\n"
              << "  --no-aligned-depth               Disable software depth-to-color output.\n"
              << "  --no-point-cloud                 Disable point-cloud output.\n"
              << "  --rgb-point-cloud-experimental   Generate OB_FORMAT_RGB_POINT instead of XYZ.\n"
              << "  --help                           Print this text.\n";
}

} // namespace

int main(int argc, char** argv) {
    if(hasArg(argc, argv, "--help")) {
        printUsage();
        return 0;
    }

    try {
        prepareProcessWorkingDirectory();

        fbp::CameraOptions options{};
        options.enableColor = !hasArg(argc, argv, "--no-color");
        options.enableDepth = !hasArg(argc, argv, "--no-depth");
        options.enableIr = !hasArg(argc, argv, "--no-ir");
        options.enableImu = !hasArg(argc, argv, "--no-imu");
        options.enableFrameSync = !hasArg(argc, argv, "--no-frame-sync");
        options.enableHardwareD2CAlignment = !hasArg(argc, argv, "--no-hardware-d2c");
        options.enableSoftwareD2CAlignment = !hasArg(argc, argv, "--no-aligned-depth");
        options.enablePointCloud = !hasArg(argc, argv, "--no-point-cloud");
        options.enableRgbPointCloudExperimental = hasArg(argc, argv, "--rgb-point-cloud-experimental");

        if(!options.enableColor || !options.enableDepth) {
            options.enableSoftwareD2CAlignment = false;
            options.enableRgbPointCloudExperimental = false;
        }
        if(!options.enableDepth) {
            options.enablePointCloud = false;
        }

        const std::string mappingNameUtf8 = argValue(
            argc,
            argv,
            "--mapping-name",
            "Local\\FemtoBoltPipeline_CameraHost_v2"
        );
        const std::wstring mappingName(mappingNameUtf8.begin(), mappingNameUtf8.end());

        std::vector<fbp::StreamAllocation> allocations;
        if(options.enableColor) {
            allocations.push_back({fbp::StreamKind::Color, fbp::PayloadKind::RawFrame, "color", 2, 64ull * 1024 * 1024});
        }
        if(options.enableDepth) {
            allocations.push_back({fbp::StreamKind::Depth, fbp::PayloadKind::RawFrame, "depth", 2, 16ull * 1024 * 1024});
        }
        if(options.enableIr) {
            allocations.push_back({fbp::StreamKind::Infrared, fbp::PayloadKind::RawFrame, "ir", 2, 16ull * 1024 * 1024});
        }
        if(options.enablePointCloud) {
            allocations.push_back({fbp::StreamKind::PointCloud, fbp::PayloadKind::PointCloud, "point_cloud", 2, 32ull * 1024 * 1024});
        }
        if(options.enableImu) {
            allocations.push_back({fbp::StreamKind::Accel, fbp::PayloadKind::ImuSample, "accel", 128, 1024});
            allocations.push_back({fbp::StreamKind::Gyro, fbp::PayloadKind::ImuSample, "gyro", 128, 1024});
        }
        allocations.push_back({fbp::StreamKind::Calibration, fbp::PayloadKind::Utf8Text, "calibration", 2, 64ull * 1024});
        allocations.push_back({fbp::StreamKind::Status, fbp::PayloadKind::Utf8Text, "status", 8, 64ull * 1024});
        if(options.enableSoftwareD2CAlignment) {
            allocations.push_back({fbp::StreamKind::AlignedDepth, fbp::PayloadKind::RawFrame, "depth_aligned_to_color", 2, 16ull * 1024 * 1024});
        }

        fbp::SharedMemoryPublisher publisher;
        publisher.create(mappingName, allocations);
        std::cout << "[CameraHost] Shared memory mapping: " << mappingNameUtf8 << "\n";
        std::cout << "[CameraHost] Shared memory bytes: " << publisher.totalBytes() << "\n";

        fbp::OrbbecNativeCamera camera(publisher);
        camera.start(options);
        camera.runUntilEnter();
        camera.stop();
        return 0;
    }
    catch(const ob::Error& e) {
        std::cerr << "[CameraHost] Orbbec SDK error\n"
                  << "  function: " << e.getName() << "\n"
                  << "  args: " << e.getArgs() << "\n"
                  << "  message: " << e.getMessage() << "\n"
                  << "  type: " << e.getExceptionType() << "\n";
        return 2;
    }
    catch(const std::exception& e) {
        std::cerr << "[CameraHost] Fatal error: " << e.what() << "\n";
        return 1;
    }
}
