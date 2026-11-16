"""Deterministic stand-in for a CH7218A with SPI flash, seen through the USB
register interface. It is not a model of the real silicon. Its job is to answer
the updater the same way every time, so the vendor binary (run in an emulator)
and the Python port can be driven through the same paths and their USB traffic
compared."""


class SimChip:
    def __init__(self, flash, *, status_protect=False, wel_fail_every=0, busy_reads=2,
                 slow_write_every=0, corrupt=None):
        self.flash = bytearray(flash)
        assert len(self.flash) == 0x10000
        self.regs = {}
        self.regs_page(0x50)[2:4] = bytes([0x91, 0x33])   # running firmware version
        self.regs_page(3)[7] = 0                           # chip type: flash
        self.srp = 0x80 if status_protect else 0
        self.wel = 0
        self.busy = 0
        self.busy_reads = busy_reads
        self.wel_fail_every = wel_fail_every
        self.slow_write_every = slow_write_every
        self.corrupt = dict(corrupt or {})   # address -> number of programs to corrupt
        self.wren_count = 0
        self.write_count = 0
        self.unlocked = False
        self.trace = []

    def regs_page(self, page):
        return self.regs.setdefault(page, bytearray(256))

    # USB side --------------------------------------------------------------
    def write(self, windex, data):
        data = bytes(data)
        self.trace.append(("W", windex, data))
        page, reg = windex >> 8, windex & 0xFF
        regs = self.regs_page(page)
        for i, b in enumerate(data):
            r = (reg + i) & 0xFF
            regs[r] = b
            if page == 4 and r == 0x17:
                self.unlocked = True
            if page == 5 and r == 0x2B and b in (1, 3, 5):
                self._spi()

    def read(self, windex, length):
        self.trace.append(("R", windex, length))
        page, reg = windex >> 8, windex & 0xFF
        regs = self.regs_page(page)
        return bytes(regs[(reg + i) & 0xFF] for i in range(length))

    def sleep_ms(self, ms):
        self.trace.append(("S", ms))

    # SPI flash -------------------------------------------------------------
    def _spi(self):
        r = self.regs_page(5)
        op = r[0x20]
        addr = (r[0x21] << 16) | (r[0x22] << 8) | r[0x23]
        n = r[0x2C] | (r[0x2D] << 8)
        if op == 0x06:
            self.wren_count += 1
            if not (self.wel_fail_every and self.wren_count % self.wel_fail_every == 0):
                self.wel = 1
        elif op == 0x05:
            st = self.srp | (self.wel << 1) | (1 if self.busy else 0)
            if self.busy:
                self.busy -= 1
            r[0x10] = st
        elif op == 0x01:
            if self.wel:
                self.srp = r[0x30] & 0x80
            self.wel = 0
        elif op == 0x0B:
            for i in range(n):
                r[0x10 + i] = self.flash[(addr + i) & 0xFFFF]
        elif op == 0x02:
            self.write_count += 1
            if self.wel and self.unlocked:
                left = self.corrupt.get(addr, 0)
                if left:
                    self.corrupt[addr] = left - 1     # this program is silently dropped
                else:
                    for i in range(n):
                        self.flash[(addr + i) & 0xFFFF] &= r[0x30 + i]
            self.wel = 0
            slow = self.slow_write_every and self.write_count % self.slow_write_every == 0
            r[0x10] = 1 if slow else 2
        elif op in (0x52, 0x20):
            if self.wel and self.unlocked:
                size = 0x8000 if op == 0x52 else 0x1000
                base = addr & ~(size - 1)
                self.flash[base:base + size] = b"\xff" * size
                self.busy = self.busy_reads
            self.wel = 0
        else:
            raise AssertionError(f"unexpected SPI opcode {op:#x}")
