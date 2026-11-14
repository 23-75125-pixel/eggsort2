"""Disposable Egg Records server: real routes, private SQLite, no device modules."""
import json
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
# Preserve the real lifecycle module identity across the auth harness import.
import sorting_lifecycle
import auth_test_support as lab
from egg_standards import classify_egg_size
from flask import abort, jsonify, request
from models import User, SortingSession, EggRecord, AuditLog, PasswordResetAttempt
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

app = lab.app
secret = secrets.token_urlsafe(32)
password = secrets.token_urlsafe(20)
temp = tempfile.TemporaryDirectory(prefix='eggsort-records-lab-')
database = Path(temp.name) / 'records.sqlite3'
today = datetime.now(ZoneInfo('Asia/Manila')).date()
midnight = datetime.combine(today, datetime.min.time(), tzinfo=ZoneInfo('Asia/Manila'))
mode = {'value': 'normal'}
requests = []
app.config.update(TESTING=False, SECRET_KEY=secrets.token_hex(32),
    SESSION_COOKIE_NAME='eggsort_records_lab', LOCAL_HARDWARE_ENABLED=False,
    AUTO_START_SORTING_ON_LOGIN=False)

with app.app_context():
    lab.db.engines[None].dispose()
    engine = create_engine(URL.create('sqlite', database=str(database)),
        connect_args={'check_same_thread': False})
    @event.listens_for(engine, 'connect')
    def sqlite_foreign_keys(connection, _record):
        connection.execute('PRAGMA foreign_keys=ON')
    lab.db._app_engines[app][None] = engine
    assert engine.url.get_backend_name() == 'sqlite'
    assert Path(engine.url.database).resolve().parent == Path(temp.name).resolve()
    lab.db.metadata.create_all(engine, tables=[User.__table__, SortingSession.__table__,
        EggRecord.__table__, AuditLog.__table__, PasswordResetAttempt.__table__])
    for username, role in [('recordadmin','admin'), ('recordstaff','staff')]:
        lab.db.session.add(User(username=username,email=username+'@example.invalid',
            display_name='Records Lab '+role, role=role, is_active=True,password_set=True,
            password=generate_password_hash(password)))
    lab.db.session.commit()


def insert_record(id, weight, size, quality, verified, at, session='LAB-TODAY'):
    if lab.db.session.get(SortingSession, session) is None:
        lab.db.session.add(SortingSession(session_ref=session, status='historical'))
        lab.db.session.flush()
    lab.db.session.add(EggRecord(id=id,weight_grams=weight,size=size,quality=quality,
        quality_verified=verified,confidence=.94 if verified else 0,
        sorted_at=at.astimezone(timezone.utc),session_ref=session))


with app.app_context():
    entries = [
        (1,55,'Small','Good',True,0,'LAB-TODAY'),
        (2,59,'Medium','Good',True,8,'LAB-TODAY'),
        (3,0,'N/A','Crack',True,9,'LAB-TODAY'),
        (4,0,'N/A','Rotten',True,10,'LAB-TODAY'),
        (5,63,'Large','Not Classified',False,11,'LAB-TODAY'),
        (6,68,'Extra Large','Good',None,12,'LAB-TODAY'),
        (7,72,'Jumbo','Good',True,13,'FARM-ALPHA'),
        (8,40,'Peewee','Good',None,14,'LAB-IMPORTED'),
        (9,41,'Peewee','Good',None,-1,'LAB-HISTORY'),
        (10,64,'Large','Good',True,23,'LAB-TODAY'),
        (11,58,'Medium','Good',True,24,'LAB-FUTURE'),
        (12,51,'Small','Good',False,15,'LAB-TODAY'),
        (13,52,'Small','Good',None,16,'LAB-TODAY'),
        (14,60,'Medium','Good',True,17,'LAB-"A,B'),
        (15,54,'Small','Good',True,18,'LAB-<svg onload=alert(1)>'),
    ]
    for id, weight, size, quality, verified, hours, session in entries:
        at=midnight+timedelta(hours=hours)
        if id==10: at=midnight+timedelta(days=1,microseconds=-1)
        if id==9: at=midnight-timedelta(microseconds=1)
        insert_record(id,weight,size,quality,verified,at,session)
    lab.db.session.commit()


@app.before_request
def isolate_records_lab():
    if request.remote_addr != '127.0.0.1': abort(403)
    if request.endpoint not in {'static','login','logout','dashboard','egg_records',
                                'egg_records_data','lab_control','lab_state'}: abort(403)
    if request.endpoint=='egg_records_data':
        requests.append(dict(request.args))
        if mode['value']=='database_error': raise SQLAlchemyError('Simulated private DB outage')
        if mode['value']=='html_error': return '<html>Simulated proxy error</html>',502


def authorized():
    if request.headers.get('X-Lab-Secret')!=secret: abort(403)


@app.get('/__records_state')
def lab_state():
    authorized()
    return jsonify(requests=requests[-100:], count=EggRecord.query.count(),mode=mode['value'])


@app.post('/__records_control')
def lab_control():
    authorized()
    data=request.get_json()
    command=data['command']
    if command=='mode':
        assert data['value'] in {'normal','database_error','html_error'}
        mode['value']=data['value']
    elif command=='clear':
        EggRecord.query.delete(); lab.db.session.commit()
    elif command=='add':
        insert_record(20,59,'Medium','Good',True,midnight+timedelta(hours=19),'LAB-NEW')
        lab.db.session.commit()
    elif command=='bulk':
        EggRecord.query.delete()
        for n in range(505):
            insert_record(1000+n,59,'Medium','Good',True,midnight+timedelta(seconds=n),'LAB-BULK')
        lab.db.session.commit()
    elif command=='disable':
        user=User.query.filter_by(username=data.get('username','recordadmin')).one()
        user.is_active=False; lab.db.session.commit()
    else: abort(400)
    return jsonify(ok=True)


server=make_server('127.0.0.1',0,app,threaded=True)
print(json.dumps({'port':server.server_port,'username':'recordadmin','password':password,
    'control':secret,'today':today.isoformat(),'yesterday':(today-timedelta(days=1)).isoformat(),
    'tomorrow':(today+timedelta(days=1)).isoformat(),
    'classifications':{str(w):classify_egg_size(w) for w in [51,55,59,60,64,68,72]}}),flush=True)
try:
    with (patch('socket.socket.connect',side_effect=AssertionError('Outbound network forbidden')),
          patch('socket.socket.connect_ex',side_effect=AssertionError('Outbound network forbidden'))):
        server.serve_forever()
finally:
    server.server_close()
    with app.app_context(): lab.db.engine.dispose()
    temp.cleanup()
