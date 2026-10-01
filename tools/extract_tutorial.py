"""Extract the APK's tutorial script without executing BinaryFormatter data."""
from pathlib import Path
import re
import zipfile
import UnityPy
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES

ROOT = Path(__file__).resolve().parents[1]
with zipfile.ZipFile(ROOT / 'downloads/linksBiu-priv_64ver.apk') as archive:
    env = UnityPy.load(archive.read('assets/bin/Data/globalgamemanagers'))
    manager = next(obj for obj in env.objects if obj.type.name == 'ResourceManager')
    pointer = dict(manager.read_typetree()['m_Container'])['tutorial/text/firsttutorial']
    asset_file = manager.assets_file.externals[pointer['m_FileID'] - 1].path
    env = UnityPy.load(archive.read('assets/bin/Data/' + asset_file))
    text_asset = next(obj for obj in env.objects if obj.path_id == pointer['m_PathID']).read()
    payload = text_asset.m_Script
    if isinstance(payload, str):
        payload = payload.encode('utf-8', errors='surrogateescape')

source = next((ROOT / 'source-client').glob('astiob-*')) / 'PersistentData/CryptoHelper.cs'
arrays = re.findall(r'new byte\[\]\s*\{([^}]+)', source.read_text())
key, iv = [bytes(int(n) for n in re.findall(r'\d+', value)) for value in arrays[:2]]
decryptor = Cipher(TripleDES(key * 3), modes.CBC(iv)).decryptor()
raw = decryptor.update(payload) + decryptor.finalize()
padding = raw[-1]
assert 1 <= padding <= 8 and raw[-padding:] == bytes([padding]) * padding
raw = raw[:-padding]
# Only parse a single NRBF string, no object construction/deserialization.
assert raw[:17] == bytes.fromhex('0001000000ffffffff0100000000000000')
assert raw[17:22] == bytes.fromhex('0601000000')
pos, size, shift = 22, 0, 0
while True:
    value = raw[pos]
    pos += 1
    size |= (value & 127) << shift
    shift += 7
    assert shift <= 35
    if value < 128:
        break
assert raw[pos + size:] == b'\x0b'
script = raw[pos:pos + size].decode('utf-8')
(ROOT / 'research/FirstTutorial.bytes').write_bytes(payload)
(ROOT / 'research/FirstTutorial.txt').write_text(script, encoding='utf-8')
print('Extracted tutorial:', size, 'bytes; checkpoints:', re.findall(r'^#save\s+(\d+)', script, re.M))
