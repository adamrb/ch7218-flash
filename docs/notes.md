# Notes on the CH7218A update protocol and firmware

Everything here was worked out from Antank's public firmware kit (the macOS updater ships with debug symbols) and from one S3 Max. Nothing comes from Chrontel documentation. Where something is a guess, it says so.

## USB side

With only the PD port connected to a host, the dock enumerates as a USB 2.0 full-speed Billboard device:

```
idVendor 0x0639 (Chrontel)   idProduct 0x7218
Manufacturer "Chrontel"   Product "Billboard"   Serial "CH7218"
```

The updater uses nothing but vendor control transfers on endpoint 0. No interface is claimed.

| | bmRequestType | bRequest | wValue | wIndex | data |
|---|---|---|---|---|---|
| write registers | 0x40 | 0xA0 | 0 | (page << 8) \| register | 1 to 64 bytes |
| read registers | 0xC0 | 0xB0 | 0 | (page << 8) \| register | 1 to 64 bytes |

The chip exposes pages of 256 registers. The page is the high byte of wIndex. It is not a separate transfer.

Registers the updater touches:

| Page | Register | Use |
|---|---|---|
| 0x50 | 0x02, 0x03 | Running firmware version, high byte first. |
| 0x03 | 0x07 | Bit 0: 0 means the firmware is in SPI flash, 1 means EEPROM. |
| 0x04 | 0x10 to 0x17 | Eight-byte key that has to be written before flash access. |
| 0x05 | 0x10... | SPI read data, and status after a program. |
| 0x05 | 0x20... | SPI command bytes: opcode, then a 24-bit address. |
| 0x05 | 0x2A | Command length/control: 0x10 for one byte commands, 0x40 for opcode plus address, 0x50 for fast read. |
| 0x05 | 0x2B | Trigger. Written as a pair: 1 then 0 (no data), 3 then 2 (write data), 5 then 4 (read data). |
| 0x05 | 0x2C, 0x2D | Data length, low byte first. |
| 0x05 | 0x30... | SPI write data. |

The key is derived from four bytes at 0xFFEA in the image with a fixed bit shuffle (`decode_chip_id` in the code), followed by the four bytes at 0xFFEE. For the S3 image it comes out as `5d 5c 5b 5a 00 00 00 00`, which is also the fallback built into Chrontel's tool.

The SPI opcodes are the standard ones: 0x05 read status, 0x06 write enable, 0x01 write status, 0x0B fast read, 0x02 page program, 0x52 32K block erase, 0x20 4K sector erase. Reads and programs go 16 bytes at a time.

## Flash layout (64 KB)

| Range | Contents |
|---|---|
| 0x0000 | Firmware length N, 16 bits little endian. |
| 0x0002 to N-1 | 8051 code. The reset vector is at image offset 2. The last two bytes are the version. |
| N, N+1 | Checksum: 16-bit sum of bytes 2 to N-1, little endian. |
| 0xF000 to 0xFFFF | Settings. Never erased by a firmware-only update. |
| 0xF200 (from the pointer at 0xFFFA, minus 0x100) | USB IDs, strings and other customer settings. |
| 0xFFCA | 18 bytes of analog trim, per unit. |
| 0xFFE0 | 32-byte chip info block. |
| 0xFFFA | Pointer to the settings block (0xF300 on the S3). |

## Update sequence

This is the path Chrontel's tool takes with `UPGFLAG = 1` on a flash-type chip, and what `flash` does:

1. Read the running version and the chip type.
2. Write the key to page 4.
3. Read the SPI status register. If bit 7 is set, write enable and write status 0.
4. Read the chip info block at 0xFFE0.
5. Write enable, erase the 32K block at 0, then the seven 4K sectors from 0x8000 to 0xE000, polling the busy bit after each.
6. Program bytes 0 to N+1 in 16-byte chunks. Write enable before each chunk. Poll register 0x10 on page 5 until bit 0 or 1 is set.
7. Read everything back and compare. A chunk that differs is rewritten up to three times.

The chip is not reset afterwards. The new firmware starts on the next power-up.

One oddity is kept on purpose: the data length is always sent as 16, so when the last chunk is shorter, a couple of stale bytes from the previous chunk get programmed just past the checksum. The vendor tool does the same.

## What the firmware does

The code is for the 8051 core inside the CH7218A. It negotiates USB Power Delivery, takes DisplayPort alt mode from the console, runs the conversion to HDMI, reads and edits the display's EDID, and serves the Billboard device described above.

The part that makes it a Switch dock is small. All of it is USB-PD vendor defined messages:

1. The dock sends Discover Identity to whatever is plugged in. If the answer carries Nintendo's vendor ID (0x057E), it switches to a Nintendo mode.
2. When the console sends Discover Identity, the dock answers with Nintendo's identity in place of Chrontel's. In Switch 2 mode the VDOs are `0x5460057E 0x00000000 0x20620000 0x65000031`, product ID 0x2062. There is a second identity with product ID 0x2003 for the original Switch.
3. Discover SVIDs returns DisplayPort (0xFF01) and Nintendo (0x057E). The dock accepts Enter Mode for both.
4. The console then sends unstructured VDMs with SVID 0x057E. The dock sets bit 6 of the header in its reply, repeats the command word and adds fixed data:

| Command | Reply data |
|---|---|
| 0x01 | `0x03010503` |
| 0x02 | `0x0003006E` |
| 0x04 | `0x330107DA 0x06016D61`, twice |
| 0x06 | `0x330107DA 0x06016D68`, twice |
| 0x20 | `0x00000000` |
| 0x29, 0x23 | Echo. 0x29 with payload `0x0190` sets a flag, and a later 0x23 then sets bit 7 of chip register 0x1D1E. That looks like the switch that lets video out, but it is a guess. |
| others | Echo. |

There is no challenge and response in these handlers. Every reply is a constant or an echo.

These values match the capture of an official dock that Michał Kopeć published a few days before this was written, in ["Making a €40 USB-C hub work as a Nintendo Switch 2 dock"](https://blog.nozomi.space/posts/sw2_dock/). His write-up is the better reference for the protocol as seen on the wire.

The original Switch uses a different style: the header's low byte is zero, the reply increments two bytes of the command word, and commands 0x06, 0x0B, 0x10, 0x16 and 0x18 get fixed data.

## What changed between V09.09.53 and V09.09.81

The older version is what the tested dock shipped with, so the difference covers several of Antank's releases.

- The reply to command 0x02 went from `0x0003005F` to `0x0003006E`. Kopeć notes that this value looks like a version number and that another project saw it change with an official dock update. That makes it the likely fix for Switch 2 system version 23.0.0, but it is not proven.
- Command 0x20 now also resets some output state. Original Switch command 0x16 now gets a data word in its reply.
- The firmware compares the display's EDID product name against a built-in list to apply per-display quirks. The list grew from about a dozen names to about 25. New entries include Odyssey G95NC, Odyssey G70D, AORUS FO32U2P, PG27UCDM, XG27UCS and AG276UZD.
- About 1,700 more bytes of code, mostly in the video and EDID handling. That fits the VRR support Antank announced, but that code has not been read in detail.
