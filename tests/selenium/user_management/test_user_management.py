"""Functional browser tests for the actual User Management routes and page."""
import json
from urllib.parse import parse_qs, urlparse

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def wait(driver, predicate, timeout=12):
    return WebDriverWait(driver, timeout).until(predicate)


def visible(driver, selector):
    return driver.find_element(By.CSS_SELECTOR, selector)


def signin(driver, lab, role="admin"):
    driver.get(f"{lab['base']}/__um_auth/{role}?key={lab['control_key']}")
    if role == "admin":
        wait(driver, lambda d: d.current_url.endswith("/user-management"))
        # sign_in_user emits the normal one-time success dialog on navigation.
        if driver.find_elements(By.CSS_SELECTOR, ".swal2-popup"):
            settle_dialog(driver)
    driver.execute_script("window.__umLabKey=arguments[0]", lab["control_key"])


def js_fetch(driver, url, method="GET", body=None, *, csrf=True):
    return driver.execute_async_script("""
      const [url,method,body,csrf,done] = arguments;
      const headers = {};
      if (body !== null) headers['Content-Type'] = 'application/json';
      if (window.__umLabKey) headers['X-Lab-Secret'] = window.__umLabKey;
      const run=async()=>{
        if(csrf){let token=document.querySelector('meta[name="csrf-token"]')?.content||'';
          if(!token&&window.__umLabKey)token=(await fetch('/__um_state',{headers:{'X-Lab-Secret':window.__umLabKey}}).then(r=>r.json())).csrf_token;
          headers['X-CSRF-Token']=token;}
        return fetch(url,{method,headers,body:body===null?undefined:JSON.stringify(body)});
      };
      run()
        .then(async response=>{const text=await response.text();let data=null;try{data=JSON.parse(text)}catch{};done({status:response.status,url:response.url,body:data,text:text.slice(0,2000)})})
        .catch(error=>done({error:String(error)}));
    """, url, method, body, csrf)


def api(driver, path, method="GET", body=None, *, csrf=True):
    return js_fetch(driver, path, method, body, csrf=csrf)


def lab_state(driver, lab):
    return driver.execute_async_script("""
      const [key,done]=arguments;
      fetch('/__um_state',{headers:{'X-Lab-Secret':key}})
        .then(r=>r.json()).then(done).catch(e=>done({error:String(e)}));
    """, lab["control_key"])


def lab_control(driver, lab, command, **values):
    driver.execute_script("window.__umLabKey=arguments[0]", lab["control_key"])
    return js_fetch(driver, "/__um_control", "POST", {"command": command, **values})


def user_by_username(state, username):
    return next(user for user in state["users"] if user["username"] == username)


def create_payload(username="new.staff", email="new.staff@example.invalid", **extra):
    return {"display_name": "New Staff", "username": username, "email": email, **extra}


def open_invite_form(driver):
    visible(driver, "#register-operator").click()
    wait(driver, lambda d: "hidden" not in visible(d, "#user-modal").get_attribute("class"))


def fill_user_form(driver, *, name="New Staff", username="new.staff", email="new.staff@example.invalid", avatar=""):
    form = visible(driver, "#user-form")
    for field, value in (("display_name", name), ("username", username), ("email", email), ("avatar_url", avatar)):
        form.find_element(By.NAME, field).clear()
        form.find_element(By.NAME, field).send_keys(value)


def dialog_text(driver, contains=None):
    def read(d):
        value = d.find_element(By.CSS_SELECTOR, ".swal2-popup").text
        return value if contains is None or contains in value else False
    return wait(driver, read)


def settle_dialog(driver, confirm=True, timeout=12):
    selector = ".swal2-confirm" if confirm else ".swal2-cancel"
    button = wait(driver, EC.element_to_be_clickable((By.CSS_SELECTOR, selector)), timeout=timeout)
    button.click()


def card_for(driver, text):
    return wait(driver, lambda d: next(
        article for article in d.find_elements(By.CSS_SELECTOR, "#users-grid article")
        if text in article.text
    ))


def issue_invite(driver, username="invitee", email="invitee@example.invalid"):
    result = api(driver, "/api/users", "POST", create_payload(username, email))
    assert result["status"] == 201, result
    return result["body"]


def submit_invite_password(driver, url, password, confirmation):
    driver.get(url)
    token = visible(driver, 'meta[name="csrf-token"]').get_attribute("content")
    return driver.execute_async_script("""
      const [password,confirmation,token,done]=arguments;
      fetch(location.href,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-CSRF-Token':token},
        body:new URLSearchParams({password,password_confirmation:confirmation})})
       .then(async r=>done({status:r.status,url:r.url,text:(await r.text()).slice(0,5000)}))
       .catch(e=>done({error:String(e)}));
    """, password, confirmation, token)


@pytest.mark.case("UM-01", "Anonymous page access", "Open /user-management with no authenticated cookie.", "Fresh temporary Chrome profile and empty cookies.", "Anonymous users are redirected to Login.")
def test_01_anonymous_page(um_browser, request):
    driver, lab = um_browser
    driver.get(lab["base"] + "/user-management")
    assert driver.current_url.endswith("/login")
    request.node.actual = "Anonymous request redirected to /login."


@pytest.mark.case("UM-02", "Anonymous user-list API", "Request GET /api/users from the anonymous browser.", "No account session cookie.", "The API returns 401 Authentication is required JSON.")
def test_02_anonymous_api(um_browser, request):
    driver, lab = um_browser
    driver.get(lab["base"] + "/login")
    result = api(driver, "/api/users", csrf=False)
    assert result["status"] == 401 and "Authentication is required" in result["body"]["error"]
    request.node.actual = f"Anonymous GET /api/users returned {result['status']} with Authentication is required JSON."


@pytest.mark.case("UM-03", "Admin page navigation and implemented controls", "Authenticate as admin; inspect heading, navigation, actions and panels.", "Disposable administrator; desktop viewport 1440x1000.", "Admin sees the Access Management Center, Operators Registry, Invite Staff, Audit and Docs tabs; unsupported search/filter/pagination controls are absent.")
def test_03_admin_layout_and_navigation(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    driver.get(lab["base"] + "/dashboard")
    visible(driver, 'a[href="/user-management"]').click()
    wait(driver, lambda d: d.current_url.endswith("/user-management"))
    assert "Access Management Center" in driver.title
    assert visible(driver, "#tab-operators") and visible(driver, "#tab-audit") and visible(driver, "#tab-docs")
    assert "Invite Staff" in visible(driver, "#register-operator").text
    assert not driver.find_elements(By.CSS_SELECTOR, "input[type=search], #user-search, #user-filter, [aria-label*=Pagination i]")
    visible(driver, "#tab-audit").click()
    assert "Audit History Trail" in visible(driver, "#audit-panel").text
    assert "not recorded yet" in visible(driver, "#audit-panel").text
    visible(driver, "#tab-docs").click()
    assert "Only non-current operator accounts can be deleted" in visible(driver, "#docs-panel").text
    request.node.actual = "Admin page and three tabs rendered; Invite Staff available. Audit panel is an explicit placeholder. No search, filter or pagination control exists."


@pytest.mark.case("UM-04", "Staff page access restriction", "Authenticate as staff and open /user-management.", "Active disposable staff account.", "Staff is denied with HTTP 403.")
def test_04_staff_page_forbidden(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab, "staff")
    response = driver.execute_script("return performance.getEntriesByType('navigation')[0]?.responseStatus || 0")
    assert response == 403 or "Forbidden" in driver.page_source
    request.node.actual = f"Staff request rendered the forbidden response (navigation status {response})."


@pytest.mark.case("UM-05", "Staff user-list API restriction", "Authenticate as staff and request GET /api/users.", "Active disposable staff; valid session.", "Authenticated staff receives 403 Administrator access is required JSON.")
def test_05_staff_api_forbidden(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab, "staff")
    result = api(driver, "/api/users")
    assert result["status"] == 403 and "Administrator access" in result["body"]["error"]
    request.node.actual = "Staff GET /api/users returned 403 Administrator access is required JSON."


@pytest.mark.case("UM-06", "Staff mutation restriction", "Attempt POST /api/users with a valid session CSRF token.", "Staff role; syntactically valid synthetic profile.", "The request is rejected with 403 and creates no account.")
def test_06_staff_write_forbidden(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab, "staff")
    before = len(lab_state(driver, lab)["users"])
    result = api(driver, "/api/users", "POST", create_payload("staff.try", "staff.try@example.invalid"))
    after = len(lab_state(driver, lab)["users"])
    assert result["status"] == 403 and before == after
    request.node.actual = f"Staff POST returned 403; user count remained {after}."


@pytest.mark.case("UM-07", "User list account information", "Inspect GET /api/users and the rendered operator cards.", "Admin, active staff, target staff; current admin session.", "List is ordered by role/email and provides the implemented profile, role, active, password, invite, Google and current-account fields without secrets.")
def test_07_user_list_fields(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    result = api(driver, "/api/users")
    assert result["status"] == 200
    users = result["body"]["users"]
    assert users == sorted(users, key=lambda user: (user["role"], user["email"] or ""))
    admin = next(user for user in users if user["username"] == "umadmin")
    staff = next(user for user in users if user["username"] == "umstaff")
    assert admin["is_current"] is True and admin["role"] == "admin"
    assert staff["is_active"] is True and staff["password_set"] is True
    assert {"password", "invite_token_hash", "password_reset_token_hash"}.isdisjoint(admin)
    assert "Management Admin" in driver.find_element(By.ID, "users-grid").text
    assert "Existing Staff" in driver.find_element(By.ID, "users-grid").text
    request.node.actual = "API returned ordered profile/state fields without password or token secrets; page rendered admin and staff display names."


@pytest.mark.case("UM-08", "Account status labels and current-account safeguards", "Inspect card labels and action disabled states.", "Admin current session, active existing staff, active target staff.", "Cards distinguish Google Admin/Current Session and Active staff; admin edit and current-account delete controls are disabled.")
def test_08_status_and_action_states(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    admin_card = card_for(driver, "Management Admin")
    staff_card = card_for(driver, "Existing Staff")
    assert "GOOGLE ADMIN" in admin_card.text and "CURRENT SESSION" in admin_card.text
    assert "STAFF" in staff_card.text and "ACTIVE" in staff_card.text
    admin_edit = admin_card.find_element(By.CSS_SELECTOR, 'button[title="Google admin profile"]')
    current_delete = admin_card.find_element(By.CSS_SELECTOR, 'button[title="Cannot delete current account"]')
    assert admin_edit.get_attribute("disabled") is not None
    assert current_delete.get_attribute("disabled") is not None
    request.node.actual = "Cards showed Google Admin / Current Session and Staff / Active; admin edit and current-account deletion controls were disabled."


@pytest.mark.case("UM-09", "Invite staff through the page", "Open Invite Staff, submit a valid profile and confirm both prompts.", "Synthetic user name, username, email; mail is mocked to fail safely.", "User is persisted as a pending staff invitation; UI shows its local link and nonfatal email-delivery warning.")
def test_09_ui_create_invite(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    open_invite_form(driver)
    fill_user_form(driver, name="  Riley Example  ", username="New.Operator", email="NEW.OP@example.invalid")
    visible(driver, '#user-form button[type="submit"]').click()
    assert "Invite staff?" in dialog_text(driver)
    settle_dialog(driver)
    assert "Success" in dialog_text(driver, "Success")
    settle_dialog(driver)
    wait(driver, lambda d: "hidden" not in visible(d, "#invite-modal").get_attribute("class"))
    invite_url = visible(driver, "#invite-url").get_attribute("value")
    assert urlparse(invite_url).path.startswith("/accept-invite/")
    assert "Email Not Sent" in visible(driver, "#invite-modal-title").text
    assert "external email delivery disabled" in visible(driver, "#invite-delivery-status").text.lower()
    state = lab_state(driver, lab)
    created = user_by_username(state, "new.operator")
    assert created["display_name"] == "Riley Example" and created["role"] == "staff"
    assert created["is_active"] is True and created["password_set"] is False and created["invite_hash_present"]
    assert len(state["mail_attempts"]) == 1
    # The delivery warning is a second dialog after the copyable link is shown.
    if driver.find_elements(By.CSS_SELECTOR, ".swal2-popup"):
        settle_dialog(driver)
    request.node.actual = "UI created normalized new.operator, persisted a pending staff invitation, displayed the local setup link, and reported the mocked email failure without undoing the account."


@pytest.mark.case("UM-10", "Create normalization and role/activation assignment", "POST a profile containing mixed case and attempted privileged fields.", "Valid profile; role=admin and is_active=false supplied by caller.", "Email/username are normalized; creation is forced to staff and active with password_set=false; response omits password/token secrets.")
def test_10_create_normalize_and_fixed_role(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    payload = create_payload("Case.Name", "Case.Name@Example.Invalid", role="admin", is_active=False, password="attacker-value")
    result = api(driver, "/api/users", "POST", payload)
    assert result["status"] == 201 and result["body"]["username"] == "case.name"
    assert result["body"]["email"] == "case.name@example.invalid" and result["body"]["role"] == "staff"
    assert "password" not in result["body"] and "invite_token_hash" not in result["body"]
    created = user_by_username(lab_state(driver, lab), "case.name")
    assert created["role"] == "staff" and created["is_active"] and not created["password_set"]
    request.node.actual = "Mixed-case identity was normalized; attempted role/active/password fields were ignored, account stayed active staff pending setup, and response exposed no password/token."


@pytest.mark.case("UM-11", "Cancel invite form without saving", "Open Invite Staff and close the modal without submission.", "No form submitted.", "No account or invitation is created.")
def test_11_cancel_form(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    before = len(lab_state(driver, lab)["users"])
    open_invite_form(driver)
    visible(driver, "#close-user-modal").click()
    wait(driver, lambda d: "hidden" in visible(d, "#user-modal").get_attribute("class"))
    assert len(lab_state(driver, lab)["users"]) == before
    request.node.actual = "Closing the unsubmitted invite form created no user."


@pytest.mark.case("UM-12", "Username format and length validation", "Submit several invalid usernames through the real create API.", "Two-character, 31-character, whitespace and non-ASCII usernames.", "Each violates the 3–30 ASCII character policy and returns 400 without adding a user.")
def test_12_username_validation(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    before = len(lab_state(driver, lab)["users"])
    invalid = ["ab", "u" * 31, "bad name", "naïve"]
    results = [api(driver, "/api/users", "POST", create_payload(f"{name}", f"{i}@example.invalid")) for i, name in enumerate(invalid)]
    assert all(result["status"] == 400 for result in results)
    assert len(lab_state(driver, lab)["users"]) == before
    request.node.actual = f"All four invalid usernames returned 400; user count remained {before}."


@pytest.mark.case("UM-13", "Email validation", "Submit malformed email values through the real create API.", "Missing @, missing domain suffix, and whitespace-containing values.", "Each malformed email returns 400 with the existing validation error and no record is created.")
def test_13_email_validation(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    before = len(lab_state(driver, lab)["users"])
    values = ["not-an-email", "missing-suffix@example", "has space@example.invalid"]
    results = [api(driver, "/api/users", "POST", create_payload(f"mail{i}", email)) for i, email in enumerate(values)]
    assert all(result["status"] == 400 and "valid email" in result["body"]["error"].lower() for result in results)
    assert len(lab_state(driver, lab)["users"]) == before
    request.node.actual = "All three malformed email values returned the existing 400 validation response; no account was added."


@pytest.mark.case("UM-14", "Profile validation boundaries", "Create users at supported minimum and maximum username/name lengths.", "Username length 3 and 30; display-name length 2 and 120; unique synthetic emails.", "Boundary values are accepted by server validation and stored without truncation.")
def test_14_profile_boundaries(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    cases = [("abc", "xy"), ("u" * 30, "N" * 120)]
    responses = []
    for i, (username, name) in enumerate(cases):
        responses.append(api(driver, "/api/users", "POST", create_payload(username, f"boundary{i}@example.invalid", display_name=name)))
    assert [response["status"] for response in responses] == [201, 201]
    users = lab_state(driver, lab)["users"]
    assert user_by_username({"users": users}, "abc")["display_name"] == "xy"
    assert len(user_by_username({"users": users}, "u" * 30)["display_name"]) == 120
    request.node.actual = "Username/name boundary profiles at 3/2 and 30/120 characters were accepted and stored at full length."


@pytest.mark.case("UM-15", "Duplicate username prevention", "Attempt create using an existing username with different casing.", "Existing umtarget; unique email; username UMTARGET.", "Case-insensitive duplicate is rejected with HTTP 409 and no new row.")
def test_15_duplicate_username(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    before = len(lab_state(driver, lab)["users"])
    result = api(driver, "/api/users", "POST", create_payload("UMTARGET", "different@example.invalid"))
    assert result["status"] == 409 and "already registered" in result["body"]["error"]
    assert len(lab_state(driver, lab)["users"]) == before
    request.node.actual = "Case-insensitive duplicate username returned 409; no account was added."


@pytest.mark.case("UM-16", "Duplicate email prevention", "Attempt create using an existing email with different casing.", "Existing target.um@example.invalid; unique username.", "Case-insensitive duplicate is rejected with HTTP 409 and no new row.")
def test_16_duplicate_email(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    before = len(lab_state(driver, lab)["users"])
    result = api(driver, "/api/users", "POST", create_payload("different.user", "TARGET.UM@EXAMPLE.INVALID"))
    assert result["status"] == 409 and "already registered" in result["body"]["error"]
    assert len(lab_state(driver, lab)["users"]) == before
    request.node.actual = "Case-insensitive duplicate email returned 409; no account was added."


@pytest.mark.case("UM-17", "Browser form validation", "Inspect required, username-pattern, email and URL validity before submission.", "Empty form; then malformed username/email/avatar inputs.", "Browser-native constraints block empty or malformed required values before the API request.")
def test_17_native_form_validation(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    open_invite_form(driver)
    initial = driver.execute_script("return document.getElementById('user-form').checkValidity()")
    fill_user_form(driver, username="bad name", email="not-an-email", avatar="not-a-url")
    invalid = driver.execute_script("return document.getElementById('user-form').checkValidity()")
    assert initial is False and invalid is False
    assert len(lab_state(driver, lab)["users"]) == 3
    request.node.actual = "Native form validation rejected empty required fields and malformed username/email/avatar values before any request."


@pytest.mark.case("UM-18", "Edit staff profile and persist after refresh", "Edit target staff name, username and email through the modal; leave avatar blank, reload and inspect.", "Target staff has a saved HTTPS avatar; unique normalized replacement identity.", "Profile updates persist, blank avatar preserves the saved image, and the card reflects the new display name.")
def test_18_edit_staff_profile(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    card = card_for(driver, "Target Staff")
    card.find_element(By.CSS_SELECTOR, 'button[title="Edit staff"]').click()
    form = visible(driver, "#user-form")
    assert "Edit Staff" in visible(driver, "#user-modal-title").text
    fill_user_form(driver, name="Renamed Target", username="target.renamed", email="renamed@example.invalid", avatar="")
    visible(driver, '#user-form button[type="submit"]').click()
    assert "Save staff changes?" in dialog_text(driver)
    settle_dialog(driver)
    assert "Success" in dialog_text(driver, "Success")
    settle_dialog(driver)
    wait(driver, lambda d: d.execute_script("return Array.from(document.querySelectorAll('#users-grid article')).some(a=>a.innerText.includes('Renamed Target'))"))
    driver.refresh()
    wait(driver, lambda d: "Renamed Target" in visible(d, "#users-grid").text)
    edited = user_by_username(lab_state(driver, lab), "target.renamed")
    assert edited["email"] == "renamed@example.invalid" and edited["avatar_url"] == "https://images.example.invalid/target.png"
    request.node.actual = "Staff profile was updated in the real UI, survived refresh, and retained its existing avatar when the edit field was blank."


@pytest.mark.case("UM-19", "Edit duplicate identity prevention", "Patch target staff to another account's email and then username.", "Existing target and umstaff; each update collides with a different unique identity.", "Each conflict returns 409 and leaves the target profile unchanged.")
def test_19_edit_duplicate_identity(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    target = user_by_username(lab_state(driver, lab), "umtarget")
    base = {"display_name": "Changed", "username": "new.unique", "email": "new.unique@example.invalid"}
    email_result = api(driver, f"/api/users/{target['id']}", "PATCH", {**base, "email": "staff.um@example.invalid"})
    username_result = api(driver, f"/api/users/{target['id']}", "PATCH", {**base, "username": "umstaff"})
    assert email_result["status"] == username_result["status"] == 409
    current = user_by_username(lab_state(driver, lab), "umtarget")
    assert current["email"] == "target.um@example.invalid" and current["display_name"] == "Target Staff"
    request.node.actual = "Both duplicate edit attempts returned 409; target profile remained unchanged."


@pytest.mark.case("UM-20", "Role and active state cannot be changed through profile edit", "PATCH staff profile while including role=admin and is_active=false.", "Valid profile fields and attempted role/activation changes.", "Supported profile fields update while role remains staff and account remains active.")
def test_20_edit_role_ignored(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    target = user_by_username(lab_state(driver, lab), "umtarget")
    result = api(driver, f"/api/users/{target['id']}", "PATCH", {
        "display_name": "Target Staff", "username": "umtarget", "email": "target.um@example.invalid",
        "role": "admin", "is_active": False,
    })
    current = user_by_username(lab_state(driver, lab), "umtarget")
    assert result["status"] == 200 and current["role"] == "staff" and current["is_active"]
    request.node.actual = "Profile route accepted supported fields and ignored role/active fields; account remained active staff."


@pytest.mark.case("UM-21", "Administrator profile is protected", "Inspect the admin edit control and attempt a direct PATCH.", "Current Google admin account.", "UI edit is disabled and the API rejects updates with HTTP 409.")
def test_21_admin_profile_locked(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    card = card_for(driver, "Management Admin")
    assert card.find_element(By.CSS_SELECTOR, 'button[title="Google admin profile"]').get_attribute("disabled") is not None
    admin = user_by_username(lab_state(driver, lab), "umadmin")
    result = api(driver, f"/api/users/{admin['id']}", "PATCH", {"display_name": "Changed", "username": "umadmin", "email": "admin.um@example.invalid"})
    assert result["status"] == 409 and "managed by sign-in" in result["body"]["error"]
    request.node.actual = "Admin edit control was disabled and direct PATCH returned 409 because Google admin profile is managed by sign-in."


@pytest.mark.case("UM-22", "Current account deletion guard", "Attempt to delete the current admin via UI and API.", "Current signed-in admin row.", "UI delete is disabled and API returns 409 without deleting the account.")
def test_22_delete_current_account(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    card = card_for(driver, "Management Admin")
    control = card.find_element(By.CSS_SELECTOR, 'button[title="Cannot delete current account"]')
    assert control.get_attribute("disabled") is not None
    admin = user_by_username(lab_state(driver, lab), "umadmin")
    result = api(driver, f"/api/users/{admin['id']}", "DELETE")
    assert result["status"] == 409 and "currently signed in" in result["body"]["error"]
    assert user_by_username(lab_state(driver, lab), "umadmin")["id"] == admin["id"]
    request.node.actual = "Current-account delete button was disabled; direct DELETE returned 409 and admin row remained."


@pytest.mark.case("UM-23", "Cancel delete confirmation", "Click Delete operator for target staff and choose Cancel.", "Existing target staff; no concurrent updates.", "Cancelled confirmation performs no DELETE and preserves the account.")
def test_23_cancel_delete(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    card = card_for(driver, "Target Staff")
    card.find_element(By.CSS_SELECTOR, 'button[title="Delete operator"]').click()
    assert "Delete operator?" in dialog_text(driver)
    settle_dialog(driver, confirm=False)
    assert user_by_username(lab_state(driver, lab), "umtarget")["email"] == "target.um@example.invalid"
    request.node.actual = "Cancel left target staff intact and did not send a delete request."


@pytest.mark.case("UM-24", "Confirmed staff deletion and persistence", "Confirm Delete operator, dismiss success, then reload the page.", "Disposable target staff account only.", "Target staff is deleted, removed from the grid, and remains absent after refresh.")
def test_24_delete_staff(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    card = card_for(driver, "Target Staff")
    card.find_element(By.CSS_SELECTOR, 'button[title="Delete operator"]').click()
    settle_dialog(driver)
    assert "Success" in dialog_text(driver, "Success")
    settle_dialog(driver)
    wait(driver, lambda d: not any("Target Staff" in article.text for article in d.find_elements(By.CSS_SELECTOR, "#users-grid article")))
    driver.refresh()
    wait(driver, lambda d: len(d.find_elements(By.CSS_SELECTOR, "#users-grid article")) == 2)
    assert not any(user["username"] == "umtarget" for user in lab_state(driver, lab)["users"])
    request.node.actual = "Confirmed deletion removed target staff from the database and UI; refresh kept it absent."


@pytest.mark.case("UM-25", "Invitation regeneration", "Regenerate target staff invitation twice and open both returned links.", "Existing password-set staff account; mail delivery mocked as unavailable.", "Each request returns a new URL/token, the previous token is invalidated, and staff identity is preserved.")
def test_25_regenerate_invitation(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    target = user_by_username(lab_state(driver, lab), "umtarget")
    first = api(driver, f"/api/users/{target['id']}/invite", "POST")
    second = api(driver, f"/api/users/{target['id']}/invite", "POST")
    assert first["status"] == second["status"] == 200
    old_token = urlparse(first["body"]["invite_url"]).path.rsplit("/", 1)[-1]
    new_token = urlparse(second["body"]["invite_url"]).path.rsplit("/", 1)[-1]
    assert old_token != new_token and not first["body"]["email_sent"] and not second["body"]["email_sent"]
    driver.get(first["body"]["invite_url"])
    assert "Invitation unavailable" in driver.page_source
    driver.get(second["body"]["invite_url"])
    assert "Set up your staff account" in driver.page_source
    current = user_by_username(lab_state(driver, lab), "umtarget")
    assert current["password_set"] is True and current["role"] == "staff"
    request.node.actual = "Second invite used a different token; old link was invalid and new link opened setup, while target account remained staff. Mail was mocked."


@pytest.mark.case("UM-26", "Invitation expiry and invalid token", "Open an invented token and an expired real invitation.", "Random invalid token; valid token expired through isolated lab control.", "Both links show the existing unavailable invitation response and cannot activate an account.")
def test_26_invitation_expiry(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    driver.get(lab["base"] + "/accept-invite/not-a-real-token")
    assert "Invitation unavailable" in driver.page_source
    created = issue_invite(driver)
    state = lab_state(driver, lab)
    lab_control(driver, lab, "expire_invite", username="invitee")
    driver.get(created["invite_url"])
    assert "Invitation unavailable" in driver.page_source
    assert not user_by_username(lab_state(driver, lab), "invitee")["password_set"]
    request.node.actual = "Unknown token and expired valid token both displayed Invitation unavailable; account stayed pending."


@pytest.mark.case("UM-27", "Invitation password validation", "Submit short, mismatched and over-limit passwords to the real invite acceptance route.", "Valid pending invitation; passwords of 7, mismatching 8, and 129 characters.", "Each attempt shows the route's validation message and leaves invitation reusable/password unset.")
def test_27_password_policy(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    created = issue_invite(driver)
    short = submit_invite_password(driver, created["invite_url"], "short7!", "short7!")
    assert "at least 8 characters" in short["text"]
    mismatch = submit_invite_password(driver, created["invite_url"], "goodpass8!", "otherpass8!")
    assert "Passwords do not match" in mismatch["text"]
    long = submit_invite_password(driver, created["invite_url"], "x" * 129, "x" * 129)
    assert "128 characters" in long["text"]
    current = user_by_username(lab_state(driver, lab), "invitee")
    assert not current["password_set"] and current["invite_hash_present"]
    request.node.actual = "7-character, mismatched, and 129-character passwords were rejected with route messages; invitation remained valid and password unset."


@pytest.mark.case("UM-28", "Invitation password activation and secure persistence", "Set an 8-character password, verify invitation consumption, then sign in with that password.", "Synthetic invite and password only; no external authentication.", "Valid password activates staff account, stores a hash, consumes the one-time token, and authenticates the invited user.")
def test_28_password_setup_success(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    created = issue_invite(driver)
    # Drop the administrator test session so invitation completion is observed
    # in the same browser state a real invited staff member would use.
    driver.get(f"{lab['base']}/__um_logout?key={lab['control_key']}")
    result = submit_invite_password(driver, created["invite_url"], "pass1234", "pass1234")
    assert result["status"] == 200 and result["url"].endswith("/login")
    state = lab_state(driver, lab)
    account = user_by_username(state, "invitee")
    assert account["password_set"] and not account["invite_hash_present"]
    assert account["password_hash_scheme"] in {"scrypt", "pbkdf2:sha256"} and account["password_hash_length"] > 40
    driver.get(lab["base"] + "/login")
    token = visible(driver, 'meta[name="csrf-token"]').get_attribute("content")
    login_result = driver.execute_async_script("""
      const [token,done]=arguments;
      const body=new URLSearchParams({identifier:'invitee',password:'pass1234'});
      fetch('/login',{method:'POST',headers:{'X-CSRF-Token':token,'Content-Type':'application/x-www-form-urlencoded'},body})
       .then(async r=>done({status:r.status,url:r.url,text:(await r.text()).slice(0,1200)})).catch(e=>done({error:String(e)}));
    """, token)
    assert login_result["status"] == 200 and login_result["url"].endswith("/dashboard")
    request.node.actual = "Password setup consumed the invitation, set password_set=true, stored a password hash, and the new password authenticated to Dashboard."


@pytest.mark.case("UM-29", "CSRF protection on user mutations", "Send create requests without a token and with an incorrect token using raw XMLHttpRequest.", "Admin session; unique profile; valid/missing and invalid CSRF values.", "Both requests return 400 and create no user.")
def test_29_csrf_protection(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    before = len(lab_state(driver, lab)["users"])
    results = driver.execute_async_script("""
      const done=arguments[0],payload=JSON.stringify({display_name:'CSRF',username:'csrf.test',email:'csrf.test@example.invalid'});
      const send=(token)=>new Promise(resolve=>{const x=new XMLHttpRequest();x.open('POST','/api/users');x.setRequestHeader('Content-Type','application/json');if(token!==null)x.setRequestHeader('X-CSRF-Token',token);x.onload=()=>resolve(x.status);x.onerror=()=>resolve(0);x.send(payload)});
      Promise.all([send(null),send('invalid-token')]).then(done);
    """)
    after = len(lab_state(driver, lab)["users"])
    assert results == [400, 400] and after == before
    request.node.actual = f"Missing and invalid CSRF tokens returned {results}; user count remained {after}."


@pytest.mark.case("UM-30", "User-list error and recovery", "Inject a private API 503, load the page, then restore the list endpoint and reload.", "Disposable lab mode only; no production outage.", "Page shows existing error feedback on load failure; normal API response restores operator cards.")
def test_30_api_error_recovery(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    lab_control(driver, lab, "mode", value="error")
    driver.refresh()
    text = dialog_text(driver)
    assert "Simulated disposable user-list outage" in text
    lab_control(driver, lab, "mode", value="normal")
    driver.refresh()
    wait(driver, lambda d: len(d.find_elements(By.CSS_SELECTOR, "#users-grid article")) == 3)
    request.node.actual = "Injected 503 was shown in existing SweetAlert error feedback; restoring the isolated endpoint and reloading restored all three cards."


@pytest.mark.case("UM-31", "Disabled account loses access", "Disable the current admin in disposable SQLite and request the protected page again.", "Authenticated admin becomes is_active=false in private lab only.", "Inactive session is cleared and protected page redirects to Login.")
def test_31_disabled_admin(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    lab_control(driver, lab, "disable_user", username="umadmin")
    driver.get(lab["base"] + "/user-management")
    assert driver.current_url.endswith("/login")
    request.node.actual = "After disposable admin was marked inactive, /user-management cleared the session and redirected to /login."


@pytest.mark.case("UM-32", "Mobile responsive layout", "Set the headless viewport to 390x844 and inspect navigation, invite control and cards.", "Disposable admin; mobile CDP metrics in the isolated browser only.", "Mobile sidebar opens; invite button and user cards remain usable without document-wide horizontal overflow.")
def test_32_mobile_layout(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
    metrics = driver.execute_script("""const button=document.querySelector('#register-operator');return {
      width:innerWidth,documentWidth:document.documentElement.scrollWidth,
      buttonWidth:button.getBoundingClientRect().width,
      cards:document.querySelectorAll('#users-grid article').length,
      menu:!!document.querySelector('button[onclick="toggleSidebar()"]')};""")
    assert metrics["width"] == 390 and metrics["documentWidth"] <= 390 and metrics["buttonWidth"] > 0 and metrics["cards"] == 3 and metrics["menu"]
    driver.find_element(By.CSS_SELECTOR, 'button[onclick="toggleSidebar()"]').click()
    wait(driver, lambda d: "-translate-x-full" not in visible(d, "#sidebar").get_attribute("class"))
    request.node.actual = f"390x844 layout rendered three cards and usable invite/menu controls with no page-wide overflow: {metrics}; sidebar opened."


@pytest.mark.case("UM-33", "Cancel invite confirmation", "Submit a valid invite profile, then cancel the confirmation dialog.", "Unique synthetic profile; no API operation should occur.", "Confirmation cancellation leaves the form open and creates no user.")
def test_33_cancel_invite_confirmation(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    before = len(lab_state(driver, lab)["users"])
    open_invite_form(driver)
    fill_user_form(driver, username="cancel.invite", email="cancel.invite@example.invalid")
    visible(driver, '#user-form button[type="submit"]').click()
    assert "Invite staff?" in dialog_text(driver)
    settle_dialog(driver, confirm=False)
    assert len(lab_state(driver, lab)["users"]) == before
    assert "hidden" not in visible(driver, "#user-modal").get_attribute("class")
    request.node.actual = "Cancelling Invite staff? kept the form open and created no account."


@pytest.mark.case("UM-34", "Cancel edit confirmation", "Change target profile fields and cancel the Save staff changes? dialog.", "Existing target staff; modified name and unique username/email remain unsaved.", "Cancellation retains original account data and sends no PATCH.")
def test_34_cancel_edit_confirmation(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    card_for(driver, "Target Staff").find_element(By.CSS_SELECTOR, 'button[title="Edit staff"]').click()
    fill_user_form(driver, name="Unsaved Name", username="unsaved.target", email="unsaved@example.invalid")
    visible(driver, '#user-form button[type="submit"]').click()
    assert "Save staff changes?" in dialog_text(driver)
    settle_dialog(driver, confirm=False)
    current = user_by_username(lab_state(driver, lab), "umtarget")
    assert current["display_name"] == "Target Staff" and current["email"] == "target.um@example.invalid"
    request.node.actual = "Cancelling edit confirmation preserved the original target profile and sent no update."


@pytest.mark.case("UM-35", "Cancel invitation regeneration", "Click the existing staff invitation action and cancel its confirmation.", "Password-set target staff; no prior invitation token.", "Cancellation does not create or deliver a new invitation.")
def test_35_cancel_regeneration(um_browser, request):
    driver, lab = um_browser
    signin(driver, lab)
    target_card = card_for(driver, "Target Staff")
    target_card.find_element(By.CSS_SELECTOR, 'button[data-invite-user]').click()
    assert "Create invitation?" in dialog_text(driver)
    settle_dialog(driver, confirm=False)
    state = lab_state(driver, lab)
    target = user_by_username(state, "umtarget")
    assert not target["invite_hash_present"] and not state["mail_attempts"]
    request.node.actual = "Cancel left invitation token absent and made no mocked mail attempt."
