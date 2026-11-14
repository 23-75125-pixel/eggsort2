"""Unexpected HTML response through the real Sorting Sessions controls."""
import pytest
from test_sorting_sessions import (open_sessions, element, dialog, wait, text,
    lab_state, success_modal_visible, wait_ready)


@pytest.mark.case('SS-37', 'Unexpected non-JSON startup response',
    'Readable fallback, no parser/HTML leakage or false success; controls recover for a subsequent normal start.',
    'Return synthetic HTML 500 for one browser start request; dismiss error and start normally with fake workers.',
    'Browser Response HTML 500 containing private traceback marker; disposable SQLite; fake camera and ESP32.')
def test_html_start_error_and_retry(sorting_browser):
    driver, lab = sorting_browser
    open_sessions(driver, lab)
    driver.execute_script("""window.ss31OriginalFetch=window.fetch;
      window.fetch=(url,options)=>String(url)==='/api/camera/start'
        ? Promise.resolve(new Response('<html>private traceback marker</html>',{status:500}))
        : window.ss31OriginalFetch(url,options);""")
    try:
        element(driver, '#start-camera').click()
        dialog(driver, True, 'Restart camera')
        wait(driver, lambda d: success_modal_visible(d, 'Operation failed'))
        popup = text(driver, '.swal2-popup')
        assert 'sorting computer returned an unreadable response' in popup
        assert 'Unexpected token' not in popup and 'private traceback marker' not in popup
        assert not success_modal_visible(driver, 'Restarted Successfully!')
        assert lab_state(driver, lab)['sessions_count'] == 0
    finally:
        driver.execute_script('window.fetch=window.ss31OriginalFetch')
    dialog(driver, True)
    wait(driver, lambda d: d.find_element('id', 'start-camera').is_enabled())
    wait_ready(driver, lab)
    assert lab_state(driver, lab)['lifecycle']['status'] == 'running'
