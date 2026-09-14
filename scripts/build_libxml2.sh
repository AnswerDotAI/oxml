#!/usr/bin/env bash
# Wheel-only static dependency, avoiding a runtime install or a newer host-library deployment target.
set -euo pipefail
oxml_build_dir="${1:-target/libxml2}"
mkdir -p "$oxml_build_dir"
oxml_build_dir="$(cd "$oxml_build_dir" && pwd)"
curl --fail --location --max-time 60 https://download.gnome.org/sources/libxml2/2.15/libxml2-2.15.3.tar.xz -o "$oxml_build_dir/source.tar.xz"
tar -xf "$oxml_build_dir/source.tar.xz" -C "$oxml_build_dir"
cmake -S "$oxml_build_dir/libxml2-2.15.3" -B "$oxml_build_dir/build" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$oxml_build_dir/install" -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_SHARED_LIBS=OFF \
    -DLIBXML2_WITH_ICONV=OFF -DLIBXML2_WITH_ZLIB=OFF -DLIBXML2_WITH_TESTS=OFF -DLIBXML2_WITH_PROGRAMS=OFF \
    -DCMAKE_OSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
cmake --build "$oxml_build_dir/build" --parallel 4
cmake --install "$oxml_build_dir/build"
