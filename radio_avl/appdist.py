"""
The KenTrackIt phone app's installer, kept on the box so phones can be told a newer version is out
and download it from the box itself (no app store). An administrator uploads the APK in Settings.

The version is read from the APK's own manifest (Android's binary XML), so a wrong or older file is
caught at upload. Ported from RapidPTT's appdist package.
"""
import hashlib
import io
import json
import os
import struct
import time
import zipfile

PACKAGE = "nz.co.asiltd.kentrackit"
MAX_SIZE = 150 << 20
ATTR_VERSION_CODE = 0x0101021B
ATTR_VERSION_NAME = 0x0101021C


class APKError(ValueError):
    pass


def parse_manifest(b):
    """-> (package, version_code, version_name) from a binary AndroidManifest.xml."""
    u16 = lambda o: struct.unpack_from("<H", b, o)[0]
    u32 = lambda o: struct.unpack_from("<I", b, o)[0]
    if len(b) < 8 or u16(0) != 0x0003:
        raise APKError("not an Android manifest")
    strs, ids = [], []
    off = u16(2)
    while off + 8 <= len(b):
        typ, head, size = u16(off), u16(off + 2), u32(off + 4)
        if size < 8 or head < 8 or head > size or off + size > len(b):
            raise APKError("not an Android manifest")
        chunk = b[off:off + size]
        if typ == 0x0001:
            strs = _string_pool(chunk)
        elif typ == 0x0180:
            ids = [struct.unpack_from("<I", chunk, p)[0] for p in range(head, size - 3, 4)]
        elif typ == 0x0102:
            ext = chunk[head:]
            s = lambda i: strs[i] if i < len(strs) else ""
            if s(struct.unpack_from("<I", ext, 4)[0]) != "manifest":
                raise APKError("not an Android manifest")
            start, each, count = struct.unpack_from("<HHH", ext, 8)
            pkg, code, name = "", 0, ""
            for i in range(count):
                p = head + start + i * each
                a = chunk[p:p + 20]
                if len(a) < 20:
                    raise APKError("not an Android manifest")
                aname, raw = struct.unpack_from("<II", a, 4)
                dtype, data = a[15], struct.unpack_from("<I", a, 16)[0]
                text = s(data) if dtype == 0x03 else s(raw)
                rid = ids[aname] if aname < len(ids) else 0
                if rid == ATTR_VERSION_CODE:
                    code = data
                elif rid == ATTR_VERSION_NAME:
                    name = text
                elif rid == 0 and s(aname) == "package":
                    pkg = text
            if not pkg:
                raise APKError("not an Android manifest")
            return pkg, code, name
        off += size
    raise APKError("not an Android manifest")


def _string_pool(c):
    head, count, flags, base = struct.unpack_from("<H", c, 2)[0], *struct.unpack_from("<I", c, 8), \
        struct.unpack_from("<I", c, 16)[0], struct.unpack_from("<I", c, 20)[0]
    if count > 1 << 16 or head + 4 * count > len(c) or base > len(c):
        raise APKError("not an Android manifest")
    out = []
    for i in range(count):
        p = base + struct.unpack_from("<I", c, head + 4 * i)[0]
        if flags & (1 << 8):                # UTF-8: char count, then byte count
            n = 0
            for _ in range(2):
                n = c[p]; p += 1
                if n & 0x80:
                    n = (n & 0x7F) << 8 | c[p]; p += 1
            out.append(c[p:p + n].decode("utf-8", "replace"))
        else:
            n = struct.unpack_from("<H", c, p)[0]; p += 2
            if n & 0x8000:
                n = (n & 0x7FFF) << 16 | struct.unpack_from("<H", c, p)[0]; p += 2
            out.append(c[p:p + 2 * n].decode("utf-16-le", "replace"))
    return out


def inspect(path_or_bytes):
    try:
        z = zipfile.ZipFile(io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes)
        manifest = z.read("AndroidManifest.xml")
    except (zipfile.BadZipFile, KeyError, OSError):
        raise APKError("That file isn't an Android app installer (APK).")
    pkg, code, name = parse_manifest(manifest)
    return pkg, code, name


class AppStore:
    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "app")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "kentrackit.apk")
        self.meta_path = os.path.join(self.dir, "kentrackit.json")

    def info(self):
        try:
            with open(self.meta_path) as f:
                m = json.load(f)
            if os.path.exists(self.path):
                return m
        except (OSError, ValueError):
            pass
        return None

    def put(self, data):
        if len(data) > MAX_SIZE:
            raise APKError(f"That file is larger than {MAX_SIZE >> 20} MB.")
        pkg, code, name = inspect(data)
        if pkg != PACKAGE:
            raise APKError(f"That APK is a different app ({pkg}), not the KenTrackIt app.")
        if not code:
            raise APKError("That APK doesn't say which version it is.")
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self.path)
        m = {"version_code": code, "version_name": name, "size": len(data),
             "sha256": hashlib.sha256(data).hexdigest(), "uploaded": time.time()}
        with open(self.meta_path, "w") as f:
            json.dump(m, f)
        return m

    def remove(self):
        for p in (self.path, self.meta_path):
            try:
                os.unlink(p)
            except OSError:
                pass
