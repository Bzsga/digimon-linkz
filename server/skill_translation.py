"""Translate recognized skill-description grammar; unknown sentences stay intact."""
import re
import json
from pathlib import Path

SPECIAL = json.loads((Path(__file__).resolve().parent / 'localization/cn/skill-special.json').read_text(encoding='utf-8'))

ATTRIBUTES = {'火': '火', '水': '水', '雷': '雷', '自然': '自然', '光': '光', '闇': '暗', '無': '无'}
TARGETS = {'敵単体': '单个敌人', '敵全体': '所有敌人', '味方単体': '单个我方', '味方全体': '所有我方', '自身': '自身'}
SIZES = {'微小': '微量', '極小': '微量', '小': '少量', '中': '中量', '大': '大量', '特大': '巨量', '極大': '极大量'}
STATS = {'HP': '生命', '攻撃力': '攻击力', '防御力': '防御力', '魔法攻撃力': '魔法攻击力',
         '魔法防御力': '魔法防御力', 'スピード': '速度', 'クリティカル率': '暴击率', '命中率': '命中率',
         '最大HP': '最大生命', '与えるダメージ': '造成的伤害'}
STATUS = {'マヒ': '麻痹', '毒': '中毒', '眠り': '睡眠', 'スタン': '击晕', '混乱': '混乱'}


def translate_clause(text):
    match = re.fullmatch(r'(火|水|雷|自然|光|闇|無)属性で(敵単体|敵全体)に(複数回)?(物理|魔法)(極小|小|中|大|特大|極大)ダメージ', text)
    if match:
        attr, target, multiple, kind, size = match.groups()
        return ('对' + TARGETS[target] + ('多次' if multiple else '') + '造成' + SIZES[size] +
                ATTRIBUTES[attr] + '属性' + kind + '伤害')
    match = re.fullmatch(r'さらに(敵単体|敵全体)に(物理|魔法)(極小|小|中|大|特大|極大)ダメージ', text)
    if match:
        target, kind, size = match.groups()
        return '并对' + TARGETS[target] + '造成' + SIZES[size] + kind + '伤害'
    match = re.fullmatch(r'確率で(敵単体|敵全体)を(マヒ|毒|眠り|スタン|混乱)にする', text)
    if match:
        target, status = match.groups()
        return '有概率使' + TARGETS[target] + STATUS[status]
    match = re.fullmatch(r'確率で(マヒ|毒|眠り|スタン|混乱)にする', text)
    if match:
        return '有概率使目标' + STATUS[match[1]]
    match = re.fullmatch(r'(確率で)?ダメージの一部を吸収する', text)
    if match:
        return ('有概率' if match[1] else '') + '吸收部分伤害'
    match = re.fullmatch(r'確率で(攻撃力|防御力|魔法攻撃力|魔法防御力|スピード|命中率)をダウンさせる', text)
    if match:
        return '有概率降低目标' + STATS[match[1]]
    match = re.fullmatch(r'確率でAPを(\d+)減らす', text)
    if match:
        return '有概率减少目标' + match[1] + '点AP'
    if text == '確率でスキルを封じる':
        return '有概率封锁目标技能'
    if text == '確率でアップ補正を解除する':
        return '有概率清除目标增益修正'
    match = re.fullmatch(r'(HP|攻撃力|防御力|魔法攻撃力|魔法防御力|スピード|クリティカル率|命中率)が(微小|極小|小|中|大|特大|極大)アップ', text)
    if match:
        stat, size = match.groups()
        return STATS[stat] + SIZES[size] + '提升'
    match = re.fullmatch(r'(敵単体|敵全体|味方単体|味方全体|自身)の(攻撃力|防御力|魔法攻撃力|魔法防御力|スピード|クリティカル率|命中率)(?:が|を)(微小|極小|小|中|大|特大|極大)(アップ|ダウン)', text)
    if match:
        target, stat, size, direction = match.groups()
        return TARGETS[target] + '的' + STATS[stat] + SIZES[size] + ('提升' if direction == 'アップ' else '降低')
    match = re.fullmatch(r'(敵単体|敵全体|味方単体|味方全体|自身)のHPを(極小|小|中|大|特大|極大)回復(し)?', text)
    if match:
        target, size, _ = match.groups()
        return '为' + TARGETS[target] + '恢复' + SIZES[size] + '生命'
    if text == 'すべての状態異常を解除':
        return '解除所有异常状态'
    if text == '敵から受けた物理攻撃をカウンター':
        return '反击受到的敌人物理攻击'
    if text == '敵から受けた魔法攻撃をカウンター':
        return '反击受到的敌人魔法攻击'
    if text == '味方全体のダメージを肩がわりする':
        return '替所有我方承受伤害'
    match = re.fullmatch(r'(敵単体|敵全体|味方単体|味方全体)の(アップ|ダウン)補正を解除', text)
    if match:
        return '清除' + TARGETS[match[1]] + ('增益' if match[2] == 'アップ' else '减益') + '修正'
    match = re.fullmatch(r'(?:確率で|(\d+)%の確率で)(敵単体|敵全体)のスキルを封じる', text)
    if match:
        chance, target = match.groups()
        return (chance + '%概率' if chance else '有概率') + '封锁' + TARGETS[target] + '技能'
    match = re.fullmatch(r'(\d+)%の確率で(敵単体|敵全体)を(マヒ|毒|眠り|スタン|混乱)にする', text)
    if match:
        chance, target, status = match.groups()
        return chance + '%概率使' + TARGETS[target] + STATUS[status]
    match = re.fullmatch(r'(無|火|水|雷|自然|光|闇)属性の耐性がアップ', text)
    if match:
        return ATTRIBUTES[match[1]] + '属性抗性提升'
    match = re.fullmatch(r'(毒|混乱|マヒ|眠り|スタン|スキル封印|即死)耐性がアップ', text)
    if match:
        return {**STATUS, 'スキル封印': '技能封印', '即死': '即死'}[match[1]] + '抗性提升'
    match = re.fullmatch(r'(味方単体|自身)の(毒|混乱|マヒ|眠り|スタン|スキル封印|すべての状態異常)を解除', text)
    if match:
        return '解除' + TARGETS[match[1]] + '的' + {**STATUS, 'スキル封印': '技能封印', 'すべての状態異常': '所有异常状态'}[match[2]]
    match = re.fullmatch(r'HPが(\d+)%(以下)?で(.+)がアップ', text)
    if match and match[3] in STATS:
        return '生命为' + match[1] + '%' + ('及以下' if match[2] else '') + '时，' + STATS[match[3]] + '提升'
    match = re.fullmatch(r'リーダーと同種族で(.+)がアップ', text)
    if match and match[1] in STATS:
        return '与队长同种族时，' + STATS[match[1]] + '提升'
    match = re.fullmatch(r'(無|火|水|雷|自然|光|闇)属性スキル攻撃を受ける時、バトル中に(\d+)回まで被ダメージ-(\d+)%', text)
    if match:
        attr, count, percent = match.groups()
        return '受到' + ATTRIBUTES[attr] + '属性技能攻击时，承受伤害减少' + percent + '%，每场战斗最多' + count + '次'
    match = re.fullmatch(r'攻撃相手の種族が(.+)の時、与ダメージ\+(\d+)%。装着時DEF-(\d+)%､S-DEF-(\d+)%', text)
    if match:
        tribe, damage, defense, magic = match.groups()
        tribes = {'幻生系': '幻生系', '陽炎系': '阳炎系', '氷河系': '冰河系', '大地系': '大地系', '電磁系': '电磁系', '深淵系': '深渊系', '光芒系': '光芒系'}
        if tribe in tribes:
            return '攻击' + tribes[tribe] + '敌人时，造成伤害+' + damage + '%。装备时防御-' + defense + '%、魔防-' + magic + '%'
    match = re.fullmatch(r'バトル中に(\d+)回まで敵を戦闘不能にすると(\d+)ターン与ダメ(\d+)%アップ', text)
    if match:
        count, turns, percent = match.groups()
        return '击倒敌人后，' + turns + '回合内造成伤害提升' + percent + '%，每场战斗最多' + count + '次'
    match = re.fullmatch(r'バトル中に(\d+)回まで敵を戦闘不能にすると自分のHPを(\d+)%回復させる', text)
    if match:
        return '击倒敌人后，恢复自身' + match[2] + '%生命，每场战斗最多' + match[1] + '次'
    return None


def translate_description(text):
    if text in SPECIAL:
        return SPECIAL[text]
    if text.endswith('。バトル中1回使用可能'):
        body = text.removesuffix('。バトル中1回使用可能')
        translated = translate_description(body)
        return translated + '。每场战斗可使用1次' if translated != body else text
    if text.startswith('【ストーリー専用】'):
        body = text.removeprefix('【ストーリー専用】')
        translated = translate_description(body)
        return '【剧情专用】' + translated if translated != body else text
    duration = re.match(r'^(\d+)ラウンドの間、', text)
    body = text[duration.end():] if duration else text
    whole = translate_clause(body)
    if whole is not None:
        return ((duration[1] + '回合内，') if duration else '') + whole
    clauses = [translate_clause(part) for part in body.split('、')]
    if not clauses or any(part is None for part in clauses):
        return text
    return ((duration[1] + '回合内，') if duration else '') + '，'.join(clauses)
