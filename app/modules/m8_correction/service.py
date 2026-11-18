from app.modules.m8_correction import rules
from app.numeric import serial


def metrics(correction):
    state = correction['state']
    suspended = len(correction.get('suspended', []))
    confirmed = len(correction.get('confirmed', []))
    unaffected = len(correction.get('unaffected', []))
    changed = sum(1 for item in correction.get('confirmed', []) if item['结论是否变化'])
    return [
        ['纠错证书', len(correction['corrected_certs']), '份'],
        ['受影响证书', len(correction['affected_certs']), '份'],
        ['未变沿用原结论', unaffected, '条'],
        ['停用待处理', suspended, '条'],
        ['确认更新', confirmed, '条'],
        ['其中结论翻转', changed, '条'],
        ['纠错状态', state, ''],
    ]


def summarize(correction):
    """供页面与 /api/m8 使用的可序列化视图，保持与其它模块 stage 结构一致。"""
    result = {
        'stage': 8,
        'name': '证书纠错补录',
        'state': correction['state'],
        'correction_id': correction['correction_id'],
        'source_run_id': correction['source_run_id'],
        'source_revision': correction['source_revision'],
        'revision': correction['revision'],
        'reused': correction.get('reused', False),
        'corrected_certs': correction['corrected_certs'],
        'affected_certs': correction['affected_certs'],
        'rows': _rows(correction),
        'metrics': metrics(correction),
        'checkpoint': correction.get('checkpoint', {}),
        'quarantine_delta': correction.get('quarantine_delta', {}),
        'recomputed': correction.get('recomputed', {}),
    }
    return serial(result)


def _rows(correction):
    rows = []
    for item in correction.get('unaffected', []):
        rows.append({**item, '对象类别': '未变测量'})
    for item in correction.get('suspended', []):
        rows.append({**item, '对象类别': '受影响测量·停用'})
    for item in correction.get('confirmed', []):
        rows.append({**item, '对象类别': '受影响测量·已更新'})
    for item in correction.get('rolled_back_items', []):
        rows.append({**item, '对象类别': '回退恢复'})
    return rows
