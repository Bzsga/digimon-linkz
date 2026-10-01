"""Audit the current APK's bundled monster textures; requires workspace UnityPy.

Read-only for the APK; writes only a provenance report below research/.
Run with .venv/Scripts/python.exe -X utf8 tools/extract_internal_icons.py.
"""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path
import UnityPy

ROOT = Path(__file__).resolve().parents[1]
APK = ROOT / 'client-local/linkz-local-cn-hitfix.apk'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'research/apk-internal-icons-latest.json')
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'research').resolve()) or output.suffix != '.json':
        parser.error('--output must be a JSON report inside project research')
    digest = hashlib.sha256(APK.read_bytes()).hexdigest()
    report = {'apk': str(APK.relative_to(ROOT)), 'apk_sha256': digest,
              'scope': 'ResourceManager pointers, serialized Texture2D dimensions and image payloads; no rendering.',
              'icons': {}, 'failures': []}
    with zipfile.ZipFile(APK) as archive:
        env = UnityPy.load(archive.read('assets/bin/Data/globalgamemanagers'))
        manager = next(o for o in env.objects if o.type.name == 'ResourceManager')
        for path, pointer in manager.read_typetree()['m_Container']:
            if not path.lower().startswith('characterthumbnailinternal/'):
                continue
            file_id = pointer['m_FileID']
            filename = manager.assets_file.externals[file_id - 1].path if file_id else 'globalgamemanagers'
            if file_id:
                texture_env = UnityPy.load(archive.read('assets/bin/Data/' + filename))
                objects = texture_env.objects
            else:
                objects = env.objects
            obj = next(o for o in objects if o.path_id == pointer['m_PathID'])
            tree = obj.read_typetree()
            image = tree.get('image data', b'')
            if (obj.type.name != 'Texture2D' or tree.get('m_Width', 0) <= 0 or
                    tree.get('m_Height', 0) <= 0 or not image):
                report['failures'].append({'path': path, 'reason': 'missing inline texture data or dimensions'})
                continue
            report['icons'][path.lower()] = {'file': filename, 'path_id': pointer['m_PathID'],
                'width': tree['m_Width'], 'height': tree['m_Height'], 'image_bytes': len(image),
                'serialized_sha256': hashlib.sha256(obj.get_raw_data()).hexdigest()}
    if digest != hashlib.sha256(APK.read_bytes()).hexdigest():
        raise RuntimeError('APK changed during internal icon audit')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Bundled monster textures:', len(report['icons']), 'failures:', len(report['failures']))
    raise SystemExit(1 if report['failures'] else 0)


if __name__ == '__main__':
    main()
