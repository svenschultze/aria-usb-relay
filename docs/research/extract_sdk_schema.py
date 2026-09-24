"""Extract the Gen1 protobuf descriptor from an official Aria SDK wheel.

No third-party Python dependencies. Usage:
  python extract_sdk_schema.py path/to/projectaria_client_sdk-2.5.0-...whl output.json

This reads public distributed binaries, never contacts a device, and does not
implement or bypass device authentication.
"""
import hashlib
import json
import struct
import sys
import zipfile
from pathlib import Path

SYMBOL = b"_ZL38descriptor_table_protodef_aria_2eproto\0"


def extract_descriptor(elf):
    if elf[:6] != b"\x7fELF\x02\x01":
        raise ValueError("Expected 64-bit little-endian ELF")
    off = struct.unpack_from("<Q", elf, 40)[0]
    entsize, count = struct.unpack_from("<HH", elf, 58)
    sections = [struct.unpack_from("<IIQQQQIIQQ", elf, off+i*entsize) for i in range(count)]
    for symsec in sections:
        if symsec[1] != 2:
            continue
        strsec = sections[symsec[6]]
        strings = elf[strsec[4]:strsec[4]+strsec[5]]
        name_offset = strings.find(SYMBOL)
        if name_offset < 0:
            continue
        for pos in range(symsec[4], symsec[4]+symsec[5], symsec[9]):
            name, _, _, index, addr, size = struct.unpack_from("<IBBHQQ", elf, pos)
            if name == name_offset:
                sec = sections[index]
                start = addr-sec[3]+sec[4]
                # C++ generated descriptor includes a trailing C string NUL.
                return elf[start:start+size].rstrip(b"\0")
    raise ValueError("Gen1 aria.proto descriptor symbol not found")


def wire(data):
    pos = 0
    def varint():
        nonlocal pos
        result = shift = 0
        while True:
            value = data[pos]
            pos += 1
            result |= (value & 127) << shift
            if not value & 128:
                return result
            shift += 7
    fields = {}
    while pos < len(data):
        tag = varint()
        kind, number = tag & 7, tag >> 3
        if not number:
            raise ValueError("Invalid protobuf field zero")
        if kind == 0:
            value = varint()
        elif kind == 2:
            size = varint()
            value = data[pos:pos+size]
            pos += size
        elif kind in (1, 5):
            size = 8 if kind == 1 else 4
            value = data[pos:pos+size]
            pos += size
        else:
            raise ValueError(f"Unsupported protobuf wire type {kind}")
        fields.setdefault(number, []).append(value)
    return fields


def scalar(fields, key):
    return fields.get(key, [None])[0]


def text(fields, key):
    value = scalar(fields, key)
    return value.decode() if value is not None else None


def enum(data):
    fields = wire(data)
    values = []
    for raw in fields.get(2, []):
        entry = wire(raw)
        value = scalar(entry, 2)
        if value is not None and value >= 1 << 63:
            value -= 1 << 64
        values.append(dict(name=text(entry, 1), number=value))
    return dict(name=text(fields, 1), values=values)


def message(data):
    fields = wire(data)
    members = []
    for raw in fields.get(2, []):
        entry = wire(raw)
        members.append(dict(name=text(entry, 1), number=scalar(entry, 3),
                            label=scalar(entry, 4), type=scalar(entry, 5),
                            typeName=text(entry, 6), defaultValue=text(entry, 7),
                            oneofIndex=scalar(entry, 9)))
    return dict(name=text(fields, 1), fields=members,
                nestedMessages=[message(raw) for raw in fields.get(3, [])],
                enums=[enum(raw) for raw in fields.get(4, [])])


def main():
    source, output = map(Path, sys.argv[1:3])
    with zipfile.ZipFile(source) as wheel:
        elf = wheel.read("aria/libsdk_core.so")
    descriptor = extract_descriptor(elf)
    fields = wire(descriptor)
    result = dict(name=text(fields, 1), package=text(fields, 2), syntax=text(fields, 12),
                  dependencies=[v.decode() for v in fields.get(3, [])],
                  messages=[message(raw) for raw in fields.get(4, [])],
                  enums=[enum(raw) for raw in fields.get(5, [])],
                  provenance=dict(wheel=source.name,
                                  wheelSha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                                  descriptorSymbol=SYMBOL[:-1].decode(),
                                  descriptorSha256=hashlib.sha256(descriptor).hexdigest()))
    output.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(f"Wrote {len(result['messages'])} messages and {len(result['enums'])} enums to {output}")


if __name__ == "__main__":
    main()
