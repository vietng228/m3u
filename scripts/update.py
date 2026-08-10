#!/usr/bin/env python3

import sys
import requests

SOURCE_URL = "http://tv.vietanhtv.top/tv"
TARGET_FILE = "m3u.m3u"
GROUP_NAME = "VTVcab"


def fetch(url: str) -> str:
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text


def split_blocks(text: str):
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


def is_group(block, group: str) -> bool:
    return f'group-title="{group}"' in block[0]


def main():
    print(f"Đang tải playlist nguồn: {SOURCE_URL}")
    source_text = fetch(SOURCE_URL)
    source_blocks = split_blocks(source_text)
    new_group_blocks = [b for b in source_blocks if is_group(b, GROUP_NAME)]

    if not new_group_blocks:
        print(f"Không tìm thấy nhóm '{GROUP_NAME}' trong playlist nguồn, dừng lại.")
        sys.exit(1)

    print(f"Tìm thấy {len(new_group_blocks)} kênh trong nhóm '{GROUP_NAME}' ở nguồn.")

    with open(TARGET_FILE, "r", encoding="utf-8") as f:
        target_text = f.read()

    target_lines = target_text.splitlines()

    idx = 0
    header_lines = []
    while idx < len(target_lines) and not target_lines[idx].startswith("#EXTINF"):
        header_lines.append(target_lines[idx])
        idx += 1

    target_blocks = split_blocks("\n".join(target_lines[idx:]))

    group_indices = [i for i, b in enumerate(target_blocks) if is_group(b, GROUP_NAME)]
    if not group_indices:
        print(f"File {TARGET_FILE} hiện chưa có nhóm '{GROUP_NAME}' nào, dừng lại "
              f"(để tránh chèn nhầm vị trí). Hãy thêm thủ công 1 lần đầu.")
        sys.exit(1)

    start, end = group_indices[0], group_indices[-1]

    if end - start + 1 != len(group_indices):
        print(f"Cảnh báo: các block '{GROUP_NAME}' trong {TARGET_FILE} không nằm liền nhau. "
              f"Vẫn sẽ thay thế từ vị trí đầu tiên đến vị trí cuối cùng.")

    updated_blocks = target_blocks[:start] + new_group_blocks + target_blocks[end + 1:]

    output_lines = list(header_lines)
    for b in updated_blocks:
        output_lines.extend(b)
    new_text = "\n".join(output_lines) + "\n"

    if new_text == target_text:
        print("Không có gì thay đổi.")
        return

    with open(TARGET_FILE, "w", encoding="utf-8") as f:
        f.write(new_text)

    print(f"Đã cập nhật {len(new_group_blocks)} kênh trong nhóm '{GROUP_NAME}' vào {TARGET_FILE}.")


if __name__ == "__main__":
    main()
  
