#!/usr/bin/env python3
"""
One-time fail-safe migration: VXMENC2 -> VXMENC3.

Required environment variables:
  VXM_SIGNING_PRIVATE_KEY     old VXMENC2 Ed25519 private key, base64
  VXMENC3_PRIVATE_KEY_B64     new VXMENC3 EC P-256 PKCS#8 DER private key, base64

Safety:
- Never overwrites vxm.enc until V3 decrypt/verify succeeds.
- Writes vxm.enc.v2.backup first.
- Writes vxm_v3_test.enc and compares plaintext SHA-256 byte-for-byte.
- Counter starts at 1.
"""
import base64, hashlib, os, shutil, struct, time
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SRC = Path("vxm.enc")
BACKUP = Path("vxm.enc.v2.backup")
TEST = Path("vxm_v3_test.enc")

# Exact VXMENC2 production format from the current update.py:
# MAGIC(8) | signed_len(4) | [salt(16)|iv(12)|cipher_len(4)|ciphertext] |
# signature_len(4) | ECDSA-P256 signature(DER)
V2_MAGIC = b"VXMENC2\x00"
V2_AAD = b"VietMiTV/VXMENC2"
V2_K = [
    bytes([0x85,0x05,0x75,0x40,0xE5,0x2E,0xD3,0xF2]),
    bytes([0x57,0xD8,0xC3,0x3E,0x7F,0x13,0x1C,0xE3]),
    bytes([0x4A,0xBE,0xB2,0x89,0x9C,0x8E,0x21,0x63]),
    bytes([0x77,0x06,0xC3,0x70,0xD7,0xCD,0x71,0x44]),
]
V2_M = [0x5A,0xA7,0x3C,0xD1]

# VXMENC3 constants MUST match Android VxmEncCodec.java/update_vxmenc3.py.
V3_MAGIC = b"VXMENC3\x00"
V3_AAD_PREFIX = b"VietMiTV/VXMENC3"
V3_KEY_ID = 1
V3_COUNTER = 1
V3_K = V2_K
V3_M = V2_M

def b64env(name):
    raw=os.environ.get(name,"").strip()
    if not raw:
        raise RuntimeError(f"Thiếu secret {name}")
    return base64.b64decode(raw, validate=True)

def v2_private():
    pem=os.environ.get("VXM_SIGNING_PRIVATE_KEY","").strip()
    if not pem:
        raise RuntimeError("Thiếu GitHub Secret VXM_SIGNING_PRIVATE_KEY")
    try:
        key=serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as exc:
        raise RuntimeError("VXM_SIGNING_PRIVATE_KEY không phải PEM private key hợp lệ") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise RuntimeError("VXMENC2 signing key không phải EC private key")
    return key

def v2_key(salt):
    seed=bytes(v ^ V2_M[i] for i,p in enumerate(V2_K) for v in p)
    out=hashlib.sha256(seed+salt).digest()
    for _ in range(60000):
        out=hashlib.sha256(out+salt).digest()
    return out

def decrypt_v2(blob):
    if len(blob)<16 or blob[:8]!=V2_MAGIC:
        raise RuntimeError("File hiện tại không phải VXMENC2 production")

    p=8
    signed_len=struct.unpack(">I",blob[p:p+4])[0]; p+=4
    signed=blob[p:p+signed_len]; p+=signed_len
    if len(signed)!=signed_len or p+4>len(blob):
        raise RuntimeError("VXMENC2 signed body bị thiếu")

    sig_len=struct.unpack(">I",blob[p:p+4])[0]; p+=4
    signature=blob[p:p+sig_len]
    if len(signature)!=sig_len or p+sig_len!=len(blob):
        raise RuntimeError("VXMENC2 signature/length không hợp lệ")

    # Verify the exact current V2 publisher signature before decrypting.
    try:
        v2_private().public_key().verify(
            signature, V2_MAGIC+signed, ec.ECDSA(hashes.SHA256())
        )
    except Exception as exc:
        raise RuntimeError("Chữ ký VXMENC2 hiện tại không hợp lệ") from exc

    if len(signed)<32:
        raise RuntimeError("VXMENC2 signed body quá ngắn")
    salt=signed[:16]
    iv=signed[16:28]
    cipher_len=struct.unpack(">I",signed[28:32])[0]
    ciphertext=signed[32:32+cipher_len]
    if len(ciphertext)!=cipher_len or 32+cipher_len!=len(signed):
        raise RuntimeError("VXMENC2 ciphertext length không hợp lệ")

    try:
        plain=AESGCM(v2_key(salt)).decrypt(iv,ciphertext,V2_AAD)
    except Exception as exc:
        raise RuntimeError("Không giải mã/xác thực được VXMENC2") from exc
    if not plain.startswith(b"#EXTM3U"):
        raise RuntimeError("VXMENC2 plaintext không phải M3U")
    return plain

def v3_private():
    key=serialization.load_der_private_key(b64env("VXMENC3_PRIVATE_KEY_B64"),password=None)
    if not isinstance(key,ec.EllipticCurvePrivateKey) or key.curve.name!="secp256r1":
        raise RuntimeError("VXMENC3 private key phải là P-256 PKCS#8 DER")
    return key

def v3_key(salt):
    seed=bytes(v ^ V3_M[i] for i,p in enumerate(V3_K) for v in p)
    out=hashlib.sha256(seed+salt).digest()
    for _ in range(60000):
        out=hashlib.sha256(out+salt).digest()
    return out

def v3_aad(counter,key_id,created,salt,iv):
    return V3_AAD_PREFIX + struct.pack(">QIQ",counter,key_id,created)+salt+iv

def encrypt_v3(plain):
    salt=os.urandom(16); iv=os.urandom(12); created=int(time.time())
    ct=AESGCM(v3_key(salt)).encrypt(iv,plain,v3_aad(V3_COUNTER,V3_KEY_ID,created,salt,iv))
    signed=struct.pack(">QIQ",V3_COUNTER,V3_KEY_ID,created)+salt+iv+struct.pack(">I",len(ct))+ct
    sig=v3_private().sign(V3_MAGIC+signed,ec.ECDSA(hashes.SHA256()))
    return V3_MAGIC+struct.pack(">I",len(signed))+signed+struct.pack(">I",len(sig))+sig

def decrypt_v3(blob):
    if blob[:8]!=V3_MAGIC: raise RuntimeError("Test output không phải VXMENC3")
    p=8
    slen=struct.unpack(">I",blob[p:p+4])[0]; p+=4
    signed=blob[p:p+slen]; p+=slen
    siglen=struct.unpack(">I",blob[p:p+4])[0]; p+=4
    sig=blob[p:p+siglen]
    if p+siglen!=len(blob): raise RuntimeError("VXMENC3 trailing/length lỗi")
    v3_private().public_key().verify(sig,V3_MAGIC+signed,ec.ECDSA(hashes.SHA256()))
    counter,key_id,created=struct.unpack(">QIQ",signed[:20])
    salt=signed[20:36]; iv=signed[36:48]
    clen=struct.unpack(">I",signed[48:52])[0]
    ct=signed[52:52+clen]
    if counter!=1 or key_id!=1 or len(ct)!=clen or 52+clen!=len(signed):
        raise RuntimeError("VXMENC3 metadata/cipher length lỗi")
    return AESGCM(v3_key(salt)).decrypt(iv,ct,v3_aad(counter,key_id,created,salt,iv))

def main():
    if not SRC.exists(): raise RuntimeError("Không tìm thấy vxm.enc")
    original=SRC.read_bytes()
    if original[:8]==V3_MAGIC:
        print("vxm.enc đã là VXMENC3; không migration.")
        return
    plain2=decrypt_v2(original)
    h2=hashlib.sha256(plain2).hexdigest()
    channels=plain2.count(b"#EXTINF")
    print(f"V2 verified/decrypted: channels={channels}, plaintext_sha256={h2}")

    # Backup is immutable for this run; refuse to replace a different backup.
    if BACKUP.exists() and BACKUP.read_bytes()!=original:
        raise RuntimeError(f"{BACKUP} đã tồn tại nhưng khác vxm.enc hiện tại; dừng an toàn")
    if not BACKUP.exists():
        BACKUP.write_bytes(original)

    v3=encrypt_v3(plain2)
    TEST.write_bytes(v3)
    plain3=decrypt_v3(TEST.read_bytes())
    h3=hashlib.sha256(plain3).hexdigest()
    print(f"V3 self-verified: counter=1, plaintext_sha256={h3}")

    if plain2 != plain3 or h2 != h3:
        raise RuntimeError("HASH/PLAINTEXT MISMATCH - không thay vxm.enc")

    # Atomic-ish local replacement after all verification.
    tmp=Path("vxm.enc.new")
    tmp.write_bytes(v3)
    os.replace(tmp,SRC)
    print("SUCCESS: vxm.enc -> VXMENC3 counter=1")
    print(f"Backup giữ tại: {BACKUP}")
    print(f"Test copy giữ tại: {TEST}")

if __name__=="__main__":
    main()
