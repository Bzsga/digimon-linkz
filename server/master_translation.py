"""Full-match translations for repeated master text, leaving unknown text intact."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent / 'localization/cn'
SKILL_NAMES = json.loads((ROOT / 'skill-names.json').read_text(encoding='utf-8'))
TRIBES = {'幻生系': '幻生系', '陽炎系': '阳炎系', '氷河系': '冰河系',
          '大地系': '大地系', '電磁系': '电磁系', '深淵系': '深渊系', '光芒系': '光芒系'}
ATTRIBUTES = {'火': '火', '水': '水', '雷': '雷', '自然': '自然', '光': '光', '闇': '暗', '無': '无'}


def skill_name(text):
    if text in SKILL_NAMES:
        return SKILL_NAMES[text]
    # Roman levels and chip grades retain their exact spelling.
    suffix = re.fullmatch(r'(.+?)(\s*(?:VIII|VII|VI|IV|III|II|IX|I|V|X|[A-GSαβγΩ]))', text)
    if suffix and suffix[1] in SKILL_NAMES:
        return SKILL_NAMES[suffix[1]] + suffix[2]
    prefix = re.fullmatch(r'(オール)?(攻撃|防御|魔法攻撃|魔法防御|スピード|命中)(アップ|ダウン)([IVX]+|[ABCS])', text)
    if prefix:
        all_targets, stat, direction, grade = prefix.groups()
        stats = {'攻撃': '攻击', '防御': '防御', '魔法攻撃': '魔攻', '魔法防御': '魔防', 'スピード': '速度', '命中': '命中'}
        return ('全体' if all_targets else '') + stats[stat] + ('提升' if direction == 'アップ' else '降低') + grade
    status = re.fullmatch(r'(オール)?(グラン|ハイ)?(スタン|スリープ|パニック|パラライズ|ポイズン|ロック|スキルロック)([ABCS])?', text)
    if status:
        all_targets, strength, effect, grade = status.groups()
        effects = {'スタン': '击晕', 'スリープ': '睡眠', 'パニック': '混乱', 'パラライズ': '麻痹', 'ポイズン': '中毒', 'ロック': '技能封印', 'スキルロック': '技能封印'}
        return ('全体' if all_targets else '') + {None: '', 'ハイ': '强效', 'グラン': '极效'}[strength] + effects[effect] + (grade or '')
    match = re.fullmatch(r'(光|水|炎|無|自然|闇|雷)マモール ([A-GS])', text)
    if match:
        return {'光': '光', '水': '水', '炎': '火', '無': '无', '自然': '自然', '闇': '暗', '雷': '雷'}[match[1]] + '属性守护 ' + match[2]
    match = re.fullmatch(r'(幻生|陽炎|氷河|大地|電磁|深淵|光芒)キラー ([A-GS])', text)
    if match:
        return TRIBES[match[1] + '系'] + '克星 ' + match[2]
    return text


def translate(api, field, text, phrases):
    if api == '990108' and field == 'name':
        return skill_name(text)
    if api == '990201':
        match = re.fullmatch(r'(火|水|雷|自然|光|闇|無|全|コア|ハル)(?:の)?(アップデータ|プラグインver[1-4]\.0|リファクター)', text)
        if match:
            prefix = {**ATTRIBUTES, '全': '全属性', 'コア': '核心', 'ハル': '外壳'}[match[1]]
            kind = match[2]
            return prefix + {'アップデータ': '升级程序', 'リファクター': '重构因子'}.get(kind, kind.replace('プラグインver', '插件ver'))
        match = re.fullmatch(r'(.+)のデータの欠片', text)
        if match and match[1] in phrases:
            return phrases[match[1]] + '的数据碎片'
        match = re.fullmatch(r'(.+)への進化の際に必要となるデータの欠片', text)
        if match and match[1] in phrases:
            return '进化为' + phrases[match[1]] + '所需的数据碎片'
        match = re.fullmatch(r'「(火|水|雷|自然|光|闇|無)」属性の(わずかな力|力|強い力|強大な力)が組み込まれたプログラムで進化時に必要。', text)
        if match:
            strength = {'わずかな力': '少量力量', '力': '力量', '強い力': '强大力量', '強大な力': '极强力量'}[match[2]]
            return '含有' + ATTRIBUTES[match[1]] + '属性' + strength + '的程序，进化时需要。'
        match = re.fullmatch(r'(幻生系|陽炎系|氷河系|大地系|電磁系|深淵系|光芒系)のデジモンをバージョンアップする為に必要となるプログラム', text)
        if match:
            return '升级' + TRIBES[match[1]] + '数码兽版本所需的程序'
        match = re.fullmatch(r'バージョンアップした(幻生系|陽炎系|氷河系|大地系|電磁系|深淵系|光芒系)のデジモンを状態異常耐性変化する際に必要となるカード媒体', text)
        if match:
            return '已升级版本的' + TRIBES[match[1]] + '数码兽改变异常状态抗性时所需的卡片介质'
    if api == '990308':
        match = re.fullmatch(r'累積ミッションの「(\d+)人とフレンドになろう」をクリアで開放', text)
        if match:
            return '完成累计任务“结交' + match[1] + '位好友”后解锁'
    if api == '990312':
        match = re.fullmatch(r'(.+)開放鍵', text)
        if match and match[1] in phrases:
            return phrases[match[1]] + '解锁钥匙'
        match = re.fullmatch(r'「(.+)」施設ショップ追加', text)
        if match and match[1] in phrases:
            return '在设施商店中追加“' + phrases[match[1]] + '”'
    if api == '991205':
        match = re.fullmatch(r'([ABCS]\d?)ランク', text)
        if match:
            return match[1] + '段位'
    if api == '991301' and field == 'name':
        match = re.fullmatch(r'(物攻|物防|魔攻|魔防|速さ|HP)UP ([A-GS])', text)
        if match:
            return {'物攻': '物攻', '物防': '物防', '魔攻': '魔攻', '魔防': '魔防', '速さ': '速度', 'HP': 'HP'}[match[1]] + '提升 ' + match[2]
    if api == '991301' and field == 'detail':
        match = re.fullmatch(r'((?:S-)?(?:ATK|DEF)|最大HP|HP|SPD)(\+\d+)', text)
        if match:
            return {'ATK': '物攻', 'DEF': '物防', 'S-ATK': '魔攻', 'S-DEF': '魔防', 'HP': 'HP', '最大HP': '最大HP', 'SPD': '速度'}[match[1]] + match[2]
        match = re.fullmatch(r'((?:S-)?(?:ATK|DEF)|HP|SPD)(\+\d+) (幻生系|陽炎系|氷河系|大地系|電磁系|深淵系|光芒系)なら更に((?:S-)?(?:ATK|DEF)|HP|SPD)(\+\d+)', text)
        if match:
            stat, amount, tribe, bonus_stat, bonus = match.groups()
            return stat + amount + '；' + TRIBES[tribe] + '额外' + bonus_stat + bonus
    return text
