"""Fetch checksum-verified Google Android test tools into this workspace."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def install(package):
    dest = ROOT / 'downloads/android' / package['url']
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or dest.stat().st_size != package['size']:
        partial = dest.with_suffix('.partial')
        print('Downloading', package['package'], package['size'], flush=True)
        with urllib.request.urlopen(package['base'] + package['url'], timeout=60) as response, partial.open('wb') as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
        if partial.stat().st_size != package['size']:
            raise ValueError('Download size mismatch: ' + package['package'])
        partial.replace(dest)
    digest = hashlib.sha1()
    with dest.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != package['checksum']:
        raise ValueError('Checksum mismatch: ' + package['package'])
    targets = {'build-tools;30.0.3': ROOT / 'android-sdk/build-tools',
               'system-images;android-30;google_apis;x86_64': ROOT / 'android-sdk/system-images/android-30/google_apis'}
    target = targets.get(package['package'], ROOT / 'android-sdk')
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest) as archive:
        for name in archive.namelist():
            (target / name).resolve().relative_to(target.resolve())
        archive.extractall(target)
    print('Verified and extracted', package['package'], flush=True)


if __name__ == '__main__':
    packages = json.loads((ROOT / 'research/android-packages.json').read_text())
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(install, packages))
