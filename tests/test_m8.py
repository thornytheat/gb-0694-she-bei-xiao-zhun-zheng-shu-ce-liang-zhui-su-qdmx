"""M8 证书纠错补录：对象级纠错、原回执保留、停用/确认、续跑、回退、幂等。"""
import csv
import io
import pytest
from app.config import SAMPLE, HEADERS
from app.store import get_run, get_correction


def _csv(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


@pytest.fixture
def sample_rows():
    return list(csv.DictReader(io.StringIO(SAMPLE.read_text(encoding='utf-8-sig'))))


@pytest.fixture
def batch(client):
    run = client.post('/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    client.post('/api/review', json={'id': rid, 'revision': 1, 'decision': '通过'})
    return client.get('/api/runs/' + rid).get_json()


def _widen_c0007(rows, maximum='12'):
    """C0007 原量程 0-10，测量 11V 被拒；放宽到 12 后应翻转为可采用。"""
    target = [r for r in rows if r['cert'] == 'C0007'][0]
    target['maximum'] = maximum
    return _csv([target])


def test_correction_keeps_original_receipt_identity(client, batch, sample_rows):
    rid = batch['manifest']['run_id']
    corr_text = _widen_c0007(sample_rows)
    out = client.post('/api/m8/corrections', json={'run_id': rid, 'csv': corr_text}).get_json()
    # 纠错是独立对象，原批次 run_id/revision/复核结论完全不变。
    assert out['source_run_id'] == rid
    untouched = client.get('/api/runs/' + rid).get_json()
    assert untouched['revision'] == batch['revision'] == 2
    assert untouched['decision'] == '通过'
    assert untouched['stages']['6']['rows'] == batch['stages']['6']['rows']


def test_unaffected_measurements_keep_original_verdict(client, batch, sample_rows):
    corr = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': _widen_c0007(sample_rows)}).get_json()
    unaffected = [r for r in corr['rows'] if r['对象类别'] == '未变测量']
    assert len(unaffected) == 40
    assert all(r['状态'] == '沿用原回执' for r in unaffected)
    assert all(r['原结论'] == r['修正结论'] for r in unaffected)


def test_affected_measurements_suspended_then_updated(client, batch, sample_rows):
    rid = batch['manifest']['run_id']
    corr = client.post('/api/m8/corrections', json={'run_id': rid, 'csv': _widen_c0007(sample_rows)}).get_json()
    assert corr['state'] == '停用待确认'
    suspended = [r for r in corr['rows'] if '停用' in r['对象类别']]
    # C0007 自身及下游 C0027 受影响，确认前一律停用，不直接改判。
    assert {r['证书'] for r in suspended} == {'C0007', 'C0027'}
    assert all(r['状态'] == '停用待确认' for r in suspended)
    c0007 = next(r for r in suspended if r['证书'] == 'C0007')
    assert c0007['原结论'] == '拒用' and c0007['修正结论'] == '可采用' and c0007['结论是否变化'] is True

    confirmed = client.post(f"/api/m8/corrections/{corr['correction_id']}/confirm",
                            json={'revision': corr['revision'], 'decision': '通过'}).get_json()
    assert confirmed['state'] == '已确认更新'
    updated = [r for r in confirmed['rows'] if '已更新' in r['对象类别']]
    assert {r['证书'] for r in updated} == {'C0007', 'C0027'}
    assert all(r['修正结论'] == r['状态'] or r['状态'] == '已确认更新' for r in updated)
    assert all('按修正证书更新' in r['停用去向'] for r in updated)


def test_reject_keeps_measurements_suspended(client, batch, sample_rows):
    corr = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': _widen_c0007(sample_rows)}).get_json()
    out = client.post(f"/api/m8/corrections/{corr['correction_id']}/confirm",
                      json={'revision': corr['revision'], 'decision': '退回'}).get_json()
    assert out['state'] == '退回停用'
    assert all(r['状态'] == '退回停用' for r in out['rows'] if '停用' in r['对象类别'])
    # 退回后原批次裁决依旧不动。
    original = client.get('/api/runs/' + batch['manifest']['run_id']).get_json()
    c0007 = next(r for r in original['stages']['6']['rows'] if r['证书'] == 'C0007')
    assert c0007['结论'] == '拒用'


def test_rerun_does_not_create_second_correction(client, batch, sample_rows):
    rid = batch['manifest']['run_id']
    text = _widen_c0007(sample_rows)
    first = client.post('/api/m8/corrections', json={'run_id': rid, 'csv': text}).get_json()
    second = client.post('/api/m8/corrections', json={'run_id': rid, 'csv': text}).get_json()
    assert second['reused'] is True
    assert first['correction_id'] == second['correction_id']
    listing = client.get('/api/m8/corrections?run_id=' + rid).get_json()
    assert len(listing) == 1


def test_resumable_after_interruption_uses_saved_state(client, batch, sample_rows):
    """中断续跑：确认动作已持久化后，重新载入拿到的是最新状态，不会重复处理。"""
    corr = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': _widen_c0007(sample_rows)}).get_json()
    client.post(f"/api/m8/corrections/{corr['correction_id']}/confirm",
                json={'revision': corr['revision'], 'decision': '通过'})
    reloaded = client.get('/api/m8/corrections/' + corr['correction_id']).get_json()
    assert reloaded['state'] == '已确认更新'
    assert reloaded['checkpoint']['step'] == 'decided'
    assert len([r for r in reloaded['rows'] if '已更新' in r['对象类别']]) == 2
    # 已确认的纠错再次确认被状态机拒绝，保证续跑不产生重复副作用。
    again = client.post(f"/api/m8/corrections/{corr['correction_id']}/confirm",
                        json={'revision': reloaded['revision'], 'decision': '通过'})
    assert again.status_code == 422 and again.get_json()['code'] == 'CORRECTION_STATE'


def test_optimistic_lock_stale_revision_conflicts(client, batch, sample_rows):
    corr = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': _widen_c0007(sample_rows)}).get_json()
    ok = client.post(f"/api/m8/corrections/{corr['correction_id']}/confirm",
                     json={'revision': corr['revision'], 'decision': '退回'})
    assert ok.status_code == 200
    stale = client.post(f"/api/m8/corrections/{corr['correction_id']}/confirm",
                        json={'revision': corr['revision'], 'decision': '通过'})
    assert stale.status_code == 409 and stale.get_json()['code'] == 'VERSION'


def test_rollback_restores_original_review_state(client, batch, sample_rows):
    rid = batch['manifest']['run_id']
    corr = client.post('/api/m8/corrections', json={'run_id': rid, 'csv': _widen_c0007(sample_rows)}).get_json()
    confirmed = client.post(f"/api/m8/corrections/{corr['correction_id']}/confirm",
                            json={'revision': corr['revision'], 'decision': '通过'}).get_json()
    rolled = client.post(f"/api/m8/corrections/{corr['correction_id']}/rollback",
                         json={'revision': confirmed['revision']}).get_json()
    assert rolled['state'] == '已回退'
    restored = [r for r in rolled['rows'] if r['对象类别'] == '回退恢复']
    assert len(restored) == 2
    assert all('恢复旧版复核状态' in r['停用去向'] for r in restored)
    # 原批次复核状态与裁决没有被纠错改写。
    original = client.get('/api/runs/' + rid).get_json()
    assert original['decision'] == '通过'
    c0007 = next(r for r in original['stages']['6']['rows'] if r['证书'] == 'C0007')
    assert c0007['结论'] == '拒用'
    view = client.get('/api/m8/effective?correction_id=' + corr['correction_id']).get_json()
    assert '恢复原回执' in view['banner']


def test_downstream_certificates_are_in_impact_scope(client, batch, sample_rows):
    corr = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': _widen_c0007(sample_rows)}).get_json()
    # C0027 在样例中以 C0007 为父证书，纠错影响必须沿追溯链下传。
    assert 'C0007' in corr['affected_certs'] and 'C0027' in corr['affected_certs']


def test_supplementary_new_certificate_is_treated_as_new_object(client, batch, sample_rows):
    """补录一份原批次不存在的新证书，应作为新对象进入停用而非混入旧测量。"""
    new_row = {h: '' for h in HEADERS}
    new_row.update({'cert': 'C9001', 'device': 'D9001', 'parent': 'ROOT',
                    'valid_from': '2026-01-01', 'valid_to': '2027-01-01', 'minimum': '0',
                    'maximum': '10', 'unit': 'V', 'revoked': 'no', 'measured_at': '2026-11-17',
                    'value': '5', 'measure_unit': 'V', 'version': '1'})
    corr = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': _csv([new_row])}).get_json()
    new_objects = [r for r in corr['rows'] if r['原原因码'] == 'NEW_OBJECT']
    assert len(new_objects) == 1 and new_objects[0]['证书'] == 'C9001'


def test_invalid_correction_rows_are_quarantined_not_silent(client, batch, sample_rows):
    bad = [r for r in sample_rows if r['cert'] == 'C0007'][0].copy()
    bad['value'] = ''  # 必填数值缺失
    resp = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': _csv([bad])})
    # 结构可解析但行无效：纠错仍建立，问题进入纠错新增隔离。
    assert resp.status_code == 200
    delta = resp.get_json()['quarantine_delta']
    assert len(delta['纠错新增隔离']) >= 1


def test_evidence_csv_links_receipt_scope_and_destination(client, batch, sample_rows):
    corr = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': _widen_c0007(sample_rows)}).get_json()
    text = client.get('/api/m8/evidence.csv?correction_id=' + corr['correction_id']).data.decode('utf-8-sig')
    assert batch['manifest']['run_id'] in text
    assert corr['correction_id'] in text
    assert '停用去向' in text and '纠错范围·修正证书' in text and 'C0007' in text


def test_m8_page_renders_and_unknown_correction_404(client, batch):
    rid = batch['manifest']['run_id']
    assert client.get('/work/8?run_id=' + rid).status_code == 200
    assert client.get('/api/m8/corrections/nope').status_code == 404
    assert client.get('/api/m8/evidence.csv?correction_id=nope').status_code == 404


def test_correction_requires_existing_source_run(client):
    resp = client.post('/api/m8/corrections', json={'run_id': 'missing', 'csv': 'cert\n'})
    assert resp.status_code == 404 and resp.get_json()['code'] == 'NOT_FOUND'


def test_correction_rejects_bad_header(client, batch, sample_rows):
    resp = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': 'a,b,c\n1,2,3'})
    assert resp.status_code == 422 and resp.get_json()['code'] == 'HEADER'


def test_correction_with_only_malformed_rows_rejected(client, batch):
    header = ','.join(HEADERS)
    malformed = header + '\nC0007,D0007,x,ROOT,2026-01-01\n'
    resp = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': malformed})
    assert resp.status_code == 422 and resp.get_json()['code'] == 'CORRECTION_QUARANTINE'


def test_mixed_valid_and_malformed_rows_quarantines_bad_one(client, batch, sample_rows):
    """一份有效修正 + 一行结构错误：纠错成立，坏行进纠错新增隔离且带来源行标记。"""
    good = _widen_c0007(sample_rows)
    malformed_line = 'C0008,D0008,x,ROOT,2026-01-01\n'
    resp = client.post('/api/m8/corrections',
                       json={'run_id': batch['manifest']['run_id'], 'csv': good + malformed_line})
    assert resp.status_code == 200
    added = resp.get_json()['quarantine_delta']['纠错新增隔离']
    assert any(item.get('纠错来源行') == 'C2' for item in added)
    assert resp.get_json()['corrected_certs'] == ['C0007']


def test_multipart_correction_upload(client, batch, sample_rows):
    text = _widen_c0007(sample_rows)
    resp = client.post('/api/m8/corrections',
                       data={'file': (io.BytesIO(text.encode('utf-8')), 'fix.csv'),
                             'run_id': batch['manifest']['run_id']},
                       content_type='multipart/form-data')
    assert resp.status_code == 200 and resp.get_json()['correction_id']


def test_non_object_json_rejected_on_m8(client):
    for value in ([], 'x', 7, None):
        resp = client.post('/api/m8/corrections', json=value) if value is not None \
            else client.post('/api/m8/corrections', data='null', content_type='application/json')
        assert resp.status_code == 400 and resp.get_json()['code'] == 'REQUEST'
