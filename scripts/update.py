#!/usr/bin/env python3

import re
import sys
import unicodedata
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from pathlib import Path

import requests


SOURCE_URL = os.environ.get("UPSTREAM_PLAYLIST_URL", "")

TARGET_FILE = "m3u.m3u"

WORKER_BASE_URL = "https://vietmitv-stream.viet-ng228.workers.dev"

FULL_COPY_GROUPS = ("VTV", "HTV", "Địa Phương", "Quốc Tế")
VTV_CAB_GROUP = "VTVcab"
INTERNATIONAL_GROUP = "Quốc Tế"


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
        text = text.replace("+", " plus ")

    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(name: str) -> str:
    return normalize_text(name, preserve_plus=True)


def normalize_group(group: str) -> str:
    return normalize_text(group).replace(" ", "")


def is_radio_block(block) -> bool:
    """Nhận diện block radio trong nguồn upstream để loại trước khi tạo map."""
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
    """Tạo map (group-title, tên kênh) -> block upstream, bỏ toàn bộ radio."""
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
    """Giữ EXTINF, dùng metadata nguồn và URL Worker cố định cho stream/license."""
    if not target_block:
        return source_block
    if not source_block:
        return target_block
    group = get_group_title(source_block)
    name = get_channel_name(source_block)
    query = urlencode({"group": group, "name": name})
    stream_url = f"{WORKER_BASE_URL}/channel?{query}"
    license_url = f"{stream_url}&kind=license"

    body = []
    for line in source_block[1:]:
        if re.match(
            r"^#KODIPROP:inputstream\.adaptive\.license_key=https?://",
            line,
            flags=re.IGNORECASE,
        ):
            body.append(re.sub(
                r"=https?://[^|\s]+", f"={license_url}", line, count=1
            ))
        elif re.match(r"^https?://", line, flags=re.IGNORECASE):
            body.append(stream_url)
        else:
            body.append(line)

    return [target_block[0], *body]


def get_logo(block) -> str:
    if not block:
        return ""
    match = re.search(r'tvg-logo\s*=\s*"([^"]*)"', block[0], re.IGNORECASE)
    return match.group(1).strip() if match else ""


def get_stream_url(block) -> str:
    """Lấy URL stream cuối block, bỏ phần header nối sau dấu |."""
    for line in reversed(block[1:]):
        if re.match(r"^https?://", line, re.IGNORECASE):
            return line.split("|", 1)[0].strip()
    return ""


def stream_headers(block) -> dict:
    headers = {"User-Agent": "Mozilla/5.0"}
    for line in block[1:]:
        if line.lower().startswith("#extvlcopt:http-user-agent="):
            headers["User-Agent"] = line.split("=", 1)[1].strip()
        elif line.lower().startswith("#extvlcopt:http-referrer="):
            headers["Referer"] = line.split("=", 1)[1].strip()
    return headers


def is_stream_reachable(block) -> bool:
    """GET thật và xác nhận manifest/media, thử lại một lần khi lỗi."""
    url = get_stream_url(block)
    if not url:
        return False

    headers = stream_headers(block)
    headers["Range"] = "bytes=0-65535"
    for attempt in range(2):
        try:
            with requests.get(
                url,
                headers=headers,
                timeout=(8, 15),
                allow_redirects=True,
                stream=True,
            ) as response:
                if response.status_code not in (200, 206):
                    raise requests.HTTPError(str(response.status_code))
                sample = next(response.iter_content(65536), b"")
                content_type = response.headers.get("content-type", "").lower()
                lower_url = response.url.lower().split("?", 1)[0]
                if lower_url.endswith(".m3u8") or "mpegurl" in content_type:
                    return b"#EXTM3U" in sample.upper()
                if lower_url.endswith(".mpd") or "dash+xml" in content_type:
                    return b"<MPD" in sample.upper()
                return bool(sample)
        except (requests.RequestException, StopIteration):
            if attempt == 0:
                time.sleep(1)
    return False


def filter_reachable_international(source_map: dict) -> dict:
    """Chỉ giữ kênh Quốc Tế vượt kiểm tra phát, chạy song song để Sync nhanh."""
    international = normalize_group(INTERNATIONAL_GROUP)
    candidates = {
        key: source for key, source in source_map.items()
        if key[0] == international
    }
    reachable = set()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(is_stream_reachable, source["block"]): key
            for key, source in candidates.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                if future.result():
                    reachable.add(key)
                else:
                    print(f"[REMOVE BROKEN] [International] {candidates[key]['name']}")
            except Exception as error:
                print(f"[REMOVE BROKEN] [International] {candidates[key]['name']}: {error}")

    return {
        key: source for key, source in source_map.items()
        if key[0] != international or key in reachable
    }


def sanitize_extinf(extinf: str, logo: str = "") -> str:
    """Giữ tvg-id, group-title, logo nguồn và tên kênh."""
    tvg_id_match = re.search(r'tvg-id\s*=\s*"([^"]*)"', extinf, re.IGNORECASE)
    group = get_group_title([extinf])
    name = get_channel_name([extinf])
    attributes = []
    if tvg_id_match and tvg_id_match.group(1).strip():
        attributes.append(f'tvg-id="{tvg_id_match.group(1).strip()}"')
    if group:
        attributes.append(f'group-title="{group}"')
    if logo:
        attributes.append(f'tvg-logo="{logo}"')
    suffix = f" {' '.join(attributes)}" if attributes else ""
    return f"#EXTINF:-1{suffix},{name}"


def sanitize_target_file(target_file: str) -> None:
    """Chuẩn hóa metadata EXTINF mà không cần tải upstream."""
    path = Path(target_file)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    first_block = next(
        (i for i, line in enumerate(lines) if line.startswith("#EXTINF")),
        len(lines),
    )
    output = lines[:first_block]
    for block in split_blocks("\n".join(lines[first_block:])):
        if block:
            block[0] = sanitize_extinf(block[0])
        output.extend(block)
    path.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")


def update_existing_channels(target_blocks, source_map):
    """VTVcab chỉ dùng Worker khi block nguồn có DRM key động."""
    vtvcab = normalize_group(VTV_CAB_GROUP)
    output = []

    for target_block in target_blocks:
        group = get_group_title(target_block)
        name = get_channel_name(target_block)
        key = (normalize_group(group), normalize_name(name))
        source = source_map.get(key)

        if not source:
            output.append(target_block)
            continue

        source_block = source["block"]
        extinf = sanitize_extinf(target_block[0], get_logo(source_block))
        metadata_target = [extinf, *target_block[1:]]
        if normalize_group(group) == vtvcab:
            has_key = any(
                re.match(r"^#KODIPROP:inputstream\.adaptive\.license_key=https?://", line, re.I)
                for line in source_block[1:]
            )
            output.append(merge_block(metadata_target, source_block) if has_key else [extinf, *source_block[1:]])
        else:
            output.append(metadata_target)

    return output


def replace_full_groups(target_blocks, source_map):
    """Chép trọn VTV/HTV/Địa Phương/Quốc Tế; không lấy In The Box."""
    selected = {normalize_group(group) for group in FULL_COPY_GROUPS}
    source_groups = {key: [] for key in selected}
    for source in source_map.values():
        block = source["block"]
        key = normalize_group(get_group_title(block))
        if key in source_groups:
            source_groups[key].append([sanitize_extinf(block[0], get_logo(block)), *block[1:]])

    output, inserted = [], set()
    for block in target_blocks:
        key = normalize_group(get_group_title(block))
        if key not in selected:
            output.append(block)
        elif key not in inserted:
            output.extend(source_groups[key])
            inserted.add(key)
    for key in selected - inserted:
        output.extend(source_groups[key])
    return output


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

    updated_blocks = replace_full_groups(
        update_existing_channels(target_blocks, source_map), source_map
    )
    updated_count = sum(
        (normalize_group(get_group_title(block)), normalize_name(get_channel_name(block)))
        in source_map
        for block in target_blocks
    )
    same_count = 0
    not_found_count = len(target_blocks) - updated_count
    print(f"[GIỮ DANH SÁCH] {len(target_blocks)} kênh, không thêm hoặc xóa kênh")

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
    if "--sanitize-only" in sys.argv:
        sanitize_target_file(TARGET_FILE)
        print(f"Đã chuẩn hóa metadata: {TARGET_FILE}")
        return

    print("=" * 72)
    print("       UPDATE PLAYLIST THEO NHÓM KÊNH UPSTREAM")
    print("=" * 72)
    print("\nNguồn upstream: đã cấu hình qua secret.\n")

    try:
        source_text = fetch(SOURCE_URL)
    except requests.RequestException as error:
        print(f"[LỖI] Không tải được playlist upstream:\n  {error}")
        sys.exit(1)
    except Exception as error:
        print(f"[LỖI] Có lỗi khi tải playlist:\n  {error}")
        sys.exit(1)

    source_blocks, source_map, duplicate_keys, radio_count = build_source_map(
        source_text
    )
    print(f"Tìm thấy {len(source_blocks)} block trong playlist upstream.")
    print(f"Đã loại {radio_count} block radio khỏi nguồn.")
    print(f"Tạo map được {len(source_map)} cặp nhóm + kênh TV.")

    if duplicate_keys:
        print(
            f"Cảnh báo: {len(duplicate_keys)} cặp group-title + tên kênh bị trùng."
        )
        print("Kênh trùng hoàn toàn sẽ dùng block xuất hiện sau cùng.")

    if not source_map:
        print("\n[LỖI] Playlist upstream không có dữ liệu TV hợp lệ.")
        print("Không thay đổi file nào.")
        sys.exit(1)

    source_map = filter_reachable_international(source_map)

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
