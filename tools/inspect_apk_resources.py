"""Inspect manifest/Unity resource mappings without modifying the APK."""
from pathlib import Path
import collections
import json
import zipfile
import UnityPy
from loguru import logger
from androguard.core.apk import APK

ROOT = Path(__file__).resolve().parents[1]
logger.remove()
path = ROOT / 'downloads/linksBiu-priv_64ver.apk'
apk = APK(str(path))
manifest = {'package': apk.get_package(), 'version_name': apk.get_androidversion_name(),
            'version_code': apk.get_androidversion_code(), 'min_sdk': apk.get_min_sdk_version(),
            'target_sdk': apk.get_target_sdk_version(), 'main_activity': apk.get_main_activity()}
(ROOT / 'research/apk-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
with zipfile.ZipFile(path) as z:
    env = UnityPy.load(z.read('assets/bin/Data/globalgamemanagers'))
    trees = {o.type.name: o.read_typetree() for o in env.objects
             if o.type.name in ('BuildSettings', 'ResourceManager', 'PlayerSettings')}
for name, tree in trees.items():
    (ROOT / ('research/' + name + '.json')).write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding='utf-8')
resources = trees['ResourceManager']['m_Container']
summary = {'unity_version': trees['BuildSettings']['m_Version'], 'scenes': trees['BuildSettings']['scenes'],
           'resource_mappings': len(resources),
           'resource_categories': dict(collections.Counter(k.split('/')[0] for k, v in resources)),
           'full_gameplay_asset_coverage_verified': False}
(ROOT / 'research/resource-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False, indent=2))
