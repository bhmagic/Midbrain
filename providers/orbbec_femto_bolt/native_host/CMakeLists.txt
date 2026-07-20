cmake_minimum_required(VERSION 3.24)
project(OrbbecCameraHost LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT WIN32)
    message(FATAL_ERROR "This starter CameraHost currently targets Windows shared memory APIs.")
endif()

set(ORBBEC_SDK_ROOT "" CACHE PATH "Orbbec SDK root directory. Example: C:/OrbbecSDK_v2.4.11 or C:/Program Files/Orbbec/OrbbecSDK")
set(ORBBEC_INCLUDE_DIR "" CACHE PATH "Directory containing libobsensor/ObSensor.hpp")
set(ORBBEC_LIBRARY "" CACHE FILEPATH "Orbbec SDK import library, for example OrbbecSDK.lib, obsensor.lib, or libobsensor.lib")
set(ORBBEC_BIN_DIR "" CACHE PATH "Directory containing Orbbec runtime DLLs")

add_executable(CameraHost
    src/main.cpp
    src/SharedMemoryPublisher.cpp
    src/OrbbecNativeCamera.cpp
)

target_include_directories(CameraHost PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/src
)

target_compile_definitions(CameraHost PRIVATE
    NOMINMAX
    WIN32_LEAN_AND_MEAN
    _CRT_SECURE_NO_WARNINGS
)

target_compile_options(CameraHost PRIVATE
    /W4
    /permissive-
)

# Native Orbbec SDK only. This intentionally does not use the Azure Kinect/K4A wrapper.
# Supported discovery modes:
#   1. OrbbecSDKConfig.cmake via -DOrbbecSDK_DIR=...
#   2. Manual SDK root via -DORBBEC_SDK_ROOT=...
#   3. Manual include/lib via -DORBBEC_INCLUDE_DIR=... -DORBBEC_LIBRARY=...
find_package(OrbbecSDK CONFIG QUIET)

set(ORBBEC_TARGET_CREATED FALSE)

if(OrbbecSDK_FOUND)
    if(TARGET OrbbecSDK::OrbbecSDK)
        target_link_libraries(CameraHost PRIVATE OrbbecSDK::OrbbecSDK)
        set(ORBBEC_TARGET_CREATED TRUE)
        message(STATUS "Using OrbbecSDK package target: OrbbecSDK::OrbbecSDK")
    elseif(TARGET OrbbecSDK)
        target_link_libraries(CameraHost PRIVATE OrbbecSDK)
        set(ORBBEC_TARGET_CREATED TRUE)
        message(STATUS "Using OrbbecSDK package target: OrbbecSDK")
    elseif(DEFINED OrbbecSDK_LIBRARIES OR DEFINED OrbbecSDK_INCLUDE_DIRS)
        target_include_directories(CameraHost PRIVATE ${OrbbecSDK_INCLUDE_DIRS})
        target_link_libraries(CameraHost PRIVATE ${OrbbecSDK_LIBRARIES})
        set(ORBBEC_TARGET_CREATED TRUE)
        message(STATUS "Using OrbbecSDK package variables")
    endif()
endif()

if(NOT ORBBEC_TARGET_CREATED)
    file(GLOB _orbbec_program_files_roots
        "C:/Program Files/OrbbecSDK*"
        "C:/Program Files/Orbbec/OrbbecSDK*"
    )

    set(_orbbec_roots
        ${ORBBEC_SDK_ROOT}
        $ENV{ORBBEC_SDK_ROOT}
        $ENV{ORBBECSDK_ROOT}
        $ENV{ORBBEC_HOME}
        $ENV{OrbbecSDK_ROOT}
        $ENV{OrbbecSDK_DIR}
        ${_orbbec_program_files_roots}
        "C:/Program Files/Orbbec/OrbbecSDK"
        "C:/Program Files/OrbbecSDK 2.8.6"
        "C:/Program Files/OrbbecSDK"
        "C:/OrbbecSDK"
        "C:/OrbbecSDK_v2"
    )

    if(NOT ORBBEC_INCLUDE_DIR)
        find_path(ORBBEC_INCLUDE_DIR
            NAMES libobsensor/ObSensor.hpp
            HINTS ${_orbbec_roots}
            PATH_SUFFIXES include SDK/include sdk/include OrbbecSDK/include
        )
    endif()

    if(NOT ORBBEC_LIBRARY)
        find_library(ORBBEC_LIBRARY
            NAMES OrbbecSDK obsensor libobsensor
            HINTS ${_orbbec_roots}
            PATH_SUFFIXES lib SDK/lib sdk/lib lib/x64 SDK/lib/x64 build/lib build/output/lib
        )
    endif()

    if(NOT ORBBEC_INCLUDE_DIR OR NOT ORBBEC_LIBRARY)
        message(FATAL_ERROR
            "Could not find native Orbbec SDK.\n"
            "Install Orbbec SDK v2 for Windows or point CMake at an extracted SDK.\n"
            "Try one of these from Developer PowerShell:\n"
            "  .\\scripts\\find_orbbec_sdk.ps1\n"
            "  .\\scripts\\build_release.ps1 -ProjectRoot \"${CMAKE_CURRENT_SOURCE_DIR}\" -OrbbecSdkRoot \"C:\\Path\\To\\OrbbecSDK\"\n"
            "  .\\scripts\\build_release.ps1 -ProjectRoot \"${CMAKE_CURRENT_SOURCE_DIR}\" -OrbbecIncludeDir \"C:\\Path\\To\\include\" -OrbbecLibrary \"C:\\Path\\To\\lib\\OrbbecSDK.lib\"\n"
            "Current ORBBEC_INCLUDE_DIR='${ORBBEC_INCLUDE_DIR}' ORBBEC_LIBRARY='${ORBBEC_LIBRARY}'")
    endif()

    target_include_directories(CameraHost PRIVATE ${ORBBEC_INCLUDE_DIR})
    target_link_libraries(CameraHost PRIVATE ${ORBBEC_LIBRARY})
    message(STATUS "Using Orbbec include directory: ${ORBBEC_INCLUDE_DIR}")
    message(STATUS "Using Orbbec import library: ${ORBBEC_LIBRARY}")
endif()

if(NOT ORBBEC_BIN_DIR)
    get_filename_component(_orbbec_include_root "${ORBBEC_INCLUDE_DIR}/.." ABSOLUTE)
    get_filename_component(_orbbec_library_dir "${ORBBEC_LIBRARY}" DIRECTORY)

    set(_orbbec_dll_roots
        ${ORBBEC_SDK_ROOT}
        ${_orbbec_include_root}
        ${_orbbec_library_dir}
        $ENV{ORBBEC_SDK_ROOT}
        $ENV{ORBBECSDK_ROOT}
        $ENV{ORBBEC_HOME}
        $ENV{OrbbecSDK_ROOT}
        $ENV{OrbbecSDK_DIR}
        ${_orbbec_program_files_roots}
        "C:/Program Files/Orbbec/OrbbecSDK"
        "C:/Program Files/OrbbecSDK 2.8.6"
        "C:/Program Files/OrbbecSDK"
        "C:/OrbbecSDK"
        "C:/OrbbecSDK_v2"
    )

    find_file(ORBBEC_RUNTIME_DLL
        NAMES OrbbecSDK.dll obsensor.dll libobsensor.dll
        HINTS ${_orbbec_dll_roots}
        PATH_SUFFIXES bin SDK/bin sdk/bin lib SDK/lib sdk/lib
    )
else()
    find_file(ORBBEC_RUNTIME_DLL
        NAMES OrbbecSDK.dll obsensor.dll libobsensor.dll
        HINTS ${ORBBEC_BIN_DIR}
    )
endif()

if(ORBBEC_RUNTIME_DLL)
    add_custom_command(TARGET CameraHost POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${ORBBEC_RUNTIME_DLL}"
        "$<TARGET_FILE_DIR:CameraHost>"
        COMMENT "Copying Orbbec runtime DLL next to CameraHost.exe")
    message(STATUS "Using Orbbec runtime DLL: ${ORBBEC_RUNTIME_DLL}")
else()
    message(WARNING "Could not auto-locate Orbbec runtime DLL. If CameraHost.exe starts with a missing-DLL error, copy OrbbecSDK.dll/obsensor.dll/libobsensor.dll from the SDK bin folder to build/Release.")
endif()


# OrbbecSDK.dll loads device-specific runtime extensions relative to the executable,
# for example extensions/frameprocessor/ob_frame_processor.dll and
# extensions/depthengine/depthengine.dll. Copy the whole SDK extensions directory
# beside CameraHost.exe so depth/IR and frame processing components can load.
set(ORBBEC_EXTENSIONS_DIR "" CACHE PATH "Directory containing Orbbec SDK runtime extensions")

if(NOT ORBBEC_EXTENSIONS_DIR)
    set(_orbbec_extension_roots
        ${ORBBEC_BIN_DIR}
        ${ORBBEC_SDK_ROOT}
        ${_orbbec_include_root}
        ${_orbbec_library_dir}
        ${_orbbec_program_files_roots}
        "C:/Program Files/OrbbecSDK 2.8.6"
        "C:/Program Files/OrbbecSDK"
        "C:/Program Files/Orbbec/OrbbecSDK"
    )

    find_path(ORBBEC_EXTENSIONS_DIR
        NAMES frameprocessor/ob_frame_processor.dll depthengine/depthengine.dll
        HINTS ${_orbbec_extension_roots}
        PATH_SUFFIXES extensions bin/extensions SDK/bin/extensions sdk/bin/extensions
    )
endif()

if(ORBBEC_EXTENSIONS_DIR)
    add_custom_command(TARGET CameraHost POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E make_directory "$<TARGET_FILE_DIR:CameraHost>/extensions"
        COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${ORBBEC_EXTENSIONS_DIR}"
        "$<TARGET_FILE_DIR:CameraHost>/extensions"
        COMMENT "Copying Orbbec runtime extensions next to CameraHost.exe")
    message(STATUS "Using Orbbec runtime extensions: ${ORBBEC_EXTENSIONS_DIR}")
else()
    message(WARNING "Could not auto-locate Orbbec SDK extensions. If depth/IR fail with missing extensions/depthengine or extensions/frameprocessor DLLs, copy the SDK bin/extensions folder to build/Release/extensions.")
endif()

install(TARGETS CameraHost RUNTIME DESTINATION bin)
install(FILES include/FemtoBoltPipeline/FemtoSharedMemoryLayout.hpp DESTINATION include/FemtoBoltPipeline)
