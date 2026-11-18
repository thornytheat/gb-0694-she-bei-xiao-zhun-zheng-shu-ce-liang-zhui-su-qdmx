from flask import Blueprint, request, jsonify, Response
import csv
import io
from app.errors import DomainError
from app.reader import decode
from app.store import (
    get_run, save_correction, get_correction, list_corrections,
    update_correction, rollback_correction,
)
from app.modules.m8_correction import rules, service

bp = Blueprint('m8', __name__)


def _read_correction_text():
    if request.files:
        if 'file' not in request.files:
            raise DomainError('REQUEST', '上传必须使用file字段', 400)
        return decode(request.files['file'].read())
    payload = request.get_json(silent=True) or {}
    text = payload.get('csv')
    if not isinstance(text, str) or not text.strip():
        raise DomainError('REQUEST', '须提供纠错CSV文本或file文件')
    return text


@bp.route('/api/m8/corrections', methods=['POST'])
def create_correction():
    """提交纠错 CSV：离线合并复算，形成对象级纠错。幂等，重跑不生成第二份。"""
    body = request.get_json(silent=True) if not request.files else None
    if request.files:
        source_id = request.form.get('run_id', '')
    else:
        source_id = (body or {}).get('run_id', '')
    if not source_id:
        raise DomainError('REQUEST', '缺少原回执 run_id')
    source_run = get_run(source_id)
    text = rules.parse_correction_text(_read_correction_text())
    payload = rules.build_correction(source_run, text)
    saved = save_correction(payload, source_run, text)
    return jsonify(service.summarize(saved))


@bp.route('/api/m8/corrections')
def corrections_index():
    source_id = request.args.get('run_id')
    return jsonify(list_corrections(source_id))


@bp.route('/api/m8/corrections/<ident>')
def correction_detail(ident):
    return jsonify(service.summarize(get_correction(ident)))


@bp.route('/api/m8/corrections/<ident>/confirm', methods=['POST'])
def correction_confirm(ident):
    body = request.get_json(silent=True) or {}
    action = body.get('decision')
    if action not in ('通过', '退回'):
        raise DomainError('DECISION', '复核动作无效')
    try:
        revision = int(body.get('revision', 0))
    except (ValueError, TypeError):
        raise DomainError('VERSION', '版本必须为整数')
    correction = get_correction(ident)
    # 乐观锁优先：陈旧版本一律 409，避免状态机吞掉版本冲突。
    if correction['revision'] != revision:
        raise DomainError('VERSION', '纠错版本已变化，请重新载入', 409)
    rules.confirm(correction, action)
    correction.pop('revision', None)
    saved = update_correction(ident, revision, correction)
    return jsonify(service.summarize(saved))


@bp.route('/api/m8/corrections/<ident>/rollback', methods=['POST'])
def correction_rollback(ident):
    body = request.get_json(silent=True) or {}
    try:
        revision = int(body.get('revision', 0))
    except (ValueError, TypeError):
        raise DomainError('VERSION', '版本必须为整数')
    saved = rollback_correction(ident, revision)
    return jsonify(service.summarize(saved))


@bp.route('/api/m8/effective')
def effective():
    """对外有效裁决视图：原回执 + 纠错状态合并，供页面与核对使用。"""
    correction = get_correction(request.args['correction_id'])
    source_run = get_run(correction['source_run_id'])
    return jsonify(rules.effective_view(source_run, correction))


@bp.route('/api/m8/evidence.csv')
def correction_evidence():
    """纠错证据导出：原回执身份、纠错范围、停用去向逐对象可核对。"""
    correction = get_correction(request.args['correction_id'])
    summary = service.summarize(correction)
    out = io.StringIO(newline='')
    header = ['原回执ID', '纠错ID', '纠错状态', '纠错版本', '对象类别', '证书', '设备',
              '测量值_V', '测量日', '原结论', '原原因码', '修正结论', '修正原因码',
              '结论是否变化', '状态', '停用去向', '行号']
    writer = csv.DictWriter(out, fieldnames=header)
    writer.writeheader()
    common = {'原回执ID': correction['source_run_id'], '纠错ID': correction['correction_id'],
              '纠错状态': correction['state'], '纠错版本': correction['revision']}
    for row in summary['rows']:
        writer.writerow({**common, **{k: row.get(k, '') for k in header if k not in common}})
    # 追加纠错范围与新增隔离，确保范围可核对。
    for cert in correction['corrected_certs']:
        writer.writerow({**common, '对象类别': '纠错范围·修正证书', '证书': cert,
                         '状态': correction['state'], '停用去向': '纠错输入显式修正'})
    for item in correction.get('quarantine_delta', {}).get('纠错新增隔离', []):
        writer.writerow({**common, '对象类别': '纠错新增隔离', '证书': item.get('cert', ''),
                         '行号': item.get('行号', ''), '原原因码': item.get('原因码', ''),
                         '停用去向': item.get('原因', '')})
    filename = 'correction-%s.csv' % correction['correction_id']
    return Response('﻿' + out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=%s' % filename})
