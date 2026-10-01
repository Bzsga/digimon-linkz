"""Read original bundles and export the farm grid and all master facility footprints.

Writes only server/farm-layout.json. Does not modify bundles or the player's save.
"""
import hashlib
import json
from pathlib import Path
import UnityPy

root = Path(__file__).resolve().parents[1]
base = root / 'assets-restored-all/AB_DATA/ANDROID/2.6.0'
sources = []


def behaviours(path):
    sources.append({'path': str(path.relative_to(root)),
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return [obj.read_typetree() for obj in UnityPy.load(str(path)).objects
            if obj.type.name == 'MonoBehaviour']


field = next(d for d in behaviours(base / 'Farm/Fields/Farm_Fields_0000_AS.unity3d')
             if d.get('m_Name') == 'FieldData_01')
catalog = json.loads((base / 'Farm_Builds.json').read_text(encoding='utf-8'))
master = next((root / 'source-masters').glob('astiob-*'))
facilities = json.loads(next(master.glob('* 990301 *')).read_text(encoding='utf-8-sig'))['facilityM']
footprints = {}
for row in facilities:
    ident = row['facilityId']
    bundle = next(r for r in catalog['assetBundleInfoList']
                  if row['modelResource'].lower() in [n.lower() for n in r['objNameList']])
    data = next(d for d in behaviours(base / catalog['abPath'] / (bundle['abName'] + '.unity3d'))
                if all(k in d for k in ('sizeX', 'sizeY', 'baseGridPositionX', 'baseGridPositionY')))
    footprints[ident] = {k: data[k] for k in ('sizeX', 'sizeY', 'baseGridPositionX', 'baseGridPositionY')}
result = {'provenance': 'Original FieldData_01 and FarmObject prefab serialized values; no assets modified.',
          'fields': [{'width': f['fieldHorizontal'], 'height': f['fieldVertical'],
                      'invalid': [i for i, grid in enumerate(f['grids']) if grid['invalid']]}
                     for f in field['fieldData']],
          'footprints': footprints, 'sources': sources}
(root / 'server/farm-layout.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print('Exported', len(result['fields']), 'fields and footprints:', footprints)
