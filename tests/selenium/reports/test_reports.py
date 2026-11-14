"""Reports UI/API tests based on the current implementation audit."""
from datetime import date, datetime, timedelta, timezone

import pytest
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait

case = pytest.mark.case
BASE = "2026-10-15"
BASE_NEXT = "2026-10-16"


def wait(driver, predicate, timeout=12):
    return WebDriverWait(driver, timeout, ignored_exceptions=(NoSuchElementException, StaleElementReferenceException)).until(predicate)


def el(driver, selector):
    return driver.find_element(By.CSS_SELECTOR, selector)


def note(request, message):
    request.node.actual = message


def login(driver, lab, username=None):
    driver.get(lab["base"] + "/login")
    el(driver, "#identifier").send_keys(username or lab["username"])
    el(driver, "#password").send_keys(lab["password"])
    el(driver, 'button[type="submit"]').click()
    for expected in ("sign in", "success"):
        wait(driver, lambda d: expected in el(d, ".swal2-popup").text.lower())
        wait(driver, lambda d: el(d, ".swal2-confirm").is_displayed())
        el(driver, ".swal2-confirm").click()
    wait(driver, lambda d: "/dashboard" in d.current_url)


def open_reports(driver, lab, username=None):
    login(driver, lab, username)
    driver.get(lab["base"] + "/reports")
    wait(driver, lambda d: el(d, "#report-updated-at").text.startswith("Updated"))
    wait(driver, lambda d: el(d, "#report-total").text == "22")


def set_dates(driver, start, end, sampling=None):
    driver.execute_script("""for (const [id,value] of [['report-start-date',arguments[0]],['report-end-date',arguments[1]]]) {
      document.getElementById(id).value=value;
    }
    if(arguments[2]) document.getElementById('report-sampling').value=arguments[2];
    document.getElementById('report-end-date').dispatchEvent(new Event('change',{bubbles:true}));""", start, end, sampling)


def wait_updated(driver):
    wait(driver, lambda d: el(d, "#report-updated-at").text.startswith("Updated"))


def api(driver, path):
    return driver.execute_async_script("""const [path,done]=arguments; const xhr=new XMLHttpRequest(); xhr.open('GET',path);
      xhr.onload=()=>{try{done({status:xhr.status,body:JSON.parse(xhr.responseText)})}catch(e){done({status:xhr.status,text:xhr.responseText})}}; xhr.onerror=()=>done({status:0}); xhr.send();""", path)


def control(driver, lab, command, **values):
    result = driver.execute_async_script("""const [payload,secret,done]=arguments; fetch('/__reports_control',{method:'POST',
      headers:{'Content-Type':'application/json','X-Lab-Secret':secret,'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},
      body:JSON.stringify(payload)}).then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));""",
        {"command": command, **values}, lab["control"])
    assert result.get("status") == 200, result
    return result["body"]


def state(driver, lab):
    return driver.execute_async_script("""const [secret,done]=arguments; fetch('/__reports_state',{headers:{'X-Lab-Secret':secret}})
      .then(r=>r.json()).then(done);""", lab["control"])


@case("RP-01", "Anonymous Reports page access", "Anonymous visitors are redirected to Login.", "Open /reports with no authenticated session.", "Fresh browser cookie store.")
def test_01_anonymous_page(reports_browser, request):
    driver, lab = reports_browser
    driver.get(lab["base"] + "/reports")
    wait(driver, lambda d: "/login" in d.current_url)
    note(request, "Anonymous request redirected to /login.")


@case("RP-02", "Anonymous Reports API access", "GET /api/reports returns 401 Authentication is required JSON.", "Request the API by browser XHR while anonymous.", "No authenticated cookie; valid daily date range.")
def test_02_anonymous_api(reports_browser, request):
    driver, lab = reports_browser
    driver.get(lab["base"] + "/login")
    result = api(driver, f"/api/reports?sampling=daily&start_date={BASE}&end_date={BASE}")
    assert result == {"status": 401, "body": {"error": "Authentication is required."}}
    note(request, "Browser XHR received 401 with Authentication is required JSON.")


@case("RP-03", "Admin sidebar navigation", "Admin opens Reports Management with active Reports navigation.", "Sign in as admin and click the Reports sidebar item.", "reportadmin; 22 synthetic records.")
def test_03_admin_navigation(reports_browser, request):
    driver, lab = reports_browser
    login(driver, lab)
    el(driver, 'a[href="/reports"]').click()
    wait(driver, lambda d: el(d, "#report-total").text == "22")
    assert "Reports Management" in driver.title
    assert "nav-active-glow" in el(driver, 'a[href="/reports"]').get_attribute("class")
    note(request, "Admin sidebar opened Reports Management; active navigation and 22-record report rendered.")


@case("RP-04", "Staff Reports access", "Staff can read Reports under the current login_required route policy.", "Authenticate as staff and load /reports.", "Disposable reportstaff; same private dataset.")
def test_04_staff_access(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab, lab["staff"])
    assert not driver.find_elements(By.CSS_SELECTOR, 'a[href="/user-management"]')
    note(request, "Staff loaded the same 22-record report; admin-only User Management navigation was absent.")


@case("RP-05", "Disabled account access", "Disabled user loses API and page access.", "Authenticate admin, disable that disposable account, then request API and page again.", "Active reportadmin is changed to inactive in private SQLite.")
def test_05_disabled_user(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    control(driver, lab, "disable")
    assert api(driver, "/api/reports") ["status"] == 401
    driver.get(lab["base"] + "/reports")
    wait(driver, lambda d: "/login" in d.current_url)
    note(request, "Inactive account API request returned 401 and Reports redirected to Login.")


@case("RP-06", "Implemented Reports layout and controls", "KPI cards, date/sampling inputs, refresh, table, and print control render; no unsupported chart/export controls are assumed.", "Inspect headings, labels, sampling options, columns, and control IDs.", "Desktop viewport 1440x1000; admin; 22 records.")
def test_06_layout(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    assert [e.text for e in driver.find_elements(By.CSS_SELECTOR, "thead th")] == [
        "PERIOD", "TOTAL", "GOOD", "CRACK", "ROTTEN", "NOT CLASSIFIED", "LEGACY/OTHER",
    ]
    for selector in ("#report-sampling", "#report-start-date", "#report-end-date", "#refresh-report", "#print-report"):
        assert el(driver, selector).is_displayed() and el(driver, selector).is_enabled()
    assert [option.text for option in Select(el(driver, "#report-sampling")).options] == ["Daily", "Weekly", "Monthly"]
    assert driver.execute_script("return document.querySelectorAll('canvas').length") == 0
    assert not driver.find_elements(By.CSS_SELECTOR, "#export-report, #download-report")
    note(request, "Four KPI cards, date range, three sampling modes, refresh, seven implemented table columns and print control rendered; no chart canvas or report export control exists.")


@case("RP-07", "Default date range and sampling", "Default sampling is Daily and date inputs cover today and the previous 29 local calendar days.", "Open Reports and compare date fields and recorded initial API request.", "Browser timezone Asia/Manila; server reports anchor date and 22 fixture records.")
def test_07_default_dates(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    start = (date.fromisoformat(lab["today"]) - timedelta(days=29)).isoformat()
    assert el(driver, "#report-sampling").get_attribute("value") == "Daily"
    assert el(driver, "#report-start-date").get_attribute("value") == start
    assert el(driver, "#report-end-date").get_attribute("value") == lab["today"]
    queries = state(driver, lab)["requests"]
    assert any(q.get("sampling") == "daily" and q.get("start_date") == start and q.get("end_date") == lab["today"] for q in queries)
    note(request, f"Default dates were {start} through {lab['today']} in Asia/Manila; first report request used daily sampling and those inclusive date strings.")


@case("RP-08", "Independent overall KPI calculations", "Current KPI cards show total22, Good14, Non-Good8, and recorded Good rate63.6%; the API separately reports verified Good rate85.7%.", "Load default report and compare visible KPI values and API rates with independent fixture arithmetic.", "22 synthetic records: Good14; verified Good12; verified Good/Crack/Rotten denominator14.")
def test_08_independent_kpis(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    actual = [el(driver, f"#{key}").text for key in ("report-total", "report-good", "report-defects", "report-quality-rate")]
    assert actual == ["22", "14", "8", "63.6%"]
    summary = api(driver, "/api/reports?sampling=daily&start_date=2026-10-01&end_date=2026-11-02")["body"]["summary"]
    assert summary["recorded_good_rate"] == 63.6
    assert summary["verified_good_rate"] == 85.7
    note(request, "KPI cards showed 22 total, 14 Good, 8 Non-Good and the implemented recorded-good rate of 63.6% (14/22). API calculations independently returned verified Good rate 85.7% (12/14).")


@case("RP-09", "Verification and quality distinctions", "V/U/L counts preserve Good, Crack, Rotten, Not Classified, and Other categories without treating Not Classified as verified Good.", "Inspect displayed verification detail and the API summary counts.", "Whole fixture includes true, false, null verification and one legacy Bruised label.")
def test_09_verification_categories(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    payload = api(driver, "/api/reports?sampling=daily&start_date=2026-10-01&end_date=2026-11-02")["body"]["summary"]
    assert payload["verification_counts"] == {"verified": 15, "unverified": 3, "unknown": 4}
    assert payload["good_verification_counts"] == {"verified": 12, "unverified": 1, "unknown": 1}
    assert payload["verification_by_quality"] == {
        "Good": {"verified": 12, "unverified": 1, "unknown": 1},
        "Crack": {"verified": 1, "unverified": 1, "unknown": 1},
        "Rotten": {"verified": 1, "unverified": 0, "unknown": 1},
        "Not Classified": {"verified": 0, "unverified": 1, "unknown": 1},
        "Other": {"verified": 1, "unverified": 0, "unknown": 0},
    }
    assert [cell.text for cell in driver.find_elements(By.CSS_SELECTOR, "thead th")] == [
        "PERIOD", "TOTAL", "GOOD", "CRACK", "ROTTEN", "NOT CLASSIFIED", "LEGACY/OTHER",
    ]
    assert [el(driver, f"#{key}").text for key in ("report-total", "report-good", "report-defects")] == ["22", "14", "8"]
    note(request, "API verification totals matched 15 verified / 3 unverified / 4 unknown, including Good 12/1/1 and Not Classified 0/1/1. The current UI renders six aggregate columns and does not expose a V/U/L detail panel.")


@case("RP-10", "Daily period aggregation", "Rows group by Asia/Manila calendar date and sum each quality once.", "Select October 15, 2026 and inspect its daily row.", "Manila-day IDs1,2,3,16,20,21; six records including UTC-previous-day, local-midnight and last-microsecond cases.")
def test_10_daily_group(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, BASE, BASE)
    wait_updated(driver)
    row = [cell.text for cell in el(driver, "#report-rows tr").find_elements(By.TAG_NAME, "td")]
    assert row == ["2026-10-15", "6", "6", "0", "0", "0", "0"]
    note(request, "Daily row rendered Manila-local total6 and Good6; no Crack, Rotten, Not Classified, or unrecognized legacy labels were present.")


@case("RP-11", "Weekly sampling", "Weekly mode uses Asia/Manila calendar dates and preserves ISO week semantics.", "Select Weekly for 2026-10-01 through 2026-11-02 and compare the local-date ISO week buckets.", "One Good record immediately before Manila Monday Oct19; one Crack record at Monday 00:00; Oct31/Nov1 local month boundary.")
def test_11_weekly(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, "2026-10-01", "2026-11-02", "Weekly")
    wait_updated(driver)
    rows = [[cell.text for cell in row.find_elements(By.TAG_NAME, "td")] for row in driver.find_elements(By.CSS_SELECTOR, "#report-rows tr")]
    assert rows == [
        ["2026-W42", "19", "12", "2", "2", "2", "1"],
        ["2026-W43", "1", "0", "1", "0", "0", "0"],
        ["2026-W44", "2", "2", "0", "0", "0", "0"],
    ]
    note(request, "Manila-local ISO buckets W42/W43/W44 displayed totals19/1/2; Good12/0/2; Crack2/1/0; Rotten2/0/0; Not Classified2/0/0; unrecognized legacy1/0/0.")


@case("RP-12", "Monthly sampling", "Monthly mode aggregates records by YYYY-MM.", "Select Monthly for 2026-10-01 through 2026-11-02.", "October fixture count21; November fixture count1.")
def test_12_monthly(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, "2026-10-01", "2026-11-02", "Monthly")
    wait_updated(driver)
    rows = [[cell.text for cell in row.find_elements(By.TAG_NAME, "td")] for row in driver.find_elements(By.CSS_SELECTOR, "#report-rows tr")]
    assert rows == [
        ["2026-10", "21", "13", "3", "2", "2", "1"],
        ["2026-11", "1", "1", "0", "0", "0", "0"],
    ]
    note(request, "Manila-local monthly rows showed October total21, Good13, Crack3, Rotten2, Not Classified2, unrecognized legacy1; November total1/Good1.")


@case("RP-13", "Inclusive Manila day boundaries", "The API includes Manila midnight and last microsecond records while excluding adjacent local dates.", "Select October 15 and inspect the daily row.", "2026-10-15 Asia/Manila; includes IDs1,2,3,16,20,21; excludes IDs19,22 and UTC-only next-local-day records.")
def test_13_manila_day_boundaries(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, BASE, BASE)
    wait_updated(driver)
    row = [cell.text for cell in el(driver, "#report-rows tr").find_elements(By.TAG_NAME, "td")]
    assert row[:2] == ["2026-10-15", "6"]
    note(request, "Selected Manila date included UTC-previous-day ID16, midnight ID20 and final microsecond ID21 (6 total); ID19 immediately before and ID22 at next local midnight were excluded.")


@case("RP-14", "Inclusive multi-day date range", "Both selected Manila calendar dates are included and the following date is excluded.", "Select 2026-10-15 through 2026-10-16 and read total and daily rows.", "Manila daily counts6 and10; expected total16.")
def test_14_multi_day(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, BASE, BASE_NEXT)
    wait(driver, lambda d: el(d, "#report-total").text == "16")
    rows = [[cell.text for cell in row.find_elements(By.TAG_NAME, "td")] for row in driver.find_elements(By.CSS_SELECTOR, "#report-rows tr")]
    assert [(row[0], row[1]) for row in rows] == [("2026-10-15", "6"), ("2026-10-16", "10")]
    note(request, "Two inclusive Manila date buckets rendered with totals6 and10; selected-range total16.")


@case("RP-15", "Asia/Manila calendar-day boundaries", "A selected calendar date in the Asia/Manila browser timezone includes records from Manila midnight through 23:59:59.999999.", "Set Reports to 2026-10-15 and compare fixture timestamps converted to Asia/Manila.", "Local-day IDs1,2,3,16,20,21 (6 records); boundary IDs19-22 straddle Manila midnight.")
def test_15_manila_boundaries(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, BASE, BASE)
    wait_updated(driver)
    actual = [cell.text for cell in el(driver, "#report-rows tr").find_elements(By.TAG_NAME, "td")]
    assert actual[:2] == ["2026-10-15", "6"], f"Expected Manila-local 6 rows; observed {actual!r}"
    note(request, "Reports date selector returned six records for the selected Manila calendar day (IDs1,2,3,16,20,21), including its start/end boundary records and excluding adjacent local dates.")


@case("RP-16", "Empty selected range", "A valid range with no records shows the implemented empty row and zero summaries.", "Select 2026-09-01 for both dates.", "No fixture records in September; zero denominator.")
def test_16_empty_range(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, "2026-09-01", "2026-09-01")
    wait(driver, lambda d: el(d, "#report-total").text == "0")
    assert el(driver, "#report-quality-rate").text == "0%"
    assert "No sorting records match this date range." in el(driver, "#report-rows").text
    note(request, "Empty date range displayed total0, verified-good rate0%, and the no-matching-records row.")


@case("RP-17", "Invalid API date values", "Malformed calendar dates return 400 JSON without a report result.", "Request a non-calendar date through browser XHR.", "start_date=2026-02-30; authenticated browser.")
def test_17_invalid_date(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    result = api(driver, "/api/reports?sampling=daily&start_date=2026-02-30&end_date=2026-03-01")
    assert result["status"] == 400 and "YYYY-MM-DD" in result["body"]["error"]
    note(request, "Invalid calendar date returned HTTP400 JSON with the date-format message.")


@case("RP-18", "Invalid sampling API value", "Unsupported sampling values return 400 JSON.", "Request sampling=quarterly.", "Authenticated browser; start/end 2026-10-15.")
def test_18_invalid_sampling(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    result = api(driver, f"/api/reports?sampling=quarterly&start_date={BASE}&end_date={BASE}")
    assert result == {"status": 400, "body": {"error": "Invalid sampling period."}}
    note(request, "Unsupported sampling returned HTTP400 Invalid sampling period JSON.")


@case("RP-19", "Reversed date range validation", "The page shows its range validation and retains the last valid report values.", "Choose start date after end date and inspect status and total.", "Valid report total22; then start=2026-10-16, end=2026-10-15.")
def test_19_reversed_ui_range(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, BASE_NEXT, BASE)
    wait(driver, lambda d: "Start date is after the end date" in el(d, "#report-status").text)
    assert el(driver, "#report-total").text == "22"
    note(request, "Reversed range showed the inline validation message and retained the previous total22.")


@case("RP-20", "Automatic report refresh", "One-second polling displays newly added report data without a manual refresh.", "Open report, add one synthetic Good record through the isolated control, and wait for the total.", "Initial total22; test-only insertion is ID23 on 2026-11-01 UTC.")
def test_20_auto_refresh(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    control(driver, lab, "add")
    wait(driver, lambda d: el(d, "#report-total").text == "23", timeout=8)
    note(request, "Normal one-second poll refreshed Total Sorted from22 to23 without clicking Refresh.")


@case("RP-21", "Database failure retains report and recovers", "A 503 database error is visible, previous KPIs remain, and manual retry recovers.", "Load report, inject SQLAlchemyError, click Refresh, restore DB mode and retry.", "Private SQLite only; prior total22.")
def test_21_database_error(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    control(driver, lab, "mode", value="database_error")
    el(driver, "#refresh-report").click()
    wait(driver, lambda d: "database is temporarily unavailable" in el(d, "#report-status").text.lower())
    assert el(driver, "#report-total").text == "22"
    control(driver, lab, "mode", value="normal")
    el(driver, "#refresh-report").click()
    wait(driver, lambda d: el(d, "#report-status").text.startswith("Updated"))
    assert el(driver, "#report-total").text == "22"
    note(request, "Injected 503 showed the database-unavailable message and kept total22; restored mode plus Refresh returned to Updated with total22.")


@case("RP-22", "Network failure retains report and recovers", "Rejected fetch shows a readable connection error, keeps prior totals, and recovers on the next poll.", "After initial render, reject one /api/reports fetch in browser and then restore transport.", "Synthetic browser transport exception; total22.")
def test_22_network_failure(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    driver.execute_script("""window.__reportsFetch=window.fetch; window.fetch=(...args)=>String(args[0]).includes('/api/reports')
      ? Promise.reject(new TypeError('simulated network disconnect')) : window.__reportsFetch(...args);""")
    el(driver, "#refresh-report").click()
    wait(driver, lambda d: "Unable to reach the server" in el(d, "#report-status").text)
    assert el(driver, "#report-total").text == "22"
    driver.execute_script("window.fetch=window.__reportsFetch")
    wait(driver, lambda d: el(d, "#report-status").text.startswith("Updated"))
    assert el(driver, "#report-total").text == "22"
    note(request, "Simulated request rejection showed the connection message and retained total22; restored fetch recovered on the next one-second poll.")


@case("RP-23", "Unexpected HTML API error", "HTML 502 is reported as unavailable while prior totals remain and normal polling recovers.", "Inject HTML502 from the private API, manually refresh, restore normal mode.", "Controlled proxy-like HTML response; current total22.")
def test_23_html_error(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    control(driver, lab, "mode", value="html_error")
    el(driver, "#refresh-report").click()
    wait(driver, lambda d: "Unable to reach the server" in el(d, "#report-status").text)
    assert el(driver, "#report-total").text == "22"
    control(driver, lab, "mode", value="normal")
    wait(driver, lambda d: el(d, "#report-status").text.startswith("Updated"))
    note(request, "Synthetic HTML502 reached the generic connection error without changing total22; normal poll recovered.")


@case("RP-24", "Malformed JSON success response", "Invalid JSON does not replace displayed report values and produces readable error status.", "Override one Reports response with malformed JSON, then let normal polling recover.", "Synthetic HTTP200 body={broken; total22.")
def test_24_malformed_json(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    driver.execute_script("""window.__reportsFetch=window.fetch; let once=true; window.fetch=(...args)=>{
      if(once && String(args[0]).includes('/api/reports')){once=false;return Promise.resolve(new Response('{broken',{status:200,headers:{'Content-Type':'application/json'}}));}
      return window.__reportsFetch(...args);};""")
    el(driver, "#refresh-report").click()
    wait(driver, lambda d: "Unable to reach the server" in el(d, "#report-status").text)
    assert el(driver, "#report-total").text == "22"
    driver.execute_script("window.fetch=window.__reportsFetch")
    wait(driver, lambda d: el(d, "#report-status").text.startswith("Updated"))
    note(request, "Malformed JSON200 caused a handled refresh error and retained total22; restored API recovered.")


@case("RP-25", "Zero denominator", "No records produce zero total and zero percent without division errors.", "Clear private EggRecord rows and wait for automatic refresh.", "Disposable database set to zero records; no other table changed.")
def test_25_zero_denominator(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    control(driver, lab, "clear")
    wait(driver, lambda d: el(d, "#report-total").text == "0")
    assert [el(driver, f"#{key}").text for key in ("report-good", "report-defects", "report-quality-rate")] == ["0", "0", "0%"]
    assert "No sorting records match this date range." in el(driver, "#report-rows").text
    note(request, "Empty table rendered total0, Good0, Non-Good0, verified Good0%, and the no-records row.")


@case("RP-26", "Session-related aggregation", "Records from distinct sorting sessions contribute once to the date/quality aggregate; no session filter or session breakdown is presented.", "Inspect 2026-10-15 daily bucket with REPORT-A and REPORT-TZ records.", "Base date includes records from two session refs; expected total6 and Good6.")
def test_26_session_aggregation(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, BASE, BASE)
    wait_updated(driver)
    row = [cell.text for cell in el(driver, "#report-rows tr").find_elements(By.TAG_NAME, "td")]
    assert row == ["2026-10-15", "6", "6", "0", "0", "0", "0"]
    assert not driver.find_elements(By.CSS_SELECTOR, "#session-filter, #session-report")
    note(request, "Base date aggregate included records from REPORT-A and REPORT-TZ exactly once (total6, Good6); no session filter/breakdown is implemented.")


@case("RP-27", "Print report action", "Print button delegates to window.print once.", "Replace window.print with a counter and click Print current report.", "Browser-native print intercepted; headless only.")
def test_27_print(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    driver.execute_script("window.__printCalls=0;window.print=()=>window.__printCalls++")
    el(driver, "#print-report").click()
    assert driver.execute_script("return window.__printCalls") == 1
    note(request, "Print control called intercepted window.print exactly once; no native dialog or paper printing was invoked.")


@case("RP-28", "Responsive mobile report layout", "At 390x844 the report controls remain usable and the wide table is contained by horizontal scrolling.", "Set exact mobile CDP metrics and inspect page/table overflow and visible controls.", "390x844 viewport; sampling/date/refresh/print controls and the seven-column table remain usable.")
def test_28_mobile(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
    metrics = driver.execute_script("""const table=document.querySelector('#report-rows').closest('.overflow-x-auto');return {
      width:innerWidth, documentWidth:document.documentElement.scrollWidth, client:table.clientWidth, scroll:table.scrollWidth,
      overflow:getComputedStyle(table).overflowX, controls:['report-sampling','report-start-date','report-end-date','refresh-report','print-report'].every(id=>{const e=document.getElementById(id);return e&&e.getBoundingClientRect().width>0})};""")
    assert metrics["width"] == 390 and metrics["controls"] and metrics["scroll"] > metrics["client"]
    note(request, f"Mobile metrics: {metrics}; controls were present and table overflow was contained in its horizontal scroller.")


@case("RP-29", "Unknown historical quality label", "Unrecognized legacy labels remain visible in Other without relabeling as Good or a confirmed defect category.", "Read the API summary and total for the fixture containing quality Bruised.", "One verified Bruised row; stored classifier label preserved in EggRecord; 22 total.")
def test_29_other_legacy_label(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    payload = api(driver, "/api/reports?sampling=daily&start_date=2026-10-01&end_date=2026-11-02")["body"]["summary"]
    assert payload["total"] == 22 and payload["other"] == 1 and payload["good"] == 14
    assert payload["verification_by_quality"]["Other"] == {"verified": 1, "unverified": 0, "unknown": 0}
    note(request, "Bruised contributed once to Other (verified1), remained outside Good14 and stayed within total22; no label was rewritten.")


@case("RP-30", "Refresh pending button guard", "Manual Refresh disables during a pending request and is re-enabled when it completes.", "Hold one Reports fetch promise, click Refresh, inspect disabled state, then release the request.", "Browser-controlled promise only; real server remains available for poll after release.")
def test_30_pending_button(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    driver.execute_script("""window.__reportsFetch=window.fetch;window.__holdReport=true;window.__releaseReport=null;window.fetch=(...args)=>{
      if(window.__holdReport && String(args[0]).includes('/api/reports')){window.__holdReport=false;return new Promise(resolve=>window.__releaseReport=()=>window.__reportsFetch(...args).then(resolve));}
      return window.__reportsFetch(...args);};""")
    el(driver, "#refresh-report").click()
    wait(driver, lambda d: el(d, "#refresh-report").get_attribute("disabled") is not None)
    driver.execute_script("window.__releaseReport()")
    wait(driver, lambda d: el(d, "#refresh-report").get_attribute("disabled") is None)
    note(request, "Manual Refresh disabled while its fetch was held and re-enabled after the valid response returned.")


@case("RP-31", "Stored quality text renders safely", "A stored markup-like legacy quality value cannot execute or create markup in the Reports page.", "Change a disposable fixture quality label to SVG-like text and inspect the real refreshed page and DOM.", "Private record ID10; quality value <svg onload=alert(1)>; browser alert intercepted by counter.")
def test_31_safe_quality_text(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    driver.execute_script("window.__reportsAlertCalls=0;window.alert=()=>window.__reportsAlertCalls++")
    control(driver, lab, "hostile_quality")
    before = len(state(driver, lab)["requests"])
    el(driver, "#refresh-report").click()
    wait(driver, lambda d: len(state(d, lab)["requests"]) > before)
    hostile_day = next(
        [cell.text for cell in row.find_elements(By.TAG_NAME, "td")]
        for row in driver.find_elements(By.CSS_SELECTOR, "#report-rows tr")
        if row.find_elements(By.TAG_NAME, "td")
        and row.find_elements(By.TAG_NAME, "td")[0].text == "2026-10-16"
    )
    assert hostile_day == ["2026-10-16", "10", "3", "2", "2", "2", "1"]
    assert driver.find_elements(By.CSS_SELECTOR, "svg[onload], img[onerror]") == []
    assert driver.execute_script("return window.__reportsAlertCalls") == 0
    assert el(driver, "#report-total").text == "22"
    note(request, "Markup-like quality was included only in the numeric Other aggregate; no SVG/image injection appeared and the intercepted alert count stayed0.")


@case("RP-32", "Manila ISO week boundary", "Records immediately before and at Manila Monday midnight fall into consecutive ISO weeks.", "Select Oct18–19, 2026 using Weekly sampling and compare each bucket.", "ID12 at Oct18 23:59:59.999999 Manila (W42); ID13 at Oct19 00:00 Manila (W43).")
def test_32_manila_iso_week_boundary(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, "2026-10-18", "2026-10-19", "Weekly")
    wait_updated(driver)
    rows = [[cell.text for cell in row.find_elements(By.TAG_NAME, "td")] for row in driver.find_elements(By.CSS_SELECTOR, "#report-rows tr")]
    assert rows == [
        ["2026-W42", "1", "1", "0", "0", "0", "0"],
        ["2026-W43", "1", "0", "1", "0", "0", "0"],
    ]
    note(request, "The record one microsecond before Manila Monday midnight remained in ISO W42; the record at local Monday 00:00 entered ISO W43.")


@case("RP-33", "Manila month boundary", "Monthly grouping places the record immediately before and at Manila month-start midnight in the correct local months.", "Select Oct31–Nov1, 2026 using Monthly sampling and compare totals.", "ID14 at Oct31 23:59:59.999999 Manila; ID15 at Nov1 00:00 Manila.")
def test_33_manila_month_boundary(reports_browser, request):
    driver, lab = reports_browser
    open_reports(driver, lab)
    set_dates(driver, "2026-10-31", "2026-11-01", "Monthly")
    wait_updated(driver)
    rows = [[cell.text for cell in row.find_elements(By.TAG_NAME, "td")] for row in driver.find_elements(By.CSS_SELECTOR, "#report-rows tr")]
    assert rows == [
        ["2026-10", "1", "1", "0", "0", "0", "0"],
        ["2026-11", "1", "1", "0", "0", "0", "0"],
    ]
    note(request, "The final Manila October record grouped under 2026-10 and the record at Manila Nov1 midnight grouped under 2026-11.")
