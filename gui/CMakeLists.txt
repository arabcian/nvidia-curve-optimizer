cmake_minimum_required(VERSION 3.19)
project(nvcurve-gui VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.4 REQUIRED COMPONENTS Widgets Svg)

# Where the root helpers live. Must match the polkit exec.path annotations.
set(NVCURVE_HELPER_DIR "/usr/local/libexec/nvcurve-gui" CACHE PATH "Installed root helper directory")

add_executable(nvcurve-gui
    src/main.cpp
    src/icon.cpp         src/icon.h
    src/theme.cpp        src/theme.h
    src/privileged.cpp   src/privileged.h
    src/vfcurvewidget.cpp src/vfcurvewidget.h
    src/nvidiatab.cpp    src/nvidiatab.h
    src/trialbar.cpp     src/trialbar.h
)
# Theme glyphs (checkbox tick, spin/combo chevrons), @2x for HiDPI. Compiled
# with qt_add_resources, not AUTORCC: CMake's Makefile generator runs the
# AUTORCC step through /bin/sh without escaping '(' — a checkout under a path
# like "files (1)" failed with a shell syntax error.
qt_add_resources(nvcurve-gui theme
    PREFIX /theme
    BASE data/theme
    FILES
        data/theme/check.png data/theme/check@2x.png
        data/theme/chev-down.png data/theme/chev-down@2x.png
        data/theme/chev-up.png data/theme/chev-up@2x.png
        data/theme/chev-down-off.png data/theme/chev-down-off@2x.png
        data/theme/chev-up-off.png data/theme/chev-up-off@2x.png)
# App icon (window, header mark): the same SVG that is installed to hicolor.
qt_add_resources(nvcurve-gui appicon
    PREFIX /
    BASE data
    FILES data/nvcurve-gui.svg)

target_compile_definitions(nvcurve-gui PRIVATE
    NVCURVE_GUI_VERSION="${PROJECT_VERSION}"
    NVCURVE_HELPER_DIR="${NVCURVE_HELPER_DIR}"
    QT_NO_KEYWORDS)
target_compile_options(nvcurve-gui PRIVATE -Wall -Wextra -Wpedantic)

# ── Optimization / hardening (driven by install.sh; all optional) ────────────
option(NVCURVE_LTO "Link-time optimization" ON)
option(NVCURVE_HARDEN "Stack protector, FORTIFY_SOURCE=3, CET, full RELRO, PIE" ON)

if(NVCURVE_LTO)
    include(CheckIPOSupported)
    check_ipo_supported(RESULT _ipo OUTPUT _ipo_msg LANGUAGES CXX)
    if(_ipo)
        set_property(TARGET nvcurve-gui PROPERTY INTERPROCEDURAL_OPTIMIZATION ON)
    else()
        message(WARNING "LTO not supported by this toolchain: ${_ipo_msg}")
    endif()
endif()
# Smaller binary / fewer relocations: drop unreferenced sections, link only
# libraries actually used, no PLT indirection for Qt calls.
if(NOT CMAKE_BUILD_TYPE STREQUAL "Debug")
    # -O3 explicitly: the value must not depend on CMAKE_CXX_FLAGS_RELEASE, which distro
    # toolchain files and CXXFLAGS in the environment are free to change.
    target_compile_options(nvcurve-gui PRIVATE -O3 -ffunction-sections -fdata-sections -fno-plt)
    target_link_options(nvcurve-gui PRIVATE -Wl,--gc-sections -Wl,--as-needed -Wl,-O1)
endif()
if(NVCURVE_HARDEN)
    # PIE via -pie on the link only. POSITION_INDEPENDENT_CODE put -fPIE on the link line, and with
    # LTO the final codegen followed it: copy relocations of Qt data (staticMetaObject, ...), which
    # Qt6 does not support -- the GUI crashed in the QApplication constructor. The objects are
    # already -fPIC (Qt6::Core adds it), which a PIE executable accepts.
    target_link_options(nvcurve-gui PRIVATE -pie)
    target_compile_options(nvcurve-gui PRIVATE
        -fstack-protector-strong -fstack-clash-protection
        $<$<NOT:$<CONFIG:Debug>>:-U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=3>)
    include(CheckCXXCompilerFlag)
    # libstdc++ bounds checks (vector/string/array operator[], front/back on empty containers):
    # an out-of-range index aborts instead of corrupting memory. Ignored by other C++ libraries.
    target_compile_definitions(nvcurve-gui PRIVATE _GLIBCXX_ASSERTIONS)
    # Zero-initialise automatic variables (GCC 12+, clang 16+); skipped where unsupported.
    check_cxx_compiler_flag(-ftrivial-auto-var-init=zero _autoinit)
    if(_autoinit)
        target_compile_options(nvcurve-gui PRIVATE -ftrivial-auto-var-init=zero)
    endif()
    check_cxx_compiler_flag(-fcf-protection=full _cet)
    if(_cet)
        target_compile_options(nvcurve-gui PRIVATE -fcf-protection=full)
    endif()
    target_link_options(nvcurve-gui PRIVATE -Wl,-z,relro,-z,now -Wl,-z,noexecstack)
endif()
target_link_libraries(nvcurve-gui PRIVATE Qt6::Widgets Qt6::Svg ${CMAKE_DL_LIBS})

include(GNUInstallDirs)
install(TARGETS nvcurve-gui RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
install(FILES data/nvcurve-gui.svg DESTINATION ${CMAKE_INSTALL_DATADIR}/icons/hicolor/scalable/apps)
