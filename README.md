# ch7218-flash

Firmware updater for Antank S3 series Switch 2 docks (S3 Max, S3 Air, S3 Pro) that runs on Linux and macOS. It needs one ordinary USB-A to USB-C cable.

Antank ships a Windows updater and a macOS command line tool. The macOS guide asks for a second, USB-C male to female cable, and there is no Linux tool. These docks are built around a Chrontel CH7218A chip, and this project is a Python port of Chrontel's own updater for that chip. Other CH7218A docks may work too, but only the S3 Max has been tried.

## Status

- Flashed one Antank S3 Max from Linux on 2026-11-15, firmware V09.09.53 to V09.09.81 (Antank's kit dated 2026-09-17). The flash verified and the dock works with a Switch 2 afterwards.
- macOS has not been tested. The code path is the same (pyusb on libusb), so it should work once the dock shows up on the USB bus.
- S3 Air and S3 Pro have not been tested. Antank lists them on the same firmware page.

## Risk

A firmware flash that fails partway can leave a dock unusable, and nobody outside Chrontel knows whether a half-written CH7218A can be reached again after it loses power. Antank's own advice is to leave a dock alone if it works. Use this at your own risk. There is no warranty.

The tool reduces the risk where it can:

- `info`, `backup` and `verify` never erase or program anything.
- `flash` reads the whole flash first and refuses to continue unless what it read is a valid firmware image with a good checksum. That proves reading works on your dock before anything is erased, and leaves you a file you can flash back.
- A failed attempt is retried from the erase step, up to three times, while the dock is still connected.
- The last 4 KB of the flash, which holds per-unit settings and analog trim, is never erased or written.

## What you need

- A USB-A to USB-C cable that carries data (many charging cables do not).
- Python 3.8 or newer, [pyusb](https://github.com/pyusb/pyusb) and libusb 1.0.
- The firmware kit from Antank. It is not included here.

## Steps

1. Download the kit from <https://antank.net/pages/s3-max-firmware-update> and unzip it. The file you need is the `.IMG` in the `MacOS` or `Windows` folder (they are identical), for example `CH7218A-IMG.G607E.09.09.08.IMG`.

2. Unplug everything from the dock. Connect the dock's USB-C PD port (the charging port on the back) to a USB-A port on the computer.

3. Check that the dock shows up:

   ```
   $ lsusb | grep 0639
   Bus 001 Device 096: ID 0639:7218 Chrontel, Inc. Billboard
   ```

   On a Mac, look for "Billboard" under System Information > USB. If it is not there, stop. Try another cable or port.

4. Install the dependencies and get the code:

   ```
   git clone https://github.com/adamrb/ch7218-flash
   cd ch7218-flash
   python3 -m venv .venv
   .venv/bin/pip install pyusb
   ```

   The virtual environment matters on Linux: the tool runs under sudo, and root does not see packages installed with a plain `pip install --user`.

   Linux usually has libusb already (`apt install libusb-1.0-0` if not). On macOS run `brew install libusb`.

5. Ask the dock what it is running. This only reads two registers.

   ```
   $ sudo .venv/bin/python ch7218_flash.py info path/to/CH7218A-IMG.G607E.09.09.08.IMG
   Image firmware   V09.09.81
   Billboard device 0639:7218
   Running firmware V09.09.53
   Chip type is FLASH
   ```

6. Flash. The tool asks you to type `FLASH`, saves a backup in the current directory, then erases, programs and verifies. On the tested dock the whole thing took a few seconds.

   ```
   $ sudo .venv/bin/python ch7218_flash.py flash path/to/CH7218A-IMG.G607E.09.09.08.IMG
   ```

7. When it prints `Status: Success`, unplug the dock and plug it back in.

Linux needs root for raw USB access, hence `sudo`. macOS normally does not, so drop it there. If you have [uv](https://docs.astral.sh/uv/) you can skip the venv and use the `./flash` wrapper, which runs the script with pyusb available and adds sudo on Linux: `./flash info IMAGE`.

## Commands

| Command | What it does |
|---|---|
| `info [IMAGE]` | Prints the dock's running firmware version and chip type, and the image's version if given. |
| `backup OUT.bin` | Reads the whole 64 KB flash to a new file. Refuses to overwrite. |
| `verify IMAGE` | Compares the firmware in the dock with the image. Changes nothing. |
| `flash IMAGE` | Backup, erase, program, verify. Options: `--yes` skips the prompt, `--backup FILE` names the backup, `--no-backup` skips it. |

Version numbers are printed the way Chrontel's tool prints them (the last byte in decimal), so they will not match the numbers in Antank's file names.

## Going back

The backup made before a flash is a normal firmware image. To return to the old firmware:

```
sudo .venv/bin/python ch7218_flash.py flash ch7218-backup-20261115-211427.bin
```

## If a flash fails

Do not unplug the dock. Run the same `flash` command again and add `--no-backup`, because the backup from the first attempt is the good one and a new one would capture a half-written chip. If the dock is no longer on the USB bus, this tool cannot help. Antank's Windows updater is the next thing to try.

## How it was checked

`tests/test_equivalence.py` runs Chrontel's own macOS updater binary (the x86-64 code, under the Unicorn emulator) against a simulated chip, runs this port against an identical simulated chip, and compares every USB transfer. Eight scenarios, about 60,000 transfers each, including write-protected flash, lost write enables, slow programs and chunks that fail verification. The traffic is identical in all of them.

The simulated chip is not the real one, so that test shows the port does what the vendor tool does and nothing more. The run on a real S3 Max is the only hardware evidence.

To run the test you need the vendor kit:

```
.venv/bin/pip install unicorn lief
.venv/bin/python tests/test_equivalence.py path/to/MacOS/CH7218Upd_macOS path/to/CH7218A-IMG...IMG
```

## For agents

If you are an AI agent doing this for someone, the rules that matter:

- Flashing is irreversible and can brick hardware. Get an explicit yes from the owner before running `flash`. Running `info` is safe.
- Do not pass `--yes` unless the owner has agreed to that specific flash. Do not pass `--no-backup` on a first attempt.
- Confirm `0639:7218` is on the USB bus before anything else. If it is missing, the fix is physical (cable, port, the PD port on the dock) and you cannot do it from software.
- Only one matching device may be connected. The tool refuses otherwise.
- Run `info` first and show the owner both version numbers. If the dock already runs the image's version, there is nothing to do.
- Keep the backup file. Tell the owner where it is.
- Exit codes: 0 success, 1 cancelled or verify mismatch, 2 nothing was written (bad image, no device, backup failed validation), 3 the flash started and did not finish. On 3, do not power cycle the dock, and follow "If a flash fails".
- If the tool reports "Chip type is EEPROM", stop. That path is not implemented.
- Never interrupt a running `flash`.

## How the update works

Notes on the USB protocol, the flash layout and what the firmware does are in [docs/notes.md](docs/notes.md).

## Legal

MIT license for the code in this repository. The firmware images and the vendor tools belong to Antank and Chrontel and are not distributed here. This project is not affiliated with Antank, Chrontel or Nintendo.
