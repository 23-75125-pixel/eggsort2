(function () {
  'use strict';

  const navItems = Array.from(document.querySelectorAll('a[href="/alerts"]'));
  if (navItems.length === 0) return;

  const badges = navItems.map((link) => {
    const badge = document.createElement('span');
    badge.className = 'ml-auto hidden min-w-6 h-6 px-1 rounded-full bg-inverse-surface text-white text-xs font-extrabold inline-flex items-center justify-center leading-none shadow-sm ring-2 ring-white/80';
    badge.style.setProperty('color', '#ffffff', 'important');
    badge.setAttribute('data-eggsor-unread-badge', '');
    badge.setAttribute('aria-label', 'Unread alerts');
    badge.setAttribute('aria-live', 'polite');
    badge.setAttribute('aria-atomic', 'true');
    link.appendChild(badge);
    return badge;
  });

  function updateBadge(unreadCount) {
    const count = Number(unreadCount);
    if (!Number.isFinite(count) || count < 0) return;

    badges.forEach((badge) => {
      badge.textContent = count > 99 ? '99+' : String(count);
      badge.setAttribute('aria-label', `${count} unread alert${count === 1 ? '' : 's'}`);
      badge.title = `${count} unread alert${count === 1 ? '' : 's'}`;
      badge.classList.toggle('hidden', count === 0);
      badge.classList.toggle('inline-flex', count > 0);
    });
  }

  window.EggSortUnreadBadge = { update: updateBadge };

  // The Alerts page already refreshes this global count every second. Reuse it
  // there instead of issuing a duplicate request or adding another timer.
  const alertsPageCount = document.getElementById('unread-count');
  if (alertsPageCount) {
    if (window.__eggsorUnreadCount !== undefined) {
      updateBadge(window.__eggsorUnreadCount);
    }
    return;
  }

  let requestPending = false;
  async function refreshUnreadCount() {
    if (requestPending) return;
    requestPending = true;
    try {
      const response = await fetch('/api/alerts?filter=unread', { cache: 'no-store' });
      if (!response.ok) throw new Error(`Unread alert refresh failed (${response.status})`);
      const data = await response.json();
      if (!Number.isFinite(Number(data.unread_count)) || Number(data.unread_count) < 0) {
        throw new Error('Unread alert response did not include a valid count');
      }
      updateBadge(Number(data.unread_count));
    } catch (error) {
      // Keep the last confirmed count. A failed refresh must not look like zero.
      console.warn('Could not refresh the unread alert badge.', error);
    } finally {
      requestPending = false;
    }
  }

  refreshUnreadCount();
  window.setInterval(refreshUnreadCount, 15000);
})();
