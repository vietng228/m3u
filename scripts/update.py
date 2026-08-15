#!/usr/bin/env python3

import sys
import re
import requests
import unicodedata

SOURCE_URL = "http://tv.vietanhtv.top/tv"

TARGET_FILES = [
    "m3u.m3u",
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


def get_group_title(block):
    """
    Lấy group-title trong #EXTINF.
    """
    if not block:
        return ""

    extinf = block[0]

    match = re.search(
        r'group-title\s*=\s*"([^"]*)"',
        extinf,
        flags=re.IGNORECASE
    )

    if not match:
        return ""

    return match.group(1).strip()


def normalize_text(text: str) -> str:
    """
    Chuẩn hóa text để đối chiếu.

    Chỉ dùng cho việc match.
    Không làm thay đổi playlist thực tế.
    """
    text = text.strip().lower()

    # đ -> d
    text = text.replace("đ", "d")

    # Bỏ dấu tiếng Việt
    text = unicodedata.normalize("NFD", text)

    text = "".join(
        c for c in text
        if unicodedata.category(c) != "Mn"
    )

    # Chuẩn hóa &
    text = text.replace("&", "and")

    # Bỏ ký tự đặc biệt
    text = re.sub(r"[^a-z0-9]+", " ", text)

    # Gom khoảng trắng
    text = re.sub(r"\s+", " ", text).strip()

    return text


def normalize_name(name: str) -> str:
    return normalize_text(name)


def normalize_group(group: str) -> str:
    return normalize_text(group)


def build_source_map(source_text: str):
    """
    Tạo map theo:

        (group-title, tên kênh) -> block VietAnh

    Ví dụ:

        ("vtvcab", "on football")
        ("vtv", "vtv1")
        ("k", "k sport 1")

    Nhờ đó sẽ không lấy nhầm kênh cùng tên
    nhưng nằm ở group-title khác.
    """
    source_blocks = split_blocks(source_text)

    source_map = {}
    duplicate_keys = set()

    for block in source_blocks:
        name = get_channel_name(block)
        group = get_group_title(block)

        if not name:
            continue

        name_key = normalize_name(name)
        group_key = normalize_group(group)

        if not name_key:
            continue

        key = (
            group_key,
            name_key,
        )

        if key in source_map:
            duplicate_keys.add(key)

        source_map[key] = {
            "name": name,
            "group": group,
            "block": block,
        }

    return source_blocks, source_map, duplicate_keys


def merge_block(target_block, source_block):
    """
    Giữ nguyên DUY NHẤT dòng #EXTINF của target.

    Toàn bộ phần còn lại lấy từ VietAnh.

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

    # Giữ nguyên #EXTINF target
    # Lấy toàn bộ phần phía sau từ VietAnh
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

    # ---------------------------------------------------------
    # Lấy danh sách group-title có trong target
    # ---------------------------------------------------------

    target_groups = {}

    for block in target_blocks:
        group = get_group_title(block)

        if not group:
            continue

        group_key = normalize_group(group)

        if group_key:
            target_groups[group_key] = group

    print(f"Tổng số kênh : {len(target_blocks)}")
    print(f"Tổng số nhóm : {len(target_groups)}")

    if target_groups:
        print()
        print("Các nhóm trong playlist:")

        for group in target_groups.values():
            print(f"  - {group}")

    print()

    updated_blocks = []

    updated_count = 0
    same_count = 0
    not_found_count = 0

    # ---------------------------------------------------------
    # Đối chiếu từng kênh
    #
    # BẮT BUỘC:
    #
    #   1. group-title giống nhau
    #   2. tên kênh giống nhau
    #
    # Nếu chỉ giống tên nhưng khác group:
    # KHÔNG UPDATE
    # ---------------------------------------------------------

    for target_block in target_blocks:

        target_name = get_channel_name(target_block)
        target_group = get_group_title(target_block)

        if not target_name:
            updated_blocks.append(target_block)
            continue

        name_key = normalize_name(target_name)
        group_key = normalize_group(target_group)

        key = (
            group_key,
            name_key,
        )

        source = source_map.get(key)

        # -----------------------------------------------------
        # Không tìm thấy đúng group + tên
        # -----------------------------------------------------

        if not source:

            if target_group:
                print(
                    f"[KHÔNG TÌM THẤY] "
                    f"[{target_group}] {target_name}"
                )

            else:
                print(
                    f"[KHÔNG TÌM THẤY] "
                    f"{target_name}"
                )

            updated_blocks.append(target_block)

            not_found_count += 1
            continue

        source_block = source["block"]

        # -----------------------------------------------------
        # Tạo block mới
        #
        # #EXTINF        = TARGET
        # phần còn lại  = VIETANH
        # -----------------------------------------------------

        new_block = merge_block(
            target_block,
            source_block
        )

        # -----------------------------------------------------
        # Không có thay đổi
        # -----------------------------------------------------

        if new_block == target_block:

            if target_group:
                print(
                    f"[GIỮ NGUYÊN]     "
                    f"[{target_group}] {target_name}"
                )

            else:
                print(
                    f"[GIỮ NGUYÊN]     "
                    f"{target_name}"
                )

            updated_blocks.append(target_block)

            same_count += 1
            continue

        # -----------------------------------------------------
        # Có thay đổi
        # -----------------------------------------------------

        if target_group:
            print(
                f"[UPDATE]          "
                f"[{target_group}] {target_name}"
            )

        else:
            print(
                f"[UPDATE]          "
                f"{target_name}"
            )

        old_body = target_block[1:]
        new_body = source_block[1:]

        if old_body != new_body:
            print(
                "  Đồng bộ metadata + link từ VietAnh"
            )

        updated_blocks.append(new_block)

        updated_count += 1

    # ---------------------------------------------------------
    # Ghép lại playlist
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
    print("       UPDATE PLAYLIST THEO NHÓM KÊNH VIETANH")
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

    source_blocks, source_map, duplicate_keys = build_source_map(
        source_text
    )

    print(
        f"Tìm thấy {len(source_blocks)} block "
        "trong playlist VietAnh."
    )

    print(
        f"Tạo map được {len(source_map)} "
        "cặp nhóm + kênh."
    )

    if duplicate_keys:
        print()
        print(
            f"Cảnh báo: {len(duplicate_keys)} cặp "
            "group-title + tên kênh bị trùng."
        )

        print(
            "Nếu trùng hoàn toàn cả group và tên, "
            "sẽ dùng block xuất hiện sau cùng."
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
