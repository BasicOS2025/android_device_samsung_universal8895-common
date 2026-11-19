#!/usr/bin/env python3
"""Reject malformed Samsung DTBH images and missing first-stage boot inputs."""
import argparse
import gzip
import hashlib
from pathlib import Path
import posixpath
import stat
import struct
import subprocess


def decompress_xz(data):
    try:
        import lzma
    except ModuleNotFoundError:
        # Soong's embedded Python omits _lzma. Keep XZ validation enabled
        # using the build host's xz-utils instead of skipping the ramdisk.
        return subprocess.run(['/usr/bin/xz', '--decompress', '--stdout'],
                              input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True).stdout
    return lzma.decompress(data)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def align(value, page):
    return (value + page - 1) // page * page


def read_cpio(data):
    files = {}
    offset = 0
    while offset + 110 <= len(data):
        require(data[offset:offset + 6] in (b'070701', b'070702'), 'Invalid ramdisk CPIO header')
        fields = [int(data[offset + 6 + i * 8:offset + 14 + i * 8], 16) for i in range(13)]
        mode, size, namesize = fields[1], fields[6], fields[11]
        require(namesize > 0 and offset + 110 + namesize <= len(data), 'Invalid CPIO filename')
        rawname = data[offset + 110:offset + 110 + namesize]
        require(rawname[-1:] == b'\0', 'Unterminated CPIO filename')
        name = posixpath.normpath(rawname[:-1].decode())
        start = align(offset + 110 + namesize, 4)
        require(start + size <= len(data), 'Truncated CPIO payload')
        if name == 'TRAILER!!!':
            return files
        files[name] = (mode, data[start:start + size])
        offset = align(start + size, 4)
    raise ValueError('Ramdisk has no CPIO trailer')


def verify(data, max_size, first_stage=False):
    require(608 <= len(data) <= max_size, 'Boot image exceeds partition or is truncated')
    header = struct.unpack_from('<8s10I16s512s32s', data)
    require(header[0] == b'ANDROID!', 'Missing Android boot header')
    kernel_size, ramdisk_size, second_size, page, dt_size = header[1], header[3], header[5], header[8], header[9]
    require(page == 2048, 'Unexpected Exynos 8895 page size')
    require((header[2], header[4], header[7]) == (0x10008000, 0x11000000, 0x10000100), 'Incorrect image load addresses')
    require(kernel_size >= 64 and ramdisk_size > 0 and dt_size >= 12, 'Missing kernel, ramdisk or DTBH')
    offset = page
    payloads = []
    for size in (kernel_size, ramdisk_size, second_size, dt_size):
        require(offset + size <= len(data), 'Image payload extends beyond file')
        payloads.append(data[offset:offset + size])
        offset += align(size, page)
    require(data[offset:] == b'SEANDROIDENFORCE', 'Missing Samsung footer or unexpected trailing data')
    kernel, ramdisk, second, dt = payloads
    require(kernel[56:60] == b'ARMd', 'Kernel is not an uncompressed ARM64 Image')
    digest = hashlib.sha1()
    for payload in payloads:
        digest.update(payload)
        digest.update(struct.pack('<I', len(payload)))
    require(header[-1][:20] == digest.digest(), 'Boot header SHA-1 mismatch')
    magic, version, count = struct.unpack_from('<4sII', dt)
    require(magic == b'DTBH' and version == 2 and count > 0, 'Invalid Samsung DTBH table')
    require(12 + count * 32 + 4 <= len(dt), 'Truncated DTBH entry table')
    for i in range(count):
        entry = struct.unpack_from('<8I', dt, 12 + i * 32)
        start, size = entry[5:7]
        require(entry[:3] == (8895, 0x50a6, 0x217584da), 'DTBH platform does not match Exynos 8895')
        require(start >= align(12 + count * 32 + 4, page) and start % page == 0,
                'Invalid DTB payload offset')
        require(size > 0 and size % page == 0 and start + size <= len(dt), 'Invalid DTB payload size')
        require(dt[start:start + 4] == b'\xd0\x0d\xfe\xed', 'DTBH entry does not point to an FDT')
    if not first_stage:
        return
    if ramdisk.startswith(b'\xfd7zXZ\x00'):
        ramdisk = decompress_xz(ramdisk)
    elif ramdisk.startswith(b'\x1f\x8b'):
        ramdisk = gzip.decompress(ramdisk)
    require(len(ramdisk) <= 128 * 1024 * 1024, 'Unexpectedly large first-stage ramdisk')
    files = read_cpio(ramdisk)
    name = 'init'
    for _ in range(8):
        require(name in files, 'Missing first-stage init or broken init symlink')
        mode, init = files[name]
        if not stat.S_ISLNK(mode):
            break
        target = init.decode()
        name = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(posixpath.dirname(name), target))
    else:
        raise ValueError('Init symlink cycle')
    require(stat.S_ISREG(mode) and mode & 0o111 and len(init) >= 64, 'First-stage init is not executable')
    require(init[:6] == b'\x7fELF\x02\x01' and struct.unpack_from('<H', init, 18)[0] == 183,
            'First-stage init is not an ARM64 ELF')
    fstabs = [files[p][1].decode() for p in ('fstab.samsungexynos8895', 'first_stage_ramdisk/fstab.samsungexynos8895') if p in files]
    require(bool(fstabs), 'Missing first-stage Samsung fstab')
    for fstab in fstabs:
        rows = [line.split() for line in fstab.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        system = [row for row in rows if len(row) >= 5 and row[1] == '/system']
        require(len(system) == 1, 'Missing or duplicate system mount in first-stage fstab')
        row = system[0]
        require(row[0] == '/dev/block/platform/11120000.ufs/by-name/SYSTEM', 'Incorrect universal8895 system block path')
        require('first_stage_mount' in row[4].split(',') and 'recoveryonly' not in row[4].split(','),
                'System fstab does not permit first-stage mounting')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--max-size', type=int, required=True)
    parser.add_argument('--first-stage', action='store_true')
    args = parser.parse_args()
    try:
        verify(args.image.read_bytes(), args.max_size, args.first_stage)
    except (ValueError, OSError, struct.error, EOFError) as error:
        parser.exit(1, f'Boot verification failed: {error}\n')
    print('Verified Samsung boot layout' + (' and first-stage init/fstab' if args.first_stage else ''))
