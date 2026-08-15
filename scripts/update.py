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
    Tách playlist thành các block bắt đầu từ #EXTINF.
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
    Lấy tên kênh nằm sau dấu phẩy cuối cùng của #EXTINF.
    """
    if not block:
        return ""

    extinf = block[0]

    if "," not in extinf:
        return ""

    return extinf.rsplit(",", 1)[1].strip()


def normalize_name(name: str) -> str:
    """
    Chuẩn hóa tên chỉ để match.
    Không làm thay đổi tên trong playlist đích.
    """
    name = name.strip().lower()

    # đ -> d
    name = name.replace("đ", "d")

    # Bỏ dấu tiếng Việt
    name = unicodedata.normalize("NFD", name)

    name = "".join(
        c for c in name
        if unicodedata.category(c) != "Mn"
    )

    # Chuẩn hóa &
    name = name.replace("&", "and")

    # Bỏ ký tự đặc biệt
    name = re.sub(r"[^a-z0-9]+", " ", name)

    # Gom khoảng trắng
    name = re.sub(r"\s+", " ", name).strip()

    return name


def build_source_map(source_text: str):
    """
    Tạo map:

        tên kênh chuẩn hóa -> block đầy đủ của VietAnh
    """
    source_blocks = split_blocks(source_text)

    source_map = {}
    duplicate_names = set()

    for block in source_blocks:
        name = get_channel_name(block)

        if not name:
            continue

        key = normalize_name(name)

        if not key:
            continue

        if key in source_map:
            duplicate_names.add(key)

        source_map[key] = {
            "name": name,
            "block": block,
        }

    return source_blocks, source_map, duplicate_names


def merge_block(target_block, source_block):
    """
    Giữ nguyên DUY NHẤT dòng #EXTINF của target.

    Toàn bộ phần còn lại lấy từ source VietAnh.

    Target:
        #EXTINF...
        metadata cũ
        URL cũ

    Source:
        #EXTINF...
        #EXTVLCOPT...
        #KODIPROP...
        license_key mới
        URL mới

    Kết quả:
        #EXTINF của TARGET
        toàn bộ phần còn lại của SOURCE
    """

    if not target_block:
        return source_block

    if not source_block:
        return target_block

    new_block = [target_block[0]]

    # Lấy toàn bộ phần sau #EXTINF từ VietAnh
    new_block.extend(source_block[1:])

    return new_block


def update_target_file(target_file: str, source_map: dict):
    print()
    print("=" * 72)
    print(f" ĐANG XỬ LÝ: {target_file}")
    print("=" * 72)

    try:
        with open(target_file, "r", encoding="utf-8") as f:
            target_text = f.read()

    except FileNotFoundError:
        print(f"[BỎ QUA] Không tìm thấy file: {target_file}")

        return {
            "file": target_file,
            "exists": False,
            "changed": False,
            "updated": 0,
            "same": 0,
            "not_found": 0,
        }

    except Exception as e:
        print(f"[LỖI] Không đọc được {target_file}: {e}")

        return {
            "file": target_file,
            "exists": True,
            "changed": False,
            "updated": 0,
            "same": 0,
            "not_found": 0,
        }

    target_lines = target_text.splitlines()

    # ---------------------------------------------------------
    # Giữ nguyên header trước #EXTINF đầu tiên
    # ---------------------------------------------------------

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

    print(f"Tổng số kênh: {len(target_blocks)}")
    print()

    updated_blocks = []

    updated_count = 0
    same_count = 0
    not_found_count = 0

    # ---------------------------------------------------------
    # Đối chiếu từng kênh
    # ---------------------------------------------------------

    for target_block in target_blocks:

        target_name = get_channel_name(target_block)

        if not target_name:
            updated_blocks.append(target_block)
            continue

        key = normalize_name(target_name)

        source = source_map.get(key)

        # Không tìm thấy bên VietAnh
        if not source:
            print(f"[KHÔNG TÌM THẤY] {target_name}")

            updated_blocks.append(target_block)

            not_found_count += 1
            continue

        source_block = source["block"]

        # Tạo block mới:
        # giữ #EXTINF target
        # lấy mọi thứ khác từ VietAnh
        new_block = merge_block(
            target_block,
            source_block
        )

        if new_block == target_block:
            print(f"[GIỮ NGUYÊN]     {target_name}")

            updated_blocks.append(target_block)

            same_count += 1
            continue

        print(f"[UPDATE]          {target_name}")

        # Hiển thị thay đổi phần kỹ thuật
        old_body = target_block[1:]
        new_body = source_block[1:]

        if old_body != new_body:
            print("  Đồng bộ metadata + link từ VietAnh")

        updated_blocks.append(new_block)

        updated_count += 1

    # ---------------------------------------------------------
    # Ghép playlist mới
    # ---------------------------------------------------------

    output_lines = list(header_lines)

    for block in updated_blocks:
        output_lines.extend(block)

    new_text = "\n".join(output_lines)

    if new_text:
        new_text += "\n"

    changed = new_text != target_text

    print()
    print("-" * 72)

    if not changed:
        print(f"{target_file}: Không có thay đổi.")

    else:
        try:
            with open(
                target_file,
                "w",
                encoding="utf-8",
                newline="\n"
            ) as f:
                f.write(new_text)

            print(f"Đã cập nhật file: {target_file}")

        except Exception as e:
            print(f"[LỖI] Không ghi được {target_file}: {e}")
            changed = False

    print()
    print(f"Đã đồng bộ     : {updated_count}")
    print(f"Đã giống nguồn : {same_count}")
    print(f"Không tìm thấy : {not_found_count}")
    print("-" * 72)

    return {
        "file": target_file,
        "exists": True,
        "changed": changed,
        "updated": updated_count,
        "same": same_count,
        "not_found": not_found_count,
    }


def main():
    print("=" * 72)
    print("          UPDATE PLAYLIST THEO VIETANH")
    print("=" * 72)

    print()
    print("Nguồn:")
    print(f"  {SOURCE_URL}")
    print()

    # ---------------------------------------------------------
    # Tải VietAnh duy nhất 1 lần
    # ---------------------------------------------------------

    try:
        source_text = fetch(SOURCE_URL)

    except requests.RequestException as e:
        print("[LỖI] Không tải được playlist VietAnh:")
        print(f"  {e}")
        sys.exit(1)

    except Exception as e:
        print("[LỖI] Có lỗi khi tải playlist:")
        print(f"  {e}")
        sys.exit(1)

    # ---------------------------------------------------------
    # Phân tích nguồn
    # ---------------------------------------------------------

    source_blocks, source_map, duplicate_names = build_source_map(
        source_text
    )

    print(
        f"Tìm thấy {len(source_blocks)} block "
        "trong playlist VietAnh."
    )

    print(
        f"Tạo map được {len(source_map)} kênh."
    )

    if duplicate_names:
        print(
            f"Cảnh báo: {len(duplicate_names)} tên kênh "
            "bị trùng trong nguồn."
        )

        print(
            "Kênh trùng sẽ dùng block xuất hiện sau cùng."
        )

    if not source_map:
        print()
        print("[LỖI] Playlist VietAnh không có dữ liệu hợp lệ.")
        print("Không thay đổi file nào.")
        sys.exit(1)

    # ---------------------------------------------------------
    # Update toàn bộ target
    # ---------------------------------------------------------

    results = []

    for target_file in TARGET_FILES:
        result = update_target_file(
            target_file,
            source_map
        )

        results.append(result)

    # ---------------------------------------------------------
    # Tổng kết
    # ---------------------------------------------------------

    existing_files = sum(
        1
        for r in results
        if r["exists"]
    )

    changed_files = sum(
        1
        for r in results
        if r["changed"]
    )

    total_updated = sum(
        r["updated"]
        for r in results
    )

    total_same = sum(
        r["same"]
        for r in results
    )

    total_not_found = sum(
        r["not_found"]
        for r in results
    )

    print()
    print("=" * 72)
    print("                           TỔNG KẾT")
    print("=" * 72)

    print(
        f"File tìm thấy    : "
        f"{existing_files}/{len(TARGET_FILES)}"
    )

    print(
        f"File có thay đổi : "
        f"{changed_files}"
    )

    print(
        f"Kênh đồng bộ     : "
        f"{total_updated}"
    )

    print(
        f"Kênh đã giống    : "
        f"{total_same}"
    )

    print(
        f"Không tìm thấy   : "
        f"{total_not_found}"
    )

    print("=" * 72)


if __name__ == "__main__":
    main()
