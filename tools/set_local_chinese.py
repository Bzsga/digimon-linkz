"""Set only language on the local test account/emulator, preserving identity/save."""
from pathlib import Path
import json
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[1]
adb = [str(root / 'android-sdk/platform-tools/adb.exe'), '-s', 'emulator-5580']
package = 'org.is_saved.digimon_links_ww'
prefs = f'/data/data/{package}/shared_prefs/{package}.v2.playerprefs.xml'
backup = root / 'runtime/backups' / ('language-' + time.strftime('%Y%m%d-%H%M%S'))
backup.mkdir(parents=True)
subprocess.run(adb + ['shell', 'am', 'force-stop', package], check=True)
raw = subprocess.check_output(adb + ['shell', 'cat', prefs])
(backup / 'playerprefs.xml').write_bytes(raw)
tree = ET.fromstring(raw)
entry = next((e for e in tree if e.attrib.get('name') == 'PlayerCountryCode'), None)
if entry is None:
    entry = ET.SubElement(tree, 'string', {'name': 'PlayerCountryCode'})
entry.text = '3'
modified = backup / 'chinese-playerprefs.xml'
modified.write_bytes(ET.tostring(tree, encoding='utf-8', xml_declaration=True))
remote_tmp = '/data/local/tmp/linkz-language.xml'
subprocess.run(adb + ['push', str(modified), remote_tmp], check=True, capture_output=True)
# Write through existing file so its ownership and SELinux label stay intact.
subprocess.run(adb + ['shell', 'sh', '-c', f'"cat {remote_tmp} > {prefs}"'], check=True)
check = ET.fromstring(subprocess.check_output(adb + ['shell', 'cat', prefs]))
assert next(e for e in check if e.attrib.get('name') == 'PlayerCountryCode').text == '3'
with sqlite3.connect(root / 'runtime/linkz.sqlite3') as db:
    with sqlite3.connect(backup / 'linkz.sqlite3') as saved:
        db.backup(saved)
    row = db.execute('SELECT state FROM accounts WHERE id=1').fetchone()
    state = json.loads(row[0])
    state['country_code'] = 3
    state['options']['103'] = '3'
    db.execute('UPDATE accounts SET state=? WHERE id=1', (json.dumps(state),))
print('Chinese locale saved; account 1 and emulator. Backup:', backup)
