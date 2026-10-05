(() => {
  'use strict';

  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  if (!token) return;

  const unsafeMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
  const originalFetch = window.fetch.bind(window);

  window.fetch = (input, options = {}) => {
    const request = input instanceof Request ? input : null;
    const method = String(options.method || request?.method || 'GET').toUpperCase();
    const url = new URL(request?.url || String(input), window.location.href);
    const sameOrigin = url.origin === window.location.origin;
    let fetchOptions = options;
    if (sameOrigin && unsafeMethods.has(method)) {
      const headers = new Headers(request?.headers || undefined);
      new Headers(options.headers || undefined).forEach((value, name) => {
        headers.set(name, value);
      });
      headers.set('X-CSRF-Token', token);
      fetchOptions = { ...options, headers };
    }

    return originalFetch(input, fetchOptions).then((response) => {
      if (sameOrigin && response.status === 401) {
        window.location.assign('/login');
      }
      return response;
    });
  };

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form').forEach((form) => {
      const method = String(form.method || 'GET').toUpperCase();
      if (!unsafeMethods.has(method) || form.querySelector('[name="_csrf_token"]')) {
        return;
      }
      const field = document.createElement('input');
      field.type = 'hidden';
      field.name = '_csrf_token';
      field.value = token;
      form.append(field);
    });
  });
})();
