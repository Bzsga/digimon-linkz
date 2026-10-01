"""Repair the community client's managed effect-pool null checks.

EffectPool was changed to MonoBehaviour but is still allocated with `new`.
Its queue operations only use managed fields; Unity Object equality incorrectly
treats this holder as null. Change only the four holder checks to CLR reference
checks, retaining Unity lifetime checks on real effects and prefabs. Both APK
architectures are guarded by exact input hashes and instruction verification.
The Chinese APK remains the recovery baseline. Output needs the existing key.
"""
from pathlib import Path
import hashlib
import io
import json
import struct
import zipfile

from capstone import Cs, CS_ARCH_ARM, CS_ARCH_ARM64, CS_MODE_ARM
from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    'arm64-v8a': {
        'sha256': '791d2f37c722a4a873f61f03d46dfcd46ad2ff1f86d1b76694ad7744dfe2ddba',
        'checks': [(0x12D3A80, True), (0x12D3BD8, False),
                   (0x12D3DB0, False), (0x12D3E54, False)],
        'equality': 0x22DA8DC, 'inequality': 0x22E704C,
    },
    'armeabi-v7a': {
        'sha256': '9253dc606ad831180a18607aa3b5de7e24c617cff9125cf32b4575c76472fff5',
        'checks': [(0xAF736C, True), (0xAF7508, False),
                   (0xAF7718, False), (0xAF77D0, False)],
        'equality': 0x1D5EAA8, 'inequality': 0x1D6E230,
    },
}


def patch_library(raw, architecture):
    cfg = CONFIG[architecture]
    if hashlib.sha256(raw).hexdigest() != cfg['sha256']:
        raise ValueError('Unexpected native library baseline: ' + architecture)
    elf = ELFFile(io.BytesIO(raw))
    decoder = Cs(CS_ARCH_ARM64 if architecture == 'arm64-v8a' else CS_ARCH_ARM, CS_MODE_ARM)
    result = bytearray(raw)
    changes = []
    for call, equality in cfg['checks']:
        segment = next(s for s in elf.iter_segments()
                       if s['p_vaddr'] <= call < s['p_vaddr'] + s['p_filesz'])
        offset = call - segment['p_vaddr'] + segment['p_offset']
        expected_target = cfg['equality' if equality else 'inequality']
        instruction = next(decoder.disasm(raw[offset:offset + 4], call))
        assert instruction.mnemonic == 'bl' and instruction.op_str == hex(expected_target).replace('0x', '#0x')
        if architecture == 'arm64-v8a':
            start, address = offset - 4, call - 4
            assert [(i.mnemonic, i.op_str) for i in decoder.disasm(raw[start:offset], address)] == [('mov', 'x2, xzr')]
            replacement = struct.pack('<2I', 0xF100001F, 0x1A9F17E0 if equality else 0x1A9F07E0)
            expected = [('cmp', 'x0, #0'), ('cset', 'w0, eq' if equality else 'w0, ne')]
        else:
            start, address = offset, call
            before = [(i.mnemonic, i.op_str) for i in decoder.disasm(raw[start:start + 12], address)]
            assert before[1] == ('cmp', 'r0, #1')
            assert before[2][0] in ('bne', 'popne')
            # Consume the reference directly. Equality's old false branch is
            # still NE; inequality's old false branch becomes EQ.
            branch = struct.unpack_from('<I', raw, offset + 8)[0]
            assert branch >> 28 == 1
            if not equality:
                branch &= 0x0FFFFFFF
            replacement = struct.pack('<3I', 0xE3500000, 0xE320F000, branch)
            expected = [('cmp', 'r0, #0'), ('nop', ''),
                        (before[2][0] if equality else before[2][0].replace('ne', 'eq'), before[2][1])]
        after = [(i.mnemonic, i.op_str) for i in decoder.disasm(replacement, address)]
        assert after == expected
        original = raw[start:start + len(replacement)]
        result[start:start + len(replacement)] = replacement
        changes.append({'address': hex(address), 'before': original.hex(), 'after': replacement.hex(),
                        'instructions': after})
    changed = {i for c in changes for i in range(int(c['address'], 16), int(c['address'], 16) + len(bytes.fromhex(c['after'])))}
    # Independently verify all differences are inside the declared instruction windows.
    file_windows = set()
    for address in changed:
        segment = next(s for s in elf.iter_segments() if s['p_vaddr'] <= address < s['p_vaddr'] + s['p_filesz'])
        file_windows.add(address - segment['p_vaddr'] + segment['p_offset'])
    assert len(raw) == len(result)
    assert all(a == b or i in file_windows for i, (a, b) in enumerate(zip(raw, result)))
    return bytes(result), changes


def main():
    source = ROOT / 'client-local/linkz-local-cn.apk'
    output = ROOT / 'client-local/linkz-local-cn-effects-unsigned.apk'
    report = {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'architectures': {}, 'signed': False, 'installed': False, 'runtime_verified': False}
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(output, 'w') as after:
        for info in before.infolist():
            if info.filename.upper().startswith('META-INF/'):
                continue
            data = before.read(info.filename)
            for architecture in CONFIG:
                if info.filename == f'lib/{architecture}/libil2cpp.so':
                    data, changes = patch_library(data, architecture)
                    report['architectures'][architecture] = {'changes': changes, 'sha256': hashlib.sha256(data).hexdigest()}
            after.writestr(info, data)
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
        assert after.testzip() is None
        for name in after.namelist():
            if name not in [f'lib/{a}/libil2cpp.so' for a in CONFIG]:
                assert after.getinfo(name).CRC == before.getinfo(name).CRC
    report['unsigned_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
    (ROOT / 'research/effect-pool-patch.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Verified four managed-pool checks per architecture; all other APK entries unchanged.')


if __name__ == '__main__':
    main()
