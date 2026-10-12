(() => {
  'use strict';

  const dialog = (options) => {
    if (window.Swal) {
      return window.Swal.fire({ confirmButtonColor: '#4a7355', cancelButtonColor: '#6b746a', ...options });
    }
    // Keep actions usable if the CDN cannot be reached.
    const message = `${options.title}\n${options.text || ''}`;
    if (options.showCancelButton) return Promise.resolve({ isConfirmed: window.confirm(message) });
    window.alert(message);
    return Promise.resolve({ isConfirmed: true });
  };

  window.EggSortAlerts = {
    confirm: async (title, text, destructive = false) => (await dialog({
      title, text, icon: destructive ? 'warning' : 'question', showCancelButton: true,
      confirmButtonText: 'Confirm', cancelButtonText: 'Cancel', focusCancel: true,
      confirmButtonColor: destructive ? '#ba1a1a' : '#4a7355',
    })).isConfirmed,
    success: (text) => dialog({ title: 'Success', text, icon: 'success' }),
    error: (text) => dialog({ title: 'Operation failed', text, icon: 'error' }),
    warning: (text) => dialog({ title: 'Please note', text, icon: 'warning' }),
    async request(url, options = {}) {
      const response = await fetch(url, options);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || response.redirected) {
        throw new Error(payload.error || 'The request could not be completed. Please try again.');
      }
      return payload;
    },
    async run({ button, title, text, destructive = false, success, action }) {
      if (button?.disabled) return;
      if (button) button.disabled = true;
      try {
        if (!await this.confirm(title, text, destructive)) return;
        const result = await action();
        await this.success(success);
        return result;
      } catch (error) {
        await this.error(error.message || 'Unable to reach the server. Please try again.');
      } finally {
        if (button) button.disabled = false;
      }
    },
  };

  document.addEventListener('DOMContentLoaded', async () => {
    const feedback = document.getElementById('auth-feedback');
    if (feedback) {
      const { icon, message } = JSON.parse(feedback.textContent);
      await dialog({ title: icon === 'error' ? 'Sign-in failed' : 'Success', text: message, icon });
    }
  });

  document.addEventListener('submit', async (event) => {
    const form = event.target;
    const path = new URL(form.action, window.location.href).pathname;
    if (!['/login', '/logout'].includes(path)) return;
    event.preventDefault();
    if (form.dataset.submitting) return;
    form.dataset.submitting = 'true';
    const button = form.querySelector('[type="submit"]');
    const loggingOut = path === '/logout';
    if (button) button.disabled = true;
    try {
      if (!await EggSortAlerts.confirm(loggingOut ? 'Log out?' : 'Sign in?',
        loggingOut ? 'Your sorting session will stop and you will be signed out.' : 'Continue signing in to EggSort+?')) return;
      const response = await fetch(form.action, { method: 'POST', body: new FormData(form) });
      const destination = new URL(response.url, window.location.href);
      if (response.ok && response.redirected && destination.pathname !== path) {
        // The redirect response contains the server-confirmed, one-time result.
        // Display it before navigating because rendering the response consumes it.
        const html = new DOMParser().parseFromString(await response.text(), 'text/html');
        const notice = html.getElementById('auth-feedback');
        const message = notice ? JSON.parse(notice.textContent).message
          : loggingOut ? 'You have been logged out.' : 'You have signed in successfully.';
        await EggSortAlerts.success(message);
        window.location.assign(response.url);
        return;
      }
      const html = new DOMParser().parseFromString(await response.text(), 'text/html');
      throw new Error(html.querySelector('[role="alert"]')?.textContent.trim()
        || (loggingOut ? 'Unable to log out. Please try again.' : 'Unable to sign in. Please try again.'));
    } catch (error) {
      await EggSortAlerts.error(error.message || 'Unable to reach the server. Please try again.');
    } finally {
      delete form.dataset.submitting;
      if (button) button.disabled = false;
    }
  });

  document.addEventListener('click', async (event) => {
    const link = event.target.closest('a[href]');
    if (!link || new URL(link.href).pathname !== '/auth/google') return;
    event.preventDefault();
    if (link.dataset.submitting) return;
    link.dataset.submitting = 'true';
    try {
      if (await EggSortAlerts.confirm('Continue with Google?', 'Continue to Google to sign in to EggSort+?')) {
        window.location.assign(link.href);
      }
    } finally {
      delete link.dataset.submitting;
    }
  });
})();
