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
import hashlib
import struct
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


SOURCE_URL = os.environ.get("UPSTREAM_PLAYLIST_URL", "")
TVG_ID_SOURCE_URL = os.environ.get(
    "TVG_ID_PLAYLIST_URL",
    os.environ.get(
        "INTERNATIONAL_PLAYLIST_URL",
        "https://raw.githubusercontent.com/vuminhthanh12/vuminhthanh12/main/vmttv",
    ),
)
LOCAL_SOURCE_URL = os.environ.get(
    "LOCAL_PLAYLIST_URL",
    "https://tv.vietanhtv.top/sex",
)
ENC_FILE = "vxm.enc"
WORKER_BASE_URL = "https://vietmitv-stream.viet-ng228.workers.dev"

VTV_CAB_GROUP = "VTVcab"
INTERNATIONAL_GROUP = "Quốc Tế"
TVG_ID_SOURCE_GROUPS = {"Quốc Tế", "In The Box"}
LOCAL_SOURCE_GROUP = "Địa Phương"
# VietAnhTV dùng tvg-id khác file đích cho 11 kênh này.
LOCAL_TVG_ID_ALIASES = {
    "antvhd": "antv-hd",
    "qpvnhd": "qpvn-hd",
    "vinhlong1hd": "thvl1hd",
    "vinhlong2hd": "thvl2hd",
    "vinhlong3hd": "thvl3hd",
    "vinhlong4hd": "thvl4hd",
    "vinhlong5hd": "thvl5hd",
    "tayninh1": "tayninhtv",
    "dongnai3": "dnrtv3",
    "dongthap1": "dongthap",
    "haiphong3": "haiphongplus",
}
LICENSE_KEY_PREFIX = "#KODIPROP:inputstream.adaptive.license_key="

VXM_MAGIC = b"VXMENC3\x00"
VXM_AAD_PREFIX = b"VietMiTV/VXMENC3"
VXM_KEY_ID = 1
VXM_K = [
    bytes([0x85,0x05,0x75,0x40,0xE5,0x2E,0xD3,0xF2]),
    bytes([0x57,0xD8,0xC3,0x3E,0x7F,0x13,0x1C,0xE3]),
    bytes([0x4A,0xBE,0xB2,0x89,0x9C,0x8E,0x21,0x63]),
    bytes([0x77,0x06,0xC3,0x70,0xD7,0xCD,0x71,0x44]),
]
VXM_M = [0x5A,0xA7,0x3C,0xD1]


def load_vxmenc3_private_key():
    raw = os.environ.get("VXMENC3_PRIVATE_KEY_B64", "").strip()
    if not raw:
        raise RuntimeError("Thiếu GitHub Secret VXMENC3_PRIVATE_KEY_B64")

    try:
        der = base64.b64decode(raw, validate=True)
        private_key = serialization.load_der_private_key(der, password=None)
    except Exception as exc:
        raise RuntimeError(
            "VXMENC3_PRIVATE_KEY_B64 không phải PKCS#8 DER base64 hợp lệ"
        ) from exc

    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise RuntimeError("VXMENC3 private key không phải EC private key")

    if private_key.curve.name != "secp256r1":
        raise RuntimeError("VXMENC3 private key phải dùng P-256/secp256r1")

    return private_key


def derive_vxmenc3_key(salt: bytes) -> bytes:
    seed = bytes(v ^ VXM_M[i] for i, part in enumerate(VXM_K) for v in part)
    key = hashlib.sha256(seed + salt).digest()

    for _ in range(60000):
        key = hashlib.sha256(key + salt).digest()

    return key


def build_vxmenc3_aad(
    counter: int,
    key_id: int,
    created_at: int,
    salt: bytes,
    iv: bytes,
) -> bytes:
    return (
        VXM_AAD_PREFIX
        + struct.pack(">QIQ", counter, key_id, created_at)
        + salt
        + iv
    )


def build_vxmenc3(playlist_text: str, counter: int) -> bytes:
    text = playlist_text.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not text.startswith("#EXTM3U"):
        text = "#EXTM3U\n" + text

    text += "\n"

    if counter <= 0:
        raise RuntimeError("VXMENC3 counter phải lớn hơn 0")

    private_key = load_vxmenc3_private_key()

    salt = os.urandom(16)
    iv = os.urandom(12)
    created_at = int(time.time())

    key = derive_vxmenc3_key(salt)
    aad = build_vxmenc3_aad(
        counter,
        VXM_KEY_ID,
        created_at,
        salt,
        iv,
    )

    ciphertext = AESGCM(key).encrypt(
        iv,
        text.encode("utf-8"),
        aad,
    )

    signed = (
        struct.pack(">QIQ", counter, VXM_KEY_ID, created_at)
        + salt
        + iv
        + struct.pack(">I", len(ciphertext))
        + ciphertext
    )

    signature = private_key.sign(
        VXM_MAGIC + signed,
        ec.ECDSA(hashes.SHA256()),
    )

    return (
        VXM_MAGIC
        + struct.pack(">I", len(signed))
        + signed
        + struct.pack(">I", len(signature))
        + signature
    )


def decrypt_vxmenc3(payload: bytes) -> tuple[str, int]:
    if len(payload) < 96 or payload[:8] != VXM_MAGIC:
        raise RuntimeError("vxm.enc không đúng định dạng VXMENC3")

    pos = 8

    if pos + 4 > len(payload):
        raise RuntimeError("VXMENC3 thiếu signed length")

    signed_len = struct.unpack(">I", payload[pos:pos+4])[0]
    pos += 4

    signed = payload[pos:pos+signed_len]
    pos += signed_len

    if len(signed) != signed_len or pos + 4 > len(payload):
        raise RuntimeError("VXMENC3 bị thiếu dữ liệu")

    sig_len = struct.unpack(">I", payload[pos:pos+4])[0]
    pos += 4

    signature = payload[pos:pos+sig_len]

    if len(signature) != sig_len or pos + sig_len != len(payload):
        raise RuntimeError("VXMENC3 signature length không hợp lệ")

    private_key = load_vxmenc3_private_key()
    public_key = private_key.public_key()

    try:
        public_key.verify(
            signature,
            VXM_MAGIC + signed,
            ec.ECDSA(hashes.SHA256()),
        )
    except Exception as exc:
        raise RuntimeError("Chữ ký VXMENC3 hiện tại không hợp lệ") from exc

    if len(signed) < 52:
        raise RuntimeError("VXMENC3 signed body quá ngắn")

    counter, key_id, created_at = struct.unpack(">QIQ", signed[:20])
    salt = signed[20:36]
    iv = signed[36:48]
    cipher_len = struct.unpack(">I", signed[48:52])[0]
    ciphertext = signed[52:52+cipher_len]

    if counter <= 0:
        raise RuntimeError("VXMENC3 counter không hợp lệ")

    if key_id != VXM_KEY_ID:
        raise RuntimeError(f"VXMENC3 key-id không hỗ trợ: {key_id}")

    if created_at <= 0:
        raise RuntimeError("VXMENC3 created_at không hợp lệ")

    if len(ciphertext) != cipher_len or 52 + cipher_len != len(signed):
        raise RuntimeError("VXMENC3 ciphertext length không hợp lệ")

    key = derive_vxmenc3_key(salt)
    aad = build_vxmenc3_aad(
        counter,
        key_id,
        created_at,
        salt,
        iv,
    )

    try:
        plaintext = AESGCM(key).decrypt(
            iv,
            ciphertext,
            aad,
        )
    except Exception as exc:
        raise RuntimeError("Không giải mã/xác thực được VXMENC3") from exc

    text = plaintext.decode("utf-8")

    if not text.startswith("#EXTM3U"):
        raise RuntimeError("VXMENC3 plaintext không phải playlist M3U")

    return text, counter


def load_current_playlist() -> tuple[str, int]:
    path = Path(ENC_FILE)

    if not path.exists():
        raise RuntimeError(f"Không tìm thấy {ENC_FILE}")

    text, counter = decrypt_vxmenc3(path.read_bytes())

    if not get_extinf_lines(text):
        raise RuntimeError(f"{ENC_FILE} giải mã được nhưng không có #EXTINF")

    print(
        f"Đã giải mã {ENC_FILE}: "
        f"{len(get_extinf_lines(text))} kênh; counter={counter}"
    )

    return text, counter


def write_encrypted_playlist(playlist_text: str, counter: int) -> None:
    payload = build_vxmenc3(playlist_text, counter)
    Path(ENC_FILE).write_bytes(payload)

    print(
        f"Đã tạo {ENC_FILE}: {len(payload):,} bytes; "
        f"counter={counter}"
    )


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
    padding = "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(value + padding)
    if len(decoded) != 16:
        raise ValueError("ClearKey phải dài đúng 16 byte")
    return decoded.hex()


def normalize_inline_clearkey(line: str) -> str:
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


def build_group_id_map(source_text: str, allowed_groups: set[str]):
    allowed = {normalize_group(group) for group in allowed_groups}
    source_map = {}
    duplicate_ids = set()

    for block in split_blocks(source_text):
        if normalize_group(get_group_title(block)) not in allowed:
            continue
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
    if not target_block or not source_block:
        return target_block

    target_extinf = target_block[0]
    group = normalize_group(get_group_title(target_block))

    if group == normalize_group(VTV_CAB_GROUP) and has_dynamic_license_url(source_block):
        source_body = build_worker_body(source_block)
    else:
        source_body = [normalize_inline_clearkey(line) for line in source_block[1:]]

    return [target_extinf, *source_body]


def update_playlist_text(
    target_text: str,
    source_map: dict,
    tvg_id_source_map: dict | None = None,
    local_source_map: dict | None = None,
):
    print()
    print("=" * 72)
    print(" ĐANG XỬ LÝ: vxm.enc (GIẢI MÃ TRONG RAM)")
    print("=" * 72)

    target_lines = target_text.splitlines()
    first_block = next(
        (i for i, line in enumerate(target_lines) if line.startswith("#EXTINF")),
        len(target_lines),
    )
    header_lines = target_lines[:first_block]
    target_blocks = split_blocks("\n".join(target_lines[first_block:]))
    tvg_id_source_groups = {normalize_group(item) for item in TVG_ID_SOURCE_GROUPS}
    local_source_group = normalize_group(LOCAL_SOURCE_GROUP)

    updated_blocks = []
    updated_count = same_count = not_found_count = 0

    for target_block in target_blocks:
        group = get_group_title(target_block)
        name = get_channel_name(target_block)
        group_key = normalize_group(group)
        tvg_id = normalize_tvg_id(get_tvg_id(target_block))

        if group_key == local_source_group and tvg_id:
            local_tvg_id = LOCAL_TVG_ID_ALIASES.get(tvg_id, tvg_id)
            source = (local_source_map or {}).get(local_tvg_id)
            match_label = f"VietAnhTV tvg-id={local_tvg_id}"
        elif group_key in tvg_id_source_groups and tvg_id:
            source = (tvg_id_source_map or {}).get(tvg_id)
            match_label = f"tvg-id={tvg_id}"
        else:
            source = source_map.get((group_key, normalize_name(name)))
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

        if new_block[0] != target_block[0]:
            raise RuntimeError(f"EXTINF bị thay đổi ngoài ý muốn: [{group}] {name}")
        updated_blocks.append(new_block)

    output_lines = list(header_lines)
    for block in updated_blocks:
        output_lines.extend(block)
    new_text = "\n".join(output_lines) + ("\n" if output_lines else "")

    if get_extinf_lines(new_text) != get_extinf_lines(target_text):
        raise RuntimeError(
            "Fail-safe: danh sách #EXTINF đã thay đổi; hủy cập nhật vxm.enc"
        )

    old_norm = target_text.replace("\r\n", "\n").replace("\r", "\n")
    changed = new_text != old_norm

    print("\n" + "-" * 72)
    print(f"Giữ nguyên danh sách : {len(target_blocks)} kênh")
    print("Giữ nguyên EXTINF    : 100% (logo/tvg-id/group/name)")
    print(f"Đã đồng bộ body      : {updated_count}")
    print(f"Đã giống nguồn       : {same_count}")
    print(f"Không tìm thấy       : {not_found_count}")
    print(f"Nội dung thay đổi    : {'Có' if changed else 'Không'}")
    print("-" * 72)

    return new_text, dict(
        file=ENC_FILE, exists=True, changed=changed, updated=updated_count,
        same=same_count, not_found=not_found_count,
    )


def main():
    print("=" * 72)
    print("       UPDATE VXMENC3 - GIỮ NGUYÊN ICON/METADATA GỐC")
    print("=" * 72)
    print("\nNguồn upstream: cấu hình qua UPSTREAM_PLAYLIST_URL.")
    print(f"Nguồn Quốc Tế/In The Box theo tvg-id: {TVG_ID_SOURCE_URL}")
    print(f"Nguồn Địa Phương theo tvg-id: {LOCAL_SOURCE_URL}\n")

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

    try:
        if LOCAL_SOURCE_URL == SOURCE_URL:
            local_source_text = source_text
        elif LOCAL_SOURCE_URL == TVG_ID_SOURCE_URL:
            local_source_text = tvg_id_source_text
        else:
            local_source_text = fetch(LOCAL_SOURCE_URL)
    except requests.RequestException as error:
        print(f"[LỖI] Không tải được nguồn Địa Phương VietAnhTV:\n  {error}")
        sys.exit(1)

    local_source_map, local_duplicate_ids = build_group_id_map(
        local_source_text,
        {LOCAL_SOURCE_GROUP},
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

    if local_duplicate_ids:
        print(
            f"Cảnh báo: {len(local_duplicate_ids)} tvg-id bị trùng trong "
            "nguồn Địa Phương VietAnhTV."
        )
        print("tvg-id trùng dùng block xuất hiện sau cùng.")

    if not source_map:
        print("\n[LỖI] Playlist upstream không có dữ liệu TV hợp lệ.")
        print("Không thay đổi vxm.enc.")
        sys.exit(1)

    source_map = filter_reachable_international(source_map)
    current_text, current_counter = load_current_playlist()
    new_text, result = update_playlist_text(
        current_text,
        source_map,
        tvg_id_source_map,
        local_source_map,
    )
    if result["changed"]:
        write_encrypted_playlist(new_text, current_counter + 1)
    else:
        print("Playlist plaintext không đổi -> giữ nguyên vxm.enc, không tạo ciphertext mới.")

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
