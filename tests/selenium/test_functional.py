from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import NoAlertPresentException, StaleElementReferenceException

def wait(d, predicate): return WebDriverWait(d, 20, ignored_exceptions=(StaleElementReferenceException,)).until(predicate)
def element(d, selector): return wait(d, lambda x: x.find_element(By.CSS_SELECTOR, selector))
def text(d, selector): return element(d, selector).text
def popup(d, expected=None):
    def find(x):
        try:
            target=x.switch_to.alert
            return ("native", target) if not expected or expected.lower() in target.text.lower() else False
        except NoAlertPresentException:
            buttons=x.find_elements(By.CSS_SELECTOR, ".swal2-confirm")
            if buttons and buttons[0].is_displayed():
                message=x.find_element(By.CSS_SELECTOR,'.swal2-popup').text
                return ("swal", buttons[0]) if not expected or expected.lower() in message.lower() else False
            return False
    kind, target=wait(d, find)
    message=target.text if kind=="native" else text(d, ".swal2-popup")
    if expected: assert expected.lower() in message.lower(), message
    target.accept() if kind=="native" else target.click()

def open_login(d, lab):
    d.get(lab['base']+'/login')
    wait(d, lambda x: x.execute_script("return typeof window.EggSortAlerts === 'object'"))

def login(d, lab, user="dashadmin", remember=False):
    open_login(d, lab)
    element(d, '#identifier').send_keys(user); element(d, '#password').send_keys(lab['password'])
    if remember: element(d, '[name="remember-me"]').click()
    element(d, 'button[type="submit"]').click(); popup(d, 'sign in'); popup(d, 'success')
    wait(d, lambda x: '/dashboard' in x.current_url)
    loaded(d)

def loaded(d): wait(d, lambda x: x.find_element(By.ID,'dashboard-message').get_attribute('hidden') is not None)

def fetch(d, path, method="GET", data=None, secret=None):
    return d.execute_async_script("""const [url,method,data,secret,done]=arguments;
      const headers={}; if(secret) headers['X-Lab-Secret']=secret;
      const token=document.querySelector('meta[name="csrf-token"]')?.content;
      if(token) headers['X-CSRF-Token']=token;
      if(data) headers['Content-Type']='application/x-www-form-urlencoded';
      fetch(url,{method,headers,body:data?new URLSearchParams(data):undefined})
      .then(async r=>done({status:r.status,body:await r.text(),url:r.url}))
      .catch(e=>done({error:String(e)}));""", path,method,data,secret)

def state(d, lab, mode=None):
    import json
    r=fetch(d,'/__selenium_state'+('?mode='+mode if mode else ''),secret=lab['control'])
    assert r['status']==200,r
    return json.loads(r['body'])

def dates(d, start, end):
    d.execute_script("""for(const [id,value] of [['dashboard-start-date',arguments[0]],['dashboard-end-date',arguments[1]]]) {
      document.getElementById(id).value=value;
    } document.getElementById('dashboard-end-date').dispatchEvent(new Event('change',{bubbles:true}));""",start,end)
    wait(d, lambda x: x.execute_script('return !dashboardRequestPending && !dashboardRefreshQueued'))
    loaded(d)

def day(offset): return (datetime.now(ZoneInfo('Asia/Manila')).date()+timedelta(days=offset)).isoformat()

def reset_link(d,lab):
    d.get(lab['base']+'/forgot-password')
    element(d,'#email').send_keys('dashadmin@example.invalid');element(d,'button[type="submit"]').click()
    wait(d,lambda x: 'If an active account' in x.page_source)
    return state(d,lab)['reset_url']

def reset(d,url,new):
    d.get(url);element(d,'#password').send_keys(new);element(d,'#password_confirmation').send_keys(new)
    element(d,'button[type="submit"]').click();wait(d,lambda x:'/login' in x.current_url)

@pytest.mark.case('LG-01','Valid Login','Dashboard opens after valid password login')
def test_lg01(browser):
    d,l=browser;login(d,l);assert text(d,'#dashboard-total-sorted')=='10'

@pytest.mark.case('LG-02','Incorrect Password','Generic failure and no authenticated session')
def test_lg02(browser):
    d,l=browser;open_login(d,l);element(d,'#identifier').send_keys('dashadmin');element(d,'#password').send_keys('wrong')
    element(d,'button[type="submit"]').click();popup(d,'sign in');popup(d,'Invalid username/email or password')
    assert fetch(d,'/api/dashboard/stats')['status']==401

@pytest.mark.case('LG-03','Empty Fields','HTML required validation prevents login')
def test_lg03(browser):
    d,l=browser;open_login(d,l);element(d,'button[type="submit"]').click()
    assert d.execute_script("return document.querySelector('#identifier').validity.valueMissing && !document.querySelector('form').checkValidity()")
    assert '/login' in d.current_url

@pytest.mark.case('LG-04','Logout','Confirmed logout revokes protected access')
def test_lg04(browser):
    d,l=browser;login(d,l);element(d,'form[action="/logout"] button').click();popup(d,'Log out')
    wait(d,lambda x:'/login' in x.current_url);assert fetch(d,'/api/dashboard/stats')['status']==401

@pytest.mark.case('LG-05','Protected Pages','Anonymous page redirects and API returns 401')
def test_lg05(browser):
    d,l=browser;d.get(l['base']+'/dashboard');wait(d,lambda x:'/login' in x.current_url)
    assert fetch(d,'/api/dashboard/stats')['status']==401

@pytest.mark.case('LG-06','Keep Me Signed In','Checked sets persistent cookie; unchecked is session-only')
def test_lg06(browser):
    d,l=browser;login(d,l,remember=True)
    assert d.get_cookie('eggsort_dashboard_lab').get('expiry')
    d.delete_all_cookies();login(d,l)
    assert 'expiry' not in d.get_cookie('eggsort_dashboard_lab')

@pytest.mark.case('LG-07','Forgot Password','Local reset delivery changes password successfully')
def test_lg07(browser):
    d,l=browser;url=reset_link(d,l);assert url.startswith(l['base'])
    new=l['password']+'New';reset(d,url,new);l['password']=new;login(d,l)

@pytest.mark.case('LG-08','Disabled Account','Correct password on inactive user is rejected')
def test_lg08(browser):
    d,l=browser;open_login(d,l);element(d,'#identifier').send_keys('disabled');element(d,'#password').send_keys(l['password'])
    element(d,'button[type="submit"]').click();popup(d,'sign in');popup(d,'Invalid username/email or password')
    assert fetch(d,'/api/dashboard/stats')['status']==401

@pytest.mark.case('LG-09','Login via Email','Case-insensitive email login succeeds')
def test_lg09(browser):
    d,l=browser;login(d,l,user='DASHADMIN@EXAMPLE.INVALID')

@pytest.mark.case('LG-10','Login Throttling','Sixth request returns HTTP 429 after five failures')
def test_lg10(browser):
    d,l=browser;open_login(d,l)
    for i in range(5):
        r=fetch(d,'/login','POST',{'identifier':'dashadmin' if i%2 else 'dashadmin@example.invalid','password':'wrong'})
        assert r['status']==200,r
    assert fetch(d,'/login','POST',{'identifier':'dashadmin','password':l['password']})['status']==429

@pytest.mark.case('LG-11','Reset Link Reuse','Consumed token returns HTTP 400')
def test_lg11(browser):
    d,l=browser;url=reset_link(d,l);reset(d,url,l['password']+'New')
    assert fetch(d,url)['status']==400

@pytest.mark.case('DB-01','Dashboard Access','Authenticated admin sees actual Dashboard')
def test_db01(browser):
    d,l=browser;login(d,l);assert 'Operational Dashboard' in d.title

@pytest.mark.case('DB-02','Default Statistics','10 total, 60% quality, 4 non-Good and all sizes visible')
def test_db02(browser):
    d,l=browser;login(d,l)
    assert [text(d,'#'+i) for i in ['dashboard-total-sorted','dashboard-quality-rate','dashboard-non-good']]==['10','60%','4']
    distribution=text(d,'#dashboard-size-distribution')
    for size in ['Small','Medium','Large','Extra Large','Jumbo','Unmeasured/N/A']:assert size in distribution

@pytest.mark.case('DB-03','Daily Trend and Filter','Eight-day range shows 11 records with distinct dates')
def test_db03(browser):
    d,l=browser;login(d,l);dates(d,day(-7),day(0))
    wait(d,lambda x:text(x,'#dashboard-total-sorted')=='11')
    assert text(d,'#dashboard-quality-rate')=='63.6%'
    titles=d.execute_script("return [...document.querySelectorAll('#dashboard-trend-chart [title]')].map(e=>e.title)")
    assert f'{day(-7)}: 1 eggs' in titles and f'{day(-6)}: 2 eggs' in titles

@pytest.mark.case('DB-04','Empty Range','Zero totals, 0% quality and empty-range message')
def test_db04(browser):
    d,l=browser;login(d,l);dates(d,day(-2),day(-2))
    assert text(d,'#dashboard-total-sorted')=='0';assert text(d,'#dashboard-quality-rate')=='0%'
    assert 'No eggs sorted' in text(d,'#dashboard-trend-chart')

@pytest.mark.case('DB-05','Rejected Only','2 rejected, 0% quality, 2 Unmeasured/N/A')
def test_db05(browser):
    d,l=browser;login(d,l);dates(d,day(-1),day(-1))
    assert text(d,'#dashboard-total-sorted')=='2';assert text(d,'#dashboard-non-good')=='2'
    assert text(d,'#dashboard-quality-rate')=='0%'
    assert d.execute_script("return [...document.querySelectorAll('#dashboard-size-distribution strong')].at(-1).textContent")=='2'

@pytest.mark.case('DB-06','Cumulative Trays','Changing egg dates preserves tray counts and balances')
def test_db06(browser):
    d,l=browser;login(d,l);before=text(d,'#dashboard-tray-progress');dates(d,day(-2),day(-2))
    assert text(d,'#dashboard-trays')=='2';assert text(d,'#dashboard-tray-progress')==before
    assert 'not date-filtered' in text(d,'#dashboard-trays-note')

@pytest.mark.case('DB-07','Date Validation','Reversed dates visible; extreme/invalid API dates return 400')
def test_db07(browser):
    d,l=browser;login(d,l)
    d.execute_script("document.querySelector('#dashboard-start-date').value=arguments[0];document.querySelector('#dashboard-start-date').dispatchEvent(new Event('change'))",day(1))
    wait(d,lambda x:'From date cannot be after' in text(x,'#dashboard-message'))
    for date in ['9999-12-31','0001-01-01','2026-02-30']:
        assert fetch(d,'/api/dashboard/stats?date='+date)['status']==400

@pytest.mark.case('DB-08','Rapid Date Changes','Last selection wins during slow request')
def test_db08(browser):
    d,l=browser;login(d,l);state(d,l,'slow')
    d.execute_script("""document.querySelector('#dashboard-start-date').value=arguments[0];document.querySelector('#dashboard-end-date').value=arguments[0];
      document.querySelector('#dashboard-end-date').dispatchEvent(new Event('change'));""",day(-1))
    wait(d,lambda x:x.execute_script('return dashboardRequestPending'))
    d.execute_script("""document.querySelector('#dashboard-start-date').value=arguments[0];document.querySelector('#dashboard-end-date').value=arguments[0];
      document.querySelector('#dashboard-end-date').dispatchEvent(new Event('change'));""",day(-2))
    wait(d,lambda x:text(x,'#dashboard-total-sorted')=='0' and x.execute_script('return !dashboardRequestPending && !dashboardRefreshQueued'))
    assert element(d,'#dashboard-start-date').get_attribute('value')==day(-2)

@pytest.mark.case('DB-09','Failure and Recovery','Visible stale/initial warnings, HTTP 503, then recovery')
def test_db09(browser):
    d,l=browser;login(d,l);state(d,l,'fail')
    for width in [1440,360]:
        d.set_window_size(width,950)
        wait(d,lambda x:element(x,'#dashboard-message').is_displayed() and 'stale' in text(x,'#dashboard-message'))
    assert fetch(d,'/api/dashboard/stats')['status']==503
    d.refresh();wait(d,lambda x:'not confirmed totals' in text(x,'#dashboard-message'))
    state(d,l,'normal');loaded(d);assert text(d,'#dashboard-total-sorted')=='10'
    d.set_window_size(1440,1000)

@pytest.mark.case('DB-10','Staff/Admin Roles','Staff sees no audit panel; admin sees audit panel')
def test_db10(browser):
    d,l=browser;login(d,l,user='dashstaff')
    assert not d.find_elements(By.ID,'dashboard-audit-logs');assert 'Operations Portal / Admin' not in d.page_source
    assert fetch(d,'/api/dashboard/audit-logs/clear','POST')['status']==403
    d.delete_all_cookies();login(d,l);assert element(d,'#dashboard-audit-logs').is_displayed()

@pytest.mark.case('DB-11','Clear Logs Safety','Disabled button; HTTP 409; all 11 operational markers preserved')
def test_db11(browser):
    d,l=browser;login(d,l);assert not element(d,'#clear-audit-logs').is_enabled()
    before=state(d,l)['markers'];assert before==11
    assert fetch(d,'/api/dashboard/audit-logs/clear','POST')['status']==409
    assert state(d,l)['markers']==before
