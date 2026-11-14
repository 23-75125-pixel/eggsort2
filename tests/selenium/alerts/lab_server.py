"""Alerts-only lab: real routes/helper, private SQLite, synthetic eggs, no devices."""
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
import sorting_lifecycle  # Retain one exception/module identity through the auth loader.
import auth_test_support as lab
from flask import abort, jsonify, request
from models import (User, EggRecord, SortingSession, TraySizeProgress, TraySizeProgressEgg,
                    TraySizeAlert, TRAY_TRACKED_SIZES)
from sqlalchemy import CheckConstraint, MetaData, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

directory=Path(os.environ['EGGSOR_ALERTS_LAB_DIR']).resolve()
assert directory.parent==Path(tempfile.gettempdir()).resolve()
assert directory.name.startswith('eggsort-alerts-lab-')
app=lab.app;auth=lab.auth
control=secrets.token_urlsafe(32);password=secrets.token_urlsafe(20)
mode={'get':'normal','post':'normal'}
calls=[]
app.config.update(TESTING=False,SECRET_KEY=secrets.token_hex(32),
    SESSION_COOKIE_NAME='eggsort_alerts_lab',LOCAL_HARDWARE_ENABLED=False,AUTO_START_SORTING_ON_LOGIN=False)
metadata=MetaData()
for table in lab.db.metadata.sorted_tables:
    copied=table.to_metadata(metadata)
    # Only this PostgreSQL cast/division expression is incompatible with SQLite.
    for constraint in list(copied.constraints):
        if isinstance(constraint,CheckConstraint) and constraint.name=='ck_tray_size_progress_state_consistency':
            copied.constraints.remove(constraint)
with app.app_context():
    lab.db.engines[None].dispose()
    engine=create_engine(URL.create('sqlite',database=str(directory/'alerts.sqlite3')),
        connect_args={'check_same_thread':False})
    @event.listens_for(engine,'connect')
    def enforce_fk(conn,_record):conn.execute('PRAGMA foreign_keys=ON')
    lab.db._app_engines[app][None]=engine
    assert engine.url.get_backend_name()=='sqlite'
    metadata.create_all(engine)
    for username,role in [('alertadmin','admin'),('alertstaff','staff')]:
        lab.db.session.add(User(username=username,email=username+'@example.invalid',
            display_name='Alerts Lab '+role,role=role,is_active=True,password_set=True,
            password=generate_password_hash(password)))
    lab.db.session.commit()


def reset(openings=None):
    TraySizeAlert.query.delete();TraySizeProgressEgg.query.delete();EggRecord.query.delete()
    TraySizeProgress.query.delete();SortingSession.query.delete()
    lab.db.session.add(SortingSession(session_ref='AL-SYNTHETIC',status='historical'))
    for size in TRAY_TRACKED_SIZES:
        opening=(openings or {}).get(size,0)
        lab.db.session.add(TraySizeProgress(egg_size=size,opening_balance=opening,
            opening_balance_set_at=datetime.now(timezone.utc),confirmed_good_eggs_since_opening=0,
            completed_milestones=0,eggs_toward_next_milestone=opening))
    lab.db.session.commit()


def good(size='Small',quality='Good',verified=True):
    weights={'Small':55,'Medium':59,'Large':64,'Extra Large':68,'Jumbo':72}
    row=EggRecord(weight_grams=weights[size],size=size,quality=quality,quality_verified=verified,
        confidence=.95,session_ref='AL-SYNTHETIC')
    lab.db.session.add(row);lab.db.session.flush()
    alert=auth._count_verified_good_egg_for_tray(row)
    return row,alert


with app.app_context():
    reset({size:29 for size in TRAY_TRACKED_SIZES})
    for size in TRAY_TRACKED_SIZES:good(size)
    for n in range(30):good('Medium')
    lab.db.session.flush()
    now=datetime.now(timezone.utc)
    for row in TraySizeAlert.query.all():
        row.is_read=row.id in (2,5)
        # ID6 deliberately has the oldest timestamp: API orders by ID, not time.
        row.created_at=now-timedelta(hours=row.id)
    lab.db.session.commit()


@app.before_request
def isolation():
    if request.remote_addr!='127.0.0.1':abort(403)
    if request.endpoint not in {'static','login','logout','dashboard','alerts','alerts_data',
        'mark_alert_read','mark_all_alerts_read','lab_state','lab_control'}:abort(403)
    if request.endpoint in {'alerts_data','mark_alert_read','mark_all_alerts_read'}:
        calls.append({'method':request.method,'path':request.path,'args':dict(request.args)})
        value=mode['get' if request.method=='GET' else 'post']
        if value=='database_error':raise SQLAlchemyError('Simulated private Alerts DB outage')
        if value=='json_error':return jsonify(error='Simulated acknowledgement failure.'),503
        if value=='html_error':return '<html>Simulated proxy error</html>',502
        if value=='malformed_json':return '{broken',200,{'Content-Type':'application/json'}
        if value=='missing_fields':return jsonify({})
        if value=='html_success':return '<html>Unexpected upstream response</html>',200
        if value=='null_count':
            rows=TraySizeAlert.query.filter_by(is_read=False).order_by(TraySizeAlert.id.desc()).all()
            return jsonify(alerts=[r.to_dict() for r in rows],unread_count=None)


def authorize():
    if request.headers.get('X-Lab-Secret')!=control:abort(403)


def snapshot():
    rows=TraySizeAlert.query.order_by(TraySizeAlert.id).all()
    return dict(alerts=[r.to_dict() for r in rows],unread=sum(not r.is_read for r in rows),
        count=len(rows),eggs=EggRecord.query.count(),claims=TraySizeProgressEgg.query.count(),
        boundaries=[{'alert_id':r.id,'egg_id':r.boundary_egg_record_id,
            'size':r.egg_size,'milestone':r.milestone_number} for r in rows],
        progress=[r.to_dict() for r in TraySizeProgress.query.order_by(TraySizeProgress.egg_size).all()],
        calls=calls[-200:],mode=mode)


@app.get('/__alerts_state')
def lab_state():authorize();return jsonify(snapshot())


@app.post('/__alerts_control')
def lab_control():
    authorize();data=request.get_json();command=data['command']
    if command=='mode':
        assert data['target'] in ('get','post')
        assert data['value'] in {'normal','database_error','json_error','html_error',
            'malformed_json','missing_fields','html_success','null_count'}
        mode[data['target']]=data['value']
    elif command=='clear':reset()
    elif command=='fresh':reset({'Small':29,'Medium':28})
    elif command=='good':
        for _ in range(int(data.get('count',1))):
            good(data.get('size','Small'),data.get('quality','Good'),data.get('verified',True))
        lab.db.session.commit()
    elif command=='repeat_claim':
        row=lab.db.session.get(EggRecord,data['egg_id'])
        auth._count_verified_good_egg_for_tray(row);lab.db.session.commit()
    elif command=='bulk':
        reset();count=int(data['count'])
        progress=lab.db.session.get(TraySizeProgress,'Small')
        progress.confirmed_good_eggs_since_opening=count*30
        progress.completed_milestones=count;progress.eggs_toward_next_milestone=0
        for n in range(1,count+1):
            row=EggRecord(weight_grams=55,size='Small',quality='Good',quality_verified=True,
                confidence=.95,session_ref='AL-SYNTHETIC')
            lab.db.session.add(row);lab.db.session.flush()
            lab.db.session.add(TraySizeProgressEgg(egg_record_id=row.id,egg_size='Small'))
            lab.db.session.flush()
            lab.db.session.add(TraySizeAlert(egg_size='Small',milestone_number=n,
                eligible_egg_count_at_milestone=n*30,boundary_egg_record_id=row.id,is_read=False))
        lab.db.session.commit()
    elif command=='disable':
        User.query.filter_by(username='alertadmin').one().is_active=False;lab.db.session.commit()
    else:abort(400)
    return jsonify(snapshot())


server=make_server('127.0.0.1',0,app,threaded=True)
print(json.dumps({'port':server.server_port,'username':'alertadmin','password':password,'control':control}),flush=True)
try:
    with (patch('socket.socket.connect',side_effect=AssertionError('Outbound network forbidden')),
          patch('socket.socket.connect_ex',side_effect=AssertionError('Outbound network forbidden'))):
        server.serve_forever()
finally:
    server.server_close()
    with app.app_context():lab.db.engine.dispose()
