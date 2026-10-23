# -*- coding: utf-8 -*-
"""按边界框重建校园底图（高德瓦片拼接）。

用法：
    python tools/build_map.py --z 19 --width 4096
    python tools/build_map.py --dry-run            # 只看范围与瓦片数，不下载

边界框优先取 places.json 的 campus.bbox；可用 --lon/--lat 覆盖。
瓦片来源为高德地图公开瓦片服务，仅供本地/校内演示使用，
不得将拼接结果对外分发（见 README 的授权说明）。
"""
import argparse
import io
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACES = os.path.join(ROOT, "places.json")
CACHE = os.path.join(ROOT, "tools", ".tilecache")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
HOSTS = ["https://wprd01.is.autonavi.com", "https://wprd02.is.autonavi.com",
         "https://wprd03.is.autonavi.com", "https://wprd04.is.autonavi.com"]

M_PER_DEG_LAT = 111320.0


def meters_per_deg_lon(lat):
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


def world_px(lat, lon, z):
    """经纬度 -> Web Mercator 像素坐标（原点左上）"""
    n = 256.0 * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def valid_tile(path):
    """瓦片完整性按尺寸判，不按字节数判。

    纯色瓦片（大片水面、农田）是合法内容，PNG 压缩后只有 179 字节；
    按字节数设阈值会把它误判成下载失败，导致图上出现一块灰洞。
    """
    from PIL import Image
    if not os.path.exists(path):
        return False
    try:
        with Image.open(path) as im:
            return im.size == (256, 256)
    except Exception:
        return False


def fetch_tile(x, y, z, path, retry=2):
    if valid_tile(path):
        return "cached"
    for attempt in range(retry + 1):
        host = HOSTS[(x + y + attempt) % len(HOSTS)]
        url = "%s/appmaptile?x=%d&y=%d&z=%d&lang=zh_cn&size=1&scl=1&style=7" % (host, x, y, z)
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
            with open(path, "wb") as f:
                f.write(data)
            if valid_tile(path):
                return "ok"
            os.remove(path)
        except Exception:
            pass
        time.sleep(0.4)
    return "fail"


def plan(bbox, z, width):
    """返回拼接方案：瓦片范围、输出尺寸、每像素米数"""
    lon0, lat0, lon1, lat1 = bbox
    px0, py1 = world_px(lat0, lon0, z)   # 左下
    px1, py0 = world_px(lat1, lon1, z)   # 右上
    native_w = px1 - px0
    native_h = py1 - py0
    ratio = width / native_w
    height = int(round(native_h * ratio))
    width_total_m = (lon1 - lon0) * meters_per_deg_lon((lat0 + lat1) / 2)
    height_total_m = (lat1 - lat0) * M_PER_DEG_LAT
    return {
        "bbox": bbox,
        "z": z,
        "px0": int(math.floor(px0)), "py0": int(math.floor(py0)),
        "px1": int(math.ceil(px1)), "py1": int(math.ceil(py1)),
        "native": (round(native_w), round(native_h)),
        "out": (width, height),
        "m_per_px": round(width_total_m / width, 3),
        "ground_m": (round(width_total_m), round(height_total_m)),
        "upscale": round(ratio, 3),
    }


def build(bbox, z, width, out_path, quality=82):
    from PIL import Image

    p = plan(bbox, z, width)
    tx0, ty0 = p["px0"] // 256, p["py0"] // 256
    tx1, ty1 = (p["px1"] - 1) // 256, (p["py1"] - 1) // 256
    nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
    total = nx * ny
    print("瓦片 %d x %d = %d 张（z%d）" % (nx, ny, total, z))

    os.makedirs(CACHE, exist_ok=True)
    jobs = []
    for i in range(nx):
        for j in range(ny):
            jobs.append((tx0 + i, ty0 + j))

    stats = {"ok": 0, "cached": 0, "fail": 0}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(fetch_tile, x, y, z,
                        os.path.join(CACHE, "z%d_%d_%d.png" % (z, x, y))): (x, y)
            for x, y in jobs
        }
        done = 0
        for fut in futures:
            pass
        for fut, (x, y) in futures.items():
            r = fut.result()
            stats[r] = stats.get(r, 0) + 1
            done += 1
            if done % 100 == 0 or done == total:
                print("  进度 %d/%d  %s" % (done, total, stats))
    if stats["fail"]:
        print("  警告：%d 张瓦片下载失败，将用底色填充" % stats["fail"])

    canvas = Image.new("RGB", (nx * 256, ny * 256), "#EEF1F5")
    for j in range(ny):
        for i in range(nx):
            path = os.path.join(CACHE, "z%d_%d_%d.png" % (z, tx0 + i, ty0 + j))
            if not os.path.exists(path):
                continue
            try:
                tile = Image.open(path).convert("RGB")
            except Exception:
                continue
            canvas.paste(tile, (i * 256, j * 256))

    ox = p["px0"] - tx0 * 256
    oy = p["py0"] - ty0 * 256
    crop_w = p["px1"] - p["px0"]
    crop_h = p["py1"] - p["py0"]
    img = canvas.crop((ox, oy, ox + crop_w, oy + crop_h))
    if img.width != p["out"][0]:
        img = img.resize(p["out"], Image.LANCZOS)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "WEBP", quality=quality, method=6)
    size = os.path.getsize(out_path)
    print("输出 %s  %dx%d  %.2f MB  %.3f m/px（放大系数 %.2f 倍原始瓦片）"
          % (out_path, img.width, img.height, size / 1048576.0, p["m_per_px"], p["upscale"]))
    return p, img.size, size


def load_bbox():
    with io.open(PLACES, encoding="utf-8") as f:
        d = json.load(f)
    return d["campus"]["bbox"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--z", type=int, default=19)
    ap.add_argument("--width", type=int, default=4096)
    ap.add_argument("--out", default=os.path.join(ROOT, "web", "assets", "campus.webp"))
    ap.add_argument("--quality", type=int, default=82)
    ap.add_argument("--bbox", default="", help="lon0,lat0,lon1,lat1，默认读 places.json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    bbox = [float(x) for x in a.bbox.split(",")] if a.bbox else load_bbox()
    p = plan(bbox, a.z, a.width)
    print("范围 lon %.5f–%.5f  lat %.5f–%.5f" % tuple(bbox))
    print("地面 %d x %d m，输出 %dx%d，%.3f m/px，瓦片原生 %dx%d"
          % (p["ground_m"][0], p["ground_m"][1], p["out"][0], p["out"][1],
             p["m_per_px"], p["native"][0], p["native"][1]))
    if a.dry_run:
        return
    build(bbox, a.z, a.width, a.out, a.quality)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
