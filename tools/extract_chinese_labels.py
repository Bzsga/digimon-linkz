"""Extract the APK's original Chinese UI labels; no executable deserialization."""
import hashlib
import json
import struct
import zipfile
from pathlib import Path
import UnityPy

root = Path(__file__).resolve().parents[1]
apk_path = root / 'downloads/linksBiu-priv_64ver.apk'
with zipfile.ZipFile(apk_path) as apk:
    env = UnityPy.load(apk.read('assets/bin/Data/globalgamemanagers'))
    manager = next(o for o in env.objects if o.type.name == 'ResourceManager')
    pointer = dict(manager.read_typetree()['m_Container'])['master/message_string_m3']
    filename = manager.assets_file.externals[pointer['m_FileID'] - 1].path
    env = UnityPy.load(apk.read('assets/bin/Data/' + filename))
    obj = next(o for o in env.objects if o.path_id == pointer['m_PathID'])
    raw = obj.get_raw_data()

position = 28  # Unity 2018 MonoBehaviour base fields before the aligned name.
def read_string():
    global position
    length, = struct.unpack_from('<i', raw, position)
    position += 4
    if not 0 <= length <= len(raw) - position:
        raise ValueError('Invalid serialized string length')
    value = raw[position:position + length].decode('utf-8')
    position = (position + length + 3) & ~3
    return value

assert read_string() == 'message_string_m3'
count, = struct.unpack_from('<i', raw, position)
position += 4
assert 0 < count < 10000
rows = [{'messageCode': read_string(), 'messageText': read_string()} for _ in range(count)]
assert position == len(raw)
assert len({r['messageCode'] for r in rows}) == len(rows)
out = root / 'server/localization/cn'
out.mkdir(parents=True, exist_ok=True)
(out / '990802.json').write_text(json.dumps({'messageStringM': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
report = {'source': str(apk_path), 'asset': filename, 'sha256': hashlib.sha256(raw).hexdigest(),
          'rows': len(rows), 'nonempty': sum(bool(r['messageText']) for r in rows),
          'locale': 'Original bundled Chinese (country 3); some entries are empty.'}
(root / 'research/chinese-labels.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
