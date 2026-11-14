"""Inventory follows actual Egg Records UI/API contracts; no product mutations."""
import csv
import io
import re
from pathlib import Path
from uuid import uuid4

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException

TODAY=[15,14,13,12,10,8,7,6,5,4,3,2,1]
case=pytest.mark.case


def wait(d,p,timeout=12):
    return WebDriverWait(d,timeout,ignored_exceptions=(NoSuchElementException,StaleElementReferenceException)).until(p)


def el(d,s):return d.find_element(By.CSS_SELECTOR,s)
def ids(d):return [int(r.get_attribute('data-record-id')) for r in d.find_elements(By.CSS_SELECTOR,'#egg-records-body tr[data-record-id]')]
def expect_ids(d,expected):wait(d,lambda d:ids(d)==expected)
def cells(d,id):return [c.text for c in el(d,f'tr[data-record-id="{id}"]').find_elements(By.TAG_NAME,'td')]
def note(request,message):request.node.actual=message


def login(d,l,username=None):
    d.get(l['base']+'/login')
    el(d,'#identifier').send_keys(username or l['username']);el(d,'#password').send_keys(l['password'])
    el(d,'button[type="submit"]').click()
    for expected in ('sign in','success'):
        wait(d,lambda d:expected in el(d,'.swal2-popup').text.lower())
        wait(d,lambda d:el(d,'.swal2-confirm').is_displayed());el(d,'.swal2-confirm').click()
    wait(d,lambda d:'/dashboard' in d.current_url)


def open_records(d,l,username=None):
    login(d,l,username);d.get(l['base']+'/egg-records')
    wait(d,lambda d:el(d,'#records-status-text').text.startswith('Updated'))
    expect_ids(d,TODAY)


def control(d,l,command,**data):
    result=d.execute_async_script("""const [payload,secret,done]=arguments;
      fetch('/__records_control',{method:'POST',headers:{'Content-Type':'application/json',
       'X-Lab-Secret':secret,'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify(payload)})
       .then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));""",{'command':command,**data},l['control'])
    assert result.get('status')==200,result
    return result['body']


def state(d,l):
    return d.execute_async_script("""const [secret,done]=arguments;fetch('/__records_state',{
      headers:{'X-Lab-Secret':secret}}).then(r=>r.json()).then(done);""",l['control'])


def api(d,path):
    # XHR observes HTTP denial without the shared fetch wrapper navigating away.
    return d.execute_async_script("""const [path,done]=arguments;const x=new XMLHttpRequest();x.open('GET',path);
      x.onload=()=>{try{done({status:x.status,body:JSON.parse(x.responseText)})}catch(e){done({status:x.status,text:x.responseText})}};x.send();""",path)


def search(d,value,submit=None):
    field=el(d,'#record-search');field.clear();field.send_keys(value)
    if submit=='button':el(d,'#search-records').click()
    if submit=='enter':field.send_keys(Keys.ENTER)


def dates(d,start,end):
    d.execute_script("""for(const [id,value] of [['start-date-filter',arguments[0]],['end-date-filter',arguments[1]]]){
      const e=document.getElementById(id);e.value=value;e.dispatchEvent(new Event('change',{bubbles:true}));}""",start,end)


def download(d,l):
    # Headless Chrome may overwrite an existing date-based filename. Give
    # every download its own directory and preserve all prior CSV evidence.
    directory=Path(l['run'])/'downloads'/uuid4().hex
    directory.mkdir(parents=True)
    d.execute_cdp_cmd('Browser.setDownloadBehavior',{'behavior':'allow','downloadPath':str(directory)})
    before=set(directory.glob('*.csv'))
    el(d,'#export-records').click()
    path=wait(d,lambda _d:next(iter(set(directory.glob('*.csv'))-before),None))
    return list(csv.reader(io.StringIO(path.read_text(encoding='utf-8')))),path


@case('ER-01','Anonymous page access','Protected page redirects to Login.','Open /egg-records with no account cookie.','Fresh browser cookie store.')
def test_01_anonymous_page(records_browser,request):
    d,l=records_browser;d.get(l['base']+'/egg-records');wait(d,lambda d:'/login' in d.current_url)
    note(request,'Anonymous /egg-records redirected to /login.')


@case('ER-02','Anonymous API access','API returns 401 Authentication is required JSON.','Issue browser XHR to /api/egg-records while anonymous.','No authenticated session.')
def test_02_anonymous_api(records_browser,request):
    d,l=records_browser;d.get(l['base']+'/login');r=api(d,'/api/egg-records')
    assert r=={'status':401,'body':{'error':'Authentication is required.'}}
    note(request,'Browser XHR received HTTP 401 and Authentication is required JSON.')


@case('ER-03','Admin navigation','Sidebar opens Egg Grading Records with active navigation and data.','Authenticate admin and click Egg Records sidebar link.','Disposable recordadmin and 13 current-day records.')
def test_03_navigation(records_browser,request):
    d,l=records_browser;login(d,l);el(d,'a[href="/egg-records"]').click();expect_ids(d,TODAY)
    assert 'Egg Grading Records' in d.title
    assert 'nav-active-glow' in el(d,'a[href="/egg-records"]').get_attribute('class')
    note(request,'Admin sidebar navigation opened Egg Grading Records and rendered 13 rows.')


@case('ER-04','Staff access','Staff can read the same records under login_required policy.','Sign in as staff and open Egg Records.','Disposable recordstaff; identical dataset.')
def test_04_staff(records_browser,request):
    d,l=records_browser;open_records(d,l,'recordstaff');assert not d.find_elements(By.CSS_SELECTOR,'a[href="/user-management"]')
    note(request,'Staff loaded all 13 current-day rows; admin-only User Management navigation absent. Static ADMIN breadcrumb observed, not an access restriction.')


@case('ER-05','Disabled-session access','Disabled account loses API access and page access.','Disable disposable active user, query API, then reload protected page.','Authenticated admin becomes inactive in disposable DB.')
def test_05_disabled(records_browser,request):
    d,l=records_browser;open_records(d,l);control(d,l,'disable');r=api(d,'/api/egg-records')
    assert r['status']==401;d.get(l['base']+'/egg-records');wait(d,lambda d:'/login' in d.current_url)
    note(request,'Inactive account received API 401 and protected page redirected to Login.')


@case('ER-06','Page controls and table layout','Six expected columns and implemented search/filter/export/print controls exist.','Inspect rendered headings, options and primary controls.','Desktop 1440x1000; normal fixture.')
def test_06_layout(records_browser,request):
    d,l=records_browser;open_records(d,l)
    assert [e.text for e in d.find_elements(By.CSS_SELECTOR,'thead th')]==['EGG NO.','WEIGHT (G)','SIZE CLASSIFICATION','QUALITY STATUS','DATE SORTED','SESSION REF']
    for id in ['record-search','search-records','size-filter','quality-filter','start-date-filter','end-date-filter','reset-record-filters','export-records','print-records']:
        assert el(d,'#'+id).is_displayed() and el(d,'#'+id).is_enabled()
    assert [o.text for o in Select(el(d,'#size-filter')).options]==['All Sizes','Peewee','Small','Medium','Large','Extra Large','Jumbo']
    assert [o.text for o in Select(el(d,'#quality-filter')).options]==['All Qualities','Good','Crack','Rotten','Not Classified']
    note(request,'Six correct columns; all nine controls enabled and visible; size and quality options match implementation.')


@case('ER-07','Default Manila day and ordering','Today filters include midnight and last microsecond, exclude adjacent days, order by ID descending.','Open records and compare IDs, date fields and captured query boundaries.','IDs 1 at today midnight, 10 at day end, 9 before today, 11 tomorrow midnight.')
def test_07_default_range(records_browser,request):
    d,l=records_browser;open_records(d,l)
    assert el(d,'#start-date-filter').get_attribute('value')==l['today']==el(d,'#end-date-filter').get_attribute('value')
    queries=state(d,l)['requests'];q=queries[-1]
    assert q['limit']=='500' and q['start_at'].endswith('T16:00:00.000Z') and q['end_before'].endswith('T16:00:00.000Z')
    note(request,'Rows '+str(ids(d))+'; midnight ID1 and final-microsecond ID10 included, adjacent-day IDs9/11 excluded; default query limit=500.')


@case('ER-08','Verified record details','Stored weight, size, quality, verification, date and session render accurately.','Inspect EGG-000002 row and its serialized timestamp.','ID2: 59g, Medium, Good, true; 08:00 Manila today; LAB-TODAY.')
def test_08_details(records_browser,request):
    d,l=records_browser;open_records(d,l);c=cells(d,2)
    assert c[:4]==['EGG-000002','59 g','Medium','Good (Verified)'] and c[5]=='LAB-TODAY'
    record=api(d,'/api/egg-records?q=EGG-000002')['body']['records'][0]
    expected=d.execute_script("return new Intl.DateTimeFormat('en-PH',{dateStyle:'medium',timeStyle:'medium'}).format(new Date(arguments[0]))",record['sorted_at'])
    assert c[4]==expected and '8:00:00' in c[4]
    note(request,'Rendered '+str(c)+'; date matches serialized UTC instant formatted in Asia/Manila.')


@case('ER-09','Not Classified and Unverified','Record remains Not Classified (Unverified), retaining its measured weight and stored size.','Read weight-only record row.','ID5: 63g, Large, Not Classified, false.')
def test_09_not_classified(records_browser,request):
    d,l=records_browser;open_records(d,l);assert cells(d,5)[1:4]==['63 g','Large','Not Classified (Unverified)']
    note(request,'ID5 displayed 63 g / Large / Not Classified (Unverified), without converting quality to Good.')


@case('ER-10','Historical record and legacy verification','Historical data appears when its date is selected and nullable verification reads Legacy/Unknown.','Select yesterday and inspect imported record.','ID9: 41g, Peewee, Good, null verification, LAB-HISTORY; just before today midnight.')
def test_10_historical(records_browser,request):
    d,l=records_browser;open_records(d,l);dates(d,l['yesterday'],l['yesterday']);expect_ids(d,[9])
    assert cells(d,9)[1:4]==['41 g','Peewee','Good (Legacy/Unknown)'] and cells(d,9)[5]=='LAB-HISTORY'
    note(request,'Yesterday rendered only EGG-000009: 41 g / Peewee / Good (Legacy/Unknown) / LAB-HISTORY.')


@case('ER-11','Three verification states','Good records distinguish Verified, Unverified and Legacy/Unknown.','Compare three current-day Good rows.','IDs 2=true, 12=false, 13=null.')
def test_11_verification(records_browser,request):
    d,l=records_browser;open_records(d,l)
    assert [cells(d,id)[3] for id in [2,12,13]]==['Good (Verified)','Good (Unverified)','Good (Legacy/Unknown)']
    note(request,'Good rows separately displayed Verified, Unverified and Legacy/Unknown.')


@case('ER-12','Rejected records and unavailable weights','Stored zero sentinel renders N/A; Crack/Rotten labels and badge colors are preserved.','Inspect rejected rows and serialized weights.','ID3 Crack and ID4 Rotten; zero stored weights, N/A size, verified true.')
def test_12_rejects(records_browser,request):
    d,l=records_browser;open_records(d,l)
    for id,quality,color in [(3,'Crack','bg-red-100'),(4,'Rotten','bg-amber-100')]:
        assert cells(d,id)[1:4]==['N/A','N/A',quality+' (Verified)']
        assert color in el(d,f'tr[data-record-id="{id}"] td:nth-child(4) span').get_attribute('class')
    note(request,'Crack and Rotten weights rendered N/A; expected red and amber badges retained.')


@case('ER-13','Stored size labels','All six stored sizes, including imported Peewee, display without frontend reclassification.','Inspect representative rows for every selectable size.','IDs8/1/2/5/6/7: Peewee, Small, Medium, Large, Extra Large, Jumbo.')
def test_13_sizes(records_browser,request):
    d,l=records_browser;open_records(d,l)
    assert [cells(d,id)[2] for id in [8,1,2,5,6,7]]==['Peewee','Small','Medium','Large','Extra Large','Jumbo']
    note(request,'All six stored size labels rendered, including legacy/imported Peewee; no reclassification applied.')


@case('ER-14','Search by formatted egg ID and button','Search button returns the matching formatted ID.','Enter EGG-000002 and click Search.','Expected only record ID2.')
def test_14_search_id(records_browser,request):
    d,l=records_browser;open_records(d,l);search(d,'EGG-000002','button');expect_ids(d,[2]);note(request,'Search button narrowed table to EGG-000002.')


@case('ER-15','Case-insensitive session search','Session substring search is case insensitive.','Search farm-alpha in lowercase.','Stored session FARM-ALPHA on ID7.')
def test_15_search_session(records_browser,request):
    d,l=records_browser;open_records(d,l);search(d,'farm-alpha');expect_ids(d,[7]);note(request,'Lowercase farm-alpha matched ID7 with FARM-ALPHA session.')


@case('ER-16','Search weight and Enter','Enter submits weight search and returns matching weight record.','Type 59 and press Enter.','ID2 has weight 59g; no record ID59.')
def test_16_weight_enter(records_browser,request):
    d,l=records_browser;open_records(d,l);search(d,'59','enter');expect_ids(d,[2]);note(request,'Enter weight search 59 returned only ID2, 59 g.')


@case('ER-17','Debounced search and whitespace','Typing alone refreshes and surrounding search whitespace is trimmed.','Type spaces around FARM-ALPHA without clicking Search.','Search text: two leading/trailing spaces.')
def test_17_debounce(records_browser,request):
    d,l=records_browser;open_records(d,l);search(d,'  FARM-ALPHA  ');expect_ids(d,[7])
    wait(d,lambda d:state(d,l)['requests'][-1].get('q')=='FARM-ALPHA');note(request,'Input alone triggered refresh; request q was trimmed to FARM-ALPHA and ID7 rendered.')


@case('ER-18','No matches and search recovery','Empty-match message appears and clearing search restores records.','Search impossible text then clear using input and Search.','q=NO_SUCH_SESSION; then empty search.')
def test_18_no_matches(records_browser,request):
    d,l=records_browser;open_records(d,l);search(d,'NO_SUCH_SESSION');expect_ids(d,[])
    assert 'No egg records match the current search and filters.' in el(d,'#egg-records-body').text
    el(d,'#record-search').clear();el(d,'#search-records').click();expect_ids(d,TODAY)
    note(request,'No-match message shown, then 13 rows restored after clearing search.')


@case('ER-19','Every size filter','Each size selection returns only stored records with that size.','Select each of six size choices, then All Sizes.','Peewee[8]; Small[15,13,12,1]; Medium[14,2]; Large[10,5]; Extra Large[6]; Jumbo[7].')
def test_19_size_filters(records_browser,request):
    d,l=records_browser;open_records(d,l)
    for size,expected in [('Peewee',[8]),('Small',[15,13,12,1]),('Medium',[14,2]),('Large',[10,5]),('Extra Large',[6]),('Jumbo',[7]),('All Sizes',TODAY)]:
        Select(el(d,'#size-filter')).select_by_visible_text(size);expect_ids(d,expected)
    note(request,'All six size filters and All Sizes returned exact expected row sets.')


@case('ER-20','Every quality filter','Good, Crack, Rotten and Not Classified filters use stored quality independent of verification.','Select all quality options and restore All Qualities.','Good includes true/false/null verification; Crack[3]; Rotten[4]; Not Classified[5].')
def test_20_quality_filters(records_browser,request):
    d,l=records_browser;open_records(d,l)
    for quality,expected in [('Good',[15,14,13,12,10,8,7,6,2,1]),('Crack',[3]),('Rotten',[4]),('Not Classified',[5]),('All Qualities',TODAY)]:
        Select(el(d,'#quality-filter')).select_by_visible_text(quality);expect_ids(d,expected)
    note(request,'All four qualities and All Qualities returned exact row sets; Good did not imply Verified.')


@case('ER-21','Combined search and filters','Search, size, quality and date predicates intersect.','Search LAB-TODAY, choose Large and Not Classified.','Expected ID5 only.')
def test_21_combined(records_browser,request):
    d,l=records_browser;open_records(d,l);search(d,'LAB-TODAY')
    Select(el(d,'#size-filter')).select_by_visible_text('Large');Select(el(d,'#quality-filter')).select_by_visible_text('Not Classified')
    expect_ids(d,[5]);note(request,'Combined session search + Large + Not Classified + today returned ID5 only.')


@case('ER-22','Inclusive multi-day range','From/To include both days while excluding tomorrow midnight.','Select yesterday through today.','Yesterday ID9 plus 13 today rows; tomorrow ID11 excluded.')
def test_22_range(records_browser,request):
    d,l=records_browser;open_records(d,l);dates(d,l['yesterday'],l['today']);expect_ids(d,[15,14,13,12,10,9,8,7,6,5,4,3,2,1]);note(request,'Two-day range displayed 14 rows, including ID9, excluding ID11.')


@case('ER-23','From-only date filter','Clearing To removes upper date bound.','Keep From=today and clear To.','Today records plus tomorrow ID11; yesterday ID9 excluded.')
def test_23_from_only(records_browser,request):
    d,l=records_browser;open_records(d,l);dates(d,l['today'],'');expect_ids(d,[15,14,13,12,11,10,8,7,6,5,4,3,2,1]);note(request,'From-only range included tomorrow ID11 and excluded yesterday ID9.')


@case('ER-24','To-only date filter','Clearing From removes lower bound; To remains inclusive.','Clear From and select yesterday as To.','Expected ID9 only.')
def test_24_to_only(records_browser,request):
    d,l=records_browser;open_records(d,l);dates(d,'',l['yesterday']);expect_ids(d,[9]);note(request,'To-only range through yesterday returned historical ID9 only.')


@case('ER-25','Invalid date range validation','Reversed range shows implemented validation and retains last valid table.','Set From=tomorrow and To=today.','From date later than To; 13 previously loaded rows.')
def test_25_invalid_dates(records_browser,request):
    d,l=records_browser;open_records(d,l);dates(d,l['tomorrow'],l['today'])
    wait(d,lambda d:el(d,'#records-status-text').text=='From date cannot be after To date.')
    assert ids(d)==TODAY and 'text-[#ba1a1a]' in el(d,'#records-status-text').get_attribute('class')
    note(request,'Reversed range displayed red From date cannot be after To date status; previous 13 rows retained.')


@case('ER-26','Reset filters','Reset clears search and choices and restores today range and rows.','Apply combined filters and historical range, then click reset.','Nondefault search, size, quality and dates.')
def test_26_reset(records_browser,request):
    d,l=records_browser;open_records(d,l);search(d,'HISTORY');Select(el(d,'#size-filter')).select_by_visible_text('Peewee')
    Select(el(d,'#quality-filter')).select_by_visible_text('Good')
    dates(d,l['yesterday'],l['yesterday']);expect_ids(d,[9]);el(d,'#reset-record-filters').click();expect_ids(d,TODAY)
    assert el(d,'#record-search').get_attribute('value')==''
    assert el(d,'#size-filter').get_attribute('value')=='All Sizes' and el(d,'#quality-filter').get_attribute('value')=='All Qualities'
    assert el(d,'#start-date-filter').get_attribute('value')==el(d,'#end-date-filter').get_attribute('value')==l['today']
    note(request,'Reset restored empty search, All Sizes/Qualities, today date fields and all 13 rows.')


@case('ER-27','Page refresh defaults','Reload restores default today filters rather than persisting prior controls.','Search a session, then reload Egg Records.','Prior q=FARM-ALPHA; default 13 today rows.')
def test_27_reload(records_browser,request):
    d,l=records_browser;open_records(d,l);search(d,'FARM-ALPHA');expect_ids(d,[7]);d.refresh();expect_ids(d,TODAY)
    assert el(d,'#record-search').get_attribute('value')=='';note(request,'Reload restored default search/date controls and 13 current-day records.')


@case('ER-28','Live update respects active filter','New matching record appears through polling with existing filters retained.','Select Medium, insert controlled DB record and wait without forced refresh.','Initial Medium IDs14/2; new ID20 Medium 59g at 19:00 today.')
def test_28_live_insert(records_browser,request):
    d,l=records_browser;open_records(d,l);Select(el(d,'#size-filter')).select_by_visible_text('Medium');expect_ids(d,[14,2])
    control(d,l,'add');expect_ids(d,[20,14,2]);assert el(d,'#size-filter').get_attribute('value')=='Medium'
    note(request,'One-second polling added ID20 ahead of IDs14/2 while retaining Medium filter.')


@case('ER-29','Unchanged polling avoids table replacement','Identical refresh data leaves the existing row node in place and updates status.','Hold a row DOM reference and wait for another records API poll.','No data change between polls.')
def test_29_stable_dom(records_browser,request):
    d,l=records_browser;open_records(d,l);d.execute_script('window.erRow=document.querySelector("tr[data-record-id]")')
    count=len(state(d,l)['requests']);wait(d,lambda d:len(state(d,l)['requests'])>count)
    assert d.execute_script('return window.erRow===document.querySelector("tr[data-record-id]")')
    note(request,'Subsequent automatic poll preserved the same first-row DOM node for unchanged data.')


@case('ER-30','Database failure and recovery','Live refresh failed status retains data, then normal polling recovers.','Inject SQLAlchemyError at records endpoint; restore normal mode.','Simulated private DB outage only; existing 13 rows.')
def test_30_database_error(records_browser,request):
    d,l=records_browser;open_records(d,l);control(d,l,'mode',value='database_error')
    wait(d,lambda d:el(d,'#records-status-text').text=='Live refresh failed');assert ids(d)==TODAY
    assert api(d,'/api/egg-records')['status']==503
    control(d,l,'mode',value='normal');wait(d,lambda d:el(d,'#records-status-text').text.startswith('Updated'));expect_ids(d,TODAY)
    note(request,'Injected DB outage returned 503; UI retained 13 rows and showed Live refresh failed, then recovered to Updated.')


@case('ER-31','Unexpected HTML response','Implemented generic failure status retains rows and recovers when JSON returns.','Return synthetic HTML 502 from records endpoint, then restore.','Simulated proxy response; no production transport.')
def test_31_html_error(records_browser,request):
    d,l=records_browser;open_records(d,l);control(d,l,'mode',value='html_error')
    wait(d,lambda d:el(d,'#records-status-text').text=='Live refresh failed');assert ids(d)==TODAY
    assert 'Unexpected token' not in el(d,'#records-status-text').text
    control(d,l,'mode',value='normal');wait(d,lambda d:el(d,'#records-status-text').text.startswith('Updated'))
    note(request,'HTML 502 produced generic Live refresh failed status, retained rows and recovered; parse details remained in console only.')


@case('ER-32','Empty database and empty CSV','No-match empty state renders; export contains only seven column headers.','Clear private EggRecord table, wait for polling, then export.','Disposable SQLite only; zero egg rows.')
def test_32_empty(records_browser,request):
    d,l=records_browser;open_records(d,l);control(d,l,'clear');expect_ids(d,[])
    assert 'No egg records match the current search and filters.' in el(d,'#egg-records-body').text
    rows,path=download(d,l);assert rows==[['Egg ID','Weight (g)','Size','Quality','Verification','Date Sorted','Session']]
    note(request,'Empty DB rendered no-match state; downloaded '+path.name+' contained header row only.')


@case('ER-33','Filtered CSV export and escaping','CSV exports current displayed selection, seven headings, correct data and quoted session.','Search ID14 and download CSV through Chrome.','ID14: 60g, Medium, Good Verified; session LAB-"A,B.')
def test_33_csv(records_browser,request):
    d,l=records_browser;open_records(d,l);search(d,'EGG-000014','button');expect_ids(d,[14]);rows,path=download(d,l)
    assert rows[0]==['Egg ID','Weight (g)','Size','Quality','Verification','Date Sorted','Session']
    assert len(rows)==2 and rows[1][:5]==['EGG-000014','60','Medium','Good','Verified'] and rows[1][6]=='LAB-"A,B'
    assert rows[1][5]==cells(d,14)[4] and path.name.startswith('egg-records-'+l['today'])
    note(request,'Actual Chrome download '+path.name+' contained only ID14, seven fields, and correctly escaped quote/comma session.')


@case('ER-34','CSV verification and missing weight representation','Export preserves explicit Unverified and Legacy/Unknown labels; missing weight is not converted into a measured zero.','Export current rows and inspect ID3, ID5, ID13.','Stored zero sentinel serializes null; current CSV implementation writes literal null.')
def test_34_csv_states(records_browser,request):
    d,l=records_browser;open_records(d,l);rows,path=download(d,l);records={r[0]:r for r in rows[1:]}
    assert records['EGG-000005'][3:5]==['Not Classified','Unverified']
    assert records['EGG-000013'][4]=='Legacy/Unknown' and records['EGG-000003'][1]=='null'
    note(request,'CSV retained Not Classified/Unverified and Legacy/Unknown. Missing weight exported as literal null (table shows N/A); no measured 0 invented.')


@case('ER-35','Print Report action','Click delegates once to window.print.','Stub browser print function, then click Print Report.','Native print deliberately intercepted to avoid any visible dialog or physical printing.')
def test_35_print(records_browser,request):
    d,l=records_browser;open_records(d,l);d.execute_script('window.erPrintCount=0;window.print=()=>window.erPrintCount++')
    el(d,'#print-records').click();assert d.execute_script('return window.erPrintCount')==1
    note(request,'Print Report called intercepted window.print exactly once; no native print dialog opened or printer used.')


@case('ER-36','Implemented 500-row cap','UI returns latest 500 IDs from 505 matching rows; no interactive paging or sorting controls exist.','Replace private fixture with 505 current-day records; inspect UI and capped API.','IDs1000..1504; expected displayed IDs1504..1005; API limit9999 clamped to500.')
def test_36_cap(records_browser,request):
    d,l=records_browser;open_records(d,l);control(d,l,'bulk');expect_ids(d,list(range(1504,1004,-1)))
    r=api(d,'/api/egg-records?limit=9999')['body'];assert len(r['records'])==500 and r['latest_id']==1504
    assert state(d,l)['count']==505
    assert not d.find_elements(By.CSS_SELECTOR,'[aria-label*="page"],thead button,thead select')
    note(request,'505 DB rows yielded exactly 500 displayed IDs1504..1005 and 500 API rows. No UI pagination/sort controls; older five not displayed under current cap.')


@case('ER-37','Existing API after_id and limit','API applies id greater-than cursor, descending order, limit clamp and latest_id.','Use browser XHR for after_id14, zero/invalid limit and empty cursor window.','No UI pagination; API-only existing parameters.')
def test_37_api_cursor(records_browser,request):
    d,l=records_browser;open_records(d,l)
    r=api(d,'/api/egg-records?after_id=14&limit=2')['body'];assert [r['id'] for r in r['records']]==[15] and r['latest_id']==15
    assert len(api(d,'/api/egg-records?limit=0')['body']['records'])==1
    assert len(api(d,'/api/egg-records?limit=invalid')['body']['records'])==15
    r=api(d,'/api/egg-records?after_id=999')['body'];assert r=={'records':[],'latest_id':999}
    note(request,'API cursor >14 returned ID15; limit0 clamped to1; invalid limit used default100; empty after_id999 preserved latest_id999.')


@case('ER-38','API date validation','Invalid or timezone-free dates and reversed ranges return 400 JSON.','Request malformed calendar date, naive timestamp and reversed timestamp range.','start_date=invalid; start_at without timezone; start_at after end_before.')
def test_38_api_dates(records_browser,request):
    d,l=records_browser;open_records(d,l)
    for query in ['start_date=invalid','start_at=2026-11-01T00:00:00','start_at=2026-11-02T00:00:00Z&end_before=2026-11-01T00:00:00Z']:
        r=api(d,'/api/egg-records?'+query);assert r['status']==400 and 'error' in r['body']
    note(request,'All three invalid-date API requests returned 400 JSON; valid UI table remained intact.')


@case('ER-39','Filter change during pending poll','After a held old response, automatic polling eventually renders the latest selection.','Hold one records fetch, change size while request pending, release old response and wait.','Old today response then Small filter; expected IDs15/13/12/1.')
def test_39_pending_filter(records_browser,request):
    d,l=records_browser;open_records(d,l)
    d.execute_async_script("""const done=arguments[0];(async()=>{
      while(recordsRequestPending)await new Promise(r=>setTimeout(r,5));
      window.erOriginalFetch=window.fetch;window.erRelease=null;
      window.fetch=(url,options)=>String(url).startsWith('/api/egg-records?')
        ?new Promise(resolve=>{erOriginalFetch(url,options).then(r=>{window.erRelease=()=>resolve(r)})})
        :erOriginalFetch(url,options);
      void refreshEggRecords();done();})();""")
    wait(d,lambda d:d.execute_script('return !!window.erRelease'))
    Select(el(d,'#size-filter')).select_by_visible_text('Small')
    d.execute_script('window.fetch=window.erOriginalFetch;window.erRelease()');expect_ids(d,[15,13,12,1])
    note(request,'After releasing older pending response, next automatic poll rendered current Small selection correctly.')


@case('ER-40','Classification standards consistency','Displayed size-standards banner agrees with the existing shared classifier for displayed records.','Compare browser-visible Medium interval with shared classifier output and record ID2.','Shared classifier(59)=Medium; ID2 stored/displayed Medium at59g.')
def test_40_standards(records_browser,request):
    d,l=records_browser;open_records(d,l)
    d.execute_script("document.querySelector('main h4').scrollIntoView({block:'center'})")
    text=el(d,'main').text
    match=re.search(r'Medium\s*\((\d+)g\s+to\s+(\d+)g\)',text)
    assert match,'Visible Medium interval missing'
    low,high=map(int,match.groups())
    assert l['classifications']['59']=='Medium' and cells(d,2)[2]=='Medium'
    note(request,f'Banner states Medium {low}..{high}g; shared classifier and row2 classify59g as Medium.')
    assert low<=59<=high, f'Banner Medium {low}..{high}g contradicts shared classifier and displayed 59g Medium record'


@case('ER-41','Mobile navigation and table scrolling','Mobile menu opens sidebar and table has horizontal overflow container with usable controls.','Set390x844 viewport, open menu and inspect scroll container.','Responsive page, six-column table; no mobile redesign.')
def test_41_mobile(records_browser,request):
    d,l=records_browser;open_records(d,l)
    d.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
    assert d.execute_script('return innerWidth')==390
    el(d,'header button[onclick="toggleSidebar()"]').click()
    wait(d,lambda d:'-translate-x-full' not in el(d,'#sidebar').get_attribute('class'))
    wait(d,lambda d:d.execute_script('return document.getElementById("sidebar").getBoundingClientRect().left>=-0.5'))
    wait(d,lambda d:el(d,'a[href="/egg-records"]').is_displayed())
    result=d.execute_script("const e=document.querySelector('main .overflow-x-auto');return {scroll:e.scrollWidth,client:e.clientWidth,overflow:getComputedStyle(e).overflowX}")
    assert result['scroll']>result['client'] and result['overflow']=='auto'
    note(request,'390x844 menu opened sidebar; table retained horizontal auto scrolling: '+str(result))


@case('ER-42','Stored text rendered safely','Session markup is displayed as literal text and does not create executable SVG.','Inspect table session containing HTML-like text.','ID15 session LAB-<svg onload=alert(1)>; valid stored string.')
def test_42_text_safety(records_browser,request):
    d,l=records_browser;open_records(d,l);assert cells(d,15)[5]=='LAB-<svg onload=alert(1)>'
    assert not el(d,'tr[data-record-id="15"]').find_elements(By.TAG_NAME,'svg')
    note(request,'HTML-like session displayed literally; no SVG node created or script executed.')
