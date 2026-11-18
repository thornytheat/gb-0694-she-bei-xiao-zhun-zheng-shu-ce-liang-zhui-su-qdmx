import sqlite3, json, os, hashlib, time
from pathlib import Path
from app.errors import DomainError
def connection():
    path=Path(os.environ.get('APP_DB','data/runs.sqlite3'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('''CREATE TABLE IF NOT EXISTS corrections(
        id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL, digest TEXT NOT NULL,
        state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(source_run_id, digest))''')
    return db
def save(result):
    with connection() as db:
        ident=result['manifest']['run_id'];row=db.execute('SELECT payload,revision,decision FROM runs WHERE id=?',(ident,)).fetchone()
        if row: return {**json.loads(row['payload']), 'revision':row['revision'], 'decision':row['decision'], 'reused':True}
        db.execute('INSERT INTO runs(id,payload) VALUES (?,?)',(ident,json.dumps(result,ensure_ascii=False)))
    return {**result,'revision':1,'decision':'待复核','reused':False}
def get_run(ident):
    with connection() as db: row=db.execute('SELECT payload,revision,decision FROM runs WHERE id=?',(ident,)).fetchone()
    if row is None: raise DomainError('NOT_FOUND','批次不存在',404)
    return {**json.loads(row['payload']),'revision':row['revision'],'decision':row['decision']}
def latest():
    with connection() as db: row=db.execute('SELECT payload,revision,decision FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
    return {**json.loads(row['payload']),'revision':row['revision'],'decision':row['decision']} if row else None
def history():
    with connection() as db: return [dict(r) for r in db.execute('SELECT id,revision,decision FROM runs ORDER BY rowid DESC')]
def review(ident, revision, decision):
    if decision not in ('通过','退回'): raise DomainError('DECISION','复核动作无效')
    with connection() as db:
        changed=db.execute('UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?',(decision,ident,revision)).rowcount
        if not changed: raise DomainError('VERSION','版本已变化，请重新载入',409)
    return {'revision':revision+1,'decision':decision}

# ---------------- M8 证书纠错补录 ----------------
def correction_identity(source_run, text):
    """幂等身份：原回执 id + 规则版本 + 纠错原文摘要。重跑同一纠错返回同一 id。"""
    digest=hashlib.sha256(text.encode('utf-8')).hexdigest()
    basis='%s:%s:%s'%(source_run['manifest']['run_id'],source_run['manifest']['rule_version'],digest)
    return hashlib.sha256(basis.encode('utf-8')).hexdigest()[:16],digest

def save_correction(payload, source_run, text):
    """幂等创建纠错。已存在则原样返回（不生成第二份），并标记 reused。"""
    ident,digest=correction_identity(source_run,text)
    now=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    payload['correction_id']=ident
    with connection() as db:
        row=db.execute('SELECT payload,revision,state FROM corrections WHERE id=?',(ident,)).fetchone()
        if row:
            existing=json.loads(row['payload'])
            return {**existing,'revision':row['revision'],'state':row['state'],'reused':True}
        # 同一原回执允许存在多份不同纠错；但同一摘要的重跑必须复用，靠 UNIQUE 兜底。
        try:
            db.execute('INSERT INTO corrections(id,source_run_id,digest,state,revision,payload,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?)',
                       (ident,source_run['manifest']['run_id'],digest,payload['state'],json.dumps(payload,ensure_ascii=False),now,now))
        except sqlite3.IntegrityError:
            row=db.execute('SELECT payload,revision,state FROM corrections WHERE id=?',(ident,)).fetchone()
            existing=json.loads(row['payload'])
            return {**existing,'revision':row['revision'],'state':row['state'],'reused':True}
    return {**payload,'revision':1,'reused':False}

def get_correction(ident):
    with connection() as db:
        row=db.execute('SELECT payload,revision,state FROM corrections WHERE id=?',(ident,)).fetchone()
    if row is None: raise DomainError('NOT_FOUND','纠错不存在',404)
    return {**json.loads(row['payload']),'revision':row['revision'],'state':row['state']}

def list_corrections(source_run_id=None):
    with connection() as db:
        if source_run_id:
            cur=db.execute('SELECT id,source_run_id,state,revision,created_at,updated_at FROM corrections WHERE source_run_id=? ORDER BY rowid DESC',(source_run_id,))
        else:
            cur=db.execute('SELECT id,source_run_id,state,revision,created_at,updated_at FROM corrections ORDER BY rowid DESC')
        return [dict(r) for r in cur]

def update_correction(ident, revision, payload):
    """乐观锁持久化状态推进；中断后凭已提交状态续跑，不会覆盖较新版本。"""
    now=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    with connection() as db:
        changed=db.execute('UPDATE corrections SET payload=?,state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?',
                           (json.dumps(payload,ensure_ascii=False),payload['state'],now,ident,revision)).rowcount
        if not changed: raise DomainError('VERSION','纠错版本已变化，请重新载入',409)
    return get_correction(ident)

def rollback_correction(ident, revision):
    """回退恢复旧版复核状态：原批次 revision/decision 不动，纠错标记为已回退。"""
    with connection() as db:
        row=db.execute('SELECT payload FROM corrections WHERE id=? AND revision=?',(ident,revision)).fetchone()
        if row is None:
            if db.execute('SELECT 1 FROM corrections WHERE id=?',(ident,)).fetchone():
                raise DomainError('VERSION','纠错版本已变化，请重新载入',409)
            raise DomainError('NOT_FOUND','纠错不存在',404)
        payload=json.loads(row['payload'])
    from app.modules.m8_correction.rules import rollback
    payload=rollback(payload)
    return update_correction(ident,revision,payload)
