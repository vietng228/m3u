#!/usr/bin/env python3

import sys
import re
import requests
import unicodedata

SOURCE_URL = "http://tv.vietanhtv.top/tv"
TARGET_FILES = [
    "m3u.m3u",
    "new.m3u",
    "test.m3u",
]

def fetch(url: str) -> str:
    r = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    r.raise_for_status()
    return r.text


def split_blocks(text: str):
    """
    Tách playlist thành từng block:
    #EXTINF...
    #EXTVLCOPT... (nếu có)
    URL
    """
    lines = text.splitlines()
    blocks = []
    current = []

    for line in lines:
        if line.startswith("#EXTINF"):
            if current:
                blocks.append(current)
            current = [line]
        else:
            if current:
                current.append(line)

    if current:
        blocks.append(current)

    return blocks


def get_channel_name(block):
    """
    Lấy tên kênh nằm sau dấu phẩy cuối của dòng #EXTINF.
    Ví dụ:
    #EXTINF:-1 tvg-id="abc" group-title="VTV",VTV1 HD
    -> VTV1 HD
    """
    if not block:
        return ""

    extinf = block[0]

    if "," not in extinf:
        return ""

    return extinf.rsplit(",", 1)[1].strip()


def normalize_name(name: str) -> str:
    """
    Chuẩn hóa tên để match ổn định hơn nhưng KHÔNG thay đổi
    tên thực tế trong playlist đích.
    """
    name = name.strip().lower()

    # bỏ dấu tiếng Việt
    name = unicodedata.normalize("NFD", name)
    name = "".join(
        c for c in name
        if unicodedata.category(c) != "Mn"
    )

    # đ -> d
    name = name.replace("đ", "d")

    # chuẩn hóa một số ký tự
    name = name.replace("&", "and")

    # bỏ ký tự đặc biệt
    name = re.sub(r"[^a-z0-9]+", " ", name)

    # gom khoảng trắng
    name = re.sub(r"\s+", " ", name).strip()

    return name


def get_stream_url(block):
    """
    Lấy URL stream trong block.
    Bỏ qua các dòng metadata bắt đầu bằng #.
    """
    for line in reversed(block[1:]):
        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        return line

    return None


def replace_stream_url(block, new_url):
    """
    Giữ nguyên toàn bộ metadata / tên / group / logo...
    Chỉ thay URL stream cuối block.
    """
    new_block = list(block)

    for i in range(len(new_block) - 1, 0, -1):
        line = new_block[i].strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        new_block[i] = new_url
        return new_block

    # Nếu block chưa có URL thì thêm mới
    new_block.append(new_url)
    return new_block


def main():
    print("=" * 65)
    print("  UPDATE LINK PLAYLIST THEO VIETANH")
    print("=" * 65)

    print(f"\nĐang tải playlist nguồn:")
    print(f"  {SOURCE_URL}")

    try:
        source_text = fetch(SOURCE_URL)
    except Exception as e:
        print(f"\nLỗi tải playlist nguồn: {e}")
        sys.exit(1)

    source_blocks = split_blocks(source_text)

    print(f"Tìm thấy {len(source_blocks)} kênh ở playlist VietAnh.")

    # ---------------------------------------------------------
    # Tạo map:
    # normalized channel name -> stream URL
    # ---------------------------------------------------------

    source_map = {}

    duplicate_names = set()

    for block in source_blocks:
        name = get_channel_name(block)
        url = get_stream_url(block)

        if not name or not url:
            continue

        key = normalize_name(name)

        if not key:
            continue

        if key in source_map:
            duplicate_names.add(key)

        source_map[key] = {
            "name": name,
            "url": url
        }

    print(f"Tạo được {len(source_map)} link theo tên kênh.")

    if duplicate_names:
        print(
            f"Cảnh báo: có {len(duplicate_names)} tên kênh bị trùng "
            "trong playlist VietAnh."
        )

    # ---------------------------------------------------------
    # Đọc playlist đích
    # ---------------------------------------------------------

    try:
        with open(TARGET_FILE, "r", encoding="utf-8") as f:
            target_text = f.read()
    except FileNotFoundError:
        print(f"\nKhông tìm thấy file: {TARGET_FILE}")
        sys.exit(1)

    target_lines = target_text.splitlines()

    # Giữ nguyên header trước #EXTINF đầu tiên
    idx = 0
    header_lines = []

    while idx < len(target_lines):
        if target_lines[idx].startswith("#EXTINF"):
            break

        header_lines.append(target_lines[idx])
        idx += 1

    target_blocks = split_blocks(
        "\n".join(target_lines[idx:])
    )

    print(f"Playlist đích có {len(target_blocks)} kênh.")

    # ---------------------------------------------------------
    # Update
    # ---------------------------------------------------------

    updated_blocks = []

    updated_count = 0
    unchanged_count = 0
    not_found_count = 0
    same_url_count = 0

    print("\nĐang đối chiếu kênh...\n")

    for block in target_blocks:
        target_name = get_channel_name(block)

        if not target_name:
            updated_blocks.append(block)
            unchanged_count += 1
            continue

        key = normalize_name(target_name)

        source = source_map.get(key)

        if not source:
            print(f"[KHÔNG TÌM THẤY] {target_name}")
            updated_blocks.append(block)
            not_found_count += 1
            continue

        old_url = get_stream_url(block)
        new_url = source["url"]

        if old_url == new_url:
            print(f"[GIỮ NGUYÊN]    {target_name}")
            updated_blocks.append(block)
            same_url_count += 1
            continue

        updated_block = replace_stream_url(
            block,
            new_url
        )

        updated_blocks.append(updated_block)

        print(f"[UPDATE]         {target_name}")
        print(f"  Cũ : {old_url}")
        print(f"  Mới: {new_url}")

        updated_count += 1

    # ---------------------------------------------------------
    # Xuất playlist
    # ---------------------------------------------------------

    output_lines = list(header_lines)

    for block in updated_blocks:
        output_lines.extend(block)

    new_text = "\n".join(output_lines) + "\n"

    print("\n" + "=" * 65)

    if new_text == target_text:
        print("Không có thay đổi nào cần ghi.")
    else:
        with open(
            TARGET_FILE,
            "w",
            encoding="utf-8",
            newline="\n"
        ) as f:
            f.write(new_text)

        print(f"Đã cập nhật file: {TARGET_FILE}")

    print("=" * 65)
    print(f"Đã update link : {updated_count}")
    print(f"Link đã giống   : {same_url_count}")
    print(f"Không tìm thấy  : {not_found_count}")
    print(f"Block bỏ qua    : {unchanged_count}")
    print("=" * 65)


if __name__ == "__main__":
    main()
