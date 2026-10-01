"""Public help text and local rules. No player data or external page resources."""
from html import escape
from html.parser import HTMLParser
from fractions import Fraction
from urllib.parse import parse_qs, urlencode
import re


class PlainHelp(HTMLParser):
    """Keep historical text without executing HTML or requesting old screenshots."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'li', 'tr') and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'div', 'li', 'tr') and not self.hidden:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def paragraphs(text):
    parser = PlainHelp()
    parser.feed(text)
    return ''.join('<p>' + escape(line.strip()) + '</p>'
                   for line in ''.join(parser.parts).splitlines() if line.strip())


def page(title, content):
    return ('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>''' + escape(title) + ''' · Linkz 本地帮助</title><style>
*{box-sizing:border-box}body{margin:0;background:#10212d;color:#e6edf1;font:16px/1.8 sans-serif}
main{max-width:850px;margin:auto;padding:20px}nav{display:flex;gap:18px;flex-wrap:wrap}
a{color:#7fd4eb}h1{font-size:25px}h2{font-size:20px}p{margin:12px 0}
.note{background:#193441;border-left:4px solid #69c2d9;padding:14px;margin:22px 0}
.muted{color:#b4c5cc}section{margin:24px 0}ul{padding-left:24px}li{margin:6px 0}
input{width:70%;max-width:420px}input,button{font:inherit;padding:7px;border-radius:4px}
table{width:100%;border-collapse:collapse}th,td{padding:9px;text-align:left;border-bottom:1px solid #38505e}
.scroll{overflow-x:auto}footer{margin-top:28px;color:#b4c5cc;font-size:14px}
</style></head><body><main><nav><a href="/help/">帮助目录</a>
<a href="/help/materials">材料来源</a></nav><h1>''' + escape(title) + '</h1>' + content +
        '<footer>本地重建版本 · 原版帮助采用文字版，历史截图未恢复。新增功能尚待统一游戏验证。</footer>'
        '</main></body></html>').encode('utf-8')


class HelpPages:
    def __init__(self, store):
        self.original = {r['helpId']: r for r in store.masters['991002']['helpM']}
        self.chinese = {r['helpId']: r for r in store.localization.masters['991002']['helpM']}
        self.categories = store.localization.masters['991001']['helpCategoryM']
        self.gameplay = store.local_gameplay
        self.chip_names = {int(r['chipId']): r['name'] for r in store.localization.masters['991301']['chipM']}
        self.missions = store.progression.config
        names = {r['soulId']: r['soulName'] for r in store.localization.masters['990201']['soulM']}
        dungeons = {r['worldDungeonId']: r for r in store.masters['990905']['worldDungeonM']}
        self.materials = {}
        for ident, config in store.normal_world.items():
            for drop in store.normal_material_drops(config):
                sid = str(drop['assetValue'])
                name = names[sid]
                amount = drop['assetNum'] * int(dungeons[ident]['battleNum'])
                self.materials.setdefault(sid, {'name': name, 'sources': []})['sources'].append((int(ident), amount))
        self.dungeon_count = len(store.normal_world)

    def local_notes(self, ident):
        g, m = self.gameplay, self.missions
        notes = []
        if ident in ('1', '15', '16', '19', '23', '35', '54'):
            notes.append(f'当前本地普通探索开放{self.dungeon_count}关，插件与数据碎片可重复获取。新增敌阵、等级及确定掉落为本地重建规则；普通胜利还可获得Link点。按原前置顺序解锁，具体难度和演出仍待游戏验证。')
        if ident in ('12', '13', '14', '58', '98'):
            notes += [f'当前有{len(m["missions"])}条本地任务。领取任务后，奖励先进入礼物箱，还需在礼物箱领取才能入库存。',
                f'每日任务按UTC{m["dailyUtcOffsetHours"]:+d}的午夜重置；当天首次请求前的活动不追溯。累计任务按已保存计数计算。任务内容、门槛、奖励和该重置时区为本地设定。',
                f'礼物保留{m["giftExpiryDays"]}天，30天期限来自原版帮助；每页{m["giftPageSize"]}条是本地分页规则。领取超过库存上限时整份保留，腾出容量后再领。过期礼物隐藏，不能再领。',
                '登录奖励、任务云端备份和称号奖励获取尚未开放。原版登录奖励的5:00切换不适用于本地每日任务。']
        if ident in ('7', '56', '57', '58', '97'):
            p = g['captureTalents']['probabilityBasisPoints']
            notes += ['普通成长捕捉每只消耗5数码石，池中有亚古兽、加布兽、甲虫兽和巴达兽；Link捕捉每只消耗300点，池中是三种不同幼年期Ⅰ物种，需进入花园培养。',
                f'每只独立抽取才能：无牌{p["none"]/100:g}%、银Ⅰ{p["silver"]/100:g}%、金Ⅰ{p["gold"]/100:g}%；有牌时六项等概率。概率为本地设定，十连没有额外保底。教学和固定奖励不使用此概率。',
                '才能继承采用原主数据费用及成功率；失败也会消耗金币和素材。基础最多两项金牌，超出空位时从成功的新金牌中随机保留，余项按失败规则处理。操作前确认素材。']
        if ident in ('5', '55'):
            f = g['friendship']
            b = g['battleFriendship']
            notes.append(f'本地农场互动采用滚动{f["cooldownSeconds"]/3600:g}小时冷却，每次+{f["gainPerTap"]}友情；只对常用队伍中成长期及以上伙伴开放。每天一次来自原帮助，冷却方式和增量为本地选择。')
            notes.append(f'本地普通战斗胜利后，每名存活队员独立有{b["probabilityBasisPoints"]/100:g}%概率增加{b["gainOnSuccess"]}点友情，达到当前成长阶段上限后停止；败北、退出和倒下的队员不增加。胜利可能增加友情来自原帮助，概率和数量为本地设定，不占用农场互动冷却。旧战斗沿用进入时的规则，教学不使用此概率。')
        if ident in ('46', '47', '48', '54', '100'):
            garden = g['garden']
            notes += ['本地研究需要两只可研究的究极体，双方都会被消耗。请使用额外培养的伙伴，并先从所有队伍移除、解除锁定。基础的继承技能、才能、幸运和队长技能保留；友情和变更耐性重置。没有已知出生路线时会拒绝研究。',
                '满友情研究可提高一档觉醒，最高+4；版本对象作为基础会退回+4并清空第二继承技能槽。同形态版本升级已有本地实现，原说明的等级上限以本地实际主数据为准。',
                f'本地数码蛋立即可免费孵化。幼年期Ⅰ成长需{garden["growthSeconds"]["2"]/3600:g}小时，幼年期Ⅱ需{garden["growthSeconds"]["3"]/3600:g}小时；花园最多3只。到时仍需点击成长，不自动退出花园。',
                f'提前成长按剩余时间向上取整收费：Ⅰ阶段每石缩短{garden["shorteningHoursPerStone"]["2"]}小时，Ⅱ阶段每石缩短{garden["shorteningHoursPerStone"]["3"]}小时。这些计时和价格为本地规则。培养期间不可编队或作为强化、继承、研究素材。',
                '普通进化需要满级、金币及对应材料；原48条模式变化已有本地实现，不要求满级或插件。特殊进化与core/hull版本转换仍未实现。']
        if ident in ('19', '23', '35', '45', '100'):
            notes.append('本地第70–76关依次重复掉落无、火、水、雷、自然、光、暗更新材料及对应重构材料，第83关重复掉落万能更新材料；每种每波1份，完整三波胜利共3份。原事件材料来源尚未重建，这些后期普通掉落是明确的本地获取规则。core/hull更新材料仍未开放。失败、退出和重复结算不会额外发放；在途战斗采用进入时的掉落快照。')
        if ident == '100':
            notes.append('本地同形态版本升级需要觉醒+4究极体满级，消耗原配方各槽材料和135000金币。万能更新材料只能替代更新材料，不能代替插件或数据碎片；多个替代槽合并计算总用量。core/hull特殊转换尚未实现。更新材料可在本地后期普通关卡重复取得，实际培养与战斗难度仍待游戏验证。')
            notes.append('升级产生新伙伴编号并删除旧编号，操作前需从所有队伍移除并解除锁定。芯片自动卸下并留在库存。持久收据避免原操作重复扣款；重连后返回当前升级伙伴，已消耗的伙伴不会重新出现。')
            notes.append('本地保留已赚取的经验和等级、出生路线、第一继承技能、队长技能、才能、幸运、友情与农场互动冷却，第二继承技能槽初始为空。等级上限取目标主数据。该结转方式为本地规则，原服务器结转细节未恢复。')
        if ident == '54':
            notes += ['本地模式变化按原48条双向路线及目标觉醒档位收费：无觉醒/+1/+2/+3/+4/版本分别为7200/12000/24000/36000/60000/108000金币，不消耗插件、不要求满级。仅能选择原路线；请先结束当前战斗。',
                '变化保留伙伴编号、等级、经验和才能；固有技能按原子编号对应目标形态，基础属性按目标重算，附加耐性与天生耐性重复时重抽。本地也保留友情、幸运、继承/队长技能、出生路线、互动冷却、队伍和芯片；这些附加结转是本地规则。',
                '相同会话请求号不重复扣款；重连后当前已是最近一次变化目标时返回当前伙伴。正常双向变化逐次收费，中间已经反向变化的旧请求无法与主动再次变化区分。模式变化不计入本地普通进化任务；原服务器任务统计细节未恢复。']
        if ident == '53':
            notes.append(f'本地强化接受1–5只素材，按原主数据计算经验和金币；同种族幸运成功率取最高成长阶段。本地成功+{g["fusion"]["luckGainOnSuccess"]}幸运并封顶；大成功尚未实现。素材会消失，其芯片留在库存。已锁定、配队、花园或当前战斗中的对象受到保护。')
        if ident == '44':
            notes.append('本地技能继承需要满级素材，会消耗金币和素材。基础可替换第一技能槽；第二槽需版本对象，可通过本地同形态版本升级开放。锁定、配队、花园、数码蛋、当前战斗中的素材不能消耗；素材的芯片留在库存。请在确认前核对伙伴和技能。')
        if ident in ('8', '36', '37', '38', '40', '41', '42', '43', '51', '52', '88'):
            notes.append('当前可建造肉田、仓库、餐厅、道场、芯片工厂和已解锁装饰物；肉田、仓库、餐厅与初始小屋可升级，芯片工厂当前不开放升级。可移动、收纳、取出和出售符合条件的设施，核心设施受收纳/出售保护。最多两处同时施工，施工加速已接通；其他功能设施仍不完整。')
        if ident in ('9', '42'):
            notes.append('餐厅在第16关首次通关后解锁，建造需1000金币和30秒。按原主数据升至10级时，体力上限依次为40、45、50、55、60、65、70、75、80、100。每次建造或升级完工确认后补到新上限，保留已有超额体力；重试和重连不会再次补满。超额体力可继续用于探索，降至上限以下后才开始自然恢复，只补实际缺口。')
        if ident in ('8', '38', '51'):
            notes.append('初始小屋从50只容量起，按原主数据最多升至20级、999只。升级消耗数码石而非金币；工期为0秒，仍需完工确认后才增加容量，受两处施工名额限制。')
        if ident == '43':
            notes.append('道场在第9关首次通关后解锁，建造需2000金币和30分钟，最高1级。建造、完工与加速已接通，技能继承和耐性变化已有本地实现。需要先建好道场。')
        if ident in ('43', '45'):
            notes.append('属性耐性变化按原表消耗同名伙伴和进化材料，或碎片及对应材料；伙伴会消失，芯片留在库存。被锁定、配队或培养中的伙伴不能作为消耗对象。特殊多材料路线仅使用原专用材料。没有适用原材料表时拒绝操作。')
            notes.append('本地每次等概率附加一项属性耐性，排除已天生抵抗或免疫的项，再次变化替换之前附加的项，也可能再次抽到相同项。原抽取权重未恢复，等概率是本地规则。进化保留附加耐性；与新形态天生耐性重复时重抽。')
            notes.append('状态耐性变化仅适用于版本对象，按原表消耗状态材料，并按上述本地等概率规则附加一项状态耐性；属性与状态附加项互不覆盖。同形态版本升级与普通更新、重构材料已有本地获取路线；特殊转换与材料仍不完整。')
            notes.append('消耗伙伴的操作有持久收据；只消耗材料的重复变化可再次付费抽取。同会话原请求重试不重复消耗，重新登录后先查看当前耐性和材料，重新确认的变化会再次消耗材料。当前没有额外金币或数码石费用。')
        if ident == '9':
            notes.append('本地体力每180秒恢复1点，达到当前上限后停止；新账号本地初始上限为20，餐厅完工后采用原设施上限。原版初始30未恢复，当前存档不会仅因阅读帮助而改变。')
        if ident in ('24', '25'):
            notes.append('本地普通战斗从进入起保留72小时，期限来自原版帮助。到期后不能续战或领取奖励，已消耗体力不返还；可返回农场重新探索。旧普通战斗按原进入时间计算，教学不使用此期限；已经成功结算的奖励收据不因到期而丢失。')
        if ident in ('65', '66', '67', '68', '70', '71'):
            notes.append('本地账号保存在本地服务存档中，原版iCloud/Google迁移、服务器引继和好友云数据不能用于此版本。保留本地存档、当前APK和模拟器应用数据；不要照历史步骤卸载、清除数据或创建新账号来迁移。')
        if ident in ('12', '13', '90', '91', '92', '94'):
            notes.append('本地每日普通胜利1次可领1个取外补丁，3次可领5个扩张补丁，5次可领1枚物攻G芯片；先领任务再领礼物。这三项门槛与奖励为本地设定，道具或芯片超容量时整份保留。')
        if ident in ('90', '91', '92', '94'):
            notes.append('第12关原首通奖励可取得工厂钥匙，芯片工厂需要100金币建造，并完成完工确认；一级芯片容量为原表300枚。当前没有开放工厂升级。')
            notes.append(f'工厂完工后可进行本地基础芯片捕捉，每枚{g["chips"]["linkCostPerChip"]} Link点，支持单抽和十连。物攻、物防、魔攻、魔防、速度、HP六种G级等概率，抽取费用与概率为本地设定，十连没有保底。特殊或活动芯片尚未恢复。')
            advanced = g['chips'].get('advancedGacha')
            if advanced:
                odds = '、'.join(f'{self.rank_name(rank)}级{weight/100:g}%' for rank, weight in
                                sorted(advanced['rankWeightsBasisPoints'].items(), key=lambda r: int(r[0])))
                notes.append(f'通关第{advanced["unlockDungeonId"]}关且工厂已完工后，解锁本地进阶芯片捕捉，每枚{advanced["linkCostPerChip"]} Link点。先抽等级（{odds}），再独立等概率抽六种属性，每种1/6；支持单抽和十连，没有保底。费用、解锁与概率为本地规则，原服务器进阶池未恢复。')
            notes.append('当前支持六类基础芯片的G至S级共48种，按原主数据提供常驻属性；不能凭离线结果认定实际战斗效果已通过。免费槽按成长阶段及觉醒+2/+4开放；本地提供5个额外槽，按顺序消耗1、5、10、20、40个扩张补丁。卸下消耗1个取外补丁，更换同一槽时旧芯片留库。')
            notes.append('强化消耗原表指定数量的同编号、未装备芯片，基础编号保留；S级不能继续强化。同一属性类别的芯片即使等级不同，也不能同时装备在同一伙伴上。出售按等级×100金币，金币超上限时拒绝，原补丁返还概率未恢复，当前没有随机返还。')
            notes.append('战斗未结算、数码蛋或花园培养中的对象不能修改芯片。原请求重试不重复支付；消耗型强化和出售保留持久收据。重连后先检查当前装配和数量，新的抽取或再次装卸仍会付费。')
        unsupported = {'14': '登录奖励', '63': '云端公告',
            '17': '曜日关卡', '18': '降临关卡',
            '32': '付费复活', '33': '付费续关',
            '35': '幸运追加掉落', '39': '告别与碎片返还',
            '50': '联机募集',
            '59': '购买数码石', '60': '在线好友交互', '89': '聊天',
            '95': '限时关卡券', '96': '关卡券捕捉'}
        if ident in unsupported:
            notes.append('当前尚未开放或未完整恢复：' + unsupported[ident] + '。下方原版文字仅供了解历史规则。')
        if self.original[ident]['helpCategoryId'] in ('6', '7', '10') or ident in ('99', '101', '102'):
            notes.append('联机、竞技场、在线排名与聊天尚未开放；原版BP/DP、赛季和奖励时间不在本地生效。')
        return notes or ['以下是原版历史说明的中文文字版；未列出的功能不能仅凭帮助正文视为本地已开放。']

    @staticmethod
    def rank_name(rank):
        return 'GFEDCBAS'[int(rank) - 1]

    def render(self, path, query):
        try:
            if len(query) > 4096:
                raise ValueError()
            if path == '/gacha/GachaChipList':
                fields = parse_qs(query, keep_blank_values=True, max_num_fields=32, errors='strict')
                chips = self.gameplay['chips']
                advanced = chips.get('advancedGacha')
                if advanced and fields.get('gachaId') == [str(advanced['gachaId'])]:
                    body = f'<p>本地进阶芯片捕捉，每枚消耗{advanced["linkCostPerChip"]} Link点，支持单抽和十连；每枚独立抽取，十连没有额外保底。</p>'
                    body += f'<p>需通关第{advanced["unlockDungeonId"]}关，并建好芯片工厂、确认完工。此页只介绍规则，不会判断或改变账号解锁状态。</p>'
                    body += '<p>先按以下概率抽等级，再独立抽取六种属性，每种1/6。费用、解锁、池与概率均为本地设定，原服务器进阶池未恢复。</p>'
                    for rank, weight in sorted(advanced['rankWeightsBasisPoints'].items(), key=lambda r: int(r[0])):
                        body += f'<h2>{self.rank_name(rank)}级：{weight/100:g}%</h2><ul>'
                        odds = Fraction(weight, 60000)
                        body += ''.join('<li>' + escape(self.chip_names[i]) + f'：{odds}（单枚总概率）</li>'
                                        for i in advanced['chipIdsByRank'][rank])
                        body += '</ul>'
                    body += '<p>基础100 Link的G级池和原强化配方继续保留；特殊／活动芯片和工厂升级尚未开放。</p>'
                    return 200, page('本地进阶芯片抽取说明', body)
                if fields.get('gachaId') != [str(chips['gachaId'])]:
                    return 404, page('未找到芯片捕捉', '<p>此芯片捕捉尚未开放。</p>')
                body = f'<p>本地基础芯片捕捉，每枚消耗{chips["linkCostPerChip"]} Link点，支持单抽和十连；十连没有额外保底。</p>'
                body += '<p>以下六种G级芯片各占1/6；抽取费用、池与等概率为本地设定，原服务器概率未恢复。</p><ul>'
                body += ''.join('<li>' + escape(self.chip_names[i]) + '：1/6</li>' for i in chips['gachaChipIds'])
                body += '</ul><p>第12关首通取得工厂钥匙，建好工厂并确认完工后可抽取。当前支持六类基础芯片G至S级，特殊／活动芯片和工厂升级尚未开放。</p>'
                return 200, page('本地基础芯片抽取说明', body)
            params = parse_qs(query, keep_blank_values=True, max_num_fields=32, errors='strict')
            for key in ('helpId', 'countryCode', 'q'):
                if len(params.get(key, [])) > 1:
                    raise ValueError()
            country = params.get('countryCode', ['3'])[0]
            if country not in ('1', '2', '3'):
                raise ValueError()
            q = params.get('q', [''])[0].strip()
            if len(q) > 200:
                raise ValueError()
            if path in ('/help/HelpView', '/help/HelpView/'):
                ident = params.get('helpId', [''])[0]
                if not re.fullmatch(r'[1-9][0-9]{0,5}', ident):
                    raise ValueError()
                if ident not in self.original:
                    return 404, page('未找到帮助', '<p>此帮助条目不存在，请返回目录。</p>')
                row = (self.chinese if country == '3' else self.original)[ident]
                notes = ''.join('<p>' + escape(n) + '</p>' for n in self.local_notes(ident))
                body = '<aside class="note"><h2>本地版本说明</h2>' + notes + '</aside>'
                if country != '3':
                    body += '<p class="muted">此语言未提供独立翻译，以下保留原始日文文字。</p>'
                body += '<h2>原版说明</h2><article id="help">' + paragraphs(row['detail']) + '</article>'
                return 200, page(row['helpTitle'], body)
            if path == '/help/materials':
                return 200, self.material_page(q)
            return 200, self.index(q, country)
        except (ValueError, UnicodeError):
            return 400, page('请求无效', '<p>帮助编号或查询条件无效，请从目录重新打开。</p>')

    def search_form(self, q, action):
        return '<form method="get" action="' + action + '"><label for="q">搜索 </label><input id="q" name="q" value="' + escape(q, quote=True) + '"><button type="submit">查找</button></form>'

    def index(self, q, country):
        body = '<p>查看原版文字说明，并了解当前本地规则及功能限制。</p>' + self.search_form(q, '/help/')
        categories = [(r['helpCategoryId'], r['helpCategoryName']) for r in sorted(self.categories, key=lambda r: int(r['helpCategoryPriority']))]
        # Original help71 references absent category99; retain it under a fallback.
        categories.append(('99', '历史数据迁移 / 其他'))
        rows = self.chinese if country == '3' else self.original
        count = 0
        for cid, title in categories:
            links = []
            for ident, row in sorted(rows.items(), key=lambda pair: (int(pair[1]['helpPriority']), int(pair[0]))):
                if row['helpCategoryId'] != cid or q.casefold() not in (row['helpTitle'] + row['detail']).casefold():
                    continue
                count += 1
                target = '/help/HelpView/?' + urlencode({'helpId': ident, 'countryCode': country})
                links.append('<li><a href="' + escape(target, quote=True) + '">' + escape(row['helpTitle']) + '</a></li>')
            if links:
                body += '<section><h2>' + escape(title) + '</h2><ul>' + ''.join(links) + '</ul></section>'
        if not count:
            body += '<p>没有匹配条目。</p>'
        return page('本地帮助', body)

    def material_page(self, q):
        body = f'<p>普通探索{self.dungeon_count}关提供{len(self.materials)}种可重复材料。表中是当前本地设定的胜利掉落；首通奖励另计。按原前置顺序解锁，金币和等级仍需培养积累。特殊插件、版本升级与活动路线尚不完整。</p>'
        body += self.search_form(q, '/help/materials')
        rows = []
        name_counts = {}
        for item in self.materials.values():
            name_counts[item['name']] = name_counts.get(item['name'], 0) + 1
        for sid, item in sorted(self.materials.items(), key=lambda pair: (pair[1]['name'], int(pair[0]))):
            name, sources = item['name'], item['sources']
            if q.casefold() not in (name + ' ' + sid).casefold():
                continue
            if name_counts[name] > 1:
                name += '（材料编号' + sid + '）'
            label = '；'.join(f'第{ident}关 ×{amount}' for ident, amount in sorted(sources))
            rows.append('<tr><td>' + escape(name) + '</td><td>' + escape(label) + '</td></tr>')
        body += '<p class="muted">同名但编号不同的碎片分别列出，彼此不能互换；请对照进化所需材料。</p>'
        body += '<div class="scroll"><table><thead><tr><th>材料</th><th>每次胜利获取</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
        if not rows:
            body += '<p>没有匹配材料。</p>'
        return page('普通探索材料来源', body)
