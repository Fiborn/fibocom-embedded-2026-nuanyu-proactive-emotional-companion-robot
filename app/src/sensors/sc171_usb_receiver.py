#!/usr/bin/env python3
"""Read an ESP32 USB CDC/JTAG serial stream from SC171 via libusb.

This is a userspace fallback for SC171 kernels without cdc_acm/ch343. It
supports ESP32-S3 native USB and the QinHeng bridge used by the older board.
"""

import argparse
import ctypes
import ctypes.util
import json
import struct
import sys
import time


CDC_CONTROL_INTERFACE = 0
CDC_DATA_INTERFACE = 1
USB_PROFILES = (
    # ESP32-S3 USB Serial/JTAG: CDC data interface 1, bulk IN endpoint 0x81.
    (0x303A, 0x1001, 0x81, "ESP32-S3 native USB"),
    # QinHeng CH343 bridge used by the previous ESP32-C3 board.
    (0x1A86, 0x55D3, 0x82, "CH343 USB bridge"),
)

LIBUSB_SUCCESS = 0
LIBUSB_ERROR_TIMEOUT = -7


def check(code: int, operation: str) -> None:
    if code < LIBUSB_SUCCESS:
        raise RuntimeError(f"{operation} failed: libusb error {code}")


def load_libusb():
    path = ctypes.util.find_library("usb-1.0")
    if not path:
        path = "/usr/lib/aarch64-linux-gnu/libusb-1.0.so.0"
    lib = ctypes.CDLL(path)

    context_p = ctypes.c_void_p
    handle_p = ctypes.c_void_p

    lib.libusb_init.argtypes = [ctypes.POINTER(context_p)]
    lib.libusb_init.restype = ctypes.c_int
    lib.libusb_exit.argtypes = [context_p]
    lib.libusb_exit.restype = None
    lib.libusb_open_device_with_vid_pid.argtypes = [
        context_p,
        ctypes.c_uint16,
        ctypes.c_uint16,
    ]
    lib.libusb_open_device_with_vid_pid.restype = handle_p
    lib.libusb_close.argtypes = [handle_p]
    lib.libusb_close.restype = None
    lib.libusb_set_auto_detach_kernel_driver.argtypes = [
        handle_p,
        ctypes.c_int,
    ]
    lib.libusb_set_auto_detach_kernel_driver.restype = ctypes.c_int
    lib.libusb_claim_interface.argtypes = [handle_p, ctypes.c_int]
    lib.libusb_claim_interface.restype = ctypes.c_int
    lib.libusb_release_interface.argtypes = [handle_p, ctypes.c_int]
    lib.libusb_release_interface.restype = ctypes.c_int
    lib.libusb_control_transfer.argtypes = [
        handle_p,
        ctypes.c_uint8,
        ctypes.c_uint8,
        ctypes.c_uint16,
        ctypes.c_uint16,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_uint16,
        ctypes.c_uint,
    ]
    lib.libusb_control_transfer.restype = ctypes.c_int
    lib.libusb_bulk_transfer.argtypes = [
        handle_p,
        ctypes.c_ubyte,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
    ]
    lib.libusb_bulk_transfer.restype = ctypes.c_int
    return lib, context_p()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--baud", type=int, default=115200)
    args = parser.parse_args()

    lib, context = load_libusb()
    check(lib.libusb_init(ctypes.byref(context)), "libusb_init")
    handle = None
    claimed = []
    try:
        selected = None
        for vid, pid, endpoint, label in USB_PROFILES:
            handle = lib.libusb_open_device_with_vid_pid(context, vid, pid)
            if handle:
                selected = (vid, pid, endpoint, label)
                break
        if not handle or not selected:
            expected = ", ".join(
                f"{vid:04x}:{pid:04x}" for vid, pid, _, _ in USB_PROFILES
            )
            raise RuntimeError(f"supported USB device not found ({expected})")
        vid, pid, data_in_endpoint, device_label = selected

        # No kernel driver is expected, but this is harmless if one is added.
        lib.libusb_set_auto_detach_kernel_driver(handle, 1)
        for interface in (CDC_CONTROL_INTERFACE, CDC_DATA_INTERFACE):
            check(
                lib.libusb_claim_interface(handle, interface),
                f"claim interface {interface}",
            )
            claimed.append(interface)

        # CDC SET_LINE_CODING: baud (LE32), 1 stop bit, no parity, 8 data bits.
        coding_bytes = struct.pack("<IBBB", args.baud, 0, 0, 8)
        coding = (ctypes.c_ubyte * len(coding_bytes)).from_buffer_copy(
            coding_bytes
        )
        check(
            lib.libusb_control_transfer(
                handle,
                0x21,
                0x20,
                0,
                CDC_CONTROL_INTERFACE,
                coding,
                len(coding_bytes),
                1000,
            ),
            "CDC SET_LINE_CODING",
        )

        # Assert DTR only. Avoid RTS because some bridges use it for reset.
        empty = ctypes.POINTER(ctypes.c_ubyte)()
        check(
            lib.libusb_control_transfer(
                handle,
                0x21,
                0x22,
                0x0001,
                CDC_CONTROL_INTERFACE,
                empty,
                0,
                1000,
            ),
            "CDC SET_CONTROL_LINE_STATE",
        )

        print(
            f"[SC171-USB] opened {vid:04x}:{pid:04x} "
            f"({device_label}), {args.baud} 8N1, "
            f"endpoint=0x{data_in_endpoint:02X}",
            flush=True,
        )
        deadline = time.monotonic() + args.seconds
        rx = (ctypes.c_ubyte * 4096)()
        transferred = ctypes.c_int()
        buffer = bytearray()
        total = 0
        valid_json = 0

        while time.monotonic() < deadline:
            code = lib.libusb_bulk_transfer(
                handle,
                data_in_endpoint,
                rx,
                len(rx),
                ctypes.byref(transferred),
                250,
            )
            if code == LIBUSB_ERROR_TIMEOUT:
                continue
            check(code, "bulk read")
            if transferred.value <= 0:
                continue

            chunk = bytes(rx[: transferred.value])
            total += len(chunk)
            buffer.extend(chunk)
            while b"\n" in buffer:
                raw_line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                print(f"[RX] {line}", flush=True)
                if not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                required = {
                    "light",
                    "air_ppb",
                    "temperature_c",
                    "humidity_rh",
                    "radar",
                }
                if required.issubset(payload):
                    valid_json += 1
                    radar = payload["radar"]
                    print(
                        "[SC171-USB] SENSOR_DATA_OK "
                        f"light={payload['light']} "
                        f"air_ppb={payload['air_ppb']} "
                        f"temp_c={payload['temperature_c']} "
                        f"humidity_rh={payload['humidity_rh']} "
                        f"radar_online={radar.get('online')} "
                        f"presence={radar.get('presence')} "
                        f"motion={radar.get('motion')} "
                        f"heart_bpm={radar.get('heart_bpm')} "
                        f"breath_bpm={radar.get('breath_bpm')} "
                        f"frames={radar.get('valid_frames')}",
                        flush=True,
                    )

        if buffer:
            print(
                "[RX-PARTIAL] "
                + buffer.decode("utf-8", errors="replace").strip(),
                flush=True,
            )
        print(
            f"[SC171-USB] summary bytes={total} "
            f"valid_sensor_json={valid_json}",
            flush=True,
        )
        return 0 if valid_json else 2
    finally:
        if handle:
            for interface in reversed(claimed):
                lib.libusb_release_interface(handle, interface)
            lib.libusb_close(handle)
        lib.libusb_exit(context)


if __name__ == "__main__":
    sys.exit(main())
