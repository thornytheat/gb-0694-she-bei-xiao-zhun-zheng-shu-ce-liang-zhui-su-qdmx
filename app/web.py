from flask import Blueprint, render_template, request
from app.config import TITLE, MODULES, SHORT, SAMPLE
from app.pipeline import calculate
from app.store import latest, save, get_run, get_correction, list_corrections
from app.modules.m8_correction import service as m8_service
bp=Blueprint('web',__name__)
@bp.route('/')
@bp.route('/work/<int:stage>')
def work(stage=1):
    if stage<1 or stage>8:
        from flask import abort
        abort(404)
    run=get_run(request.args['run_id']) if request.args.get('run_id') else latest()
    if run is None: run=save(calculate())
    if stage==8:
        correction_id=request.args.get('correction_id')
        correction=get_correction(correction_id) if correction_id else None
        if correction is None:
            entries=list_corrections(run['manifest']['run_id'])
            correction=get_correction(entries[0]['id']) if entries else None
        current=m8_service.summarize(correction) if correction else _empty_m8(run)
        corrections=list_corrections(run['manifest']['run_id'])
        return render_template('m8.html', title=TITLE, modules=MODULES, short=SHORT, run=run,
                               current=current, stage=stage, sample=SAMPLE.read_text(encoding='utf-8-sig'),
                               correction=correction, corrections=corrections)
    current=run['stages'][str(stage)]
    return render_template('m'+str(stage)+'.html', title=TITLE, modules=MODULES, short=SHORT, run=run, current=current, stage=stage, sample=SAMPLE.read_text(encoding='utf-8-sig'))
def _empty_m8(run):
    return {'stage':8,'name':'证书纠错补录','state':'尚未提交纠错','correction_id':None,
            'source_run_id':run['manifest']['run_id'],'source_revision':run['revision'],
            'revision':0,'reused':False,'corrected_certs':[],'affected_certs':[],
            'rows':[],'metrics':[['纠错证书',0,'份'],['受影响证书',0,'份'],['未变沿用原结论',0,'条'],
                                 ['停用待处理',0,'条'],['确认更新',0,'条'],['其中结论翻转',0,'条'],
                                 ['纠错状态','尚未提交纠错','']],
            'checkpoint':{},'quarantine_delta':{},'recomputed':{}}
