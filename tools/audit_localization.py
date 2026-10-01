"""Audit translated masters in a temporary store; never open the player's save."""
import argparse
import json
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from linkz_server import Store
from localization import TEXT_FIELDS, parameters

FIELDS = TEXT_FIELDS | {'message', 'messageTitle', 'messageText'}
KANA = re.compile('[\u3040-\u30ff]')


def texts(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FIELDS and isinstance(item, str):
                yield path + (key,), item
            else:
                yield from texts(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from texts(item, path + (index,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'research/localization-audit.json')
    parser.add_argument('--pending', type=Path, default=ROOT / 'research/localization-pending.json')
    args = parser.parse_args()
    stats = defaultdict(lambda: {'nonempty': 0, 'changed': 0, 'kana_remaining': 0})
    pending, mismatches, suspicious = [], [], []
    with tempfile.TemporaryDirectory() as temporary:
        store = Store(Path(temporary) / 'audit.sqlite3')
        for api, source in sorted(store.masters.items()):
            translated = store.localization.masters.get(api, source)
            before = dict(texts(source))
            for path, value in texts(translated):
                original = before.get(path, '')
                field = path[-1]
                group = stats[api + '.' + field]
                group['nonempty'] += bool(value)
                group['changed'] += original != value
                if KANA.search(value):
                    group['kana_remaining'] += 1
                    pending.append({'api': api, 'path': list(path), 'source': original, 'current': value})
                if original and parameters(original) != parameters(value):
                    mismatches.append({'api': api, 'path': list(path)})
        original_ui = {r['messageCode']: r['messageText'] for r in store.masters['990802']['messageStringM']}
        bundled_ui = {r['messageCode']: r['messageText'] for r in store.chinese_labels['messageStringM']}
        simplified = str.maketrans(json.loads((ROOT / 'server/localization/cn/simplified-characters.json').read_text(encoding='utf-8')))
        for row in store.localization.masters['990802']['messageStringM']:
            key, value = row['messageCode'], row['messageText']
            source = original_ui.get(key, '')
            # A review queue, not proof of mistranslation. Short Chinese can be valid.
            if (value and value == bundled_ui.get(key, '').translate(simplified) and
                    (len(value) < len(source) * 0.45 or re.search(r'(?:Facebo|Faceb|Li)$', value))):
                suspicious.append({'code': key, 'source': source, 'current': value})
    report = {'scope': 'Display text only; kana counts cannot detect every Japanese kanji phrase or texture label.',
              'fields': dict(sorted(stats.items())), 'parameter_mismatches': mismatches,
              'suspected_truncations': suspicious, 'kana_remaining_total': len(pending),
              'player_database_accessed': False}
    for path, data in ((args.output, report), (args.pending, pending)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'audit': str(args.output), 'pending': str(args.pending),
          'kana_remaining': len(pending), 'parameter_mismatches': len(mismatches),
          'suspected_truncations': len(suspicious)}, ensure_ascii=False))
    if mismatches:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
