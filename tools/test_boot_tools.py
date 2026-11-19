#!/usr/bin/env python3
"""Check actual host packers against Samsung's binary layout with synthetic DTBs."""
import hashlib
import lzma
from pathlib import Path
import struct
import subprocess
import tempfile
from verify_boot_image import verify

root = Path(__file__).resolve().parents[1]
tools = root / 'dtbhtool'


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def align(value, page):
    return (value + page - 1) // page * page


with tempfile.TemporaryDirectory() as temporary:
    work = Path(temporary)
    for binary, source, libs in [('dtbtool', 'mkdtbimg.c', ['-lfdt']),
                                  ('mkbootimg', 'mkbootimg.c', ['-lfdt', '-lcrypto'])]:
        run('cc', '-Wall', '-Wextra', '-Werror', '-Wno-deprecated-declarations',
            '-I' + str(root / 'dtbhtool'), '-I' + str(tools / 'libdtbimg'),
            tools / source, tools / 'dtbimg.c', *libs, '-o', work / binary)

    dtbs = work / 'dtbs'
    dtbs.mkdir()
    # Enough entries to cross a header page; this catches incorrect offsets and
    # the upstream quadratic header-size calculation, which breaks multi-DTB images.
    for revision in range(70):
        source = work / 'fixture.dts'
        source.write_text('/dts-v1/; / { model_info-chip = <8895>; '
                          'model_info-platform = "android"; model_info-subtype = "samsung"; '
                          f'model_info-hw_rev = <{revision}>; model_info-hw_rev_end = <{revision}>; }};')
        run('dtc', '-I', 'dts', '-O', 'dtb', '-o', dtbs / f'{revision:03}.dtb', source)

    kernel, ramdisk = bytes(range(256)) * 17, b'RAMDISK' * 293
    (work / 'kernel').write_bytes(kernel)
    (work / 'ramdisk').write_bytes(ramdisk)
    for page in (2048, 4096):
        run(work / 'dtbtool', '-o', work / 'dt.img', '-s', page, '--dt_dir', dtbs)
        dt = (work / 'dt.img').read_bytes()
        assert struct.unpack_from('<4sII', dt) == (b'DTBH', 2, 70)
        for revision in range(70):
            chip, platform, subtype, first, last, offset, size, delimiter = struct.unpack_from('<8I', dt, 12 + revision * 32)
            assert (chip, platform, subtype, first, last, delimiter) == (8895, 0x50a6, 0x217584da, revision, revision, 0x20)
            payload = (dtbs / f'{revision:03}.dtb').read_bytes()
            assert offset % page == 0 and size == align(len(payload), page)
            assert dt[offset:offset + len(payload)] == payload

        run(work / 'mkbootimg', '--kernel', work / 'kernel', '--ramdisk', work / 'ramdisk',
            '--dt', work / 'dt.img', '--pagesize', page, '--base', '0x10000000',
            '--kernel_offset', '0x8000', '--ramdisk_offset', '0x01000000',
            '--tags_offset', '0x100', '--cmdline', 'console=ttySAC0', '--output', work / 'boot.img')
        boot = (work / 'boot.img').read_bytes()
        header = struct.unpack_from('<8s10I16s512s32s', boot)
        assert header[:11] == (b'ANDROID!', len(kernel), 0x10008000, len(ramdisk), 0x11000000,
                               0, 0x10f00000, 0x10000100, page, len(dt), 0)
        assert boot[page:page + len(kernel)] == kernel
        ramdisk_offset = page + align(len(kernel), page)
        dt_offset = ramdisk_offset + align(len(ramdisk), page)
        assert boot[ramdisk_offset:ramdisk_offset + len(ramdisk)] == ramdisk
        assert boot[dt_offset:dt_offset + len(dt)] == dt
        digest = hashlib.sha1()
        for payload in (kernel, ramdisk, b'', dt):
            digest.update(payload)
            digest.update(struct.pack('<I', len(payload)))
        assert header[-1][:20] == digest.digest()

    # Exercise the production validator against a packed image containing real
    # newc/XZ layout and an ARM64 init header, then remove boot-critical inputs.
    def entry(name, payload, mode=0o100755):
        filename = name.encode() + b'\0'
        fields = [1, mode, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(filename), 0]
        record = b'070701' + ''.join(f'{value:08x}' for value in fields).encode() + filename
        record += bytes((-len(record)) % 4)
        record += payload
        return record + bytes((-len(payload)) % 4)

    elf = bytearray(64)
    elf[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', elf, 18, 183)
    kernel = bytearray(4096)
    kernel[56:60] = b'ARMd'
    (work / 'kernel').write_bytes(kernel)
    run(work / 'dtbtool', '-o', work / 'dt.img', '-s', '2048', '--dt_dir', dtbs)
    fstab = b'/dev/block/platform/11120000.ufs/by-name/SYSTEM /system ext4 ro wait,first_stage_mount\n'

    def pack_ramdisk(records):
        raw = b''.join(records) + entry('TRAILER!!!', b'', 0)
        compressed = lzma.compress(raw, check=lzma.CHECK_CRC32)
        (work / 'ramdisk').write_bytes(compressed)
        run(work / 'mkbootimg', '--kernel', work / 'kernel', '--ramdisk', work / 'ramdisk',
            '--dt', work / 'dt.img', '--pagesize', '2048', '--output', work / 'boot.img')
        return (work / 'boot.img').read_bytes() + b'SEANDROIDENFORCE'

    good = pack_ramdisk([entry('init', bytes(elf)), entry('fstab.samsungexynos8895', fstab)])
    verify(good, 41943040, True)
    for records, expected in [
        ([entry('init', bytes(elf))], 'Missing first-stage Samsung fstab'),
        ([entry('init', bytes(elf)), entry('fstab.samsungexynos8895', fstab.replace(b'first_stage_mount', b'recoveryonly'))], 'System fstab does not permit'),
        ([entry('init', b'/system/bin/init', 0o120777), entry('fstab.samsungexynos8895', fstab)], 'broken init symlink'),
    ]:
        try:
            verify(pack_ramdisk(records), 41943040, True)
        except ValueError as error:
            assert expected in str(error), str(error)
        else:
            raise AssertionError('Invalid boot inputs were accepted')
    corrupted = bytearray(good)
    corrupted[2048 + 100] ^= 1
    try:
        verify(corrupted, 41943040, True)
    except ValueError as error:
        assert 'SHA-1 mismatch' in str(error)
    else:
        raise AssertionError('Corrupt boot image was accepted')
    print('PASS: production boot validator accepts XZ/newc first-stage inputs and rejects missing fstab, recovery-only system, broken init and corrupt payloads')

    invalid = work / 'invalid'
    invalid.mkdir()
    (work / 'fixture.dts').write_text('/dts-v1/; / { model = "invalid"; };')
    run('dtc', '-I', 'dts', '-O', 'dtb', '-o', invalid / 'invalid.dtb', work / 'fixture.dts')
    result = subprocess.run([str(work / 'dtbtool'), '-o', str(work / 'bad.img'), '-s', '2048', '--dt_dir', str(invalid)])
    assert result.returncode > 0, 'Invalid DTB must be rejected without a crash'
print('PASS: DTBH entries, payload offsets, Samsung boot header, SHA-1 and invalid-DTB rejection')
