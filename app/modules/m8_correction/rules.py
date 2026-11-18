"""M8 证书纠错补录：对象级纠错规则。

设计原则（与整批重审严格区分）：
- 原批次（复核回执）身份与全部历史结论保持不变，纠错是挂在原批次下的独立对象；
- 仅对纠错 CSV 中实际涉及的证书及其下游证书关联的测量重判，其余测量沿用原结论；
- 受影响测量在确认前先停用（SUSPENDED），确认通过后才更新为修正结论；
- 全流程状态机可中断续跑，回退恢复原批次复核状态；
- 幂等键由原回执 id + 规则版本 + 纠错原文摘要决定，重跑复用同一纠错，不生成第二份。
"""
import csv
import io
from app.config import HEADERS
from app.errors import DomainError
from app.reader import decode
from app.pipeline import calculate
from app.numeric import serial

# 纠错状态机：停用待确认 → 已确认更新 / 退回停用 →（回退后）已回退
DRAFT = '停用待确认'
CONFIRMED = '已确认更新'
REJECTED = '退回停用'
ROLLED_BACK = '已回退'


def parse_correction_text(raw):
    """接受上传字节或 CSV 文本，返回去 BOM 的文本。"""
    if isinstance(raw, bytes):
        raw = decode(raw)
    if not isinstance(raw, str):
        raise DomainError('TYPE', '纠错CSV必须是文本')
    return raw.lstrip('﻿')


def correction_cert_keys(text):
    """从纠错 CSV 中提取证书编号，保持去重后的出现顺序。

    列数不符的行不静默丢弃：以 None 占位，交给合并管线显式隔离。
    """
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        head = [x.strip() for x in next(reader)]
    except (csv.Error, StopIteration):
        raise DomainError('STRUCTURE', '纠错表头损坏')
    if set(head) != set(HEADERS) or len(head) != len(HEADERS):
        raise DomainError('HEADER', '纠错字段缺失或重复')
    cert_index = head.index('cert')
    keys, seen = [], set()
    for cells in reader:
        if not cells or all(not x.strip() for x in cells) or cells == head:
            continue
        if len(cells) != len(head):
            keys.append(None)  # 结构错误行：保留位置，合并复算时隔离
            continue
        cert = cells[cert_index].strip()
        if cert and cert not in seen:
            seen.add(cert)
            keys.append(cert)
    if not [k for k in keys if k]:
        # None 占位表示存在结构错误行；完全无业务行才是空文件语义。
        if any(k is None for k in keys):
            raise DomainError('CORRECTION_QUARANTINE', '纠错行全部结构无效或被隔离，没有可修正证书')
        raise DomainError('CORRECTION_EMPTY', '纠错文件没有有效证书')
    return keys


def measurement_key(row):
    """测量对象身份键：基于 M6 裁决行的归一化字段（证书、测量日、伏特值）。"""
    return row['证书'], row['测量日'], str(row['测量值_V'])


def build_correction(source_run, text):
    """合并原批次输入与纠错输入，离线重算并形成对象级差异。

    原批次内容不被修改：合并文本仅用于复算，所有差异以独立纠错对象保存。
    不查询任何外部源。
    """
    original_rows = source_run['manifest'].get('source_csv', [])
    merged_text, line_map = _merge_csv(original_rows, text)
    recomputed = calculate(merged_text)

    original_verdicts = {measurement_key(r): r for r in source_run['stages']['6']['rows']}
    new_verdicts = {measurement_key(r): r for r in recomputed['stages']['6']['rows']}

    corrected_certs = [k for k in correction_cert_keys(text) if k]
    if not corrected_certs:
        raise DomainError('CORRECTION_QUARANTINE', '纠错行全部结构无效或被隔离，没有可修正证书')
    corrected_set = set(corrected_certs)
    # 下游证书：撤销/溯源关系变化会沿追溯链传播，因此受影响面包含修正证书的全部下游。
    affected_certs = _impacted_certificates(corrected_set, recomputed['certs'])

    unaffected, suspended, original_only = [], [], []
    for key, old in original_verdicts.items():
        if old['证书'] not in affected_certs:
            unaffected.append(_carry(key, old, new_verdicts.get(key), '未变沿用原结论'))
            continue
        fresh = new_verdicts.get(key)
        suspended.append({
            '测量键': '|'.join(key),
            '证书': old['证书'], '设备': old['设备'], '测量值_V': old['测量值_V'],
            '测量日': old['测量日'], '原结论': old['结论'], '原原因码': old['原因码'],
            '修正结论': fresh['结论'] if fresh else '对象消失',
            '修正原因码': fresh['原因码'] if fresh else 'NOT_IN_RECOMPUTE',
            '结论是否变化': _changed(old, fresh), '行号': old['行号'],
            '状态': DRAFT,
        })
    # 纠错补录可能带来原批次不存在的新测量。
    for key, fresh in new_verdicts.items():
        if key not in original_verdicts and fresh['证书'] in affected_certs:
            original_only.append({
                '测量键': '|'.join(key),
                '证书': fresh['证书'], '设备': fresh['设备'], '测量值_V': fresh['测量值_V'],
                '测量日': fresh['测量日'], '原结论': '原批次无此测量', '原原因码': 'NEW_OBJECT',
                '修正结论': fresh['结论'], '修正原因码': fresh['原因码'],
                '结论是否变化': True, '行号': fresh['行号'], '状态': DRAFT,
            })
    suspended.extend(original_only)

    quarantine_delta = _quarantine_diff(source_run, recomputed, line_map)
    payload = {
        'correction_id': None,  # 由 store 以幂等键填充
        'source_run_id': source_run['manifest']['run_id'],
        'source_revision': source_run['revision'],
        'state': DRAFT,
        'corrected_certs': corrected_certs,
        'affected_certs': sorted(affected_certs),
        'unaffected': unaffected,
        'suspended': suspended,
        'confirmed': [],
        'recomputed': _slim_recomputed(recomputed),
        'quarantine_delta': quarantine_delta,
        'checkpoint': {'step': 'built', 'resumable': True},
    }
    return serial(payload)


def confirm(correction, action):
    """质量员确认：通过则把停用测量更新为修正结论；退回则保持停用并记录去向。"""
    if correction['state'] not in (DRAFT, REJECTED):
        raise DomainError('CORRECTION_STATE', '当前纠错状态不允许确认操作')
    if action == '通过':
        confirmed = []
        for item in correction['suspended']:
            confirmed.append({**item, '状态': CONFIRMED, '停用去向': '确认后按修正证书更新结论'})
        correction['suspended'] = []
        correction['confirmed'] = confirmed
        correction['state'] = CONFIRMED
    elif action == '退回':
        for item in correction['suspended']:
            item['状态'] = REJECTED
            item['停用去向'] = '退回维持停用，等待证书再次修正'
        correction['state'] = REJECTED
    else:
        raise DomainError('DECISION', '复核动作无效')
    correction['checkpoint'] = {'step': 'decided', 'resumable': True}
    return correction


def rollback(correction):
    """回退纠错：全部测量恢复原批次复核状态，纠错对象保留为已回退备查。"""
    if correction['state'] == ROLLED_BACK:
        return correction
    restored = []
    for bucket in ('suspended', 'confirmed'):
        for item in correction.pop(bucket, []):
            restored.append({**item, '状态': '原结论恢复', '停用去向': '纠错回退，恢复旧版复核状态'})
    correction['confirmed'] = []
    correction['suspended'] = []
    correction['rolled_back_items'] = restored
    correction['state'] = ROLLED_BACK
    correction['checkpoint'] = {'step': 'rolled_back', 'resumable': False}
    return correction


def effective_view(source_run, correction):
    """对外的有效裁决视图：未变测量沿用原结论，受影响测量按纠错状态体现停用/更新。"""
    rows = list(correction['unaffected'])
    if correction['state'] == ROLLED_BACK:
        rows.extend(_restored_rows(correction))
        banner = '纠错已回退，全部测量恢复原回执结论'
    else:
        rows.extend(correction.get('suspended', []))
        rows.extend(correction.get('confirmed', []))
        banner = {
            DRAFT: '受影响测量停用中，待质量员确认',
            REJECTED: '受影响测量退回停用，未更新结论',
            CONFIRMED: '受影响测量已按修正证书更新',
        }[correction['state']]
    return {'source_run_id': correction['source_run_id'],
            'correction_id': correction['correction_id'],
            'state': correction['state'], 'banner': banner, 'rows': rows}


def _merge_csv(original_rows, correction_text):
    """对象级合并：纠错行按 cert 替换同编号原行，新增证书追加；不产生同证书重复定义。

    全部在本地离线完成，不接外部源。返回 (合并CSV, 纠错行号映射)，
    映射把合并文本中的物理行号对应到纠错来源标记 C1..Cn，供隔离追溯。
    """
    if not original_rows:
        raise DomainError('CORRECTION_SOURCE', '原批次缺少存档输入，无法离线合并')
    reader = csv.reader(io.StringIO(correction_text, newline=''), strict=True)
    head = [x.strip() for x in next(reader)]
    if set(head) != set(HEADERS):
        raise DomainError('HEADER', '纠错字段缺失或重复')
    corrected, order, seq, malformed = {}, [], 0, []
    for cells in reader:
        if not cells or all(not x.strip() for x in cells) or cells == head:
            continue
        seq += 1
        if len(cells) != len(head):
            # 结构错误行补齐到表头宽度后追加，交由标准 M1/M2 隔离并保留来源行。
            padded = (cells + [''] * len(head))[:len(head)]
            malformed.append(({k: (v or '').strip() for k, v in zip(head, padded)}, 'C%d' % seq))
            continue
        row = {k: (v or '').strip() for k, v in zip(head, cells)}
        row['_corr_line'] = 'C%d' % seq
        if row.get('cert'):
            corrected[row['cert']] = row
            if row['cert'] not in order:
                order.append(row['cert'])
    merged, line_map = [], {}
    replaced = set()
    for original in original_rows:
        if original['cert'] in corrected:
            fixed = corrected[original['cert']]
            merged.append({h: fixed.get(h, '') for h in HEADERS})
            line_map[len(merged) + 1] = fixed['_corr_line']  # +1：表头占第1行
            replaced.add(original['cert'])
        else:
            merged.append(dict(original))
    for cert in order:
        if cert not in replaced:
            fixed = corrected[cert]
            merged.append({h: fixed.get(h, '') for h in HEADERS})
            line_map[len(merged) + 1] = fixed['_corr_line']
    for row, source_line in malformed:
        merged.append({h: row.get(h, '') for h in HEADERS})
        line_map[len(merged) + 1] = source_line
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(HEADERS)
    for row in merged:
        writer.writerow([row.get(h, '') for h in HEADERS])
    return out.getvalue(), line_map


def _impacted_certificates(corrected, certs):
    reached = set(corrected)
    todo = list(corrected)
    while todo:
        node = todo.pop()
        for key, value in certs.items():
            if value.get('parent') == node and key not in reached:
                reached.add(key)
                todo.append(key)
    return reached


def _carry(key, old, fresh, note):
    return {
        '测量键': '|'.join(key),
        '证书': old['证书'], '设备': old['设备'], '测量值_V': old['测量值_V'],
        '测量日': old['测量日'], '原结论': old['结论'], '原原因码': old['原因码'],
        '修正结论': fresh['结论'] if fresh else old['结论'],
        '修正原因码': fresh['原因码'] if fresh else old['原因码'],
        '结论是否变化': _changed(old, fresh), '行号': old['行号'],
        '状态': '沿用原回执', '停用去向': note,
    }


def _changed(old, fresh):
    if fresh is None:
        return False
    return old['结论'] != fresh['结论'] or old['原因码'] != fresh['原因码']


def _quarantine_diff(source_run, recomputed, line_map=None):
    line_map = line_map or {}
    old = {(r.get('行号'), r.get('原因码'), r.get('原值', '')): r for r in source_run['quarantine']}
    added = []
    for r in recomputed['quarantine']:
        if (r.get('行号'), r.get('原因码'), r.get('原值', '')) in old:
            continue
        # 物理行号映射回纠错来源行（C1..Cn），明确该隔离由纠错输入引入。
        item = dict(r)
        item['纠错来源行'] = line_map.get(r.get('行号'), r.get('行号'))
        added.append(item)
    return {'原隔离': len(source_run['quarantine']), '纠错新增隔离': added}


def _slim_recomputed(recomputed):
    """只保留核对所需的重算摘要，不复制整份批次，避免与原回执身份混淆。"""
    return {
        'run_id': recomputed['manifest']['run_id'],
        'sha256': recomputed['manifest']['sha256'],
        'rule_version': recomputed['manifest']['rule_version'],
        'stage6_metrics': recomputed['stages']['6']['metrics'],
        'stage7_rows': recomputed['stages']['7']['rows'],
    }


def _restored_rows(correction):
    return [{**item, '状态': '沿用原回执'} for item in correction.get('rolled_back_items', [])]
