"""Functional inventory from actual Alerts routes, scripts and active tray helper."""
import pytest
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException

case=pytest.mark.case
UNREAD=['Medium tray 2 completed','Extra Large tray 1 completed','Large tray 1 completed','Small tray 1 completed']
READ=['Jumbo tray 1 completed','Medium tray 1 completed']


def wait(d,p,timeout=12):return WebDriverWait(d,timeout,ignored_exceptions=(NoSuchElementException,StaleElementReferenceException)).until(p)
def el(d,s):return d.find_element(By.CSS_SELECTOR,s)
def titles(d):return d.execute_script("return Array.from(document.querySelectorAll('#alerts-list article h3'),e=>e.textContent.trim())")
def expect(d,rows):wait(d,lambda d:titles(d)==rows)
def note(request,text):request.node.actual=text
def visible(d,s):return el(d,s).is_displayed()
def badge(d):return el(d,'[data-eggsor-unread-badge]')
def count(d,n):wait(d,lambda d:el(d,'#unread-count').text==str(n))


def login(d,l,username=None):
    d.get(l['base']+'/login');el(d,'#identifier').send_keys(username or l['username']);el(d,'#password').send_keys(l['password'])
    el(d,'button[type="submit"]').click()
    for term in ('sign in','success'):
        wait(d,lambda d:term in el(d,'.swal2-popup').text.lower());wait(d,lambda d:visible(d,'.swal2-confirm'));el(d,'.swal2-confirm').click()
    wait(d,lambda d:'/dashboard' in d.current_url)


def open_alerts(d,l,username=None):
    login(d,l,username);d.get(l['base']+'/alerts');expect(d,UNREAD);count(d,4)


def control(d,l,command,**data):
    r=d.execute_async_script("""const [payload,secret,done]=arguments;fetch('/__alerts_control',{
      method:'POST',headers:{'Content-Type':'application/json','X-Lab-Secret':secret,
      'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify(payload)})
      .then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));""",{'command':command,**data},l['control'])
    assert r.get('status')==200,r
    return r['body']


def state(d,l):return d.execute_async_script("""const [secret,done]=arguments;fetch('/__alerts_state',{
      headers:{'X-Lab-Secret':secret}}).then(r=>r.json()).then(done);""",l['control'])


def api(d,path,method='GET',csrf=True):
    return d.execute_async_script("""const [path,method,csrf,done]=arguments;const x=new XMLHttpRequest();x.open(method,path);
      if(method==='POST' && csrf!==false)x.setRequestHeader('X-CSRF-Token',csrf===true?document.querySelector('meta[name="csrf-token"]').content:csrf);
      x.onload=()=>{try{done({status:x.status,body:JSON.parse(x.responseText)})}catch(e){done({status:x.status,text:x.responseText})}};x.send();""",path,method,csrf)


def refresh(d):d.execute_async_script("""const done=arguments[0];(async()=>{
      while(alertsRequestPending)await new Promise(r=>setTimeout(r,5));await fetchAlerts(true);done();})();""")


def tab(d,name,rows):el(d,'#filter-'+name).click();expect(d,rows);wait(d,lambda d:d.execute_script('return loadedFilter===activeFilter'))
def search(d,text):el(d,'#alert-search').clear();el(d,'#alert-search').send_keys(text)
def card(d,title):return next(e for e in d.find_elements(By.CSS_SELECTOR,'#alerts-list article') if e.find_element(By.TAG_NAME,'h3').text==title)
def modal(d,text):wait(d,lambda d:text.lower() in el(d,'.swal2-popup').text.lower());return el(d,'.swal2-popup').text
def dismiss(d):el(d,'.swal2-confirm').click();wait(d,lambda d:not any(e.is_displayed() for e in d.find_elements(By.CSS_SELECTOR,'.swal2-popup')))
def start_all(d,confirm=True):
    el(d,'#mark-all-read').click();modal(d,'Mark all unread alerts as read?');el(d,'.swal2-confirm' if confirm else '.swal2-cancel').click()
def mark_all(d):start_all(d);modal(d,'Success');assert 'All alerts marked as read.' in el(d,'.swal2-popup').text;dismiss(d)


@case('AL-01','Anonymous page access','Anonymous visitor redirects to Login.','Open /alerts without authentication.','No account cookie.')
def test_01_anonymous_page(alerts_browser,request):
    d,l=alerts_browser;d.get(l['base']+'/alerts');wait(d,lambda d:'/login' in d.current_url);note(request,'Protected Alerts page redirected to Login.')


@case('AL-02','Anonymous retrieval API','GET /api/alerts returns401 JSON.','Use browser XHR while anonymous.','No authenticated session.')
def test_02_anonymous_get(alerts_browser,request):
    d,l=alerts_browser;d.get(l['base']+'/login');r=api(d,'/api/alerts');assert r=={'status':401,'body':{'error':'Authentication is required.'}};note(request,'Anonymous GET returned401 Authentication is required JSON.')


@case('AL-03','Anonymous acknowledgements','Both state-changing endpoints reject anonymous requests with401 and no mutation.','POST mark-one and read-all through browser XHR.','No session or CSRF token.')
def test_03_anonymous_posts(alerts_browser,request):
    d,l=alerts_browser;d.get(l['base']+'/login')
    for path in ['/api/alerts/size:1/read','/api/alerts/read-all']:assert api(d,path,'POST',False)['status']==401
    assert state(d,l)['unread']==4;note(request,'Anonymous mark-one and read-all both returned401; unread DB count stayed4.')


@case('AL-04','Admin sidebar navigation','Admin opens Notification Center using Alerts navigation.','Authenticate and click sidebar Alerts.','Six synthetic size alerts; four unread.')
def test_04_admin_nav(alerts_browser,request):
    d,l=alerts_browser;login(d,l);el(d,'a[href="/alerts"]').click();expect(d,UNREAD)
    assert 'Notification Center' in el(d,'main h2').text;note(request,'Admin sidebar opened Notification Center with the four expected unread cards.')


@case('AL-05','Staff access and acknowledgement','Staff can read and acknowledge alerts under login_required policy.','Sign in as staff and acknowledge the first unread card.','alertstaff; shared global unread count4.')
def test_05_staff(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l,'alertstaff');el(d,'#alerts-list article button').click();count(d,3)
    assert state(d,l)['unread']==3 and not d.find_elements(By.CSS_SELECTOR,'a[href="/user-management"]')
    note(request,'Staff read and acknowledged Medium tray2; global unread count became3. Admin-only navigation absent.')


@case('AL-06','Default controls and ordering','Unread selected; cards ordered descending alert ID, not timestamp.','Inspect controls, active-tab style, cards and API ordering.','Unread IDs6/4/3/1; ID6 has oldest timestamp.')
def test_06_layout_order(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l)
    for id in ['mark-all-read','alert-search','filter-unread','filter-read']:assert visible(d,'#'+id)
    assert 'bg-inverse-surface' in el(d,'#filter-unread').get_attribute('class')
    rows=api(d,'/api/alerts?filter=unread')['body']['alerts'];assert [r['id'] for r in rows]==['size:6','size:4','size:3','size:1']
    note(request,'Default Unread controls rendered; descending IDs6/4/3/1 displayed despite ID6 being oldest by timestamp.')


@case('AL-07','Per-size notification fields and relationships','Stored IDs, sizes, milestones, boundary eggs and displayed text agree.','Inspect all API alerts and the Medium milestone2 card.','All five active sizes; Medium milestone2 has eligible total60 and tray capacity30.')
def test_07_fields(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);s=state(d,l);rows=api(d,'/api/alerts')['body']['alerts']
    assert len(rows)==6 and {r['egg_size'] for r in rows}=={'Small','Medium','Large','Extra Large','Jumbo'}
    r=rows[0];assert (r['id'],r['tray_number'],r['egg_count'],r['session_ref'])==('size:6',2,60,None)
    c=card(d,'Medium tray 2 completed').text;assert 'Medium tray reached 30 eggs at size-specific milestone 2.' in c and 'Medium size tray' in c
    assert s['eggs']==s['claims']==35 and len({b['egg_id'] for b in s['boundaries']})==6
    note(request,'Five sizes and six alerts serialized correctly; Medium milestone2 shows per-tray30 with eligible total60;35 real-helper synthetic egg claims and six unique boundary links.')


@case('AL-08','Read tab','Read tab shows only stored read alerts with READ labels and no acknowledgement buttons.','Switch to Read and inspect cards.','Read IDs5 Jumbo1 and2 Medium1; unread count remains4.')
def test_08_read_tab(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);tab(d,'read',READ);count(d,4)
    assert not d.find_elements(By.CSS_SELECTOR,'#alerts-list article button')
    assert all('READ' in c.text and 'UNREAD' not in c.text for c in d.find_elements(By.CSS_SELECTOR,'#alerts-list article'))
    note(request,'Read showed Jumbo1 and Medium1 with READ labels and no Mark buttons; count stayed4.')


@case('AL-09','Case-insensitive trimmed title search','Typing filters the selected tab immediately and trims whitespace.','Search mixed-case Medium tray2 with surrounding spaces.','Search term: two spaces + mEDium TRAY2 text + two spaces.')
def test_09_title_search(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);search(d,'  mEDium tray 2  ');expect(d,[UNREAD[0]]);count(d,4)
    note(request,'Trimmed mixed-case title search returned Medium tray2 only; count remained global4.')


@case('AL-10','Message search','Search includes message content.','Search size-specific milestone2 text.','Message substring milestone2 rather than a size name.')
def test_10_message_search(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);search(d,'milestone 2');expect(d,[UNREAD[0]])
    note(request,'Message substring milestone2 returned only Medium tray2.')


@case('AL-11','Search stays within selected tab','Same search only matches records within the active Read/Unread tab.','Search Medium, switch Read then Unread.','Medium1 read, Medium2 unread.')
def test_11_tab_search(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);search(d,'Medium');expect(d,[UNREAD[0]]);tab(d,'read',['Medium tray 1 completed']);tab(d,'unread',[UNREAD[0]])
    note(request,'Medium search returned tray2 in Unread and tray1 in Read, preserving the search term across tabs.')


@case('AL-12','No matches and clearing search','No-match state displays and clearing restores current-tab cards.','Search nonexistent text then clear and dispatch input.','NO_SUCH_ALERT; four underlying unread alerts.')
def test_12_no_match(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);search(d,'NO_SUCH_ALERT');expect(d,[])
    assert el(d,'#alerts-empty-title').text=='No matching alerts found.'
    el(d,'#alert-search').clear();d.execute_script("document.getElementById('alert-search').dispatchEvent(new Event('input',{bubbles:true}))");expect(d,UNREAD)
    note(request,'No matching alerts found displayed; clearing input restored all four unread cards.')


@case('AL-13','Mark One as Read','Only selected alert changes, disappears from Unread and appears in Read; no alerts fabricated.','Acknowledge Medium tray2, inspect DB and Read tab.','ID6 unread; IDs2/5 already read; six total alerts.')
def test_13_mark_one(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);card(d,UNREAD[0]).find_element(By.TAG_NAME,'button').click();expect(d,UNREAD[1:]);count(d,3)
    s=state(d,l);assert [r['id'] for r in s['alerts'] if r['is_read']]==['size:2','size:5','size:6'] and s['count']==6
    tab(d,'read',['Medium tray 2 completed',*READ]);note(request,'Only ID6 transitioned; read IDs2/5/6, unread3, total6 unchanged; selected card moved to Read.')


@case('AL-14','Read-state persistence','Acknowledgement persists in DB across reload; default tab returns to Unread.','Mark Small tray1, reload, then inspect Read.','ID1 becomes read; six stored alerts.')
def test_14_persistence(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);card(d,'Small tray 1 completed').find_element(By.TAG_NAME,'button').click();count(d,3);d.refresh();expect(d,UNREAD[:-1]);count(d,3)
    tab(d,'read',[*READ,'Small tray 1 completed']);assert state(d,l)['count']==6
    note(request,'ID1 read state survived reload; default Unread excluded it, Read included it, total stayed6.')


@case('AL-15','Repeated mark-one requests','Acknowledging an already-read alert is idempotent.','POST size:1/read twice and inspect counts.','Both requests target the same positive size ID.')
def test_15_repeat_one(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l)
    for _ in range(2):
        r=api(d,'/api/alerts/size:1/read','POST');assert r['status']==200 and r['body']['ok'] is True and r['body']['unread_count']==3
    s=state(d,l);assert s['count']==6 and s['unread']==3;note(request,'Repeated acknowledgement returned200/ok with unread3 both times; total6 unchanged.')


@case('AL-16','Cancel Mark All','Cancel leaves all stored states and counts unchanged.','Open Mark All confirmation and Cancel.','Four unread, two already read.')
def test_16_cancel_all(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);start_all(d,False);wait(d,lambda d:el(d,'#mark-all-read').is_enabled())
    assert state(d,l)['unread']==4;expect(d,UNREAD);note(request,'Cancel reenabled button, retained four unread and six total alerts.')


@case('AL-17','Confirm Mark All','All unread stored alerts become read, previous read stay read, badge hides.','Confirm Mark All and inspect DB, Unread empty state and Read tab.','Six alerts, four eligible unread.')
def test_17_all(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);mark_all(d);expect(d,[]);count(d,0)
    s=state(d,l);assert s['count']==6 and all(r['is_read'] for r in s['alerts']);assert not badge(d).is_displayed()
    assert el(d,'#alerts-empty-title').text=="You're all caught up!"
    tab(d,'read',['Medium tray 2 completed','Jumbo tray 1 completed','Extra Large tray 1 completed','Large tray 1 completed','Medium tray 1 completed','Small tray 1 completed'])
    note(request,'Mark All showed confirmed success, read all six, left total6, hid zero badge and moved cards to Read.')


@case('AL-18','Mark All ignores local search scope','Read-all endpoint acknowledges all global unread alerts, not only searched cards.','Search Small then confirm Mark All.','One visible search match, four total unread.')
def test_18_all_scope(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);search(d,'Small');expect(d,['Small tray 1 completed']);mark_all(d)
    assert state(d,l)['unread']==0 and state(d,l)['count']==6;note(request,'Mark All with only Small visible acknowledged all four global unread alerts; total6 unchanged.')


@case('AL-19','Repeated read-all','Repeated read-all remains successful with no new records or count drift.','Call read-all twice through authenticated browser XHR.','Second request has zero eligible unread rows.')
def test_19_repeat_all(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l)
    for _ in range(2):assert api(d,'/api/alerts/read-all','POST')=={'status':200,'body':{'ok':True,'unread_count':0}}
    s=state(d,l);assert s['count']==6 and s['unread']==0;note(request,'Two read-all requests returned200/ok/count0; all six rows retained.')


@case('AL-20','Unread badge accuracy and accessibility','Badge matches stored unread count with exact accessible label.','Inspect badge before and after mark-one.','Initial4, then3; current tab may show search subset.')
def test_20_badge(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);assert badge(d).text=='4' and badge(d).get_attribute('aria-label')=='4 unread alerts'
    el(d,'#alerts-list article button').click();count(d,3);wait(d,lambda d:badge(d).text=='3');assert badge(d).get_attribute('title')=='3 unread alerts'
    note(request,'Badge and accessible title changed from4 to3 in agreement with persisted unread state.')


@case('AL-21','No-alert empty states and hidden zero badge','Successful zero dataset shows caught-up Unread, No read alerts in Read, hidden badge.','Clear private alerts/eggs/progress then refresh and switch tabs.','Zero alerts; no hardware events.')
def test_21_empty(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'clear');refresh(d);expect(d,[]);count(d,0)
    assert el(d,'#alerts-empty-title').text=="You're all caught up!" and not badge(d).is_displayed()
    assert not visible(d,'#alerts-empty-dashboard-link');tab(d,'read',[])
    wait(d,lambda d:el(d,'#alerts-empty-title').text=='No read alerts yet.')
    note(request,'Zero dataset showed caught-up Unread and No read alerts yet in Read; badge and empty-state dashboard link hidden.')


@case('AL-22','Badge greater than99','Count above99 displays99+ but accessible count stays exact.','Seed101 valid synthetic milestone alerts and wait for refresh.','101 unread Small milestones with valid boundary links.')
def test_22_99plus(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'bulk',count=101);count(d,101)
    assert badge(d).text=='99+' and badge(d).get_attribute('aria-label')=='101 unread alerts'
    note(request,'101 stored unread alerts yielded badge99+ and accessible label101 unread alerts.')


@case('AL-23','Automatic Alerts refresh','New actual-helper synthetic milestone appears without forced refresh and badge increments.','Persist30 additional verified Small eggs through test control.','Existing Small opening29/count1;30 more eggs create milestone2 and ID7.')
def test_23_live(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'good',size='Small',count=30)
    expect(d,['Small tray 2 completed',*UNREAD]);count(d,5);assert badge(d).text=='5'
    note(request,'Normal one-second polling added Small tray2 and updated count/badge to5; no forced refresh or physical event.')


@case('AL-24','Shared badge polling outside Alerts','Shared sidebar badge independently refreshes on a non-Alerts page.','Stay on authentication landing page, create milestone and wait for actual15-second poll.','Dashboard used only as shared-badge host; unread4 becomes5.')
def test_24_other_page_badge(alerts_browser,request):
    d,l=alerts_browser;login(d,l);wait(d,lambda d:badge(d).text=='4')
    control(d,l,'good',size='Small',count=30);wait(d,lambda d:badge(d).text=='5',timeout=22)
    note(request,'Shared badge on auth landing page changed4 to5 through its normal15-second poll; no Dashboard feature suite executed.')


@case('AL-25','Inactive account loses access','Disabled user receives401 and subsequent protected page redirects.','Disable disposable admin, request Alerts API and reload.','Previously authenticated account becomes inactive.')
def test_25_disabled(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'disable');assert api(d,'/api/alerts')['status']==401
    d.get(l['base']+'/alerts');wait(d,lambda d:'/login' in d.current_url);note(request,'Inactive user received401 and protected Alerts page redirected to Login.')


@case('AL-26','Initial load failure and Retry','Initial failure shows explicit error without misleading empty state; Retry recovers.','Inject DB failure before opening Alerts, then restore and click Retry.','Real SQLAlchemy error handler on disposable DB request path.')
def test_26_initial_error(alerts_browser,request):
    d,l=alerts_browser;login(d,l);control(d,l,'mode',target='get',value='database_error');d.get(l['base']+'/alerts')
    wait(d,lambda d:visible(d,'#alerts-error'));assert el(d,'#alerts-error-message').text=='Could not load alerts. Check your connection and retry.'
    assert not visible(d,'#alerts-empty');control(d,l,'mode',target='get',value='normal');el(d,'#alerts-retry').click();expect(d,UNREAD)
    assert not visible(d,'#alerts-error');note(request,'Initial503 displayed initial-load error and Retry, not an empty success; normal Retry recovered four cards.')


@case('AL-27','Refresh failure retains data and count','Failed refresh retains same-tab cards and confirmed unread badge; Retry recovers.','Inject GET503 after initial load, then restore.','Four loaded unread alerts, stored count4.')
def test_27_refresh_error(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'mode',target='get',value='database_error');refresh(d)
    assert visible(d,'#alerts-error') and 'previously loaded alerts are shown' in el(d,'#alerts-error-message').text
    expect(d,UNREAD);count(d,4);assert badge(d).text=='4'
    control(d,l,'mode',target='get',value='normal');el(d,'#alerts-retry').click();wait(d,lambda d:not visible(d,'#alerts-error'))
    note(request,'Refresh503 retained four cards/count/badge with stale-data warning; Retry cleared warning after recovery.')


@case('AL-28','Failed tab switch hides wrong-tab data','Switch failure hides old-tab cards and shows filter-specific Retry feedback.','Load Unread, fail requests, click Read, then restore and Retry.','Unread4 previously loaded, Read2 stored.')
def test_28_tab_error(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'mode',target='get',value='json_error');el(d,'#filter-read').click()
    wait(d,lambda d:'Older results are hidden' in el(d,'#alerts-error-message').text);expect(d,[])
    assert not visible(d,'#alerts-empty');control(d,l,'mode',target='get',value='normal');el(d,'#alerts-retry').click();expect(d,READ)
    note(request,'Read-tab503 hid old Unread cards and false empty state; Retry loaded only the two Read cards.')


@case('AL-29','Network failure recovery','Rejected fetch retains loaded data with error and recovers on Retry.','Reject Alerts GET promises in browser, then restore transport.','Synthetic network rejection only.')
def test_29_network(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l)
    d.execute_script("window.alFetch=window.fetch;window.fetch=(u,o)=>String(u).startsWith('/api/alerts?')?Promise.reject(new Error('Simulated Alerts network loss')):window.alFetch(u,o)")
    try:refresh(d);assert visible(d,'#alerts-error');expect(d,UNREAD);assert badge(d).text=='4'
    finally:d.execute_script('window.fetch=window.alFetch')
    el(d,'#alerts-retry').click();wait(d,lambda d:not visible(d,'#alerts-error'));note(request,'Simulated network rejection retained cards/count and showed error; restored transport plus Retry recovered.')


@case('AL-30','Mark-one API error','Failed acknowledgement displays readable error and retains DB/read state.','Return JSON503 for POST then click Mark as Read.','Injected acknowledgement failure; GET remains normal.')
def test_30_one_error(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'mode',target='post',value='json_error');el(d,'#alerts-list article button').click()
    assert 'Simulated acknowledgement failure.' in modal(d,'Operation failed');assert state(d,l)['unread']==4
    dismiss(d);expect(d,UNREAD);note(request,'Mark-one503 showed readable Operation failed; four unread and six total records preserved.')


@case('AL-31','Read-all API error','Failed read-all never shows success and re-enables control.','Confirm Mark All with injected POST503.','No database changes; normal GET polling.')
def test_31_all_error(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'mode',target='post',value='json_error');start_all(d)
    assert 'Simulated acknowledgement failure.' in modal(d,'Operation failed');assert state(d,l)['unread']==4
    dismiss(d);wait(d,lambda d:el(d,'#mark-all-read').is_enabled());note(request,'Read-all503 showed failure rather than success; unread4 persisted and button reenabled.')


@case('AL-32','CSRF protection','Missing or invalid CSRF token rejects both acknowledgements with400 and no mutation.','Use native XHR to bypass automatic valid-header injection.','Authenticated account; missing token then wrong ASCII token.')
def test_32_csrf(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l)
    for path in ['/api/alerts/size:1/read','/api/alerts/read-all']:
        for token in [False,'wrong-token']:
            r=api(d,path,'POST',token);assert r['status']==400 and 'security token' in r['body']['error']
    assert state(d,l)['unread']==4;note(request,'Four invalid-CSRF requests returned400 JSON; all stored states unchanged.')


@case('AL-33','Invalid and missing alert IDs','Invalid formats return400; absent positive size ID returns404 without mutation.','POST legacy numeric, nonnumeric, zero, negative, then nonexistent size ID.','IDs1,size:nope,size:0,size:-1,size:9999.')
def test_33_bad_ids(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l)
    for id in ['1','size:nope','size:0','size:-1']:assert api(d,f'/api/alerts/{id}/read','POST')['status']==400
    r=api(d,'/api/alerts/size:9999/read','POST');assert r=={'status':404,'body':{'error':'Alert not found.'}}
    assert state(d,l)['unread']==4;note(request,'Four invalid IDs returned400; nonexistent size:9999 returned404; DB unread4 preserved.')


@case('AL-34','Duplicate mark-one click guard','Disabled initiating button does not issue a second request; DB transition occurs once.','Hold POST promise, dispatch duplicate click on same disabled button, release real request.','One pending acknowledgement for ID6.')
def test_34_duplicate_one(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l)
    d.execute_script("""window.alOriginal=window.fetch;window.alRelease=null;window.alCalls=0;
      window.fetch=(u,o)=>String(u).includes('/read')&&o?.method==='POST'?
      (window.alCalls++,new Promise(resolve=>window.alRelease=()=>alOriginal(u,o).then(resolve))):alOriginal(u,o);
      window.alButton=document.querySelector('#alerts-list article button');window.alButton.click();window.alButton.click();""")
    assert d.execute_script('return window.alButton.disabled && window.alCalls===1')
    d.execute_script('window.fetch=window.alOriginal;window.alRelease()');count(d,3)
    assert state(d,l)['count']==6;note(request,'Same-button duplicate click issued one POST while disabled; release persisted one transition and kept six alerts.')


@case('AL-35','Duplicate Mark All while confirming','Mark All disables during confirmation and duplicate click does not create extra operations.','Open confirmation, dispatch duplicate click, then cancel.','Four unread alerts; no POST should reach read-all endpoint.')
def test_35_duplicate_all(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);el(d,'#mark-all-read').click();modal(d,'Mark all unread alerts as read?')
    assert not el(d,'#mark-all-read').is_enabled();d.execute_script("document.getElementById('mark-all-read').click()")
    el(d,'.swal2-cancel').click();wait(d,lambda d:el(d,'#mark-all-read').is_enabled())
    s=state(d,l);assert not any(c['path']=='/api/alerts/read-all' for c in s['calls']);assert s['unread']==4
    note(request,'Duplicate click while confirmation open sent no POST; cancel restored control and unread4.')


@case('AL-36','Malformed JSON retrieval','Invalid JSON body shows refresh error and retains prior cards/count.','Inject malformed JSON200 on GET after initial load.','Body {broken, HTTP200; valid DB unchanged.')
def test_36_invalid_json(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'mode',target='get',value='malformed_json');refresh(d)
    assert visible(d,'#alerts-error');expect(d,UNREAD);assert badge(d).text=='4'
    note(request,'Malformed JSON parse error displayed refresh failure while preserving four cards and confirmed count4.')


@case('AL-37','Missing required response fields','Malformed success payload must not falsely clear loaded alerts as caught-up.','Return empty JSON object for GET and inspect feedback.','HTTP200 body{}; DB still four unread.')
def test_37_missing_fields(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'mode',target='get',value='missing_fields');refresh(d)
    assert state(d,l)['unread']==4
    assert titles(d)==UNREAD and visible(d,'#alerts-error'), 'Missing alerts field was accepted as empty success; stored unread alerts must remain visible with error feedback'
    note(request,'Malformed missing-fields response retained prior data with explicit error.')


@case('AL-38','Null unread count in unexpected response','Invalid null count must not falsely hide a confirmed nonzero badge.','Return valid unread rows with null count and inspect badge.','Four unread DB rows; HTTP200 unread_count:null.')
def test_38_null_count(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'mode',target='get',value='null_count');refresh(d);expect(d,UNREAD)
    assert state(d,l)['unread']==4
    assert el(d,'#unread-count').text=='4' and badge(d).is_displayed(), 'Null was coerced to zero despite four stored/rendered unread alerts'
    note(request,'Invalid null count retained prior confirmed badge4.')


@case('AL-39','Unexpected acknowledgement response','Non-JSON success response must not produce misleading read-all success.','Inject HTML200 for POST, confirm Mark All and inspect modal plus DB.','No mutation performed by fault response; four unread remain.')
def test_39_html_success(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'mode',target='post',value='html_success');start_all(d)
    wait(d,lambda d:any(term in el(d,'.swal2-popup').text for term in ['Success','Operation failed']))
    assert state(d,l)['unread']==4
    assert 'Operation failed' in el(d,'.swal2-popup').text, 'Unexpected HTML200 produced a success message while all four alerts remained unread'
    note(request,'Unexpected acknowledgement payload showed failure without DB mutation or misleading success.')


@case('AL-40','Mobile controls and sidebar','390px layout provides usable tabs/search, sidebar navigation and acknowledgement control.','Set exact CDP viewport, open sidebar, close overlay and use Read/Unread.','390x844 headless viewport; normal six-alert fixture.')
def test_40_mobile(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);d.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
    assert d.execute_script('return innerWidth')==390;el(d,'header button[onclick="toggleSidebar()"]').click()
    wait(d,lambda d:d.execute_script('return document.getElementById("sidebar").getBoundingClientRect().left>=-0.5'))
    assert visible(d,'a[href="/alerts"]')
    # Native WebDriver pointer action inside the headless viewport: the
    # overlay's center is covered by the open sidebar, its right edge is free.
    offset=d.execute_script('return innerWidth/2-12')
    ActionChains(d).move_to_element_with_offset(el(d,'#sidebar-overlay'),offset,0).click().perform()
    wait(d,lambda d:'-translate-x-full' in el(d,'#sidebar').get_attribute('class'))
    wait(d,lambda d:d.execute_script('return document.getElementById("sidebar").getBoundingClientRect().right<=0.5'))
    tab(d,'read',READ);tab(d,'unread',UNREAD);search(d,'Small');expect(d,['Small tray 1 completed'])
    def acknowledge(current):
        # Polling reconstructs cards every second. Reacquire a current element
        # if a normal refresh detaches the one Selenium was about to click.
        if el(current,'#unread-count').text=='3':return True
        try:card(current,'Small tray 1 completed').find_element(By.TAG_NAME,'button').click();return True
        except StopIteration:return False
    wait(d,acknowledge);count(d,3)
    note(request,'Exact390x844 viewport supported animated sidebar, tabs, search and one persisted acknowledgement.')


@case('AL-41','Opening balance and real tray helper','Opening29 plus one verified Good egg creates exactly one Small milestone alert; duplicate claim creates none.','Reset private fixture, add one eligible egg via existing helper, then replay same egg claim.','Small opening29; one Good/verified Small egg; synthetic DB event only.')
def test_41_real_creation(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'fresh');refresh(d);expect(d,[])
    s=control(d,l,'good',size='Small');assert s['count']==s['unread']==1 and s['claims']==1
    expect(d,['Small tray 1 completed']);count(d,1)
    p=next(p for p in s['progress'] if p['egg_size']=='Small');assert (p['opening_balance'],p['confirmed_good_eggs_since_opening'],p['completed_milestones'],p['eggs_toward_next_milestone'])==(29,1,1,0)
    after=control(d,l,'repeat_claim',egg_id=s['boundaries'][0]['egg_id']);assert after['count']==1 and after['claims']==1
    note(request,'Actual helper created one valid Small milestone from29 opening +1 Good egg; replay kept one alert and one boundary claim.')


@case('AL-42','Ineligible egg records do not create tray notifications','Not Classified, Unverified, Legacy/Unknown and non-Good records do not advance eligible tray alerts.','Add four controlled ineligible records through existing helper.','Small opening29; Not Classified false, Good false, Good null, Crack true.')
def test_42_ineligible(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'fresh')
    for quality,verified in [('Not Classified',False),('Good',False),('Good',None),('Crack',True)]:control(d,l,'good',size='Small',quality=quality,verified=verified)
    s=state(d,l);assert (s['eggs'],s['claims'],s['count'],s['unread'])==(4,0,0,0);refresh(d);expect(d,[])
    note(request,'Four ineligible synthetic records persisted, but helper created zero claims/alerts and no fabricated tray event.')


@case('AL-43','Independent per-size tray progress','One size reaching30 does not consume another size progress.','Fresh Small29/Medium28; add Medium, Small, then Medium through existing helper.','Three verified Good eggs, two size-specific milestones.')
def test_43_per_size(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'fresh')
    assert control(d,l,'good',size='Medium')['count']==0
    assert control(d,l,'good',size='Small')['count']==1
    s=control(d,l,'good',size='Medium');assert s['count']==2 and {a['egg_size'] for a in s['alerts']}=={'Small','Medium'}
    expect(d,['Medium tray 1 completed','Small tray 1 completed']);note(request,'Independent Small29+1 and Medium28+2 each created only their own milestone alert.')


@case('AL-44','200-row retrieval cap and global read-all','List caps at200 but unread count and read-all cover all205 matching stored rows.','Seed205 valid linked milestones, inspect capped retrieval, confirm Mark All.','205 synthetic unread alerts; no UI pagination implementation.')
def test_44_cap_all(alerts_browser,request):
    d,l=alerts_browser;open_alerts(d,l);control(d,l,'bulk',count=205);count(d,205)
    wait(d,lambda d:len(titles(d))==200);r=api(d,'/api/alerts?filter=unread')['body'];assert len(r['alerts'])==200 and r['unread_count']==205
    mark_all(d);count(d,0);s=state(d,l);assert s['count']==205 and all(a['is_read'] for a in s['alerts'])
    note(request,'API/UI returned200 cards with global unread205; Mark All acknowledged all205, kept total205 and hid zero badge.')
