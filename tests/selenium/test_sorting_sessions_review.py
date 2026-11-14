"""Additional restored-code checks. All hardware responses are simulated."""
import pytest
from selenium.webdriver.common.by import By
from test_sorting_sessions import (open_sessions, element, text, wait, control,
    lab_state, wait_ready, start, stop, dialog, wait_success_modal_closed,
    force_ui_refresh, success_modal_visible)


@pytest.mark.case('SS-23', 'Initial layout and station state', 'Idle visible, active panel hidden, stopped ESP32 and empty counters; navigation resolves.',
                  'Open authenticated page through sidebar link; inspect controls, labels and initial polling.', 'Fresh disposable admin; no worker started.')
def test_initial_layout(sorting_browser):
    d,l=sorting_browser
    open_sessions(d,l)
    element(d,'a[href="/sorting-sessions"]').click()
    wait(d,lambda d: d.find_element(By.ID,'camera-idle').is_displayed())
    assert not element(d,'#camera-active').is_displayed()
    for id in ['camera-feed','camera-loading','camera-indicator','hardware-status','hardware-event','latest-egg-record','detection-counts','egg-zone-count']:
        assert len(d.find_elements(By.ID,id))==1
    wait(d,lambda d: d.find_element(By.ID,'hardware-status').get_attribute('textContent')=='Stopped')
    assert element(d,'#egg-zone-count').get_attribute('textContent')=='0'
    assert element(d,'#latest-egg-record').get_attribute('textContent')=='No completed egg yet'
    assert lab_state(d,l)['sessions_count']==0
    assert d.execute_script('return document.documentElement.scrollWidth<=innerWidth')


@pytest.mark.case('SS-24', 'Preview readiness', 'Loading overlay is visible while starting, then decoded fake video and hidden overlay after readiness.',
                  'Start without first frame, inspect loading; inject ready and inspect image natural dimensions.', 'Synthetic JPEG MJPEG stream; no webcam.')
def test_preview(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l); start(d)
    assert element(d,'#camera-loading').is_displayed()
    control(d,l,{'command':'ready'})
    wait(d,lambda d:not d.find_element(By.ID,'camera-loading').is_displayed())
    wait(d,lambda d:d.execute_script('return document.getElementById("camera-feed").naturalWidth>0'))


@pytest.mark.case('SS-25', 'Restart after Stop', 'New session reference and exactly two camera/bridge starts; prior session completed.',
                  'Start and ready, stop and close success notice, then start and ready again.', 'Two sequential simulated station sessions.')
def test_restart(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l); wait_ready(d,l)
    old=lab_state(d,l)['lifecycle']['session_ref']; stop(d)
    wait(d,lambda d:success_modal_visible(d,'Closed Successfully!'))
    wait_success_modal_closed(d,'Closed Successfully!'); wait_ready(d,l)
    s=lab_state(d,l)
    assert s['lifecycle']['session_ref']!=old
    assert s['sessions_count']==2
    assert s['camera_start_count']==s['bridge_start_count']==2
    assert any(x['session_ref']==old and x['status']=='completed' for x in s['sessions'])


@pytest.mark.case('SS-26', 'Refresh running and stopped sessions', 'Refresh restores running preview without duplicate start; stopped session stays stopped on refresh.',
                  'Ready session, refresh, inspect reference and start counts; stop, refresh and inspect idle.', 'Existing fake station session.')
def test_refresh(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l); wait_ready(d,l)
    old=lab_state(d,l)['lifecycle']['session_ref']; d.refresh()
    wait(d,lambda d:d.find_element(By.ID,'camera-active').is_displayed())
    assert lab_state(d,l)['lifecycle']['session_ref']==old
    assert lab_state(d,l)['camera_start_count']==1
    stop(d); wait(d,lambda d:d.find_element(By.ID,'camera-idle').is_displayed())
    d.refresh(); wait(d,lambda d:d.find_element(By.ID,'camera-idle').is_displayed())
    assert lab_state(d,l)['lifecycle']['status']=='completed'
    assert lab_state(d,l)['camera_start_count']==1


@pytest.mark.case('SS-27', 'Staff sorting permissions', 'Authenticated staff can operate Sorting Sessions under current login_required policy.',
                  'Change disposable account to staff, reload, start and stop through UI.', 'Staff role; current routes permit both admin and staff.')
def test_staff(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l); control(d,l,{'command':'role'})
    d.refresh(); wait_ready(d,l); stop(d)
    wait(d,lambda d:d.find_element(By.ID,'camera-idle').is_displayed())
    assert lab_state(d,l)['lifecycle']['status']=='completed'


@pytest.mark.case('SS-28', 'Remote station restriction', 'Start disabled; start/stop/status/feed/hardware APIs reject operation with 409.',
                  'Disable local hardware in lab config; reload page and issue same-origin browser API calls.', 'LOCAL_HARDWARE_ENABLED=false; admin cookie; valid CSRF.')
def test_remote_restriction(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l); control(d,l,{'command':'hardware_enabled','enabled':False}); d.refresh()
    assert not element(d,'#start-camera').is_enabled()
    assert 'Sorting Computer' in text(d,'#camera-idle')
    statuses=d.execute_async_script("""const done=arguments[0];const csrf=document.querySelector('meta[name="csrf-token"]').content;
    Promise.all(['/api/camera/start','/api/camera/stop','/api/camera/status','/api/camera/feed','/api/hardware/status'].map(
      (url,i)=>fetch(url,{method:i<2?'POST':'GET',headers:{'X-CSRF-Token':csrf}}).then(r=>r.status))).then(done);""")
    assert statuses==[409]*5


@pytest.mark.case('SS-29', 'Weight, sensor and quality display branches', 'Live zero, stable measurement, final weight, sensor and unavailable-servo messages render correctly.',
                  'Ready station and publish diagnostics one at a time; wait for actual hardware polling text.', 'Fake readings 0g, 58.5g, 59g, Good; PCA9685 unavailable.')
def test_diagnostics(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l); wait_ready(d,l)
    cases=[({'live_weight_grams':0,'measurement_quality':'Good'},'0 g'),
      ({'measurement_quality':'Good','measurement_weight_grams':58.5,'measurement_reading_number':2,'capture_id':7},'Scale reading 2: 58.5 g'),
      ({'measurement_quality':'Good','final_weight_grams':59,'capture_id':7},'Final weight: 59 g'),
      ({'awaiting_egg':True,'latest_sensor_event':'IR sensor: egg detected'},'IR sensor: egg detected'),
      ({'automatic_release':True,'final_weight_grams':59,'pca9685_ready':False},'Load-cell servo unavailable')]
    for diagnostics,expected in cases:
        control(d,l,{'command':'diagnostics','diagnostics':diagnostics})
        wait(d,lambda d:expected in text(d,'#hardware-event'))


@pytest.mark.case('SS-30', 'Completed classification UI', 'Latest egg record displays 59g, Medium, Good and Verified.',
                  'Emit fake Good capture and completed controller cycle; inspect actual record polling display.', 'Fake capture 77, confidence .94, 59g; temporary SQLite only.')
def test_record_ui(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l); wait_ready(d,l)
    control(d,l,{'command':'fresh_good'})
    wait(d,lambda d:all(x in text(d,'#latest-egg-record') for x in ['59 g','Medium','Good','Verified']))


@pytest.mark.case('SS-31', 'ESP32 startup unavailable', 'Error explains simulated ESP32 startup failure in readable language.',
                  'Configure fake bridge start failure, confirm start and read visible error.', 'Fake bridge raises LifecycleError; no COM port opened.')
def test_bridge_error(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l)
    control(d,l,{'command':'configure','bridge_fail_start':True})
    element(d,'#start-camera').click(); dialog(d,True,'Restart camera')
    wait(d,lambda d:success_modal_visible(d,'Operation failed'))
    assert 'Simulated ESP32 bridge startup failure' in text(d,'.swal2-popup')
    assert 'Unexpected token' not in text(d,'.swal2-popup')
    state=lab_state(d,l)
    assert state['lifecycle']['status']=='failed'
    assert state['lifecycle']['recovery_required'] is True
    assert not state['camera']['running'] and not state['bridge']['running']
    assert not success_modal_visible(d,'Restarted Successfully!')


@pytest.mark.case('SS-32', 'Start/Stop pending button guards', 'Pending operations disable the initiating control, reject duplicate starts/stops and restore control afterward.',
                  'Hold browser fetch for start and stop, inspect pending buttons, dispatch duplicate and release real request.', 'Controlled browser promise delays; real local endpoints with fake workers.')
def test_pending_buttons(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l)
    for endpoint,button,title in [('/api/camera/start','#start-camera','Restart camera'),('/api/camera/stop','#stop-camera','Stop sorting session')]:
        d.execute_script("""window.reviewOriginalFetch=window.fetch;window.reviewRelease=null;window.reviewCalls=0;
        window.reviewEndpoint=arguments[0];
        window.fetch=(url,options)=>{
          const requestUrl=url instanceof Request?url.url:String(url);
          if(new URL(requestUrl,location.href).pathname===window.reviewEndpoint){
            window.reviewCalls++;
            return new Promise(resolve=>window.reviewRelease=()=>reviewOriginalFetch(url,options).then(resolve));
          }
          return reviewOriginalFetch(url,options);
        };""",endpoint)
        # Observe the click handler's synchronous disabled transition in-page;
        # the 250ms status poll can otherwise change it before WebDriver's next
        # remote read. The later held-request check verifies the pending state.
        d.execute_script("""const button=document.querySelector(arguments[0]);
        window.reviewDisabledObserved=false;
        window.reviewDisabledObserver?.disconnect();
        window.reviewDisabledObserver=new MutationObserver(()=>{
          if(button.disabled){window.reviewDisabledObserved=true;window.reviewDisabledObserver.disconnect();}
        });
        window.reviewDisabledObserver.observe(button,{attributes:true,attributeFilter:['disabled']});""",button)
        element(d,button).click()
        wait(d,lambda d:d.execute_script('return window.reviewDisabledObserved'))
        assert d.execute_script('return window.reviewDisabledObserved') is True
        dialog(d,True,title)
        wait(d,lambda d:not success_modal_visible(d,title))
        wait(d,lambda d:d.execute_script('return !!window.reviewRelease'))
        wait(d,lambda d:not d.find_element(By.CSS_SELECTOR,button).is_enabled())
        d.execute_script('document.querySelector(arguments[0]).click()',button)
        assert d.execute_script('return window.reviewCalls')==1
        d.execute_script('window.fetch=reviewOriginalFetch;window.reviewRelease()')
        wait(d,lambda d:d.find_element(By.CSS_SELECTOR,button).is_enabled())
        if endpoint.endswith('start'):
            control(d,l,{'command':'ready'})
            wait(d,lambda d:success_modal_visible(d,'Restarted Successfully!')); wait_success_modal_closed(d)


@pytest.mark.case('SS-33', 'Hardware polling outage and recovery', 'UI reports unavailable on request failure and returns Connected after transport recovers.',
                  'Reject hardware status fetch, execute UI refresh, restore fetch and poll again.', 'Simulated browser transport failure only.')
def test_poll_recovery(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l); wait_ready(d,l)
    d.execute_async_script("""const done=arguments[0];(async()=>{
      while(hardwareStatusPending)await new Promise(r=>setTimeout(r,5));
      const old=window.fetch;window.fetch=(u,o)=>String(u).includes('/api/hardware/status')?Promise.reject(new Error('Simulated ESP32 transport unavailable')):old(u,o);
      await refreshHardwareState();window.fetch=old;done();})();""")
    assert text(d,'#hardware-status')=='Hardware status unavailable'
    wait(d,lambda d:'Connected on COM-FAKE' in text(d,'#hardware-status'))


@pytest.mark.case('SS-34', 'Stop cleanup failure and retry', 'Failure remains visible with recovery required; retry after fault removal completes stop.',
                  'Ready session, inject fake stop failure, confirm stop; clear fault and retry.', 'Fake camera cleanup exception, then normal stop.')
def test_stop_failure_retry(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l); wait_ready(d,l)
    control(d,l,{'command':'configure','camera_fail_stop':True}); stop(d)
    wait(d,lambda d:success_modal_visible(d,'Operation failed'))
    assert lab_state(d,l)['lifecycle']['recovery_required']
    assert not success_modal_visible(d,'Closed Successfully!')
    dialog(d,True)
    wait(d,lambda d:not any(p.is_displayed() for p in d.find_elements(By.CSS_SELECTOR,'.swal2-popup')))
    control(d,l,{'command':'configure'})
    wait(d,lambda d:d.find_element(By.ID,'stop-camera').is_enabled())
    d.find_element(By.ID,'stop-camera').click()
    dialog(d,True,'Stop sorting session')
    wait(d,lambda d:d.find_element(By.ID,'camera-idle').is_displayed())
    assert lab_state(d,l)['lifecycle']['status']=='completed'


@pytest.mark.case('SS-35', 'Unverified record display', 'Weight-only cycle renders 59g Medium Not Classified Unverified.',
                  'Send fake completed cycle without camera capture; wait for record display.', 'Fake controller cycle; no camera inspection.')
def test_unverified_ui(sorting_browser):
    d,l=sorting_browser; open_sessions(d,l); wait_ready(d,l)
    control(d,l,{'command':'weight_only'})
    wait(d,lambda d:all(x in text(d,'#latest-egg-record') for x in ['59 g','Medium','Not Classified','Unverified']))


@pytest.mark.case('SS-36', 'Unauthorized sorting APIs', 'Anonymous camera, feed, hardware and records GETs return 401 JSON Authentication is required.',
                  'Request protected sorting APIs using browser fetch with no session.', 'Fresh browser cookies; unauthenticated.')
def test_anonymous_apis(sorting_browser):
    d,l=sorting_browser; d.get(l['base']+'/login')
    results=d.execute_async_script("""const done=arguments[0];Promise.all(
      ['/api/camera/status','/api/camera/feed','/api/hardware/status','/api/egg-records?limit=1'].map(
        u=>fetch(u).then(async r=>({status:r.status,body:await r.json()})))).then(done);""")
    assert all(r['status']==401 and r['body']['error']=='Authentication is required.' for r in results)
