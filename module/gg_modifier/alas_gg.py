#!/usr/bin/env python3
"""AzurLane multiplier patcher (no GameGuardian UI required).

Replicates the GG Lua script pattern:
    searchNumber("0.0001;1::30", TYPE_DOUBLE) -> refine 1 -> edit 1 to N

Requires a rooted emulator with adb access.

Usage:
    python alas_gg.py status
    python alas_gg.py apply [--value 555] [--dry-run]
    python alas_gg.py restore [--value 555] [--dry-run]
    python alas_gg.py scan
    python alas_gg.py undo

Notes:
    * Only the low application-heap window (default 0x40000000-0x46000000) is
      scanned, which is where the game's damage formula tables live.
    * Patched addresses are cached per game PID.  While the game process keeps
      running, apply/restore only re-verifies and writes 8 bytes per address.
    * If the game restarts (new PID), a fresh scan is performed automatically.
"""
import argparse
import json
import os
import re
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ALAS_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DATA_DIR = os.environ.get("GG_MODIFIER_DATA_DIR", os.path.join(ALAS_ROOT, "config"))
CACHE_PATH = os.path.join(DATA_DIR, "gg_modifier_cache.json")
BACKUP_PATH = os.path.join(DATA_DIR, "gg_modifier_backup.json")


def _find_adb():
    """Locate adb: prefer adbutils' bundled binary, then known installs."""
    try:
        from adbutils import adb_path
        path = adb_path()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    for candidate in (
        os.path.join(ALAS_ROOT, "toolkit", "Lib", "site-packages", "adbutils", "binaries", "adb.exe"),
        r"D:/Game_D/MuMuPlayer/nx_main/adb.exe",
    ):
        if os.path.exists(candidate):
            return candidate
    return "adb"


DEFAULT_ADB = _find_adb()
DEFAULT_SERIAL = "127.0.0.1:16384"
DEFAULT_PACKAGE = "com.bilibili.azurlane"
DEFAULT_WINDOW = (0x40000000, 0x46000000)

SKIP_MARKERS = [
    "dalvik", "[stack]", "jit-cache", "ashmem", "/dev/", "Video",
    "vdex", ".art", ".oat", ".odex", ".pma", ".dm", "/memfd:",
    "thread signal stack", "stack_and_tls",
]

PAGE = 4096
BATCH_BYTES = 12 * 1024 * 1024


def log(msg):
    print(msg, flush=True)


class AdbError(RuntimeError):
    pass


class MemPatcher:
    def __init__(self, adb=DEFAULT_ADB, serial=DEFAULT_SERIAL,
                 package=DEFAULT_PACKAGE, window=DEFAULT_WINDOW):
        self.adb = adb
        self.serial = serial
        self.package = package
        self.window = window

    # ---------- adb helpers ----------
    def _run(self, cmd, timeout=120, binary=False):
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        if p.returncode != 0 and not binary:
            raise AdbError(p.stderr.decode(errors="replace").strip())
        return p.stdout

    def adb_cmd(self, args):
        return f'"{self.adb}" -s {self.serial} {args}'

    def shell(self, cmd):
        return self._run(self.adb_cmd(f"shell \"{cmd}\""))

    def shell_root(self, cmd, binary=False):
        return self._run(
            self.adb_cmd(f'exec-out su 0 sh -c "{cmd}"'), binary=binary
        )

    # ---------- process / maps ----------
    def get_pid(self):
        out = self.shell(f"pidof {self.package}").decode(errors="replace")
        pids = out.split()
        if not pids:
            raise AdbError(f"{self.package} is not running")
        return int(pids[0])

    def get_maps(self, pid):
        out = self.shell_root(f"cat /proc/{pid}/maps")
        return out.decode(errors="replace")

    def regions(self, maps):
        regs = []
        for line in maps.splitlines():
            m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S{4}) (\S+) \S+ \S+\s*(.*)$", line)
            if not m:
                continue
            s, e, perm, off, path = m.groups()
            s, e = int(s, 16), int(e, 16)
            if not perm.startswith("rw"):
                continue
            if any(x in path for x in SKIP_MARKERS):
                continue
            if not (self.window[0] <= s < self.window[1]):
                continue
            regs.append((s, e, path))
        regs.sort()
        return regs

    # ---------- memory I/O ----------
    def read_region(self, pid, start, length):
        skip = start // PAGE
        count = length // PAGE
        if count <= 0:
            return b""
        return self.shell_root(
            f"dd if=/proc/{pid}/mem bs={PAGE} skip={skip} count={count} 2>/dev/null",
            binary=True,
        )

    def read_regions_batched(self, pid, regs):
        """Read many regions using batched adb calls.  Returns {start: bytes}."""
        result = {}
        batch = []
        batch_len = 0

        def flush():
            nonlocal batch, batch_len
            if not batch:
                return
            parts = []
            for s, e, _ in batch:
                skip, count = s // PAGE, (e - s) // PAGE
                parts.append(f"dd if=/proc/{pid}/mem bs={PAGE} skip={skip} count={count} 2>/dev/null")
            data = self.shell_root("; ".join(parts), binary=True)
            expected = sum((e - s) // PAGE * PAGE for s, e, _ in batch)
            if len(data) == expected:
                pos = 0
                for s, e, _ in batch:
                    n = (e - s) // PAGE * PAGE
                    result[s] = data[pos:pos + n]
                    pos += n
            else:
                # Fallback: read one by one so a short read cannot desync others.
                for s, e, _ in batch:
                    result[s] = self.read_region(pid, s, e - s)
            batch, batch_len = [], 0

        for r in regs:
            size = (r[1] - r[0]) // PAGE * PAGE
            if size <= 0:
                continue
            if batch and batch_len + size > BATCH_BYTES:
                flush()
            batch.append(r)
            batch_len += size
        flush()
        return result

    def patch(self, pid, address, value):
        """Write one double at address using dd on the device."""
        data = struct.pack("<d", float(value))
        tmp_local = os.path.join(HERE, "_patch.bin")
        with open(tmp_local, "wb") as f:
            f.write(data)
        remote = f"/data/local/tmp/alas_gg_patch_{os.getpid()}.bin"
        self._run(self.adb_cmd(f'push "{tmp_local}" {remote}'), timeout=30)
        try:
            self.shell_root(
                f"dd if={remote} of=/proc/{pid}/mem bs=1 seek={address} conv=notrunc 2>/dev/null"
            )
        finally:
            try:
                self.shell(f"rm -f {remote}")
            except AdbError:
                pass
            try:
                os.remove(tmp_local)
            except OSError:
                pass

    def read_mem(self, pid, address, length):
        data = self.shell_root(
            f"dd if=/proc/{pid}/mem bs=1 skip={address} count={length} 2>/dev/null",
            binary=True,
        )
        return data

    def read_many(self, pid, addresses, length=40, target_offset=32):
        """Read `length` bytes at `address - target_offset` for each address, one adb call.

        Returns {address: bytes}.
        """
        parts = []
        for a in addresses:
            start = a - target_offset
            parts.append(
                f"dd if=/proc/{pid}/mem bs=1 skip={start} count={length} 2>/dev/null"
            )
        data = self.shell_root("; ".join(parts), binary=True)
        out = {}
        pos = 0
        for a in addresses:
            out[a] = data[pos:pos + length]
            pos += length
        return out

    def patch_many(self, pid, addresses, value):
        """Write the same double to many addresses with one push + one adb call."""
        data = struct.pack("<d", float(value))
        tmp_local = os.path.join(HERE, "_patch.bin")
        with open(tmp_local, "wb") as f:
            f.write(data)
        remote = f"/data/local/tmp/alas_gg_patch_{os.getpid()}.bin"
        self._run(self.adb_cmd(f'push "{tmp_local}" {remote}'), timeout=30)
        try:
            parts = [
                f"dd if={remote} of=/proc/{pid}/mem bs=1 seek={a} conv=notrunc 2>/dev/null"
                for a in addresses
            ]
            self.shell_root("; ".join(parts))
        finally:
            try:
                self.shell(f"rm -f {remote}")
            except AdbError:
                pass
            try:
                os.remove(tmp_local)
            except OSError:
                pass

    def read_double(self, pid, address):
        data = self.read_mem(pid, address, 8)
        if len(data) != 8:
            return None
        return struct.unpack("<d", data)[0]

    # ---------- pattern search ----------
    @staticmethod
    def find_pairs(buf, anchor, target, window=30):
        """Yield offsets of aligned `target` doubles preceded by `anchor` within window."""
        a = struct.pack("<d", float(anchor))
        t = struct.pack("<d", float(target))
        i = buf.find(a)
        while i != -1:
            # target may start anywhere in [i+8, i+8+window)
            seg = buf[i + 8:i + 8 + window + 8]
            j = seg.find(t)
            while j != -1:
                yield i + 8 + j
                j = seg.find(t, j + 1)
            i = buf.find(a, i + 1)

    def scan(self, pid, anchor, target):
        regs = self.regions(self.get_maps(pid))
        total = sum(e - s for s, e, _ in regs)
        log(f"扫描 {len(regs)} 个区域 / {total/1048576:.1f} MB ...")
        t0 = time.time()
        data = self.read_regions_batched(pid, regs)
        addrs = []
        for s, buf in data.items():
            for off in self.find_pairs(buf, anchor, target):
                addr = s + off
                # GG type results are 8-byte aligned for doubles
                if addr % 8 == 0:
                    addrs.append(addr)
        addrs = sorted(set(addrs))
        log(f"扫描完成：{len(addrs)} 个匹配，用时 {time.time()-t0:.1f}s")
        for a in addrs:
            log(f"  {a:#x}")
        return addrs


# ---------- cache ----------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def cmd_status(p):
    pid = p.get_pid()
    cache = load_json(CACHE_PATH, {})
    log(f"游戏 PID: {pid}")
    if cache.get("pid") != pid:
        log("缓存：无（游戏进程已变化或从未扫描）")
    else:
        log(f"缓存：{len(cache.get('addresses', []))} 个地址，值={cache.get('value')}")
        for a in cache.get("addresses", []):
            log(f"  {a:#x} -> {p.read_double(pid, a)!r}")
    backup = load_json(BACKUP_PATH, {})
    if backup.get("pid") == pid:
        log(f"备份：{len(backup.get('values', {}))} 个地址（可 undo）")


def cache_valid(p, pid, expect_value):
    cache = load_json(CACHE_PATH, {})
    if cache.get("pid") != pid or not cache.get("addresses"):
        return None
    if abs(float(cache.get("value", -1)) - float(expect_value)) > 1e-9:
        return None
    addrs = cache["addresses"]
    bufs = p.read_many(pid, addrs, length=40, target_offset=32)
    anchor = struct.pack("<d", 0.0001)
    for a in addrs:
        buf = bufs[a]
        if len(buf) != 40:
            return None
        v = struct.unpack("<d", buf[32:40])[0]
        if abs(v - float(expect_value)) > 1e-9:
            return None
        if anchor not in buf[:32]:
            return None
    return addrs


def cmd_apply(p, value, dry_run):
    pid = p.get_pid()
    addrs = cache_valid(p, pid, 1.0)
    if addrs:
        log(f"使用缓存地址（{len(addrs)} 处，当前均为 1.0）")
    else:
        addrs = p.scan(pid, anchor=0.0001, target=1.0)
    if not addrs:
        log("未找到目标；可能已经修改过，或界面/进程状态不对。")
        return 1
    if dry_run:
        log("dry-run：不写入。")
        return 0

    backup = {"pid": pid, "values": {}}
    old = p.read_many(pid, addrs, length=8, target_offset=0)
    for a in addrs:
        backup["values"][str(a)] = (
            struct.unpack("<d", old[a])[0] if len(old[a]) == 8 else None
        )
    save_json(BACKUP_PATH, backup)

    p.patch_many(pid, addrs, value)
    # verify
    new = p.read_many(pid, addrs, length=8, target_offset=0)
    bad = [
        a for a in addrs
        if len(new[a]) != 8 or abs(struct.unpack("<d", new[a])[0] - value) > 1e-9
    ]
    if bad:
        log(f"写入校验失败: {[hex(a) for a in bad]}")
        return 2
    save_json(CACHE_PATH, {"pid": pid, "value": value, "addresses": addrs, "time": time.time()})
    log(f"已修改 {len(addrs)} 处：1.0 -> {value}")
    return 0


def cmd_restore(p, value, dry_run):
    pid = p.get_pid()
    addrs = cache_valid(p, pid, value)
    if addrs:
        log(f"使用缓存地址（{len(addrs)} 处，当前均为 {value}）")
    else:
        addrs = p.scan(pid, anchor=0.0001, target=value)
    if not addrs:
        log(f"未找到 {value} 的目标；可能已经还原。")
        return 1
    if dry_run:
        log("dry-run：不写入。")
        return 0
    p.patch_many(pid, addrs, 1.0)
    new = p.read_many(pid, addrs, length=8, target_offset=0)
    bad = [
        a for a in addrs
        if len(new[a]) != 8 or abs(struct.unpack("<d", new[a])[0] - 1.0) > 1e-9
    ]
    if bad:
        log(f"写入校验失败: {[hex(a) for a in bad]}")
        return 2
    save_json(CACHE_PATH, {"pid": pid, "value": 1.0, "addresses": addrs, "time": time.time()})
    log(f"已还原 {len(addrs)} 处：{value} -> 1.0")
    return 0


def cmd_undo(p):
    pid = p.get_pid()
    backup = load_json(BACKUP_PATH, {})
    if backup.get("pid") != pid or not backup.get("values"):
        log("没有可用于当前进程的备份。")
        return 1
    for a_s, v in backup["values"].items():
        p.patch(pid, int(a_s), v)
    log(f"已从备份恢复 {len(backup['values'])} 处原始值。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="AzurLane damage multiplier patcher (no GG UI)")
    ap.add_argument("action", choices=["status", "scan", "apply", "restore", "undo"])
    ap.add_argument("--value", type=float, default=555.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("--serial", default=DEFAULT_SERIAL)
    ap.add_argument("--package", default=DEFAULT_PACKAGE)
    args = ap.parse_args()

    p = MemPatcher(args.adb, args.serial, args.package)
    try:
        if args.action == "status":
            cmd_status(p)
            return 0
        if args.action == "scan":
            pid = p.get_pid()
            p.scan(pid, anchor=0.0001, target=args.value)
            return 0
        if args.action == "apply":
            return cmd_apply(p, args.value, args.dry_run)
        if args.action == "restore":
            return cmd_restore(p, args.value, args.dry_run)
        if args.action == "undo":
            return cmd_undo(p)
    except AdbError as e:
        log(f"adb 错误: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
