"""Run Chrontel's CH7218Upd_macOS (x86_64 slice) under Unicorn, from its entry
point, with only the edges replaced: config and image file reading, console
output, sleeping, and the two libusb control-transfer wrappers, which are
pointed at a SimChip. All flashing logic that runs is the vendor's own code."""
import re
import struct

import lief
from unicorn import Uc, UC_ARCH_X86, UC_MODE_64, UC_HOOK_CODE, UC_PROT_ALL
from unicorn.x86_const import (UC_X86_REG_RAX, UC_X86_REG_RDI, UC_X86_REG_RSI, UC_X86_REG_RDX,
                               UC_X86_REG_RCX, UC_X86_REG_R8, UC_X86_REG_R9, UC_X86_REG_RSP)

TRAP = 0x7000_0000
STACK = 0x6000_0000
HEAP = 0x5000_0000
FAT_X86_64 = 0x01000007


def thin_x86_64(path):
    data = open(path, "rb").read()
    magic, n = struct.unpack(">II", data[:8])
    if magic != 0xCAFEBABE:
        return data
    for i in range(n):
        cpu, _, off, size, _ = struct.unpack(">IIIII", data[8 + 20 * i:28 + 20 * i])
        if cpu == FAT_X86_64:
            return data[off:off + size]
    raise ValueError("no x86_64 slice")


def run_vendor(binary_path, image, sim, image_name="CH7218A-IMG.G607E.09.09.08.IMG", upgflag=1):
    raw = thin_x86_64(binary_path)
    b = lief.MachO.parse(list(raw)).at(0)
    syms = {s.name: s.value for s in b.symbols if s.value}
    uc = Uc(UC_ARCH_X86, UC_MODE_64)
    for seg in b.segments:
        if seg.name == "__PAGEZERO" or seg.virtual_size == 0:
            continue
        size = (seg.virtual_size + 0xFFF) & ~0xFFF
        uc.mem_map(seg.virtual_address, size, UC_PROT_ALL)
        uc.mem_write(seg.virtual_address, bytes(seg.content))
    uc.mem_map(TRAP, 0x10000, UC_PROT_ALL)
    uc.mem_write(TRAP, b"\xc3" * 0x10000)
    uc.mem_map(STACK, 0x100000, UC_PROT_ALL)
    uc.mem_map(HEAP, 0x10000, UC_PROT_ALL)
    uc.mem_write(HEAP, image_name.encode() + b"\0")
    uc.mem_write(HEAP + 0x100, struct.pack("<Q", 0x1122334455667788))   # stack canary

    out = []
    handlers = {}

    def arg(i):
        return uc.reg_read([UC_X86_REG_RDI, UC_X86_REG_RSI, UC_X86_REG_RDX,
                            UC_X86_REG_RCX, UC_X86_REG_R8, UC_X86_REG_R9][i])

    def cstr(addr):
        s = b""
        while True:
            c = bytes(uc.mem_read(addr, 1))
            if c == b"\0":
                return s.decode("latin1")
            s += c
            addr += 1

    def h_printf():
        fmt = cstr(arg(0))
        rsp = uc.reg_read(UC_X86_REG_RSP)
        state = {"n": 1}

        def nxt():
            i = state["n"]
            state["n"] += 1
            if i < 6:
                return arg(i)
            return struct.unpack("<Q", uc.mem_read(rsp + 8 * (i - 5), 8))[0]

        def sub(m):
            spec = m.group(0)
            if spec == "%%":
                return "%"
            v = nxt()
            if spec[-1] == "s":
                return cstr(v)
            py = spec.replace("l", "")
            if spec[-1] == "d":
                v &= 0xFFFFFFFF
                if v & 0x80000000:
                    v -= 1 << 32
            return py % v
        out.append(re.sub(r"%%|%[0-9]*l?[sdXx]", sub, fmt))
        return 0

    def h_memset():
        uc.mem_write(arg(0), bytes([arg(1) & 0xFF]) * arg(2))
        return arg(0)

    def h_memcpy():
        uc.mem_write(arg(0), bytes(uc.mem_read(arg(1), arg(2))))
        return arg(0)

    def h_fail():
        raise RuntimeError("stack check failed in vendor code")

    def h_ini_int():
        return upgflag

    def h_ini_str():
        return HEAP

    def h_read_bin():
        n = min(arg(2) & 0xFFFFFFFF, len(image))
        uc.mem_write(arg(1), bytes(image[:n]))
        return n

    def h_open():
        uc.mem_write(syms["_gCurPage"], b"\x50")
        return 1

    def h_write():
        req, wvalue, windex, buf, ln = arg(0) & 0xFF, arg(1) & 0xFFFF, arg(2) & 0xFFFF, arg(3), arg(4) & 0xFFFF
        assert req == 0xA0 and wvalue == 0, (req, wvalue)
        sim.write(windex, bytes(uc.mem_read(buf, ln)))
        return ln

    def h_read():
        req, wvalue, windex, buf, ln = arg(0) & 0xFF, arg(1) & 0xFFFF, arg(2) & 0xFFFF, arg(3), arg(4) & 0xFFFF
        assert req == 0xB0 and wvalue == 0, (req, wvalue)
        uc.mem_write(buf, sim.read(windex, ln))
        return ln

    def h_delay():
        sim.sleep_ms(arg(0) & 0xFFFFFFFF)
        return 0

    internal = {
        "_GetIniKeyInt": h_ini_int, "_GetIniKeyString": h_ini_str, "_read_bin_file": h_read_bin,
        "_OpenChUsbBB": h_open, "_CloseChUsbBBC": lambda: 0,
        "_CH_USB_VenderRequestWrite": h_write, "_CH_USB_VenderRequestRead": h_read,
        "_delay_ms": h_delay,
    }
    for name, fn in internal.items():
        uc.mem_write(syms[name], b"\xc3")       # function becomes a bare `ret`
        handlers[syms[name]] = fn
    imports = {"_printf": h_printf, "_memset": h_memset, "_memcpy": h_memcpy,
               "___stack_chk_fail": h_fail}
    slot = 0
    for bi in b.bindings:
        if not bi.has_symbol:
            continue
        name = bi.symbol.name
        if name == "___stack_chk_guard":
            uc.mem_write(bi.address, struct.pack("<Q", HEAP + 0x100))
            continue
        slot += 16
        addr = TRAP + slot
        uc.mem_write(bi.address, struct.pack("<Q", addr))

        def unexpected(name=name):
            raise RuntimeError(f"vendor code called unhandled import {name}")
        handlers[addr] = imports.get(name, unexpected)

    def on_code(uc_, address, size, user):
        fn = handlers.get(address)
        if fn is not None:
            uc_.reg_write(UC_X86_REG_RAX, fn() & 0xFFFFFFFFFFFFFFFF)

    for address in handlers:
        uc.hook_add(UC_HOOK_CODE, on_code, begin=address, end=address)

    end = TRAP + 0xFFF0
    rsp = STACK + 0xF0000
    uc.mem_write(rsp, struct.pack("<Q", end))
    uc.reg_write(UC_X86_REG_RSP, rsp)
    uc.emu_start(b.entrypoint, end)
    return "".join(out)
