"""Count actual catalogued Unity objects without decoding meshes/audio payloads."""
import argparse
import json
from pathlib import Path
import UnityPy

parser = argparse.ArgumentParser()
parser.add_argument('root', type=Path)
parser.add_argument('--apply', action='store_true')
args = parser.parse_args()
base = args.root / 'AB_DATA/ANDROID/2.6.0'
types = {0: 'GameObject', 1: 'Texture2D', 2: 'TextAsset', 3: 'AudioClip',
         4: 'MonoBehaviour', 5: 'RuntimeAnimatorController', 6: 'AnimatorController',
         7: 'TextAsset', 8: 'Font'}
audit = []
for manifest_path in sorted(base.glob('*.json')):
    data = json.loads(manifest_path.read_text(encoding='utf-8'))
    for row in data['assetBundleInfoList']:
        # A single normalized resource path cannot require duplicate-name lookup.
        if len({p.lower() for p in row['objNameList']}) <= 1:
            continue
        env = UnityPy.load(str(base / data['abPath'] / (row['abName'] + '.unity3d')))
        ids = set()
        for path, ptr in env.container.items():
            if ptr.type.name != types[row['type']]:
                continue
            prefix = next((p for p in ('assets/resources/', 'assets/assetbundles/resources/') if path.startswith(p)), None)
            if prefix is None or not path[len(prefix):].startswith(data['abPath'].lower()):
                continue
            if row['type'] == 0 and not path.endswith(('.prefab', '.fbx')):
                continue
            ids.add(ptr.path_id)
        if not ids:
            raise ValueError('No objects for ' + row['abName'])
        audit.append({'bundle': row['abName'], 'old': row['realCT'], 'actual': len(ids)})
        if args.apply:
            row['realCT'] = len(ids)
    if args.apply:
        temporary = manifest_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(manifest_path)
(args.root / 'object-count-audit.json').write_text(json.dumps(audit, indent=2), encoding='utf-8')
print('Audited', len(audit), 'multi-path bundles;', sum(r['old'] != r['actual'] for r in audit), 'count corrections')
