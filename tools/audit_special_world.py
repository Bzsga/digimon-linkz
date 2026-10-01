"""Read-only evidence for the two original special-partner reward routes.

Run with the project's optional UnityPy/cryptography/capstone environment.
Never imports the server, opens a save, starts a process, or modifies an APK.
NRBF strings are decoded as a narrow byte layout, never deserialized/executed.
"""
import argparse
import base64
import hashlib
import io
import json
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'source-client/astiob-digimon-linkz-client-250d17b'


def fingerprint(path):
    raw = path.read_bytes()
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def decode_serialized_string(payload):
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    # Original PersistentData/CryptoHelper default DES key/IV. Repeating the
    # DES key three times makes EDE equivalent to DES, without 8-byte-key use.
    key = bytes((38, 15, 34, 201, 85, 108, 209, 42)) * 3
    decrypt = Cipher(TripleDES(key), modes.CBC(bytes((246, 34, 216, 121, 8, 35, 78, 88)))).decryptor()
    raw = decrypt.update(payload) + decrypt.finalize()
    unpad = padding.PKCS7(64).unpadder()
    raw = unpad.update(raw) + unpad.finalize()
    # SerializedStreamHeader(root1/header-1/version1.0), BinaryObjectString(id1).
    prefix = bytes.fromhex('0001000000ffffffff01000000000000000601000000')
    if not raw.startswith(prefix) or len(raw) < len(prefix) + 2:
        raise ValueError('Unsupported NRBF string layout')
    position, length, shift = len(prefix), 0, 0
    while position < len(raw) and shift < 35:
        value = raw[position]
        position += 1
        length |= (value & 127) << shift
        shift += 7
        if value < 128:
            break
    else:
        raise ValueError('Unsupported NRBF string length')
    if len(raw) != position + length + 1 or raw[-1] != 11:
        raise ValueError('Unsupported NRBF string trailing records')
    return raw[position:-1].decode('utf-8')


def decode_dungeon_id(encoded):
    ident = decode_serialized_string(base64.b64decode(encoded, validate=True))
    if not ident.isdecimal():
        raise ValueError('Non-numeric event dungeon ID')
    return ident


def native_entry(apk):
    from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
    from elftools.elf.elffile import ELFFile
    symbols_path = ROOT / 'tools/il2cpp-dumper/script.json'
    symbols = json.loads(symbols_path.read_text(encoding='utf-8'))
    methods = symbols['ScriptMethod']
    names = {m['Address']: m['Name'] for m in methods}
    strings = {s['Address']: s['Value'] for s in symbols['ScriptString']}
    with zipfile.ZipFile(apk) as archive:
        raw = archive.read('lib/arm64-v8a/libil2cpp.so')
        metadata = archive.read('assets/bin/Data/Managed/Metadata/global-metadata.dat')
    source_metadata = (ROOT / 'research/il2cpp/global-metadata.dat').read_bytes()
    # Local endpoint patch changes literal lengths/data in place. Validate
    # every other byte and every literal index, rather than accepting a hash
    # mismatch as proof that the symbol addresses are still usable.
    header = struct.unpack_from('<6I', source_metadata)
    _, _, table_offset, table_size, data_offset, data_size = header
    if (len(metadata) != len(source_metadata) or metadata[:table_offset] != source_metadata[:table_offset] or
            metadata[table_offset + table_size:data_offset] != source_metadata[table_offset + table_size:data_offset] or
            metadata[data_offset + data_size:] != source_metadata[data_offset + data_size:]):
        raise ValueError('Non-literal metadata structure changed since symbol extraction')
    changed_literals = {}
    for entry in range(table_offset, table_offset + table_size, 8):
        old_length, old_index = struct.unpack_from('<2I', source_metadata, entry)
        length, index = struct.unpack_from('<2I', metadata, entry)
        if index != old_index or index + length > data_size or old_index + old_length > data_size:
            raise ValueError('Literal offset changed or out of bounds')
        old_value = source_metadata[data_offset + index:data_offset + index + old_length].decode('utf-8')
        value = metadata[data_offset + index:data_offset + index + length].decode('utf-8')
        if old_value != value:
            if old_value in changed_literals and changed_literals[old_value] != value:
                raise ValueError('Ambiguous changed string literal')
            changed_literals[old_value] = value
    original = (ROOT / 'research/il2cpp/libil2cpp.so').read_bytes()

    def at(library, address, size):
        elf = ELFFile(io.BytesIO(library))
        segment = next(s for s in elf.iter_segments() if s['p_type'] == 'PT_LOAD' and
                       s['p_vaddr'] <= address and address + size <= s['p_vaddr'] + s['p_filesz'])
        offset = address - segment['p_vaddr'] + segment['p_offset']
        return library[offset:offset + size]

    decoder = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    decoder.detail = True
    output = []
    for name in ('Quest.QuestData$$GetWorldDungeonInfo', 'Quest.QuestData$$AddWD_DngInfoDataList'):
        method = next(m for m in methods if m['Name'] == name)
        start = method['Address']
        end = min(a for a in names if a > start)
        region = at(raw, start, end - start)
        if region != at(original, start, end - start):
            raise ValueError('Audited method changed since symbol extraction: ' + name)
        instructions, pages = [], {}
        for instruction in decoder.disasm(region, start):
            note = None
            operands = instruction.operands
            if instruction.mnemonic == 'ldr' and len(operands) == 2 and operands[1].type == 3:
                memory = operands[1].mem
                if memory.base in pages and memory.index == 0:
                    slot = pages[memory.base] + memory.disp
                    try:
                        pointer = struct.unpack('<Q', at(raw, slot, 8))[0]
                        if pointer in strings:
                            value = strings[pointer]
                            note = {'literal': changed_literals.get(value, value), 'slot': hex(slot), 'pointer': hex(pointer)}
                    except (StopIteration, struct.error):
                        pass
            elif instruction.mnemonic in ('bl', 'b'):
                callee = names.get(operands[0].imm)
                if callee:
                    note = {'callee': callee}
            for register in instruction.regs_access()[1]:
                pages.pop(register, None)
            if instruction.mnemonic == 'adrp':
                pages[operands[0].reg] = operands[1].imm
            instructions.append({'address': hex(instruction.address), 'mnemonic': instruction.mnemonic,
                                 'operands': instruction.op_str, 'annotation': note})
        if len(instructions) * 4 != len(region):
            raise ValueError('Incomplete ARM64 method disassembly: ' + name)
        output.append({'name': name, 'start': hex(start), 'end': hex(end),
                       'bytes_sha256': hashlib.sha256(region).hexdigest(),
                       'same_as_symbol_input_library': True, 'instructions': instructions})
    return {'architecture': 'arm64-v8a', 'metadata_same_except_literal_contents': True,
            'metadata_literal_changes': changed_literals,
            'library_sha256': hashlib.sha256(raw).hexdigest(), 'methods': output,
            'scope': 'Static ARM64 instructions and literal pointers; no execution, no ARMv7 verification.'}


def audit():
    import UnityPy
    masters, inputs = {}, {}
    master_dir = next((ROOT / 'source-masters').glob('astiob-*'))
    for api in ('990103', '990902', '990903', '990904', '990905', '990909', '990910', '990911', '990912', '999101'):
        path = next(master_dir.glob('* ' + api + ' *.json'))
        masters[api] = json.loads(path.read_text(encoding='utf-8-sig'))
        inputs[str(path.relative_to(ROOT))] = fingerprint(path)
    dungeons = {r['worldDungeonId']: r for r in masters['990905']['worldDungeonM']}
    stages = {r['worldStageId']: r for r in masters['990904']['worldStageM']}
    areas = {r['worldAreaId']: r for r in masters['990903']['worldAreaM']}
    rewards = masters['990909']['worldDungeonRewardM']
    adventure = masters['990912']['worldDungeonAdventureSceneM']
    catalog, manifests = {}, []
    base = ROOT / 'assets-restored-all/AB_DATA/ANDROID/2.6.0'
    index_path = ROOT / 'assets-restored-all/local-index.json'
    inputs[str(index_path.relative_to(ROOT))] = fingerprint(index_path)
    for entry in json.loads(index_path.read_text(encoding='utf-8-sig'))['assetBundleFileInfo']:
        path = base / entry['name']
        manifests.append({'path': str(path.relative_to(ROOT)), **fingerprint(path)})
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
        for row in manifest['assetBundleInfoList']:
            bundle = base / manifest['abPath'] / (row['abName'] + '.unity3d')
            for key in row['objNameList']:
                catalog.setdefault((manifest['abPath'] + key).lower(), []).append((bundle, int(row['size'])))
    event_bundles = {p for p, size in catalog['quest/list/questeventinfolist']}
    event_lists = []
    for path in sorted(event_bundles):
        inputs[str(path.relative_to(ROOT))] = fingerprint(path)
        environment = UnityPy.load(str(path))
        objects = [o.read_typetree() for o in environment.objects if o.type.name == 'MonoBehaviour']
        tree = next(t for t in objects if t.get('m_Name') == 'QuestEventInfoList')
        rows = [{'worldDungeonId': decode_dungeon_id(r['dungeonId']), 'scriptFileName': r['scriptFileName']}
                for r in tree['eventInfo']]
        if len({r['worldDungeonId'] for r in rows}) != len(rows):
            raise ValueError('Duplicate decoded event IDs')
        event_lists.append(rows)
    if not event_lists or any(rows != event_lists[0] for rows in event_lists):
        raise ValueError('Event list aliases disagree')
    event_map = {r['worldDungeonId']: r['scriptFileName'] for r in event_lists[0]}
    targets, required = [], {'quest/list/questeventinfolist'}
    for target in ('11837', '9100016'):
        reverse, current = [], target
        while current not in ('0', ''):
            if current in reverse or current not in dungeons:
                raise ValueError('Cyclic or missing prerequisite: ' + current)
            reverse.append(current)
            current = dungeons[current]['prerequisiteWorldDungeonId']
        chain = list(reversed(reverse))
        rows = []
        for ident in chain:
            dungeon = dungeons[ident]
            stage = stages[dungeon['worldStageId']]
            area = areas[stage['worldAreaId']]
            scenes = [r for r in adventure if r['worldDungeonId'] == ident]
            script = event_map.get(ident)
            if script:
                required.add('adventurescene/text/' + script)
            for scene in scenes:
                required.add('adventurescene/text/' + scene['adventureValue'])
            required.add('stages/' + dungeon['background'].lower() + '/prefab')
            required.update('bgm/' + dungeon[k] + '/sound' for k in ('bgm', 'bossBgm', 'exBossBgm')
                            if dungeon[k] not in ('0', ''))
            rows.append({'dungeon': dungeon, 'stage': stage, 'area': area,
                         'pure_story_script': script, 'in_battle_adventure_scenes': scenes,
                         'rewards': [r for r in rewards if r['worldDungeonId'] == ident]})
        targets.append({'target': target, 'chain': chain, 'chain_count': len(chain), 'entries': rows,
                        'unimplemented_world6_count': sum(r['area']['worldId'] == '6' for r in rows),
                        'pure_story_count': sum(bool(r['pure_story_script']) for r in rows)})
    # Resolve direct load commands from the eight scripts in the first reward
    # chain. Generic tutorial UI/effect commands and transitive prefab references
    # remain a separate boundary, not silently treated as verified dependencies.
    script_checks, bundle_cache = [], {}
    groups = {r['monsterGroupId']: r for r in masters['990103']['monsterM']}
    messages = {r['tutorialNaviId'] for r in masters['999101']['tutorialNaviM']}
    chinese_path = ROOT / 'server/localization/cn/tutorial-messages.json'
    chinese = json.loads(chinese_path.read_text(encoding='utf-8'))
    inputs[str(chinese_path.relative_to(ROOT))] = fingerprint(chinese_path)
    for entry in targets[0]['entries']:
        script = entry['pure_story_script']
        if not script:
            continue
        bundle = catalog['adventurescene/text/' + script][0][0]
        inputs[str(bundle.relative_to(ROOT))] = fingerprint(bundle)
        if bundle not in bundle_cache:
            bundle_cache[bundle] = UnityPy.load(str(bundle))
        environment = bundle_cache[bundle]
        text = next(o.read() for o in environment.objects if o.type.name == 'TextAsset' and o.read().m_Name == script)
        payload = text.m_Script
        if isinstance(payload, str):
            payload = payload.encode('utf-8', errors='surrogateescape')
        decoded = decode_serialized_string(payload)
        dependencies, referenced_messages = set(), []
        lines = decoded.splitlines()
        for number, line in enumerate(lines):
            tokens = line.split()
            if not tokens or not tokens[0].startswith('#'):
                continue
            command = tokens[0]
            if command == '#adv_load_stage':
                dependencies.add('stages/' + tokens[1] + '/prefab')
            elif command == '#adv_load_camera_anm':
                dependencies.add('adventurescene/animation/camera/' + tokens[1])
            elif command == '#adv_load_chara_anm':
                dependencies.add('adventurescene/animation/character/' + tokens[2])
            elif command == '#adv_load_effect':
                dependencies.add('adventurescene/effect/' + tokens[2])
            elif command == '#adv_load_chara':
                if len(tokens) != 4 or tokens[3] != 'clearAnime':
                    raise ValueError('Character skill dependency loading is not covered: ' + line)
                dependencies.add('characters/' + groups[tokens[2]]['modelId'] + '/prefab')
            elif command == '#bgm' and len(tokens) > 3:
                dependencies.add(tokens[3])
            elif command == '#se' and len(tokens) > 4:
                dependencies.add(tokens[4])
            elif command == '#msg':
                value = next(l.strip() for l in lines[number + 1:] if l.strip() and not l.lstrip().startswith(';'))
                if not value.isdecimal():
                    raise ValueError('Non-ID message command is not covered: ' + value)
                referenced_messages.append(value)
        dependencies = sorted(k.lower() for k in dependencies)
        required.update(dependencies)
        script_checks.append({'dungeonId': entry['dungeon']['worldDungeonId'], 'scriptFileName': script,
                              'bundle': str(bundle.relative_to(ROOT)),
                              'payload_sha256': hashlib.sha256(payload).hexdigest(),
                              'decoded_utf8_sha256': hashlib.sha256(decoded.encode('utf-8')).hexdigest(),
                              'direct_load_paths': dependencies, 'messageIds': referenced_messages,
                              'missing_master_messages': sorted(set(referenced_messages) - messages, key=int),
                              'messages_without_chinese_overlay': sorted(set(referenced_messages) - set(chinese), key=int)})
    apk = ROOT / 'client-local/linkz-local-cn-hitfix.apk'
    with zipfile.ZipFile(apk) as archive:
        manager = next(o for o in UnityPy.load(archive.read('assets/bin/Data/globalgamemanagers')).objects
                       if o.type.name == 'ResourceManager')
        internal_keys = {k.lower() for k, p in manager.read_typetree()['m_Container']}
    asset_checks = []
    for key in sorted(required):
        entries = catalog.get(key, [])
        candidates = []
        for path, size in entries:
            exists = path.is_file()
            candidates.append({'path': str(path.relative_to(ROOT)), 'exists': exists,
                               'size_matches': exists and path.stat().st_size == size})
            if exists:
                inputs[str(path.relative_to(ROOT))] = fingerprint(path)
        asset_checks.append({'key': key, 'catalog_candidates': candidates,
                             'apk_resource_key_present': key in internal_keys,
                             'resolved': any(c['size_matches'] for c in candidates) or key in internal_keys})
    source_paths = ('Assembly-CSharp/Quest/QuestData.cs', 'Assembly-CSharp/Quest/QuestStart.cs',
                    'Assembly-CSharp/Quest/QuestEventInfoList.cs', 'PersistentData/CryptoHelper.cs',
                    'Assembly-CSharp-firstpass/TypeSerialize/TypeSerializeHelper.cs',
                    'Assembly-CSharp/BattleAdventureSceneManager.cs', 'Assembly-CSharp/PartsMenu.cs',
                    'Assembly-CSharp/GUIListPartsQuestSelect.cs', 'Assembly-CSharp/GUISelectPanelA_StageL.cs',
                    'Assembly-CSharp/GameWebAPI.cs', 'Assembly-CSharp/AssetDataMng.cs',
                    'Assembly-CSharp/APIUtil.cs', 'Assembly-CSharp/DataMng.cs',
                    'Assembly-CSharp/BattleDataStore.cs', 'Assembly-CSharp/BattleStateTimeOver.cs',
                    'Assembly-CSharp/AdventureScene/AdventureScriptEngine.cs',
                    'Assembly-CSharp/AdventureScene/LoadStageCommand.cs',
                    'Assembly-CSharp/AdventureScene/LoadCharaCommand.cs',
                    'Assembly-CSharp/AdventureScene/LoadCameraAnimeCommand.cs',
                    'Assembly-CSharp/AdventureScene/LoadEffectCommand.cs',
                    'Assembly-CSharp/AdventureScene/LoadCharaAnimeCommand.cs')
    for relative in source_paths:
        path = SOURCE / relative
        inputs[str(path.relative_to(ROOT))] = fingerprint(path)
    inputs[str(apk.relative_to(ROOT))] = fingerprint(apk)
    for relative in ('tools/il2cpp-dumper/script.json', 'research/il2cpp/libil2cpp.so',
                     'research/il2cpp/global-metadata.dat', 'server/linkz_server.py', 'tools/audit_special_world.py'):
        inputs[relative] = fingerprint(ROOT / relative)
    web_images = {r[field] for t in targets for e in t['entries'] if e['area']['worldId'] == '6'
                  for r, field in ((e['stage'], 'stageImage'), (e['area'], 'img'))
                  if r.get(field) not in (None, '', '0', 'null')}
    image_checks = [{'url_path': '/asset/img/events/' + name,
                     'local_file': 'assets-restored-all/img/events/' + name,
                     'file_present': (ROOT / 'assets-restored-all/img/events' / name).is_file()}
                    for name in sorted(web_images)]
    return {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
            'status': 'read_only_source_native_and_prerequisite_audit',
            'game_started': False, 'formal_save_written': False, 'server_reloaded': False,
            'targets': targets, 'event_list': event_lists[0], 'assets': asset_checks,
            'first_chain_script_checks': script_checks,
            'web_event_images': image_checks,
            'missing_catalog_assets': [r['key'] for r in asset_checks if not r['resolved']],
            'routing_summary': {
                'query_world_ids': ['1', '3', '8'], 'event_api': '090101',
                'world6_delivery': 'The world3 event response groups stages by worldAreaId, including world6 areas other than area1/8. No separate world6 query is required by the reviewed source.',
                'pure_story_contract': 'QuestStart.StartEventStage uses090002; after the script, EndEventScene sets clear1 and aliveInfo[1,1,1], without ordinary enemy defeat processing.',
                'multi_flag_2': 'Solo only, per WorldDungeonM.IsSoloOnly; it does not require multiplayer.',
                'original_core_source': {'dungeon': '9100011', 'reward': '9100012', 'soulId': '4000009', 'quantity': 1, 'repeatable': False},
                'implemented': False, 'client_runtime_verified': False},
            'native_entry': native_entry(apk), 'inputs': inputs, 'catalog_manifests': manifests,
            'scope': 'Original prerequisites/rewards, decoded first-chain script direct load paths/message IDs, declared bundle sizes and APK ResourceManager key presence. Internal object contents, generic tutorial UI/effects, transitive dependencies, script playback, web images, encounters and gameplay remain unverified.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'research/special-world-entry-audit-20261001.json')
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'research').resolve()) or output.suffix != '.json':
        parser.error('--output must be JSON inside research')
    report = audit()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'report': str(output), 'targets': [{k: t[k] for k in
          ('target', 'chain_count', 'unimplemented_world6_count', 'pure_story_count')} for t in report['targets']],
          'event_list_count': len(report['event_list']), 'missing_catalog_assets': report['missing_catalog_assets'],
          'scope': report['scope']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
