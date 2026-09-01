#!/usr/bin/env bash
set -euo pipefail

IMAGE_FINGERPRINT="${BLEHUB_IMAGE_FINGERPRINT:?missing BLEHUB_IMAGE_FINGERPRINT}"
PIXI_VERSION="${BLEHUB_PIXI_VERSION:?missing BLEHUB_PIXI_VERSION}"
PIXI_SHA256="${BLEHUB_PIXI_SHA256:?missing BLEHUB_PIXI_SHA256}"
PIXI_ROOT="/opt/blehub/pixi"
PIXI_ARCHIVE="/tmp/blehub-provision/pixi.tar.gz"
PIXI_BIN="${PIXI_ROOT}/bin/pixi"
FIXTURE_ROOT="/opt/blehub/fixture"

install -d -m 0755 \
    "${PIXI_ROOT}/bin" \
    "${FIXTURE_ROOT}/environments" \
    "${FIXTURE_ROOT}/releases" \
    /var/lib/blehub
export DEBIAN_FRONTEND=noninteractive
cat > /etc/apt/sources.list.d/blehub-non-free-firmware.list <<EOF
deb https://deb.debian.org/debian trixie non-free-firmware
EOF
apt-get update
apt-get install --yes --no-install-recommends \
    bluez \
    ca-certificates \
    curl \
    dbus \
    firmware-realtek \
    openssh-server \
    rfkill \
    usbutils \
    xz-utils

if [[ ! -x "${PIXI_BIN}" ]]; then
    echo "${PIXI_SHA256}  ${PIXI_ARCHIVE}" | sha256sum --check --strict
    tar -xzf "${PIXI_ARCHIVE}" -C "${PIXI_ROOT}/bin" pixi
fi

actual_version="$(${PIXI_BIN} --version)"
if [[ "${actual_version}" != "pixi ${PIXI_VERSION}" ]]; then
    echo "unexpected Pixi version: ${actual_version}" >&2
    exit 1
fi

install -m 0644 /opt/blehub/provision/blehub-peripheral.service /etc/systemd/system/blehub-peripheral.service
systemctl daemon-reload
systemctl enable bluetooth.service
systemctl restart bluetooth.service

cat > /var/lib/blehub/provisioned.json <<EOF
{"schema_version":1,"image_fingerprint":"${IMAGE_FINGERPRINT}","pixi_version":"${PIXI_VERSION}"}
EOF
