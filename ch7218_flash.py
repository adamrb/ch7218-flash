#!/usr/bin/env python3
"""Firmware updater for Chrontel CH7218A USB-C docks (Antank S3 Max / S3 Air / S3 Pro)
that runs on Linux and macOS.

It is a port of Chrontel's "CH7218A TypeC Upgrade Tool V2.2 for macOS" (the
CH7218Upd_macOS binary in Antank's firmware kit). The chip shows up as a USB
"Billboard" device (0639:7218) and is driven entirely with vendor control
transfers on endpoint 0: request 0xA0 writes and 0xB0 reads a chip register at
wIndex = (page << 8) | register. The SPI flash behind the chip is reached
through the page 5 registers.

Only the path the vendor kit uses is implemented: SPI-flash chips ("Chip Type
is FLASH"), UPGFLAG = 1 (firmware only, customer settings kept).

Commands:
  info   [IMG]       show the dock's running firmware and, if given, the image's
  backup OUT.bin     read the whole 64 KiB flash to a file
  flash  IMG         back up, erase, program and verify
  verify IMG         compare the firmware region in the chip against IMG

Needs pyusb and libusb 1.0. See README.md. On Linux run it as root.
"""
import argparse
import os
import struct
import sys
import time

DEFAULT_VID = 0x0639
DEFAULT_PID = 0x7218
IMG_SIZE = 0x10000
CHUNK = 0x10

REQ_WRITE = 0xA0
REQ_READ = 0xB0
DEFAULT_CHIP_KEY = bytes.fromhex("5d5c5b5a00000000")   # the vendor tool's fallback ID
ERASE_END = 0xF000                                     # an upgrade erases 0x0000-0xEFFF


class FlashError(Exception):
    pass


# ---------------------------------------------------------------- transport

class UsbTransport:
    """Vendor control transfers to the Billboard device."""

    def __init__(self, dev, timeout_ms=1000):
        self.dev = dev
        self.timeout_ms = timeout_ms

    def _retry(self, fn):
        import usb.core
        last = None
        for attempt in range(3):
            try:
                return fn()
            except usb.core.USBError as e:
                last = e
                time.sleep(0.05 * (attempt + 1))
        hint = ""
        if getattr(last, "errno", None) == 13:
            hint = "\nNo permission to use the USB device. On Linux run the tool with sudo."
        raise FlashError(f"USB transfer failed three times: {last}{hint}")

    def write(self, windex, data):
        def go():
            n = self.dev.ctrl_transfer(0x40, REQ_WRITE, 0, windex, bytes(data), self.timeout_ms)
            if n != len(data):
                import usb.core
                raise usb.core.USBError(f"short write {n}/{len(data)}")
        self._retry(go)

    def read(self, windex, length):
        def go():
            buf = bytes(self.dev.ctrl_transfer(0xC0, REQ_READ, 0, windex, length, self.timeout_ms))
            if len(buf) != length:
                import usb.core
                raise usb.core.USBError(f"short read {len(buf)}/{length}")
            return buf
        return self._retry(go)


def open_usb(ids):
    try:
        import usb.core
    except ImportError:
        raise FlashError("pyusb is not installed. See README.md, or use the ./flash wrapper (needs uv).")
    try:
        for vid, pid in ids:
            devs = list(usb.core.find(find_all=True, idVendor=vid, idProduct=pid))
            if len(devs) > 1:
                raise FlashError(f"{len(devs)} devices with ID {vid:04x}:{pid:04x} are connected. "
                                 "Leave only the dock you want to update plugged in.")
            if devs:
                return UsbTransport(devs[0]), (vid, pid)
    except usb.core.NoBackendError:
        raise FlashError("libusb 1.0 not found (Linux: apt install libusb-1.0-0, macOS: brew install libusb)")
    want = ", ".join(f"{v:04x}:{p:04x}" for v, p in ids)
    raise FlashError(
        f"No USB Billboard device found (looked for {want}).\n"
        "Connect the dock's USB-C PD (charging) port to this computer with a\n"
        "USB-A to USB-C data cable, then check `lsusb` (Linux) or System\n"
        "Information > USB (Mac) for a Chrontel 'Billboard' device.")


# --------------------------------------------------------------------- chip

class Chip:
    """Register access plus the SPI-flash command sequences of the vendor tool
    (its gDriveType == 2 / gChiptype == 0 code paths)."""

    def __init__(self, xfer, sleep=time.sleep, wait_timeout=60.0):
        self.xfer = xfer
        self.sleep = sleep
        self.wait_timeout = wait_timeout
        self.page = 0x50

    # register primitives -------------------------------------------------
    def set_page(self, page):
        # The page is not a transfer of its own, it is the high byte of wIndex.
        self.page = page & 0xFF

    def wr(self, reg, data):
        data = bytes(data)
        idx = (self.page << 8) | reg
        while data:
            part = data[:0x40]
            self.xfer.write(idx & 0xFFFF, part)
            idx += len(part)
            data = data[len(part):]

    def wr_byte(self, reg, value):
        self.wr(reg, bytes([value & 0xFF]))

    def rd(self, reg, length):
        idx = (self.page << 8) | reg
        out = b""
        while len(out) < length:
            n = min(0x40, length - len(out))
            out += self.xfer.read(idx & 0xFFFF, n)
            idx += n
        return out

    def delay_ms(self, ms):
        self.sleep(ms / 1000.0)

    # identification ------------------------------------------------------
    def running_fw_version(self):
        self.set_page(0x50)
        b = self.rd(2, 2)
        return (b[0] << 8) | b[1]

    def chip_type(self):
        """0 = SPI flash, 1 = EEPROM."""
        self.set_page(3)
        return self.rd(7, 1)[0] & 1

    # SPI flash orders ----------------------------------------------------
    def flash_wren(self):
        self.set_page(5)
        self.wr_byte(0x20, 0x06)
        self.wr_byte(0x2A, 0x10)
        self.wr_byte(0x2B, 1)
        self.wr_byte(0x2B, 0)

    def flash_read_status(self):
        self.set_page(5)
        self.wr_byte(0x20, 0x05)
        self.wr_byte(0x2A, 0x10)
        self.wr(0x2C, b"\x01\x00")
        self.wr_byte(0x2B, 5)
        self.wr_byte(0x2B, 4)
        self.set_page(5)
        return self.rd(0x10, 1)[0]

    def flash_write_status_zero(self):
        self.set_page(5)
        self.wr_byte(0x20, 0x01)
        self.wr_byte(0x2A, 0x10)
        self.wr_byte(0x2B, 3)
        self.wr(0x2C, b"\x01\x00")
        self.wr_byte(0x30, 0)
        self.wr_byte(0x2B, 2)

    def _erase_order(self, opcode, addr):
        self.wr(0x20, bytes([opcode, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF]))
        self.wr_byte(0x2A, 0x40)
        self.wr_byte(0x2B, 1)
        self.wr_byte(0x2B, 0)

    def _wait_not_busy(self):
        deadline = time.monotonic() + self.wait_timeout
        while self.flash_read_status() & 1:
            if time.monotonic() > deadline:
                raise FlashError("flash stayed busy during erase")

    def flash_erase_60k(self):
        """Erase 0x0000-0xEFFF (one 32K block and seven 4K sectors).
        0xF000-0xFFFF, which holds the customer settings and trim, is kept."""
        self.flash_wren()
        if self.flash_read_status() & 2 != 2:
            return False
        self._erase_order(0x52, 0)
        self._wait_not_busy()
        for i in range(7):
            self.flash_wren()
            if self.flash_read_status() & 2 != 2:
                return False
            self._erase_order(0x20, 0x8000 + i * 0x1000)
            self._wait_not_busy()
        return True

    def flash_read_chunk(self, addr, length):
        self.set_page(5)
        self.wr(0x20, bytes([0x0B, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF, 0]))
        self.wr_byte(0x2A, 0x50)
        self.wr(0x2C, b"\x10\x00")
        self.wr_byte(0x2B, 5)
        self.wr_byte(0x2B, 4)
        self.rd(0x10, length)
        return self.rd(0x10, length)  # the vendor tool reads twice and keeps the second

    def flash_write_chunk(self, addr, data):
        self.set_page(5)
        self.wr(0x20, bytes([0x02, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF]))
        self.wr_byte(0x2A, 0x40)
        self.wr(0x2C, b"\x10\x00")
        self.wr(0x30, data)
        self.wr_byte(0x2B, 3)
        self.wr_byte(0x2B, 2)
        deadline = time.monotonic() + self.wait_timeout
        while True:
            self.set_page(5)
            st = self.rd(0x10, 1)[0]
            if st & 3:
                break
            if time.monotonic() > deadline:
                raise FlashError(f"flash write at {addr:#06x} never completed")
        if st == 1:
            self.delay_ms(5)

    # multi-chunk helpers ---------------------------------------------------
    def read(self, addr, length, progress=None):
        out = bytearray()
        for off in range(0, length, CHUNK):
            if progress:
                progress("Read", off, length)
            out += self.flash_read_chunk(addr + off, min(CHUNK, length - off))
        if progress:
            progress("Read", length, length, done=True)
        return bytes(out)

    def _wren_checked(self):
        self.flash_wren()
        if self.flash_read_status() & 2 != 2:
            self.flash_wren()

    def program(self, addr, data, progress=None):
        for off in range(0, len(data), CHUNK):
            if progress:
                progress("Program", off, len(data))
            self._wren_checked()
            self.flash_write_chunk(addr + off, data[off:off + CHUNK])
        if progress:
            progress("Program", len(data), len(data), done=True)

    def verify(self, addr, data, progress=None, repair=True):
        """Returns the number of bytes that still differ. With repair, a
        differing chunk is rewritten up to three times, as the vendor tool does."""
        bad = 0
        tries = 0
        off = 0
        while off < len(data):
            if progress:
                progress("Verify", off, len(data))
            want = data[off:off + CHUNK]
            diff = _count_diff(self.flash_read_chunk(addr + off, len(want)), want)
            if diff:
                diff = _count_diff(self.flash_read_chunk(addr + off, len(want)), want)
            if diff and repair:
                tries += 1
                if tries <= 3:
                    self._wren_checked()
                    self.flash_write_chunk(addr + off, want)
                    self.delay_ms(10)
                    continue
            tries = 0
            off += CHUNK
            bad += diff
        if progress:
            progress("Verify", len(data), len(data), done=True)
        return bad

    # the upgrade -----------------------------------------------------------
    def unlock(self, img=None):
        """Send the chip ID that opens flash access. Uses the image's ID, or the
        vendor tool's built-in default when there is no image. Changes nothing
        in the flash."""
        if img is None:
            key = DEFAULT_CHIP_KEY
        else:
            chip_id = decode_chip_id(struct.unpack_from("<I", img, 0xFFEA)[0])
            key = struct.pack("<I", chip_id) + bytes(img[0xFFEE:0xFFF2])
        self.set_page(4)
        self.wr(0x10, key)

    def effective_image(self, img):
        """The bytes an upgrade actually leaves in 0..fw1_len+2. Differs from the
        file only when the firmware overlaps the settings block, which is then
        taken from the chip and the checksum redone."""
        img = bytearray(img)
        fw1_len, blk = image_layout(img)
        svid_start = blk - 0x100
        if svid_start < fw1_len + 2:
            img[svid_start:svid_start + 0x100] = self.read(svid_start, 0x100)
            struct.pack_into("<H", img, fw1_len, sum(img[2:fw1_len]) & 0xFFFF)
        return bytes(img[:fw1_len + 2])

    def prepare(self, img):
        """Unlock flash access with the image's chip ID, clear write protection,
        and return the chip's 32-byte info block (flash 0xFFE0)."""
        self.unlock(img)
        self.set_page(5)
        if self.flash_read_status() & 0x80:
            self.flash_wren()
            self.flash_write_status_zero()
            self.delay_ms(50)
            self.flash_read_status()
        return self.read(0xFFE0, 0x20)

    def upgrade_firmware(self, img, progress=None):
        """UPGFLAG = 1: replace firmware, keep customer settings.
        Call prepare() first. Returns the number of bytes that failed verification."""
        data = self.effective_image(img)
        if not self.flash_erase_60k():
            raise FlashError("flash refused write enable during erase")
        self.program(0, data, progress)
        return self.verify(0, data, progress)


# ------------------------------------------------------------------- image

def _count_diff(a, b):
    return sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))


def decode_chip_id(v):
    x = ((v & 0xF0000000) | ((v & 0xF00) << 16) | ((v & 0xF) << 20) | ((v >> 4) & 0xF0000)
         | (v & 0xF000) | ((v >> 16) & 0xF00) | ((v >> 12) & 0xF0) | ((v >> 4) & 0xF))
    return ((((v >> 4) << 29) & 0xFFFFFFFF) | (x >> 3)) ^ 0xFFFFFFFF


def image_layout(img):
    if len(img) != IMG_SIZE:
        raise FlashError(f"image is {len(img)} bytes, expected {IMG_SIZE}")
    fw1_len = struct.unpack_from("<H", img, 0)[0]
    blk = struct.unpack_from("<H", img, 0xFFFA)[0]
    if fw1_len < 0x400 or blk < 0xF300:
        raise FlashError("not a CH7218 firmware image (bad layout fields)")
    if fw1_len + 2 > ERASE_END:
        raise FlashError("firmware in this image is larger than the region an upgrade erases")
    return fw1_len, blk


def fmt_version(v):
    return f"{(v >> 12) & 0xF:02d}.{(v >> 8) & 0xF:02d}.{v & 0xFF:02d}"


def describe_image(img):
    fw1_len, blk = image_layout(img)
    ver = (img[fw1_len - 1] << 8) | img[fw1_len - 2]
    stored = struct.unpack_from("<H", img, fw1_len)[0]
    calc = sum(img[2:fw1_len]) & 0xFFFF
    vid = struct.unpack_from("<H", img, blk - 0x6E)[0]
    pid = struct.unpack_from("<H", img, blk - 0x6C)[0]
    return {"fw1_len": fw1_len, "blk": blk, "version": ver, "vid": vid, "pid": pid,
            "checksum_ok": stored == calc}


def load_image(path):
    with open(path, "rb") as f:
        img = f.read()
    info = describe_image(img)
    if not info["checksum_ok"]:
        raise FlashError(f"{path}: firmware checksum does not match, refusing to use it")
    return img, info


# --------------------------------------------------------------------- CLI

def progress_printer(label, pos, total, done=False):
    end = "  done\n" if done else ""
    sys.stdout.write(f"\r{label + '...':<11}{pos:04X}/{total:04X}{end}")
    sys.stdout.flush()


def connect(info=None):
    ids = []
    if info and info["vid"]:
        ids.append((info["vid"], info["pid"]))
    if (DEFAULT_VID, DEFAULT_PID) not in ids:
        ids.append((DEFAULT_VID, DEFAULT_PID))
    xfer, found = open_usb(ids)
    chip = Chip(xfer)
    print(f"Billboard device {found[0]:04x}:{found[1]:04x}")
    running = chip.running_fw_version()
    print(f"Running firmware V{fmt_version(running)}")
    ctype = chip.chip_type()
    print("Chip type is " + ("EEPROM" if ctype else "FLASH"))
    return chip, running, ctype


def require_flash(ctype):
    if ctype != 0:
        raise FlashError("this dock stores firmware in an EEPROM, which this tool does not support")


def save_new(path, data):
    """Write a file, refusing to replace one that exists (it may be the only
    copy of a dock's original firmware)."""
    try:
        with open(path, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        raise FlashError(f"{path} already exists, choose another backup file name")


def cmd_info(args):
    info = None
    if args.image:
        _, info = load_image(args.image)
        print(f"Image firmware   V{fmt_version(info['version'])}  ({args.image})")
    connect(info)


def cmd_backup(args):
    chip, _, ctype = connect()
    require_flash(ctype)
    chip.unlock()
    save_new(args.out, chip.read(0, IMG_SIZE, progress_printer))
    print(f"Saved the flash contents to {args.out}")


def cmd_verify(args):
    img, info = load_image(args.image)
    print(f"Image firmware   V{fmt_version(info['version'])}")
    chip, _, ctype = connect(info)
    require_flash(ctype)
    chip.unlock(img)
    bad = chip.verify(0, chip.effective_image(img), progress_printer, repair=False)
    print("Chip matches the image." if bad == 0 else f"{bad} bytes differ.")
    return 0 if bad == 0 else 1


RECOVERY = ("The dock's firmware may now be partly erased. Do not unplug it or cut its power:\n"
            "it is not known whether it can be reached again after a restart in this state.\n"
            "While it is still connected, run the same flash command again with --no-backup\n"
            "(the backup taken before the first attempt is the good one). To go back to the\n"
            "old firmware instead, flash that backup file the same way.")


def cmd_flash(args):
    img, info = load_image(args.image)
    print(f"Image firmware   V{fmt_version(info['version'])}  ({args.image})")
    chip, running, ctype = connect(info)
    require_flash(ctype)
    if not args.yes:
        print("\nThis erases and rewrites the dock's firmware. Keep the cable still until it finishes.")
        if input("Type FLASH to continue: ").strip() != "FLASH":
            print("Cancelled, nothing was written.")
            return 1
    chip_inf = chip.prepare(img)
    if chip_inf[:0x1E] != img[0xFFE0:0xFFFE]:
        print("Note: the chip's info block differs from the image's (the vendor tool ignores this too).")
    if not args.no_backup:
        out = args.backup or time.strftime("ch7218-backup-%Y%m%d-%H%M%S.bin")
        data = chip.read(0, IMG_SIZE, progress_printer)
        # The backup doubles as the last gate before erasing: if what was read
        # back is not a well-formed firmware image with a good checksum, then
        # either flash reads do not work on this dock or its firmware is already
        # damaged, and in both cases nothing should be erased on autopilot.
        try:
            binfo = describe_image(data)
            good = binfo["checksum_ok"]
        except FlashError:
            good = False
        if not good:
            save_new(out + ".unverified", data)
            raise FlashError(
                "what was read back from the dock is not a valid firmware image, so reading\n"
                f"its flash may not work with this tool. Nothing was erased. Raw read saved to {out}.unverified.\n"
                "Only if you know the dock's firmware is already broken: rerun with --no-backup.")
        save_new(out, data)
        print(f"Backup saved to {out} (firmware V{fmt_version(binfo['version'])}, checksum good)")
    # From here on the firmware gets erased. A failure is retried in place while the
    # dock is still connected, since nobody knows whether it would come back after
    # a power cycle with a half-written firmware.
    why = ""
    for attempt in range(1, 4):
        try:
            bad = chip.upgrade_firmware(img, progress_printer)
        except KeyboardInterrupt:
            print(f"\n\nFLASH INCOMPLETE (interrupted).\n{RECOVERY}", file=sys.stderr)
            return 3
        except FlashError as e:
            why = str(e)
        else:
            if bad == 0:
                break
            why = f"{bad} bytes did not verify"
        print(f"\nAttempt {attempt} failed ({why}).", file=sys.stderr)
        if attempt < 3:
            print("Trying again from the erase step.", file=sys.stderr)
            time.sleep(1)
    else:
        print(f"\nFLASH INCOMPLETE ({why}).\n{RECOVERY}", file=sys.stderr)
        return 3
    print("Status: Success. Unplug the dock and plug it back in to start the new firmware.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="CH7218A dock firmware updater (Antank S3 series) for Linux and macOS")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info", help="show running firmware (and image version)")
    s.add_argument("image", nargs="?")
    s.set_defaults(fn=cmd_info)
    s = sub.add_parser("backup", help="read the whole flash to a file")
    s.add_argument("out")
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("verify", help="compare the chip's firmware with an image")
    s.add_argument("image")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("flash", help="back up, erase, program, verify")
    s.add_argument("image")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.add_argument("--backup", help="where to save the pre-flash backup")
    s.add_argument("--no-backup", action="store_true")
    s.set_defaults(fn=cmd_flash)
    args = p.parse_args(argv)
    try:
        return args.fn(args) or 0
    except FlashError as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
