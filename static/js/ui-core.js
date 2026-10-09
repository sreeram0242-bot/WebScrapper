/**
 * ══════════════════════════════════════════════════════════════════════════════
 * FAST MAP LEADS — UI CORE RUNTIME (v4.0)
 * Dual-Theme Manager · Web Audio Synthesizer · Hotkeys · Toast Engine
 * ══════════════════════════════════════════════════════════════════════════════
 */

(function () {
  'use strict';

  // ── 1. THEME ENGINE (Pure White Default / Obsidian Noir Optional) ──
  const THEME_KEY = 'photon_theme_preference';
  const THEME_VERSION_KEY = 'photon_theme_ver_white_v1';

  // Ensure one-time migration to pure white canvas
  if (!localStorage.getItem(THEME_VERSION_KEY)) {
    localStorage.setItem(THEME_KEY, 'light');
    localStorage.setItem(THEME_VERSION_KEY, 'true');
  }
  
  function getPreferredTheme() {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved) return saved;
    return 'light';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem(THEME_KEY, theme);
    const themeBtn = document.getElementById('btnThemeToggle');
    if (themeBtn) {
      themeBtn.innerHTML = theme === 'dark' 
        ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>`
        : `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;
      themeBtn.setAttribute('title', theme === 'dark' ? 'Switch to Pure White Titanium' : 'Switch to Obsidian Dark');
    }
  }

  window.toggleTheme = function () {
    const current = document.documentElement.getAttribute('data-theme') || 'light';
    const next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    window.playHaptic('click');
  };

  // ── 2. WEB AUDIO MICRO-HAPTIC SYNTHESIZER ──
  let audioCtx = null;
  const AUDIO_KEY = 'photon_audio_enabled';
  let isAudioEnabled = localStorage.getItem(AUDIO_KEY) !== 'false'; // Default enabled

  function initAudioContext() {
    if (!audioCtx) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) audioCtx = new AudioContext();
    }
    if (audioCtx && audioCtx.state === 'suspended') {
      audioCtx.resume();
    }
  }

  window.playHaptic = function (type = 'click') {
    if (!isAudioEnabled) return;
    try {
      initAudioContext();
      if (!audioCtx) return;

      const now = audioCtx.currentTime;
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.connect(gain);
      gain.connect(audioCtx.destination);

      if (type === 'click') {
        // Soft, crisp 30ms snap
        osc.type = 'sine';
        osc.frequency.setValueAtTime(820, now);
        osc.frequency.exponentialRampToValueAtTime(340, now + 0.025);
        gain.gain.setValueAtTime(0.04, now);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.025);
        osc.start(now);
        osc.stop(now + 0.025);
      } else if (type === 'success') {
        // High harmonic double chirp
        osc.type = 'sine';
        osc.frequency.setValueAtTime(587.33, now); // D5
        osc.frequency.setValueAtTime(880.00, now + 0.04); // A5
        gain.gain.setValueAtTime(0.05, now);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.12);
        osc.start(now);
        osc.stop(now + 0.12);
      } else if (type === 'radar_ping') {
        // High-frequency faint blip
        osc.type = 'sine';
        osc.frequency.setValueAtTime(1400, now);
        gain.gain.setValueAtTime(0.02, now);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.03);
        osc.start(now);
        osc.stop(now + 0.03);
      } else if (type === 'warning') {
        osc.type = 'triangle';
        osc.frequency.setValueAtTime(220, now);
        gain.gain.setValueAtTime(0.08, now);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.08);
        osc.start(now);
        osc.stop(now + 0.08);
      }
    } catch (e) {
      // Audio autoplay policy fallback
    }
  };

  window.toggleAudioFeedback = function () {
    isAudioEnabled = !isAudioEnabled;
    localStorage.setItem(AUDIO_KEY, isAudioEnabled);
    const audioBtn = document.getElementById('btnAudioToggle');
    if (audioBtn) {
      audioBtn.innerHTML = isAudioEnabled
        ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>`
        : `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>`;
      audioBtn.setAttribute('title', isAudioEnabled ? 'Mute Interface Sounds' : 'Unmute Interface Sounds');
    }
    if (isAudioEnabled) window.playHaptic('success');
  };

  // ── 3. TOAST NOTIFICATION ENGINE ──
  window.showPhotonToast = function (title, subtitle = '', type = 'info', actionText = null, onAction = null) {
    let tray = document.getElementById('photonToastTray');
    if (!tray) {
      tray = document.createElement('div');
      tray.id = 'photonToastTray';
      tray.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:999999;display:flex;flex-direction:column;gap:10px;pointer-events:none;';
      document.body.appendChild(tray);
    }

    const toast = document.createElement('div');
    toast.style.cssText = `
      pointer-events: auto;
      background: var(--bg-surface-elevated, #161C2E);
      color: var(--text-primary, #F8FAFC);
      border: 1px solid var(--border-medium, rgba(255,255,255,0.15));
      border-radius: var(--radius-md, 14px);
      box-shadow: 0 10px 30px rgba(0,0,0,0.4);
      padding: 12px 16px;
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 280px;
      max-width: 420px;
      animation: toast-enter 0.25s var(--spring-snappy) forwards;
      backdrop-filter: blur(20px);
    `;

    const iconColor = type === 'success' ? '#10B981' : type === 'error' ? '#EF4444' : '#3B82F6';
    const iconSvg = type === 'success'
      ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="${iconColor}" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`
      : type === 'error'
      ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="${iconColor}" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`
      : `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="${iconColor}" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`;

    let actionBtnHtml = '';
    if (actionText && onAction) {
      actionBtnHtml = `<button id="toastActionBtn" style="background:var(--brand-primary);color:#fff;border:none;padding:4px 10px;border-radius:6px;font-size:0.75rem;font-weight:700;cursor:pointer;white-space:nowrap;">${actionText}</button>`;
    }

    toast.innerHTML = `
      <div style="flex-shrink:0;">${iconSvg}</div>
      <div style="flex:1;min-width:0;">
        <div style="font-weight:700;font-size:0.86rem;line-height:1.2;">${title}</div>
        ${subtitle ? `<div style="font-size:0.75rem;color:var(--text-muted);margin-top:2px;">${subtitle}</div>` : ''}
      </div>
      ${actionBtnHtml}
    `;

    if (actionText && onAction) {
      toast.querySelector('#toastActionBtn').addEventListener('click', () => {
        onAction();
        toast.remove();
      });
    }

    tray.appendChild(toast);
    window.playHaptic(type === 'success' ? 'success' : 'click');

    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      toast.style.transition = 'all 0.25s ease';
      setTimeout(() => toast.remove(), 250);
    }, 4500);
  };

  // ── 4. KEYBOARD SHORTCUTS CONTROLLER ──
  document.addEventListener('keydown', function (e) {
    // Ignore when typing inside input / textarea
    const activeEl = document.activeElement;
    const isInput = activeEl && (activeEl.tagName === 'INPUT' || activeEl.tagName === 'TEXTAREA' || activeEl.isContentEditable);

    // Cmd+K or Ctrl+K or '/' -> Focus Search
    if ((e.key === 'k' && (e.metaKey || e.ctrlKey)) || (e.key === '/' && !isInput)) {
      e.preventDefault();
      const searchBox = document.getElementById('searchQuery') || document.getElementById('omniSearchInput');
      if (searchBox) {
        searchBox.focus();
        searchBox.select();
        window.playHaptic('click');
      }
      return;
    }

    // Number keys 1..4 switch views when not typing
    if (!isInput && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (e.key === '1' && typeof window.switchView === 'function') {
        window.switchView('search');
        window.playHaptic('click');
      } else if (e.key === '2' && typeof window.switchView === 'function') {
        window.switchView('wallet');
        window.playHaptic('click');
      } else if (e.key === '3' && typeof window.switchView === 'function') {
        window.switchView('files');
        window.playHaptic('click');
      } else if (e.key === '4' && typeof window.switchView === 'function') {
        window.switchView('profile');
        window.playHaptic('click');
      }
    }

    // Escape closes modals / drawers
    if (e.key === 'Escape') {
      window.closeLeadDossier();
    }
  });

  // ── 5. SLIDE-OVER LEAD DOSSIER CONTROLLER ──
  window.openLeadDossier = function (itemOrIndex) {
    let item = itemOrIndex;
    if (typeof itemOrIndex === 'number' && window.allItems && window.allItems[itemOrIndex]) {
      item = window.allItems[itemOrIndex];
    }
    if (!item) return;

    const drawer = document.getElementById('leadDossierDrawer');
    if (!drawer) return;

    const setText = (id, txt) => {
      const el = document.getElementById(id);
      if (el) el.textContent = txt || '—';
    };

    setText('dossierTitle', item.name || 'Unknown Business');
    setText('dossierCategory', item.category || 'Local Business');
    setText('dossierRatingVal', item.rating || '—');
    setText('dossierReviewsVal', item.reviews ? `${item.reviews} reviews` : 'Unrated');
    setText('dossierAddress', item.address || 'Address not listed');
    setText('dossierSchedule', item.schedule || 'Schedule not listed');

    const phoneEl = document.getElementById('dossierPhone');
    const waBtn = document.getElementById('dossierWhatsAppBtn');
    const callBtn = document.getElementById('dossierCallBtn');
    const copyPhoneBtn = document.getElementById('dossierCopyPhoneBtn');
    const verifiedBadge = document.getElementById('dossierVerifiedBadge');

    if (item.phone && item.phone.trim()) {
      if (phoneEl) phoneEl.textContent = item.phone;
      const cleanDigits = item.phone.replace(/[^0-9]/g, '');
      const waNumber = cleanDigits.length === 10 ? '91' + cleanDigits : cleanDigits;
      if (waBtn) {
        waBtn.href = `https://wa.me/${waNumber}?text=Hello%20${encodeURIComponent(item.name || '')}`;
        waBtn.style.display = 'inline-flex';
      }
      if (callBtn) {
        callBtn.href = `tel:${item.phone}`;
        callBtn.style.display = 'inline-flex';
      }
      if (copyPhoneBtn) {
        copyPhoneBtn.onclick = () => {
          if (navigator.clipboard) navigator.clipboard.writeText(item.phone);
          window.showPhotonToast('Phone Copied to Clipboard', item.phone, 'success');
        };
        copyPhoneBtn.style.display = 'inline-block';
      }
      if (verifiedBadge) verifiedBadge.style.display = 'inline-flex';
    } else {
      if (phoneEl) phoneEl.textContent = 'No phone listed';
      if (waBtn) waBtn.style.display = 'none';
      if (callBtn) callBtn.style.display = 'none';
      if (copyPhoneBtn) copyPhoneBtn.style.display = 'none';
      if (verifiedBadge) verifiedBadge.style.display = 'none';
    }

    const webEl = document.getElementById('dossierWebsite');
    if (webEl) {
      if (item.website && item.website.trim()) {
        webEl.innerHTML = `<a href="${item.website}" target="_blank" rel="noopener" style="color:var(--brand-primary);word-break:break-all;">${item.website}</a>`;
      } else {
        webEl.textContent = 'No website listed';
      }
    }

    const mapBtn = document.getElementById('dossierMapBtn');
    if (mapBtn) {
      if (item.link) {
        mapBtn.href = item.link;
        mapBtn.style.display = 'inline-flex';
      } else {
        mapBtn.style.display = 'none';
      }
    }

    drawer.classList.add('open');
    window.playHaptic('click');
  };

  window.closeLeadDossier = function () {
    const drawer = document.getElementById('leadDossierDrawer');
    if (drawer && drawer.classList.contains('open')) {
      drawer.classList.remove('open');
      window.playHaptic('click');
    }
  };

  // ── 7. ROI CAPACITY & BUDGET CALCULATOR ──
  window.updateRoiCalc = function (leads) {
    const count = parseInt(leads, 10) || 40;
    const cost = (count * 0.25).toFixed(2);
    const phones = Math.floor(count * 0.85);
    const pipeline = (count * 25).toLocaleString('en-IN');

    const costEl = document.getElementById('roiCostVal');
    if (costEl) costEl.textContent = `₹${cost}`;
    const phoneEl = document.getElementById('roiPhoneVal');
    if (phoneEl) phoneEl.textContent = `~${phones} (85%)`;
    const pipeEl = document.getElementById('roiPipeVal');
    if (pipeEl) pipeEl.textContent = `₹${pipeline}`;

    const rangeEl = document.getElementById('roiLeadRange');
    if (rangeEl && rangeEl.value != count) rangeEl.value = count;

    document.querySelectorAll('.roi-preset-chip').forEach(chip => {
      chip.classList.toggle('active', parseInt(chip.getAttribute('data-leads'), 10) === count);
    });
  };

  window.setRoiPreset = function (leads) {
    window.updateRoiCalc(leads);
    if (window.playHaptic) window.playHaptic('click');
  };

  window.applyRoiToRecharge = function () {
    const rangeEl = document.getElementById('roiLeadRange');
    const count = parseInt(rangeEl ? rangeEl.value : 1000, 10) || 1000;
    const cost = Math.max(10, Math.round(count * 0.25));
    
    if (typeof window.selectPageRechargePack === 'function' && [100, 250, 500, 1000].includes(cost)) {
      window.selectPageRechargePack(cost);
    } else if (typeof window.handlePageCustomAmount === 'function') {
      const customInp = document.getElementById('pageCustomRechargeAmount');
      if (customInp) customInp.value = cost;
      window.handlePageCustomAmount(cost);
    }
    
    const payBtn = document.getElementById('btnPagePayRazorpay');
    if (payBtn) {
      payBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
      payBtn.style.transform = 'scale(1.04)';
      setTimeout(() => { payBtn.style.transform = ''; }, 400);
    }
    
    if (window.showPhotonToast) {
      window.showPhotonToast(`Applied ₹${cost} (${count} leads) to recharge checkout!`, 'success');
    }
    if (window.playHaptic) window.playHaptic('action_complete');
  };

  // Initialize theme on load
  document.addEventListener('DOMContentLoaded', function () {
    applyTheme(getPreferredTheme());
  });

})();
