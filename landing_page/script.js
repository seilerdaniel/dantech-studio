'use strict';

(function () {
  const WA_NUMBER = '541131797343';
  const DEFAULT_MESSAGE = 'Hola Daniel, vi la página de DanTech Studio y quiero consultar por un servicio.';

  function openWhatsApp(message) {
    const url = 'https://wa.me/' + WA_NUMBER + '?text=' + encodeURIComponent(message);
    window.open(url, '_blank');
  }

  function bindWhatsApp() {
    document.querySelectorAll('.js-whatsapp').forEach(function (el) {
      el.addEventListener('click', function (event) {
        event.preventDefault();
        const message = el.getAttribute('data-message') || DEFAULT_MESSAGE;
        openWhatsApp(message);
      });
    });
  }

  function bindSmoothScroll() {
    document.querySelectorAll('a[href^="#"]').forEach(function (anchor) {
      anchor.addEventListener('click', function (event) {
        const targetId = anchor.getAttribute('href');
        if (targetId.length < 2) return;
        const target = document.querySelector(targetId);
        if (!target) return;
        event.preventDefault();
        target.scrollIntoView({ behavior: 'smooth' });
      });
    });
  }

  function setFooterYear() {
    const yearEl = document.getElementById('footer-year');
    if (yearEl) {
      yearEl.textContent = String(new Date().getFullYear());
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    bindWhatsApp();
    bindSmoothScroll();
    setFooterYear();
  });
})();