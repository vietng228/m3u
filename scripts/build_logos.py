#!/usr/bin/env python3

import io
import re
from pathlib import Path

import requests
from PIL import Image, ImageDraw


PLAYLIST = Path("m3u.m3u")
PRIMARY_LOGO_SOURCE = "https://4note.net/raw/sxmpewlsn8"
PRIMARY_LOGO_FILE = Path("logos/logo-source.m3u")
OUTPUT_DIR = Path("logos")
SIZE = (410, 230)
MAX_LOGO = (230, 105)


def background() -> Image.Image:
    image = Image.new("RGB", SIZE, (247, 247, 247))
    draw = ImageDraw.Draw(image)
    polygons = [
        ([(0, 0), (135, 0), (85, 58)], (242, 242, 242)),
        ([(135, 0), (190, 0), (205, 80), (85, 58)], (250, 250, 250)),
        ([(190, 0), (300, 0), (205, 80)], (226, 226, 226)),
        ([(300, 0), (410, 0), (410, 35), (330, 65)], (244, 244, 244)),
        ([(0, 0), (85, 58), (35, 170), (0, 165)], (245, 245, 245)),
        ([(85, 58), (205, 80), (225, 165), (35, 170)], (238, 238, 238)),
        ([(205, 80), (330, 65), (410, 118), (410, 218), (225, 165)], (242, 242, 242)),
        ([(0, 165), (35, 170), (0, 230)], (252, 252, 252)),
        ([(35, 170), (225, 165), (330, 230), (0, 230)], (245, 245, 245)),
        ([(225, 165), (410, 218), (410, 230), (330, 230)], (232, 232, 232)),
    ]
    for points, color in polygons:
        draw.polygon(points, fill=color)
    return image


def blocks(text: str):
    current = []
    for line in text.splitlines():
        if line.startswith("#EXTINF"):
            if current:
                yield current
            current = [line]
        elif current:
            current.append(line)
    if current:
        yield current


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower().replace("đ", "d"))


def channel_entries(text: str):
    for block in blocks(text):
        extinf = block[0]
        tvg_id = (re.search(r'tvg-id="([^"]+)"', extinf, re.I) or [None, ""])[1].strip().lower()
        logo = (re.search(r'tvg-logo="([^"]+)"', extinf, re.I) or [None, ""])[1].strip()
        group = (re.search(r'group-title="([^"]+)"', extinf, re.I) or [None, ""])[1].strip()
        name = extinf.rsplit(",", 1)[-1].strip()
        if tvg_id:
            yield {"id": tvg_id, "logo": logo, "group": group, "name": name}


def primary_logo_maps(text: str):
    by_id, by_name = {}, {}
    for entry in channel_entries(text):
        if not entry["logo"]:
            continue
        by_id[entry["id"]] = entry["logo"]
        by_name[(normalize(entry["group"]), normalize(entry["name"]))] = entry["logo"]
    return by_id, by_name


def remove_light_background(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    pixels = []
    for red, green, blue, alpha in rgba.getdata():
        # Chỉ bỏ màu gần trắng/xám sáng. Pixel màu và phần trắng bên trong logo
        # vẫn được giữ khi ảnh nguồn đã có alpha chuẩn.
        if alpha and min(red, green, blue) > 242 and max(red, green, blue) - min(red, green, blue) < 10:
            pixels.append((red, green, blue, 0))
        else:
            pixels.append((red, green, blue, alpha))
    rgba.putdata(pixels)
    bbox = rgba.getbbox()
    return rgba.crop(bbox) if bbox else rgba


def compose(source: Image.Image) -> Image.Image:
    logo = remove_light_background(source)
    logo.thumbnail(MAX_LOGO, Image.Resampling.LANCZOS)
    canvas = background().convert("RGBA")
    x = (SIZE[0] - logo.width) // 2
    y = (SIZE[1] - logo.height) // 2
    canvas.alpha_composite(logo, (x, y))
    return canvas.convert("RGB")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    text = PLAYLIST.read_text(encoding="utf-8")
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    if PRIMARY_LOGO_FILE.exists():
        primary_text = PRIMARY_LOGO_FILE.read_text(encoding="utf-8")
        print(f"Primary logo list: {PRIMARY_LOGO_FILE}")
    else:
        primary_response = session.get(PRIMARY_LOGO_SOURCE, timeout=30)
        primary_response.raise_for_status()
        primary_text = primary_response.text
        print(f"Primary logo list: {PRIMARY_LOGO_SOURCE}")
    primary_by_id, primary_by_name = primary_logo_maps(primary_text)

    copied = generated = failed = missing = 0
    seen = set()
    for entry in channel_entries(text):
        tvg_id = entry["id"]
        if tvg_id in seen:
            continue
        seen.add(tvg_id)
        primary_url = primary_by_id.get(tvg_id) or primary_by_name.get(
            (normalize(entry["group"]), normalize(entry["name"]))
        )
        url = primary_url or entry["logo"]
        if not url:
            missing += 1
            print(f"[LOGO MISSING] {tvg_id}")
            continue
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()
            source = Image.open(io.BytesIO(response.content))
            if primary_url:
                # Logo từ nguồn ưu tiên đã theo style mong muốn: chỉ chuẩn hóa
                # định dạng/tên file, không vẽ lại hoặc đổi bố cục.
                source.convert("RGB").save(
                    OUTPUT_DIR / f"{tvg_id}.png", "PNG", optimize=True
                )
                copied += 1
            else:
                compose(source).save(
                    OUTPUT_DIR / f"{tvg_id}.png", "PNG", optimize=True
                )
                generated += 1
        except Exception as error:
            failed += 1
            print(f"[LOGO FAILED] {tvg_id}: {error}")
    print(
        f"Copied from primary: {copied}; generated fallback: {generated}; "
        f"missing: {missing}; failed: {failed}; output: {OUTPUT_DIR}"
    )
    if copied + generated == 0:
        raise SystemExit("No logos were generated")


if __name__ == "__main__":
    main()
