"""Bounded, read-only inspection of IL2CPP effect and camera objects on the local emulator.

Never scans general memory, reads account objects, calls game methods, or writes
process memory. Addresses/field offsets are specific to the verified ARM64 client.
Requires the existing rooted local emulator; do not use on unrelated devices.
"""
import json
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADB = str(ROOT / 'android-sdk/platform-tools/adb.exe')
DEVICE = 'emulator-5580'


def adb(*args):
    return subprocess.run([ADB, '-s', DEVICE, *args], capture_output=True,
                          check=True, timeout=12).stdout


pid = adb('shell', 'pidof', 'org.is_saved.digimon_links_ww').decode().strip()
assert pid.isdigit()
maps = adb('shell', 'su', '0', 'cat', f'/proc/{pid}/maps').decode()
base = int(next(x for x in maps.splitlines() if 'libil2cpp.so' in x and
                x.split()[2] == '00000000').split('-')[0], 16)


def read(address, count):
    assert address > 0x10000 and 0 < count <= 8192
    padding = address % 8
    raw = adb('exec-out', 'su', '0', 'dd', f'if=/proc/{pid}/mem', 'bs=8',
              f'skip={address // 8}', f'count={(count + padding + 7) // 8}')
    assert len(raw) >= count + padding
    return raw[padding:padding + count]


def ptr(address):
    return struct.unpack('<Q', read(address, 8))[0]


def static(slot):
    return ptr(ptr(ptr(base + slot)) + 0xb8)


def refs(address):
    raw = read(address, 40)
    array = struct.unpack_from('<Q', raw, 0x10)[0]
    count = struct.unpack_from('<i', raw, 0x18)[0]
    assert 0 <= count < 100
    return list(struct.unpack('<' + str(count) + 'Q', read(array + 32, count * 8))) if count else []


def string(address):
    count = struct.unpack('<i', read(address + 16, 4))[0]
    assert 0 < count < 100
    return read(address + 20, count * 2).decode('utf-16-le')


def effect(address):
    raw = read(address, 152)
    camera = struct.unpack_from('<Q', raw, 80)[0]
    boards = refs(struct.unpack_from('<Q', raw, 40)[0])
    return {'address': hex(address), 'awake_initialized': bool(raw[0x5c]),
            'cached_character': hex(struct.unpack_from('<Q', raw, 0x90)[0]),
            'target': struct.unpack_from('<i', raw, 0x80)[0],
            'scale': struct.unpack_from('<f', raw, 0x58)[0],
            'camera': hex(camera), 'camera_alive': bool(ptr(camera + 16)) if camera else False,
            'billboards': [{'address': hex(x), 'look_camera': hex(ptr(x + 24))} for x in boards]}


sf = static(0x3d4f3b0)
camera = ptr(sf + 16)
registered = refs(ptr(static(0x3d069c0)))
report = {'pid': int(pid), 'battle_camera': hex(camera),
          'battle_camera_alive': bool(ptr(camera + 16)) if camera else False,
          'camera_registered': camera in registered, 'registered_camera_count': len(registered),
          'effects': []}
dictionary = ptr(sf + 8)
raw = read(dictionary, 56)
array = struct.unpack_from('<Q', raw, 24)[0]
count = struct.unpack_from('<i', raw, 32)[0]
assert 0 <= count < 200
entries = read(array + 32, count * 24) if count else b''
for i in range(count):
    hash_code, _, key, pool = struct.unpack_from('<iiQQ', entries, i * 24)
    if hash_code < 0 or not key or not pool:
        continue
    name = string(key)
    if name not in ['EFF_COM_HIT_NORMAL', 'EFF_COM_HIT_CRITICAL', 'EFF_COM_HIT_WEAK']:
        continue
    raw = read(pool, 56)
    queue, prefab = struct.unpack_from('<QQ', raw, 24)
    raw = read(queue, 40)
    array = struct.unpack_from('<Q', raw, 16)[0]
    head, _, size = struct.unpack_from('<iii', raw, 24)
    row = {'key': name, 'prefab': effect(prefab), 'queue_size': size, 'instances': []}
    length = ptr(array + 24)
    assert 0 <= size <= length < 100
    for i in range(size):
        instance = ptr(array + 32 + ((head + i) % length) * 8)
        if instance:
            row['instances'].append(effect(instance))
    report['effects'].append(row)
print(json.dumps(report, indent=2))
