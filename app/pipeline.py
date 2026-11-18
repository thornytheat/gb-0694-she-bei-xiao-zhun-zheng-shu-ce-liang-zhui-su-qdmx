from app.modules.m1_receiving.service import execute as stage1
from app.modules.m2_identity.service import execute as stage2
from app.modules.m3_validity.service import execute as stage3
from app.modules.m4_ranges.service import execute as stage4
from app.modules.m5_trace.service import execute as stage5
from app.modules.m6_verdict.service import execute as stage6
from app.modules.m7_revocation.service import execute as stage7
from app.config import SAMPLE, HEADERS
from app.manifest import make
from app.numeric import serial
from time import perf_counter
import csv as _csv
import io as _io
def calculate(text=None, stop=7):
    started=perf_counter()
    if text is None: text=SAMPLE.read_text(encoding='utf-8-sig')
    if not isinstance(text,str):
        from app.errors import DomainError
        raise DomainError('TYPE','CSV必须是文本')
    ctx={'text':text,'rows':[],'quarantine':[],'stages':{},'manifest':make(text)}
    for stage in STAGES[:stop]: stage(ctx)
    ctx['manifest']['elapsed_ms']=round((perf_counter()-started)*1000,3)
    # 存档规范化后的原始输入，供 M8 证书纠错在不接外部源的前提下离线合并复算。
    ctx['manifest']['source_csv']=_archive_rows(text)
    ctx.pop('text',None)
    return serial(ctx)
def _archive_rows(text):
    reader=_csv.DictReader(_io.StringIO(text.lstrip('﻿'),newline=''))
    if reader.fieldnames is None: return []
    rows=[]
    for raw in reader:
        if raw is None or all((v is None or not str(v).strip()) for v in raw.values()): continue
        rows.append({h:(raw.get(h) or '').strip() for h in HEADERS})
    return rows
STAGES=[stage1,stage2,stage3,stage4,stage5,stage6,stage7]
