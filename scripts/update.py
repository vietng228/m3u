#!/usr/bin/env python3

import base64
import binascii
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode

import requests


SOURCE_URL = os.environ.get("UPSTREAM_PLAYLIST_URL", "")
TVG_ID_SOURCE_URL = os.environ.get(
    "TVG_ID_PLAYLIST_URL",
    os.environ.get(
        "INTERNATIONAL_PLAYLIST_URL",
        "https://raw.githubusercontent.com/vuminhthanh12/vuminhthanh12/main/vmttv",
    ),
)
TARGET_FILE = "m3u.m3u"
WORKER_BASE_URL = "https://vietmitv-stream.viet-ng228.workers.dev"

VTV_CAB_GROUP = "VTVcab"
INTERNATIONAL_GROUP = "Quốc Tế"
TVG_ID_SOURCE_GROUPS = {"Quốc Tế", "In The Box", "Địa Phương"}
LICENSE_KEY_PREFIX = "#KODIPROP:inputstream.adaptive.license_key="


def fetch(url: str) -> str:
    if not url:
        raise RuntimeError("UPSTREAM_PLAYLIST_URL chưa được cấu hình")

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


def get_extinf_lines(text: str):
    """Lấy nguyên văn mọi dòng #EXTINF để dùng làm khóa an toàn."""
    return [line for line in text.splitlines() if line.startswith("#EXTINF")]


def get_channel_name(block) -> str:
    if not block or "," not in block[0]:
        return ""
    return block[0].rsplit(",", 1)[1].strip()


def get_group_title(block) -> str:
    if not block:
        return ""

    match = re.search(
        r'group-title\s*=\s*"([^"]*)"',
        block[0],
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def get_tvg_id(block) -> str:
    if not block:
        return ""

    match = re.search(
        r'tvg-id\s*=\s*"([^"]*)"',
        block[0],
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def normalize_text(text: str, preserve_plus: bool = False) -> str:
    """Chỉ chuẩn hóa để đối chiếu; tuyệt đối không dùng để ghi lại metadata."""
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


def normalize_tvg_id(tvg_id: str) -> str:
    return tvg_id.strip().lower()


def decode_base64url_hex(value: str) -> str:
    """Đổi một ClearKey Base64URL 128-bit sang hex mà app đang hỗ trợ."""
    padding = "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(value + padding)
    if len(decoded) != 16:
        raise ValueError("ClearKey phải dài đúng 16 byte")
    return decoded.hex()


def normalize_inline_clearkey(line: str) -> str:
    """Đổi JWK JSON thành kid:key hex; giữ nguyên URL/hex sẵn có."""
    if not line.lower().startswith(LICENSE_KEY_PREFIX.lower()):
        return line

    value = line[len(LICENSE_KEY_PREFIX):].strip()
    if not value.startswith("{"):
        return line

    try:
        payload = json.loads(value)
        keys = payload.get("keys")
        if not isinstance(keys, list) or not keys:
            return line

        pairs = []
        for item in keys:
            if not isinstance(item, dict):
                return line
            kid = item.get("kid")
            key = item.get("k")
            if not isinstance(kid, str) or not isinstance(key, str):
                return line
            pairs.append(
                f"{decode_base64url_hex(kid)}:{decode_base64url_hex(key)}"
            )

        return LICENSE_KEY_PREFIX + ",".join(pairs)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        return line


def is_radio_block(block) -> bool:
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
    """Map chính xác theo (group-title, tên kênh). Không fallback theo tên."""
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

        if not name_key or not group_key:
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


def build_source_id_map(source_text: str):
    """Map nguồn vmttv theo tvg-id, không dựa vào tên hoặc group-title."""
    source_map = {}
    duplicate_ids = set()

    for block in split_blocks(source_text):
        if is_radio_block(block):
            continue

        tvg_id = normalize_tvg_id(get_tvg_id(block))
        if not tvg_id:
            continue

        if tvg_id in source_map:
            duplicate_ids.add(tvg_id)

        source_map[tvg_id] = {
            "name": get_channel_name(block),
            "group": get_group_title(block),
            "block": block,
        }

    return source_map, duplicate_ids


def get_stream_url(block) -> str:
    for line in reversed(block[1:]):
        if re.match(r"^https?://", line, re.IGNORECASE):
            return line.split("|", 1)[0].strip()
    return ""


def stream_headers(block) -> dict:
    headers = {"User-Agent": "Mozilla/5.0"}

    for line in block[1:]:
        lower = line.lower()
        if lower.startswith("#extvlcopt:http-user-agent="):
            headers["User-Agent"] = line.split("=", 1)[1].strip()
        elif lower.startswith("#extvlcopt:http-referrer="):
            headers["Referer"] = line.split("=", 1)[1].strip()

    return headers


def is_stream_reachable(block) -> bool:
    """Chỉ dùng để lọc nguồn Quốc Tế; không thay đổi metadata file đích."""
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
    international = normalize_group(INTERNATIONAL_GROUP)
    candidates = {
        key: source
        for key, source in source_map.items()
        if key[0] == international
    }

    if not candidates:
        return source_map

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
                    print(
                        f"[BỎ LINK LỖI] [Quốc Tế] "
                        f"{candidates[key]['name']}"
                    )
            except Exception as error:
                print(
                    f"[BỎ LINK LỖI] [Quốc Tế] "
                    f"{candidates[key]['name']}: {error}"
                )

    return {
        key: source
        for key, source in source_map.items()
        if key[0] != international or key in reachable
    }


def has_dynamic_license_url(block) -> bool:
    return any(
        re.match(
            r"^#KODIPROP:inputstream\.adaptive\.license_key=https?://",
            line,
            flags=re.IGNORECASE,
        )
        for line in block[1:]
    )


def build_worker_body(source_block):
    """Dùng URL Worker cố định cho stream/license động, không đụng #EXTINF."""
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
            body.append(
                re.sub(
                    r"=https?://[^|\s]+",
                    f"={license_url}",
                    line,
                    count=1,
                )
            )
        elif re.match(r"^https?://", line, flags=re.IGNORECASE):
            body.append(stream_url)
        else:
            body.append(line)

    return body


def merge_channel(target_block, source_block):
    """
    QUY TẮC BẮT BUỘC:
    - Giữ NGUYÊN target_block[0] (#EXTINF) từng ký tự.
    - Không lấy tvg-logo/tvg-id/group-title/tên kênh từ upstream.
    - Chỉ thay phần body phía dưới #EXTINF.
    """
    if not target_block or not source_block:
        return target_block

    target_extinf = target_block[0]
    group = normalize_group(get_group_title(target_block))

    if group == normalize_group(VTV_CAB_GROUP) and has_dynamic_license_url(source_block):
        source_body = build_worker_body(source_block)
    else:
        source_body = [normalize_inline_clearkey(line) for line in source_block[1:]]

    return [target_extinf, *source_body]


def update_target_file(
    target_file: str,
    source_map: dict,
    tvg_id_source_map: dict | None = None,
):
    print()
    print("=" * 72)
    print(f" ĐANG XỬ LÝ: {target_file}")
    print("=" * 72)

    path = Path(target_file)

    try:
        target_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"[BỎ QUA] Không tìm thấy file: {target_file}")
        return dict(
            file=target_file,
            exists=False,
            changed=False,
            updated=0,
            same=0,
            not_found=0,
        )
    except Exception as error:
        print(f"[LỖI] Không đọc được {target_file}: {error}")
        return dict(
            file=target_file,
            exists=True,
            changed=False,
            updated=0,
            same=0,
            not_found=0,
        )

    target_lines = target_text.splitlines()
    first_block = next(
        (
            i
            for i, line in enumerate(target_lines)
            if line.startswith("#EXTINF")
        ),
        len(target_lines),
    )

    header_lines = target_lines[:first_block]
    target_blocks = split_blocks("\n".join(target_lines[first_block:]))
    tvg_id_source_groups = {
        normalize_group(item) for item in TVG_ID_SOURCE_GROUPS
    }

    updated_blocks = []
    updated_count = 0
    same_count = 0
    not_found_count = 0

    for target_block in target_blocks:
        group = get_group_title(target_block)
        name = get_channel_name(target_block)
        group_key = normalize_group(group)
        tvg_id = normalize_tvg_id(get_tvg_id(target_block))

        if (
            group_key in tvg_id_source_groups
            and tvg_id
        ):
            source = (tvg_id_source_map or {}).get(tvg_id)
            match_label = f"tvg-id={tvg_id}"
        else:
            key = (group_key, normalize_name(name))
            source = source_map.get(key)
            match_label = "group-title + tên"

        if not source:
            updated_blocks.append(target_block)
            not_found_count += 1
            print(f"[KHÔNG TÌM THẤY] [{group}] {name} ({match_label})")
            continue

        new_block = merge_channel(target_block, source["block"])

        if new_block == target_block:
            same_count += 1
            print(f"[GIỮ NGUYÊN]     [{group}] {name}")
        else:
            updated_count += 1
            print(f"[UPDATE BODY]    [{group}] {name}")

        # Chốt an toàn: EXTINF trước/sau sync phải giống tuyệt đối.
        if new_block[0] != target_block[0]:
            raise RuntimeError(
                f"EXTINF bị thay đổi ngoài ý muốn: [{group}] {name}"
            )

        updated_blocks.append(new_block)

    output_lines = list(header_lines)
    for block in updated_blocks:
        output_lines.extend(block)

    new_text = "\n".join(output_lines) + ("\n" if output_lines else "")
    original_extinf = get_extinf_lines(target_text)
    output_extinf = get_extinf_lines(new_text)

    # Fail-safe toàn file: không ghi nếu bất kỳ #EXTINF nào bị sửa, thêm,
    # xóa hoặc đổi thứ tự. Kiểm tra độc lập với merge_channel để bảo vệ cả
    # những thay đổi vô tình trong quá trình dựng lại playlist.
    if output_extinf != original_extinf:
        raise RuntimeError(
            "Fail-safe: danh sách #EXTINF đã thay đổi; hủy ghi m3u.m3u"
        )

    normalized_old_text = (
        target_text.replace("\r\n", "\n").replace("\r", "\n")
    )
    changed = new_text != normalized_old_text

    print("\n" + "-" * 72)
    print(f"Giữ nguyên danh sách : {len(target_blocks)} kênh")
    print("Giữ nguyên EXTINF    : 100% (logo/tvg-id/group/name)")

    if not changed:
        print(f"{target_file}: Không có thay đổi.")
    else:
        try:
            path.write_text(
                new_text,
                encoding="utf-8",
                newline="\n",
            )
            print(f"Đã cập nhật file: {target_file}")
        except Exception as error:
            print(f"[LỖI] Không ghi được {target_file}: {error}")
            changed = False

    print(f"Đã đồng bộ body : {updated_count}")
    print(f"Đã giống nguồn  : {same_count}")
    print(f"Không tìm thấy  : {not_found_count}")
    print("-" * 72)

    return dict(
        file=target_file,
        exists=True,
        changed=changed,
        updated=updated_count,
        same=same_count,
        not_found=not_found_count,
    )


def main():
    print("=" * 72)
    print("       UPDATE PLAYLIST - GIỮ NGUYÊN ICON/METADATA GỐC")
    print("=" * 72)
    print("\nNguồn upstream: cấu hình qua UPSTREAM_PLAYLIST_URL.")
    print(f"Nguồn Quốc Tế/In The Box/Địa Phương theo tvg-id: {TVG_ID_SOURCE_URL}\n")

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

    try:
        if TVG_ID_SOURCE_URL == SOURCE_URL:
            tvg_id_source_text = source_text
        else:
            tvg_id_source_text = fetch(TVG_ID_SOURCE_URL)
    except requests.RequestException as error:
        print(
            "[LỖI] Không tải được nguồn Quốc Tế/In The Box/Địa Phương:"
            f"\n  {error}"
        )
        sys.exit(1)

    tvg_id_source_map, duplicate_ids = build_source_id_map(
        tvg_id_source_text
    )

    print(f"Tìm thấy {len(source_blocks)} block upstream.")
    print(f"Đã loại {radio_count} block radio.")
    print(f"Tạo map được {len(source_map)} cặp group-title + tên kênh.")

    if duplicate_keys:
        print(
            f"Cảnh báo: {len(duplicate_keys)} cặp group-title + tên kênh bị trùng."
        )
        print("Kênh trùng hoàn toàn dùng block xuất hiện sau cùng.")

    if duplicate_ids:
        print(
            f"Cảnh báo: {len(duplicate_ids)} tvg-id bị trùng trong nguồn "
            "vmttv."
        )
        print("tvg-id trùng dùng block xuất hiện sau cùng.")

    if not source_map:
        print("\n[LỖI] Playlist upstream không có dữ liệu TV hợp lệ.")
        print("Không thay đổi file nào.")
        sys.exit(1)

    source_map = filter_reachable_international(source_map)
    result = update_target_file(
        TARGET_FILE,
        source_map,
        tvg_id_source_map,
    )

    print("\n" + "=" * 72)
    print("                           TỔNG KẾT")
    print("=" * 72)
    print(f"File xử lý       : {result['file']}")
    print(f"Tồn tại          : {'Có' if result['exists'] else 'Không'}")
    print(f"Có thay đổi      : {'Có' if result['changed'] else 'Không'}")
    print(f"Kênh đồng bộ     : {result['updated']}")
    print(f"Kênh đã giống    : {result['same']}")
    print(f"Không tìm thấy   : {result['not_found']}")
    print("EXTINF/icon       : KHÓA NGUYÊN THEO FILE ĐÍCH")
    print("=" * 72)


if __name__ == "__main__":
    main()
