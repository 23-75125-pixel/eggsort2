"""SS-01..SS-22 browser coverage against actual routes with disposable fake station workers."""
import json
from time import monotonic

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException


def wait(driver, predicate, timeout=12):
    return WebDriverWait(driver, timeout, ignored_exceptions=(
        NoSuchElementException, StaleElementReferenceException,
    )).until(predicate)


def element(driver, selector):
    return wait(driver, lambda d: d.find_element(By.CSS_SELECTOR, selector))


def text(driver, selector):
    return element(driver, selector).text


def login(driver, lab):
    driver.get(lab["base"] + "/login")
    element(driver, "#identifier").send_keys(lab["username"])
    element(driver, "#password").send_keys(lab["password"])
    element(driver, 'button[type="submit"]').click()
    for expected in ("sign in", "success"):
        wait(driver, lambda d: expected in d.find_element(By.CSS_SELECTOR, ".swal2-popup").text.lower())
        wait(driver, lambda d: d.find_element(By.CSS_SELECTOR, ".swal2-confirm").is_displayed())
        driver.find_element(By.CSS_SELECTOR, ".swal2-confirm").click()
    wait(driver, lambda d: "/dashboard" in d.current_url)


def open_sessions(driver, lab):
    login(driver, lab)
    driver.get(lab["base"] + "/sorting-sessions")
    wait(driver, lambda d: d.find_element(By.ID, "start-camera").is_displayed())


def dialog(driver, confirm=True, title=None):
    wait(driver, lambda d: d.find_element(By.CSS_SELECTOR, ".swal2-popup").is_displayed())
    if title:
        wait(driver, lambda d: title.lower() in d.find_element(By.CSS_SELECTOR, ".swal2-popup").text.lower())
    # Capture the text before clicking: SweetAlert removes its popup immediately,
    # so reading a previously-held WebElement afterward races teardown.
    popup_text = driver.find_element(By.CSS_SELECTOR, ".swal2-popup").text
    selector = ".swal2-confirm" if confirm else ".swal2-cancel"
    wait(driver, lambda d: d.find_element(By.CSS_SELECTOR, selector).is_displayed())
    driver.find_element(By.CSS_SELECTOR, selector).click()
    return popup_text


def success_modal_visible(driver, title):
    # Read visibility and text in one browser task. A popup may be removed
    # between WebDriver's is_displayed() and text calls during SweetAlert's
    # enter/exit animation, producing a spurious stale-element failure.
    return driver.execute_script("""
      const needle = arguments[0].toLowerCase();
      return Array.from(document.querySelectorAll('.swal2-popup')).some(popup => {
        const style = getComputedStyle(popup);
        return popup.getClientRects().length > 0 && style.display !== 'none' &&
          style.visibility !== 'hidden' && (popup.innerText || '').toLowerCase().includes(needle);
      });
    """, title)


def wait_success_modal_closed(driver, title="Restarted Successfully!"):
    wait(driver, lambda d: not success_modal_visible(d, title), timeout=8)


def start(driver):
    element(driver, "#start-camera").click()
    dialog(driver, True, "Restart camera")
    # Wait for the start response to render its starting lifecycle before a
    # caller sends synthetic readiness; the success modal appears only later,
    # after readiness, so it cannot be used as the startup signal here.
    def start_state_rendered(d):
        status = text(d, "#detection-status")
        return (d.find_element(By.ID, "camera-active").is_displayed()
                and (status.startswith("Starting camera")
                     or status.startswith("Camera Ready")))

    wait(driver, start_state_rendered, timeout=8)


def stop(driver, confirm=True):
    element(driver, "#stop-camera").click()
    dialog(driver, confirm, "Stop sorting session")


def control(driver, lab, payload):
    return driver.execute_async_script("""
      const [payload, secret, done] = arguments;
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
      const headers = {'Content-Type':'application/json', 'X-Lab-Secret':secret};
      if (csrf) headers['X-CSRF-Token'] = csrf;
      fetch('/__sorting_control', {method:'POST', headers, body:JSON.stringify(payload)})
        .then(async r => done({status:r.status, body:await r.json()}))
        .catch(e => done({error:String(e)}));
    """, payload, lab["control"])


def lab_state(driver, lab):
    return driver.execute_async_script("""
      const [secret, done] = arguments;
      fetch('/__sorting_state', {headers:{'X-Lab-Secret':secret}, cache:'no-store'})
        .then(async r => done({status:r.status, body:await r.json()}))
        .catch(e => done({error:String(e)}));
    """, lab["control"])["body"]


def wait_ready(driver, lab):
    start(driver)
    control(driver, lab, {"command":"ready", "camera_frame":True, "bridge_connected":True})
    wait(driver, lambda d: lab_state(d, lab)["lifecycle"]["status"] == "running")
    wait(driver, lambda d: d.find_element(By.ID, "camera-active").is_displayed())
    # Readiness itself triggers a short success modal. Wait it out before
    # interacting with controls beneath its backdrop.
    wait(driver, lambda d: success_modal_visible(d, "Restarted Successfully!"))
    wait_success_modal_closed(driver)


def force_ui_refresh(driver, function):
    driver.execute_script(f"void {function}()")


@pytest.mark.case("SS-01", "Anonymous access", "Anonymous users are redirected to Login.",
                  "Request /sorting-sessions without an authenticated browser session.", "No account cookie.")
def test_ss01_anonymous_is_redirected(sorting_browser):
    driver, lab = sorting_browser
    driver.get(lab["base"] + "/sorting-sessions")
    wait(driver, lambda d: "/login" in d.current_url)
    assert "/login" in driver.current_url


@pytest.mark.case("SS-02", "Authorized Sorting Sessions page", "Admin sees the actual page and enabled test controls.",
                  "Sign in, open Sorting Sessions, and inspect its primary controls.", "Disposable sortadmin; fake station workers.")
def test_ss02_page_and_controls(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    assert "Sorting Sessions" in driver.title or "Sorting Sessions" in driver.page_source
    assert element(driver, "#start-camera").is_enabled()
    assert not driver.find_elements(By.ID, "camera-active") or "No egg captured" in driver.page_source


@pytest.mark.case("SS-03", "Cancel Start", "Cancel creates no session and starts no fake worker.",
                  "Open the restart confirmation and choose Cancel.", "Zero sessions before action; fake components.")
def test_ss03_cancel_start_does_nothing(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    element(driver, "#start-camera").click()
    dialog(driver, False, "Restart camera")
    state = lab_state(driver, lab)
    assert state["sessions_count"] == 0
    assert state["camera_start_count"] == state["bridge_start_count"] == 0


@pytest.mark.case("SS-04", "Starting state", "Confirmed start remains Starting until both readiness signals arrive.",
                  "Confirm restart; withhold camera frame and controller readiness.", "Fake camera running without a first frame; bridge not ready.")
def test_ss04_start_is_not_reported_ready_early(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    start(driver)
    wait(driver, lambda d: "Starting camera" in text(d, "#detection-status"))
    assert not success_modal_visible(driver, "Restarted Successfully!")
    state = lab_state(driver, lab)
    assert state["lifecycle"]["status"] == "starting"
    assert state["sessions_count"] == 1


@pytest.mark.case("SS-05", "Two-part readiness", "No success until first frame and connected ready controller are both present.",
                  "Signal camera frame with controller disconnected, verify Starting, then signal controller readiness.", "Same run-scoped session reference; both simulated readiness signals.")
def test_ss05_both_readiness_signals_required(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    start(driver)
    first = control(driver, lab, {"command":"ready", "camera_frame":True, "bridge_connected":False})
    assert first["body"]["state"]["status"] == "starting"
    assert not success_modal_visible(driver, "Restarted Successfully!")
    second = control(driver, lab, {"command":"ready", "camera_frame":True, "bridge_connected":True})
    assert second["body"]["state"]["status"] == "running"
    wait(driver, lambda d: success_modal_visible(d, "Restarted Successfully!"))
    state = lab_state(driver, lab)
    assert state["lifecycle"]["session_ref"]
    assert state["lifecycle"]["status"] == "running"


@pytest.mark.case("SS-06", "Late timeout callback", "Readiness just before the deadline remains Ready; stale timeout cannot fail it.",
                  "Reach Ready, then invoke the lifecycle timeout callback as a simulated clock/deadline event.", "45-second production timeout; callback triggered immediately in the lab.")
def test_ss06_ready_wins_against_simulated_timeout(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    wait_ready(driver, lab)
    before = lab_state(driver, lab)["lifecycle"]["session_ref"]
    result = control(driver, lab, {"command":"timeout", "camera_frame":True,
                                   "bridge_connected":True, "controller_ready":True})
    assert result["body"]["state"]["status"] == "running"
    assert lab_state(driver, lab)["lifecycle"]["session_ref"] == before
    assert lab_state(driver, lab)["lifecycle"]["recovery_required"] is False


@pytest.mark.case("SS-07", "Missing camera frame timeout", "Timeout reports missing first frame and recovery; fake workers are cleaned up.",
                  "Start, withhold camera frame, and trigger the real timeout callback without waiting 45 seconds.", "Camera frame absent; controller connected and ready; callback is simulated.")
def test_ss07_missing_camera_frame_fails_clearly(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    start(driver)
    result = control(driver, lab, {"command":"timeout", "camera_frame":False,
                                   "bridge_connected":True, "controller_ready":True})
    assert result["body"]["state"]["status"] == "failed"
    assert "first camera frame" in result["body"]["state"]["failure_reason"]
    assert result["body"]["state"]["recovery_required"] is True
    force_ui_refresh(driver, "refreshSessionState")
    wait(driver, lambda d: "Startup readiness timed out waiting for first camera frame"
         in text(d, "#camera-error"))
    state = lab_state(driver, lab)
    assert not state["camera"]["running"] and not state["bridge"]["running"]


@pytest.mark.case("SS-08", "Missing controller readiness timeout", "Timeout identifies absent ESP32 readiness and keeps recovery required.",
                  "Start with camera frame and bridge connection present but omit controller READY; invoke timeout callback.", "Fake camera frame present; fake serial link connected; no controller READY event.")
def test_ss08_missing_controller_ready_fails(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    start(driver)
    result = control(driver, lab, {"command":"timeout", "camera_frame":True,
                                   "bridge_connected":True, "controller_ready":False})
    assert result["body"]["state"]["status"] == "failed"
    assert "ESP32 controller readiness signal" in result["body"]["state"]["failure_reason"]
    assert result["body"]["state"]["recovery_required"] is True


@pytest.mark.case("SS-09", "Timeout cleanup failure", "Failure and recovery remain visible when fake worker cleanup fails.",
                  "Configure camera stop failure, start without controller readiness, and invoke timeout callback.", "Camera worker stop raises; controller readiness absent; fake components only.")
def test_ss09_timeout_cleanup_failure_remains_visible(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    control(driver, lab, {"command":"configure", "camera_fail_stop":True})
    start(driver)
    result = control(driver, lab, {"command":"timeout", "camera_frame":True,
                                   "bridge_connected":False, "controller_ready":False})
    reason = result["body"]["state"]["failure_reason"]
    assert result["body"]["state"]["status"] == "failed"
    assert "Worker cleanup failed" in reason
    assert "remains running" in reason
    assert result["body"]["state"]["recovery_required"] is True
    status = driver.execute_async_script("""const done=arguments[0];fetch('/api/camera/status').then(r=>r.json()).then(done)""")
    assert status["physical_actuation_verified"] is False


@pytest.mark.case("SS-10", "Cancel Stop", "Cancel leaves the ready session and workers running.",
                  "Start and ready the station, then cancel Stop confirmation.", "One running fake camera and fake bridge.")
def test_ss10_cancel_stop_preserves_session(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    wait_ready(driver, lab)
    stop(driver, confirm=False)
    state = lab_state(driver, lab)
    assert state["lifecycle"]["status"] == "running"
    assert state["camera"]["running"] and state["bridge"]["running"]
    assert state["camera_stop_count"] == state["bridge_stop_count"] == 0


@pytest.mark.case("SS-11", "Confirmed Stop", "Stop completes, updates UI, and reports software state without claiming physical stop.",
                  "Start and ready the station, confirm Stop, then inspect status and session row.", "Fake camera/bridge; no physical actuator is attached.")
def test_ss11_stop_completes_software_lifecycle(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    wait_ready(driver, lab)
    stop(driver, confirm=True)
    wait(driver, lambda d: d.find_element(By.ID, "camera-idle").is_displayed())
    state = lab_state(driver, lab)
    assert state["lifecycle"]["status"] == "completed"
    assert not state["camera"]["running"] and not state["bridge"]["running"]
    status = driver.execute_async_script("""const done=arguments[0];fetch('/api/camera/status').then(r=>r.json()).then(done)""")
    assert status["physical_actuation_verified"] is False
    assert "not independently verified" in status["hardware_shutdown_note"]
    wait(driver, lambda d: success_modal_visible(d, "Closed Successfully!"))


@pytest.mark.case("SS-12", "Startup failure", "Failed worker startup is visible and never reports success.",
                  "Configure fake camera startup to fail, confirm restart, and inspect response UI and session state.", "Fake camera raises LifecycleError; no real worker or hardware.")
def test_ss12_start_failure_never_says_success(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    control(driver, lab, {"command":"configure", "camera_fail_start":True})
    element(driver, "#start-camera").click()
    dialog(driver, True, "Restart camera")
    wait(driver, lambda d: success_modal_visible(d, "Operation failed"))
    state = lab_state(driver, lab)
    assert state["lifecycle"]["status"] == "failed"
    assert not success_modal_visible(driver, "Restarted Successfully!")
    popup = driver.find_element(By.CSS_SELECTOR, ".swal2-popup").text
    assert "Simulated camera startup failure" in popup
    assert "Unexpected token" not in popup
    assert state["lifecycle"]["recovery_required"] is True
    assert not state["camera"]["running"] and not state["bridge"]["running"]


@pytest.mark.case("SS-13", "Audit write warning", "A start operation remains accurate and displays a nonfatal audit warning.",
                  "Make audit persistence fail, confirm start, and verify the warning and lifecycle result.", "Only audit writer is fault-injected; session persistence remains in memory SQLite.")
def test_ss13_audit_failure_does_not_misreport_start(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    control(driver, lab, {"command":"audit_fail"})
    start(driver)
    wait(driver, lambda d: "audit event could not be saved" in text(d, "#camera-error"))
    state = lab_state(driver, lab)
    assert state["lifecycle"]["status"] == "starting"
    assert state["sessions_count"] == 1


@pytest.mark.case("SS-14", "Camera status unavailable", "Status request failure is visibly unavailable, not falsely stopped.",
                  "Inject a fake camera status exception and refresh the camera status request.", "Fake status method raises; no camera device is opened.")
def test_ss14_camera_status_failure_is_unknown(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    wait_ready(driver, lab)
    driver.execute_async_script("""
      const done=arguments[0], original=window.fetch;
      window.reviewCameraFetch = original;
      window.fetch=(url,options)=>String(url).includes('/api/camera/status')
        ? Promise.reject(new Error('Simulated camera status transport failure'))
        : original(url,options);
      (async()=>{
        while(cameraStatusPending) await new Promise(r=>setTimeout(r,5));
        await refreshSessionState();done();
      })();
    """)
    try:
        wait(driver, lambda d: "Camera/session status unavailable" in text(d, "#detection-status"))
        assert "Session stopped." not in text(driver, "#detection-status")
        assert "Simulated camera status transport failure" in text(driver, "#camera-error")
    finally:
        driver.execute_script("window.fetch=window.reviewCameraFetch")


@pytest.mark.case("SS-15", "Connection loss and recovery", "Connection loss shows recovery-required and reconnect does not silently mark Ready.",
                  "Ready the fake station, inject connection loss, then send a reconnect-ready event and re-read status.", "Same session generation; fake bridge disconnect/reconnect events.")
def test_ss15_connection_loss_stays_failed_after_reconnect(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    wait_ready(driver, lab)
    control(driver, lab, {"command":"connection_lost"})
    force_ui_refresh(driver, "refreshSessionState")
    wait(driver, lambda d: "Recovery is required" in text(d, "#detection-status"))
    lost = lab_state(driver, lab)
    assert lost["lifecycle"]["status"] == "failed"
    control(driver, lab, {"command":"reconnect_ready"})
    force_ui_refresh(driver, "refreshSessionState")
    state = lab_state(driver, lab)
    assert state["lifecycle"]["status"] == "failed"
    assert state["lifecycle"]["recovery_required"] is True


@pytest.mark.case("SS-16", "Rapid duplicate Start clicks", "Rapid repeated click cannot create duplicate sessions or workers.",
                  "Click Start, dispatch another click while confirmation is open, confirm once, and count starts.", "One browser; fake lifecycle; duplicate click while button is guarded.")
def test_ss16_rapid_start_click_is_deduplicated(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    element(driver, "#start-camera").click()
    wait(driver, lambda d: d.find_element(By.CSS_SELECTOR, ".swal2-popup").is_displayed())
    driver.execute_script("document.getElementById('start-camera').click()")
    dialog(driver, True, "Restart camera")
    state = lab_state(driver, lab)
    assert state["sessions_count"] == 1
    assert state["camera_start_count"] == state["bridge_start_count"] == 1


@pytest.mark.case("SS-17", "Async response ordering and timeout", "Old polling responses are ignored and timeout covers JSON body parsing.",
                  "Resolve a delayed hardware poll after a newer state revision, then hold response.json() until request timeout.", "Controlled browser fetch promises; no server or hardware fault.")
def test_ss17_stale_poll_and_full_response_timeout(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    result = driver.execute_async_script("""
      const done=arguments[0];
      (async()=>{
        while (hardwareStatusPending) await new Promise(r=>setTimeout(r,5));
        const original=window.fetch; const pending=[];
        window.fetch=(url,options)=>new Promise(resolve=>pending.push({url:String(url),resolve,options}));
        try {
          const old=refreshHardwareState();
          const until=Date.now()+2000;
          while(pending.length<2 && Date.now()<until) await new Promise(r=>setTimeout(r,5));
          hardwareStatusRevision++; hardwareStatus.textContent='Newer operation state';
          pending.forEach(item=>item.resolve(new Response(JSON.stringify(
            item.url.includes('/api/egg-records')?{records:[]}:{connected:false,running:false,latest_event:{message:'Stale stopped'} }
          ),{status:200,headers:{'Content-Type':'application/json'}})));
          await old;
          const preserved=hardwareStatus.textContent;
          window.fetch=(_url,options)=>Promise.resolve({redirected:false,ok:true,url:'/slow-json',
            json:()=>new Promise((_resolve,reject)=>options.signal.addEventListener('abort',()=>{
              const error=new Error('aborted body');error.name='AbortError';reject(error);
            }))});
          let timeoutMessage='';
          try { await requestJson('/slow-json',{timeoutMs:40}); }
          catch(error) { timeoutMessage=error.message; }
          return {preserved,timeoutMessage};
        } finally { window.fetch=original; }
      })().then(done).catch(error=>done({error:String(error)}));
    """)
    assert result["preserved"] == "Newer operation state"
    assert "timed out" in result["timeoutMessage"].lower()


@pytest.mark.case("SS-18", "Camera passage and Egg Exit display", "UI shows controlled passage count, capture ID, and classification.",
                  "Inject fake camera status for a completed passage and refresh the actual camera status route.", "Fake Egg Exit count=2, capture ID=41, quality=good; not a physical passage.")
def test_ss18_passage_status_renders(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    wait_ready(driver, lab)
    control(driver, lab, {"command":"camera_display", "egg_count":2, "capture_id":41, "quality":"good"})
    force_ui_refresh(driver, "refreshSessionState")
    wait(driver, lambda d: text(d, "#egg-zone-count") == "2")
    assert "Egg #41" in text(driver, "#last-zone-capture")
    assert "good" in text(driver, "#capture-zone-status").lower()


@pytest.mark.case("SS-19", "Weight-only no-camera cycle", "Fake completed scale cycle saves Not Classified/Unverified with measured weight and size.",
                  "Submit egg_detected, egg_complete and sort_complete fake ESP32 events without a camera capture; inspect persisted SQLite record.", "Cycle weight=59g; expected weight-derived size=Medium; no camera capture; no physical release claim.")
def test_ss19_weight_only_is_not_classified(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    wait_ready(driver, lab)
    result = control(driver, lab, {"command":"weight_only"})["body"]
    assert len(result["eggs"]) == 1
    record = result["eggs"][0]
    assert (record["quality"], record["quality_verified"], record["confidence"]) == ("Not Classified", False, 0.0)
    assert (record["weight_grams"], record["size"]) == (59, "Medium")
    assert record["session_ref"] == result["lifecycle"]["session_ref"]
    assert result["cycle_count"] == 0
    state = lab_state(driver, lab)
    assert len(state["capture_markers"]) == 1
    assert state["capture_markers"][0].startswith(
        f"egg-capture:{result['lifecycle']['session_ref']}:"
    )


@pytest.mark.case("SS-20", "Fresh Good camera capture", "Fresh Good remains camera-verified and retains measured weight-derived size.",
                  "Queue a same-session fake Good capture, emit a completed scale cycle, and inspect the saved record.", "Capture ID=77; confidence=.94; scale weight=59g; fake session generation matches.")
def test_ss20_fresh_good_capture_unchanged(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    wait_ready(driver, lab)
    result = control(driver, lab, {"command":"fresh_good"})["body"]
    record = result["eggs"][0]
    assert (record["quality"], record["quality_verified"], record["confidence"]) == ("Good", True, 0.94)
    assert (record["weight_grams"], record["size"]) == (59, "Medium")
    assert record["session_ref"] == result["lifecycle"]["session_ref"]
    state = lab_state(driver, lab)
    assert any(marker.endswith(":77") for marker in state["capture_markers"])


@pytest.mark.case("SS-21", "Stale Good capture association limitation", "Identity-safe expected result is Unverified; stale captures must never verify a different egg.",
                  "Queue a Good capture with a different passage ID, emit one scale cycle, and assert the saved cycle is not verified.", "Capture ID=501 has no shared controller cycle ID; scale cycle ID=1; simulated same session.")
def test_ss21_stale_capture_is_not_marked_verified(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    wait_ready(driver, lab)
    result = control(driver, lab, {"command":"stale_good"})["body"]
    record = result["eggs"][0]
    # This assertion is intentionally identity-safe. Current FIFO association is
    # known to violate it; pytest must report the unsafe outcome as FAIL.
    assert record["quality_verified"] is False


@pytest.mark.case("SS-22", "Expired capture limitation", "Expired capture fails visibly without persisting a wrong verified record.",
                  "Queue an expired fake capture, emit a completed scale cycle, and inspect lifecycle, queue, and records.", "Expired capture remains at the queue head; worker is held after first error to keep this test bounded.")
def test_ss22_expired_capture_fails_closed_visibly(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    wait_ready(driver, lab)
    result = control(driver, lab, {"command":"expired_capture"})["body"]
    assert result["lifecycle"]["status"] == "failed"
    assert result["bridge_status"]["latest_event"]["event_type"] == "flow_error"
    assert "expired" in result["bridge_status"]["latest_event"]["message"].lower()
    assert result["eggs"] == []
    assert result["camera_queue_count"] == 1
    force_ui_refresh(driver, "refreshSessionState")
    wait(driver, lambda d: "Recovery is required" in text(d, "#detection-status"))
    force_ui_refresh(driver, "refreshHardwareState")
    wait(driver, lambda d: "expired" in text(d, "#hardware-event").lower())
