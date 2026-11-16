"""Check that ch7218_flash.py sends the dock exactly the USB traffic Chrontel's
own macOS updater sends, by running both against identical simulated chips.

usage: test_equivalence.py VENDOR_BINARY IMAGE
       (CH7218Upd_macOS and the .IMG from Antank's S3 Max firmware kit)
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import ch7218_flash as port          # noqa: E402
from emu_vendor import run_vendor    # noqa: E402
from sim import SimChip              # noqa: E402


def old_flash(image, seed):
    """A chip holding some other firmware: random body, the image's tail block."""
    rnd = random.Random(seed)
    data = bytearray(rnd.randbytes(0x10000))
    data[0xF000:] = image[0xF000:]
    return data


class SimXfer:
    def __init__(self, sim):
        self.sim = sim

    def write(self, windex, data):
        self.sim.write(windex, data)

    def read(self, windex, length):
        return self.sim.read(windex, length)


def run_port(image, sim):
    chip = port.Chip(SimXfer(sim), sleep=lambda s: sim.sleep_ms(round(s * 1000)))
    chip.running_fw_version()
    if chip.chip_type() != 0:
        return "eeprom"
    chip.prepare(image)
    try:
        return chip.upgrade_firmware(image)
    except port.FlashError as e:
        return f"aborted: {e}"


SCENARIOS = {
    "clean": dict(),
    "write-protected status, slow erase": dict(status_protect=True, busy_reads=5),
    "write enable sometimes lost": dict(wel_fail_every=13),
    "write enable lost during erase": dict(wel_fail_every=7),
    "slow page programs": dict(slow_write_every=5),
    "bad chunks repaired by verify": dict(corrupt={0x0000: 1, 0x1230: 2, 0x9970: 3}),
    "chunk that never verifies": dict(corrupt={0x4560: 99}),
    "everything at once": dict(status_protect=True, wel_fail_every=17, slow_write_every=3,
                               busy_reads=3, corrupt={0x0010: 1, 0x8000: 3, 0x7770: 99}),
}


def main():
    binary, image_path = sys.argv[1], sys.argv[2]
    image = open(image_path, "rb").read()
    fw1_len, _ = port.image_layout(image)
    end = fw1_len + 2
    failures = 0
    for name, opts in SCENARIOS.items():
        start = old_flash(image, seed=len(name))
        sim_v = SimChip(start, **opts)
        sim_p = SimChip(start, **opts)
        console = run_vendor(binary, image, sim_v)
        bad = run_port(image, sim_p)
        vendor_ok = "Status: Success......!" in console
        same_trace = sim_v.trace == sim_p.trace
        same_flash = sim_v.flash == sim_p.flash
        written = bytes(sim_p.flash[:end]) == image[:end]
        kept = bytes(sim_p.flash[0xF000:]) == bytes(start[0xF000:])
        expect_ok = not any(w in name for w in ("never", "everything", "during erase"))
        ok = (same_trace and same_flash and kept and vendor_ok == (bad == 0)
              and vendor_ok == expect_ok and written == expect_ok)
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {len(sim_v.trace)} vendor transfers, "
              f"{len(sim_p.trace)} port transfers, identical={same_trace}, "
              f"flash identical={same_flash}, vendor success={vendor_ok}, port bad bytes={bad}")
        if not same_trace:
            for i, (a, b) in enumerate(zip(sim_v.trace, sim_p.trace)):
                if a != b:
                    print(f"   first difference at #{i}: vendor {a} / port {b}")
                    break
    print("all scenarios identical" if not failures else f"{failures} scenario(s) differ")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
