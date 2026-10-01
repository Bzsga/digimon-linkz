"""Restore the original hit-effect dispatcher in the verified local APK.

The community build logs CharacterParams instead of calling
hitEffects.PlayAnimationTrigger(characters.CharacterParams). Effects consequently
autoplay at their spawn point. Keep the earlier pool fix and restore only this
method in both ABIs. Original, Chinese and first effect candidates stay intact.
"""
import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM, CS_ARCH_ARM64, CS_MODE_ARM
from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    'arm64-v8a': {'sha256': 'c33f9d9c1abe836a2108b8696456fd7aee8117ca8fbe5822f97e483f56431490',
                  'start': 0x1232768, 'size': 136, 'getter': 0x1143a2c,
                  'trigger': 0xf7cdec, 'log': 0x236218c},
    'armeabi-v7a': {'sha256': '9a3be8e4c7ef0a6a09944ffae54efa79c07b01d25c71a58ed80301802fa77ee6',
                    'start': 0xa3d7ac, 'size': 164, 'getter': 0x92c30c,
                    'trigger': 0x71b21c, 'log': 0x1df862c},
}


def branch(address, target, arm64, link=False, condition=None):
    delta = target - address - (0 if arm64 else 8)
    assert delta % 4 == 0
    if arm64:
        assert -(1 << 27) <= delta < (1 << 27)
        return (0x94000000 if link else 0x14000000) | ((delta // 4) & 0x3ffffff)
    assert -(1 << 25) <= delta < (1 << 25)
    return ((14 if condition is None else condition) << 28) | (0xb000000 if link else 0xa000000) | ((delta // 4) & 0xffffff)


def patch(raw, architecture):
    cfg = CONFIG[architecture]
    assert hashlib.sha256(raw).hexdigest() == cfg['sha256']
    address, size = cfg['start'], cfg['size']
    elf = ELFFile(io.BytesIO(raw))
    segment = next(s for s in elf.iter_segments() if s['p_vaddr'] <= address < s['p_vaddr'] + s['p_filesz'])
    offset = address - segment['p_vaddr'] + segment['p_offset']
    arm64 = architecture == 'arm64-v8a'
    decoder = Cs(CS_ARCH_ARM64 if arm64 else CS_ARCH_ARM, CS_MODE_ARM)
    original = raw[offset:offset + size]
    before = [(i.mnemonic, i.op_str) for i in decoder.disasm(original, address)]
    # Verify the defect itself and exact method boundary; no playback call exists.
    assert ('b', hex(cfg['log']).replace('0x', '#0x')) in before
    assert not any(i[1] == hex(cfg['trigger']).replace('0x', '#0x') for i in before)
    assert ('bl', hex(cfg['getter']).replace('0x', '#0x')) in before
    if arm64:
        # Null input/target returns; otherwise tail-call the real hit trigger.
        words = [0xa9be4ff4, 0xa9017bfd, 0x910043fd,
                 0xaa0103f3, 0xaa0203e0,
                 0xb4000173, 0xb4000140, 0xaa1f03e1,
                 branch(address + 32, cfg['getter'], True, True),
                 0xaa0003e1, 0xb40000d1, 0xaa1303e0, 0xaa1f03e2,
                 0xa9417bfd, 0xa8c24ff4,
                 branch(address + 60, cfg['trigger'], True),
                 0xa9417bfd, 0xa8c24ff4, 0xd65f03c0]
        nop = 0xd503201f
    else:
        # Preserve ARM EABI stack alignment and the caller's callee-saved registers.
        ret = address + 68
        words = [0xe92d4c10, 0xe28db008, 0xe1a04001, 0xe1a00002,
                 0xe3540000, branch(address + 20, ret, False, condition=0),
                 0xe3500000, branch(address + 28, ret, False, condition=0),
                 0xe3a01000, branch(address + 36, cfg['getter'], False, True),
                 0xe1a01000, 0xe3510000, branch(address + 48, ret, False, condition=0),
                 0xe1a00004, 0xe3a02000, 0xe8bd4c10,
                 branch(address + 64, cfg['trigger'], False), 0xe8bd8c10]
        nop = 0xe320f000
    words += [nop] * (size // 4 - len(words))
    replacement = struct.pack('<' + str(len(words)) + 'I', *words)
    after = [(i.mnemonic, i.op_str) for i in decoder.disasm(replacement, address)]
    assert len(replacement) == size
    assert ('bl', hex(cfg['getter']).replace('0x', '#0x')) in after
    assert ('b', hex(cfg['trigger']).replace('0x', '#0x')) in after
    assert not any(i[1] == hex(cfg['log']).replace('0x', '#0x') for i in after)
    # Validate every conditional guard reaches the explicit return block.
    expected_return = address + (64 if arm64 else 68)
    for instruction in decoder.disasm(replacement, address):
        if instruction.mnemonic in ('cbz', 'beq'):
            assert int(instruction.op_str.split('#')[-1], 16) == expected_return
    result = raw[:offset] + replacement + raw[offset + size:]
    assert len(result) == len(raw) and result[:offset] == raw[:offset] and result[offset + size:] == raw[offset + size:]
    return result, {'address': hex(address), 'size': size,
                    'before_sha256': hashlib.sha256(original).hexdigest(),
                    'before': before, 'after': after,
                    'sha256': hashlib.sha256(result).hexdigest()}


def main():
    source = ROOT / 'client-local/linkz-local-cn-effects.apk'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == '99610d8f444f29429a158d1fecca7f391eec5069e9f8c4f286faffddc7f59001'
    output = ROOT / 'client-local/linkz-local-cn-hitfix-unsigned.apk'
    report = {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'architectures': {}, 'signed': False, 'installed': False, 'runtime_verified': False}
    changed = {f'lib/{a}/libil2cpp.so': a for a in CONFIG}
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(output, 'w') as after:
        for info in before.infolist():
            if info.filename.upper().startswith('META-INF/'):
                continue
            data = before.read(info.filename)
            if info.filename in changed:
                arch = changed[info.filename]
                data, row = patch(data, arch)
                report['architectures'][arch] = row
            after.writestr(info, data)
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
        assert after.testzip() is None
        for name in after.namelist():
            if name not in changed:
                assert before.getinfo(name).CRC == after.getinfo(name).CRC
    report['unsigned_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
    (ROOT / 'research/hit-dispatch-patch.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Hit dispatcher restored in both ABIs; other APK entries unchanged.')


if __name__ == '__main__':
    main()
