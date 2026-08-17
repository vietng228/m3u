#!/usr/bin/env python3

import re
import sys
import unicodedata
from pathlib import Path

import requests


SOURCE_URL = "http://tv.vietanhtv.top/tv"

TARGET_FILE = "m3u.m3u"


def fetch(url: str) -> str:
    response = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    return response.text


def split_blocks(text: str):
    """Tách playlist thành các block bắt đầu bằng #EXTINF."""
    blocks = []
    current = []

    for line in text.splitlines():
        if line.startswith("#EXTINF"):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)

    if current:
        blocks.append(current)

    return blocks


def get_channel_name(block) -> str:
    """Lấy tên kênh nằm sau dấu phẩy cuối cùng của #EXTINF."""
    if not block or "," not in block[0]:
        return ""
    return block[0].rsplit(",", 1)[1].strip()


def get_group_title(block) -> str:
    """Lấy group-title trong #EXTINF."""
    if not block:
        return ""

    match = re.search(
        r'group-title\s*=\s*"([^"]*)"',
        block[0],
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def normalize_text(text: str, preserve_plus: bool = False) -> str:
    """Chuẩn hóa chuỗi chỉ để đối chiếu, không sửa dữ liệu playlist."""
    text = text.strip().lower().replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(
        char for char in text if unicodedata.category(char) != "Mn"
    )
    text = text.replace("&", "and")

    if preserve_plus:
        # Giữ ý nghĩa dấu cộng để ON SPORTS khác ON SPORTS+.
        text = text.replace("+", " plus ")

    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(name: str) -> str:
    return normalize_text(name, preserve_plus=True)


def normalize_group(group: str) -> str:
    return normalize_text(group)


def is_radio_block(block) -> bool:
    """Nhận diện block radio trong nguồn VietAnh để loại trước khi tạo map."""
    if not block:
        return False

    name = normalize_name(get_channel_name(block))
    group = normalize_group(get_group_title(block))
    extinf = normalize_text(block[0])

    if re.search(r"\b(radio|phat thanh|voice of vietnam)\b", group):
        return True
    if re.search(r"\b(radio|phat thanh|voice of vietnam)\b", extinf):
        return True

    if re.search(r"\bvov(?:\s*[1-9])?\b", name):
        return True
    if re.search(r"\b\d{2,3}(?:\s*\.\s*\d+)?\s*(?:fm|mhz)\b", name):
        return True
    if re.search(r"\bfm\b", name) or re.search(r"\bam\b", name):
        return True

    return False


def build_source_map(source_text: str):
    """Tạo map (group-title, tên kênh) -> block VietAnh, bỏ toàn bộ radio."""
    source_blocks = split_blocks(source_text)
    source_map = {}
    duplicate_keys = set()
    radio_count = 0

    for block in source_blocks:
        if is_radio_block(block):
            radio_count += 1
            continue

        name = get_channel_name(block)
        group = get_group_title(block)
        name_key = normalize_name(name)
        group_key = normalize_group(group)

        if not name_key:
            continue

        key = (group_key, name_key)
        if key in source_map:
            duplicate_keys.add(key)

        source_map[key] = {
            "name": name,
            "group": group,
            "block": block,
        }

    return source_blocks, source_map, duplicate_keys, radio_count


def merge_block(target_block, source_block):
    """Giữ nguyên #EXTINF target và thay toàn bộ body bằng body nguồn."""
    if not target_block:
        return source_block
    if not source_block:
        return target_block
    return [target_block[0], *source_block[1:]]


def update_target_file(target_file: str, source_map: dict):
    print()
    print("=" * 72)
    print(f" ĐANG XỬ LÝ: {target_file}")
    print("=" * 72)

    path = Path(target_file)
    try:
        target_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"[BỎ QUA] Không tìm thấy file: {target_file}")
        return dict(file=target_file, exists=False, changed=False,
                    updated=0, same=0, not_found=0)
    except Exception as error:
        print(f"[LỖI] Không đọc được {target_file}: {error}")
        return dict(file=target_file, exists=True, changed=False,
                    updated=0, same=0, not_found=0)

    target_lines = target_text.splitlines()
    first_block = next(
        (i for i, line in enumerate(target_lines) if line.startswith("#EXTINF")),
        len(target_lines),
    )
    header_lines = target_lines[:first_block]
    target_blocks = split_blocks("\n".join(target_lines[first_block:]))

    target_groups = {}
    for block in target_blocks:
        group = get_group_title(block)
        group_key = normalize_group(group)
        if group_key:
            target_groups[group_key] = group

    print(f"Tổng số kênh : {len(target_blocks)}")
    print(f"Tổng số nhóm : {len(target_groups)}")
    if target_groups:
        print("\nCác nhóm trong playlist:")
        for group in target_groups.values():
            print(f"  - {group}")

    updated_blocks = []
    updated_count = same_count = not_found_count = 0

    for target_block in target_blocks:
        target_name = get_channel_name(target_block)
        target_group = get_group_title(target_block)

        if not target_name:
            updated_blocks.append(target_block)
            continue

        key = (normalize_group(target_group), normalize_name(target_name))
        source = source_map.get(key)
        label = f"[{target_group}] {target_name}" if target_group else target_name

        if not source:
            print(f"[KHÔNG TÌM THẤY] {label}")
            updated_blocks.append(target_block)
            not_found_count += 1
            continue

        new_block = merge_block(target_block, source["block"])
        if new_block == target_block:
            print(f"[GIỮ NGUYÊN]     {label}")
            updated_blocks.append(target_block)
            same_count += 1
        else:
            print(f"[UPDATE]          {label}")
            print("  Đồng bộ metadata + link từ VietAnh")
            updated_blocks.append(new_block)
            updated_count += 1

    output_lines = list(header_lines)
    for block in updated_blocks:
        output_lines.extend(block)
    new_text = "\n".join(output_lines) + ("\n" if output_lines else "")
    normalized_old_text = target_text.replace("\r\n", "\n").replace("\r", "\n")
    changed = new_text != normalized_old_text

    print("\n" + "-" * 72)
    if not changed:
        print(f"{target_file}: Không có thay đổi.")
    else:
        try:
            path.write_text(new_text, encoding="utf-8", newline="\n")
            print(f"Đã cập nhật file: {target_file}")
        except Exception as error:
            print(f"[LỖI] Không ghi được {target_file}: {error}")
            changed = False

    print(f"\nĐã đồng bộ     : {updated_count}")
    print(f"Đã giống nguồn : {same_count}")
    print(f"Không tìm thấy : {not_found_count}")
    print("-" * 72)

    return dict(file=target_file, exists=True, changed=changed,
                updated=updated_count, same=same_count,
                not_found=not_found_count)


def main():
    print("=" * 72)
    print("       UPDATE PLAYLIST THEO NHÓM KÊNH VIETANH")
    print("=" * 72)
    print(f"\nNguồn:\n  {SOURCE_URL}\n")

    try:
        source_text = fetch(SOURCE_URL)
    except requests.RequestException as error:
        print(f"[LỖI] Không tải được playlist VietAnh:\n  {error}")
        sys.exit(1)
    except Exception as error:
        print(f"[LỖI] Có lỗi khi tải playlist:\n  {error}")
        sys.exit(1)

    source_blocks, source_map, duplicate_keys, radio_count = build_source_map(
        source_text
    )
    print(f"Tìm thấy {len(source_blocks)} block trong playlist VietAnh.")
    print(f"Đã loại {radio_count} block radio khỏi nguồn.")
    print(f"Tạo map được {len(source_map)} cặp nhóm + kênh TV.")

    if duplicate_keys:
        print(
            f"Cảnh báo: {len(duplicate_keys)} cặp group-title + tên kênh bị trùng."
        )
        print("Kênh trùng hoàn toàn sẽ dùng block xuất hiện sau cùng.")

    if not source_map:
        print("\n[LỖI] Playlist VietAnh không có dữ liệu TV hợp lệ.")
        print("Không thay đổi file nào.")
        sys.exit(1)

    # Xử lý trực tiếp 1 file đích
    result = update_target_file(TARGET_FILE, source_map)

    print("\n" + "=" * 72)
    print("                           TỔNG KẾT")
    print("=" * 72)
    print(f"File xử lý       : {result['file']}")
    print(f"Tồn tại          : {'Có' if result['exists'] else 'Không'}")
    print(f"Có thay đổi      : {'Có' if result['changed'] else 'Không'}")
    print(f"Kênh đồng bộ     : {result['updated']}")
    print(f"Kênh đã giống    : {result['same']}")
    print(f"Không tìm thấy   : {result['not_found']}")
    print("=" * 72)


if __name__ == "__main__":
    main()
