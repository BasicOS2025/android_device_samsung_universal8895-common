# Experimental Android 17 / LineageOS 24 migration

Targets: Galaxy S8 SM-G950F (dreamlte), S8+ SM-G955F (dream2lte), Note8 SM-N950F (greatlte).

Upstream: https://codeberg.org/universal8895/android_device_samsung_universal8895-common
Baseline branch: `lineage-23.2`. Baseline commit: `57bbd96d96622e5e7aaf968007b3a771120450a1`.

Status: initial migration, not yet verified by an Android 17 ROM build or phone boot.
Original licensing, authorship and source history are retained.
Build coordination: https://github.com/BasicOS2025/universal8895-lineage24-build

## Android 17 Samsung boot packaging

Common boot tools have unique 8895 module names to avoid collisions with
hardware/samsung. Boot and recovery use the legacy DTBH-aware packer with
2048-byte pages, original load addresses and SEANDROIDENFORCE footer; modern
mkbootimg header-version arguments must not be used with Samsung's DT-size field.
The DTBH table size/alignment and optional second-stage padding are corrected.
The boot validator checks the ARM64 Image, payload SHA-1, Exynos 8895 chip ID,
DTBH offsets, first-stage init and the 11120000.ufs SYSTEM block path.
Synthetic host tests do not establish that a ROM boots on physical hardware.

## Init service compatibility

The watchdog daemon is explicitly packaged and started from /system/bin, as
defined by Android 17 system/core/watchdogd/Android.bp. The legacy
watchdog_break and sswap services referenced binaries absent from both source
and the pinned vendor snapshot; their stale service definitions are removed.
Kernel watchdog support is retained. Existing zram setup via swapon_all remains.

## Legacy cgroups and scheduling

Use real cpu/cpuset/memory/freezer v1 controllers. In particular, memory and
freezer override Android 17's v2 descriptors, unsupported by this Linux 4.4
configuration. blkio is optional because CONFIG_BLK_CGROUP is disabled.
Remove the unsupported schedtune controller incorrectly mounted at /dev/cpuset
and the vendor task-profile overrides targeting it; inherit platform CPU
scheduling profiles. Let libprocessgroup mount controllers before early-init,
without mounting a second cpuset hierarchy over its noprefix layout. Update
Samsung cpuset commands to cpus/mems/family_boost and initialize the extra
surfaceflinger group before assigning its CPU mask.

Requires kernel cpuset_v2_mode backport documented in ANDROID17-CPUSET.md.

The remaining vendor task-profile overrides are limited to Frozen/Unfrozen:
move tasks into the existing v1 frozen group or back to its root, instead of
writing v2 cgroup.freeze commands to an unsupported kernel interface.
Other scheduling profiles remain inherited from the Android 17 platform.
