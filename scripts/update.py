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
    Tách playlist thành từng block bắt đầu bằng #EXTINF.

    Ví dụ:

    #EXTINF:-1 ...,VTV1 HD
    #EXTVLCOPT:http-referrer=...
    https://example.com/vtv1.m3u8
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
    Lấy tên kênh phía sau dấu phẩy cuối cùng của #EXTINF.

    Ví dụ:

    #EXTINF:-1 tvg-id="vtv1" group-title="VTV",VTV1 HD

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
    Chuẩn hóa tên chỉ để đối chiếu.

    Không thay đổi tên thực tế trong playlist đích.
    """
    name = name.strip().lower()

    # Xử lý riêng chữ đ trước khi loại dấu
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


def get_stream_url(block):
    """
    Tìm URL stream cuối cùng trong block.

    Các dòng bắt đầu bằng # được xem là metadata.
    """
    if not block:
        return None

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
    Chỉ thay URL stream.

    Giữ nguyên:
    - tên kênh
    - group-title
    - tvg-id
    - tvg-logo
    - catchup
    - user-agent
    - referrer
    - metadata khác
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

    # Nếu block không có URL thì thêm URL mới
    new_block.append(new_url)

    return new_block


def build_source_map(source_text: str):
    """
    Tạo bảng:

    tên kênh đã chuẩn hóa -> thông tin kênh VietAnh
    """
    source_blocks = split_blocks(source_text)

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
            "url": url,
        }

    return source_blocks, source_map, duplicate_names


def update_target_file(target_file: str, source_map: dict):
    print()
    print("=" * 70)
    print(f" ĐANG XỬ LÝ: {target_file}")
    print("=" * 70)

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
            "ignored": 0,
        }

    except Exception as e:
        print(f"[LỖI] Không thể đọc {target_file}: {e}")

        return {
            "file": target_file,
            "exists": True,
            "changed": False,
            "updated": 0,
            "same": 0,
            "not_found": 0,
            "ignored": 0,
        }

    target_lines = target_text.splitlines()

    # ---------------------------------------------------------
    # Giữ nguyên phần header trước #EXTINF đầu tiên
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

    print(f"Tổng số kênh trong file: {len(target_blocks)}")
    print()

    updated_blocks = []

    updated_count = 0
    same_url_count = 0
    not_found_count = 0
    ignored_count = 0

    # ---------------------------------------------------------
    # Đối chiếu từng kênh
    # ---------------------------------------------------------

    for block in target_blocks:
        target_name = get_channel_name(block)

        if not target_name:
            updated_blocks.append(block)
            ignored_count += 1
            continue

        key = normalize_name(target_name)

        source = source_map.get(key)

        # Không có kênh tương ứng trên VietAnh
        if not source:
            print(f"[KHÔNG TÌM THẤY] {target_name}")

            updated_blocks.append(block)

            not_found_count += 1
            continue

        old_url = get_stream_url(block)
        new_url = source["url"]

        # Link đã giống nguồn
        if old_url == new_url:
            print(f"[GIỮ NGUYÊN]     {target_name}")

            updated_blocks.append(block)

            same_url_count += 1
            continue

        # Thay URL nhưng giữ nguyên metadata
        updated_block = replace_stream_url(
            block,
            new_url
        )

        updated_blocks.append(updated_block)

        print(f"[UPDATE]          {target_name}")
        print(f"  Cũ : {old_url}")
        print(f"  Mới: {new_url}")

        updated_count += 1

    # ---------------------------------------------------------
    # Tạo playlist mới
    # ---------------------------------------------------------

    output_lines = list(header_lines)

    for block in updated_blocks:
        output_lines.extend(block)

    new_text = "\n".join(output_lines)

    # Đảm bảo kết thúc bằng newline
    if new_text:
        new_text += "\n"

    changed = new_text != target_text

    print()
    print("-" * 70)

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
            print(f"[LỖI] Không thể ghi file {target_file}: {e}")

            changed = False

    print()
    print(f"Đã update link : {updated_count}")
    print(f"Link đã giống  : {same_url_count}")
    print(f"Không tìm thấy : {not_found_count}")
    print(f"Block bỏ qua   : {ignored_count}")
    print("-" * 70)

    return {
        "file": target_file,
        "exists": True,
        "changed": changed,
        "updated": updated_count,
        "same": same_url_count,
        "not_found": not_found_count,
        "ignored": ignored_count,
    }


def main():
    print("=" * 70)
    print("        UPDATE LINK PLAYLIST THEO VIETANH")
    print("=" * 70)

    print()
    print("Playlist nguồn:")
    print(f"  {SOURCE_URL}")
    print()

    # ---------------------------------------------------------
    # Download nguồn duy nhất 1 lần
    # ---------------------------------------------------------

    try:
        source_text = fetch(SOURCE_URL)

    except requests.RequestException as e:
        print(f"[LỖI] Không tải được playlist VietAnh:")
        print(f"  {e}")
        sys.exit(1)

    except Exception as e:
        print(f"[LỖI] Có lỗi khi tải playlist:")
        print(f"  {e}")
        sys.exit(1)

    # ---------------------------------------------------------
    # Phân tích playlist VietAnh
    # ---------------------------------------------------------

    source_blocks, source_map, duplicate_names = build_source_map(
        source_text
    )

    print(f"Tìm thấy {len(source_blocks)} block ở playlist VietAnh.")
    print(f"Tạo được {len(source_map)} kênh có URL hợp lệ.")

    if duplicate_names:
        print(
            f"Cảnh báo: có {len(duplicate_names)} tên kênh "
            "bị trùng trong nguồn VietAnh."
        )

        print(
            "Với tên trùng, script sẽ sử dụng link của "
            "block xuất hiện sau cùng."
        )

    # Không có dữ liệu thì không động vào playlist đích
    if not source_map:
        print()
        print("[LỖI] Playlist VietAnh không có kênh hợp lệ.")
        print("Không cập nhật bất kỳ file nào.")
        sys.exit(1)

    # ---------------------------------------------------------
    # Update tất cả playlist
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

    total_changed_files = sum(
        1
        for r in results
        if r["changed"]
    )

    total_existing_files = sum(
        1
        for r in results
        if r["exists"]
    )

    print()
    print("=" * 70)
    print("                         TỔNG KẾT")
    print("=" * 70)

    print(
        f"File tìm thấy    : "
        f"{total_existing_files}/{len(TARGET_FILES)}"
    )

    print(
        f"File có thay đổi : "
        f"{total_changed_files}"
    )

    print(
        f"Tổng link update : "
        f"{total_updated}"
    )

    print(
        f"Link đã giống    : "
        f"{total_same}"
    )

    print(
        f"Không tìm thấy   : "
        f"{total_not_found}"
    )

    print("=" * 70)


if __name__ == "__main__":
    main()
