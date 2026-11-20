import gzip
import io
import json
import re
import urllib.request
from pathlib import Path

import UnityPy
import lz4.block
import msgpack

API = "https://jp-prd-api.holo-jong.com/resource/getassetbundlehashes"
CDN = "https://jp-prd-asset-cdn-holo-jong.akamaized.net/resources"
PLATFORM = "StandaloneWindows64"
LANG = "ja"
ENVIRONMENT = "develop"
OUTPUT = Path("data")
UA = "UnityPlayer/2022.3.62f1"

BUNDLE_RE = re.compile(
    rf"^masters_assets_masters_masterdata_aiming_{LANG}_{ENVIRONMENT}_[0-9a-f]{{32}}\.bundle$"
)
PREFIX = "{AasResource.ResourceUrl}"
EXT_LZ4 = 99
MARKERS = (0xC7, 0xC8, 0xC9)


def fetch(url, data=None):
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    method = "POST" if data is not None else "GET"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as res:
        body = res.read()
        if (res.headers.get("Content-Encoding") or "").lower() == "gzip":
            body = gzip.decompress(body)
        return body


def load_json(url, data=None):
    return json.loads(fetch(url, data).decode("utf-8"))


def to_jsonable(value):
    if isinstance(value, msgpack.Timestamp):
        return value.to_datetime().isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return value


def read_ext(buf, pos):
    marker = buf[pos]
    if marker == 0xC7:
        length, code, start = buf[pos + 1], buf[pos + 2], pos + 3
    elif marker == 0xC8:
        length = int.from_bytes(buf[pos + 1:pos + 3], "big")
        code, start = buf[pos + 3], pos + 4
    elif marker == 0xC9:
        length = int.from_bytes(buf[pos + 1:pos + 5], "big")
        code, start = buf[pos + 5], pos + 6
    else:
        raise ValueError(f"unexpected marker 0x{marker:02x} at {pos}")
    if code >= 0x80:
        code -= 0x100
    return code, buf[start:start + length]


def decode_table(buf, pos, size):
    if buf[pos] in MARKERS:
        code, payload = read_ext(buf, pos)
        if code != EXT_LZ4:
            raise ValueError(f"unexpected ext type {code}")
        if payload[0] != 0xD2:
            raise ValueError(f"unexpected length marker 0x{payload[0]:02x}")
        size = int.from_bytes(payload[1:5], "big")
        raw = lz4.block.decompress(payload[5:], uncompressed_size=size)
        return msgpack.unpackb(raw, raw=False, strict_map_key=False)
    return msgpack.unpackb(buf[pos:pos + size], raw=False, strict_map_key=False)


def parse_master_data(buf):
    unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
    unpacker.feed(buf)
    index = next(unpacker)
    data_start = unpacker.tell()

    tables = {}
    for name, (offset, size) in index.items():
        pos = data_start + offset
        tables[name] = decode_table(buf, pos, size)
    return tables


def extract_text_asset(bundle_bytes):
    env = UnityPy.load(io.BytesIO(bundle_bytes))
    assets = []
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        raw = getattr(data, "m_Script", None)
        if raw is None:
            continue
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        name = str(getattr(data, "m_Name", "") or "")
        assets.append((name, raw))
    named = [a for a in assets if a[0].startswith("MasterData")]
    return max(named or assets, key=lambda a: len(a[1]))


def find_bundle(ids):
    for raw in ids:
        if not raw.startswith(PREFIX):
            continue
        rel = raw[len(PREFIX):].replace("\\", "/").lstrip("/")
        rel = rel.replace("{AasResource.Language}", LANG)
        rel = rel.replace("{AasResource.Platform}", PLATFORM)
        if BUNDLE_RE.match(rel.rsplit("/", 1)[-1]):
            return rel
    raise LookupError("master bundle not found")


def main():
    catalog_hash = load_json(API, b"{}")["latestAssetBundleHashes"][
        f"{PLATFORM}_{LANG}"
    ]
    print(f"catalog_hash {catalog_hash}")

    catalog = load_json(
        f"{CDN}/assetbundles/catalogs/{PLATFORM}_{LANG}/{catalog_hash}/catalog_0.0.0.json"
    )
    rel = find_bundle(catalog["m_InternalIds"])
    print(f"bundle {rel}")

    bundle_bytes = fetch(f"{CDN}/{rel}")
    name, text = extract_text_asset(bundle_bytes)
    print(f"text_asset {name} {len(text)}")

    tables = parse_master_data(text)
    rows = sum(len(v) for v in tables.values())
    print(f"tables {len(tables)} rows {rows}")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    for stale in {p.stem for p in OUTPUT.glob("*.json")} - set(tables):
        (OUTPUT / f"{stale}.json").unlink()

    for table, value in tables.items():
        text_out = json.dumps(to_jsonable(value), ensure_ascii=False, indent=2)
        (OUTPUT / f"{table}.json").write_text(
            text_out + "\n", encoding="utf-8", newline="\n"
        )

    print(f"written {len(tables)}")


if __name__ == "__main__":
    main()
