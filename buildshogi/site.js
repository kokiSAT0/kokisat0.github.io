"use strict";
(() => {
  const config=window.BUILD_SHOGI_SUPPORT;
  let form;
  try {
    const url=new URL(config?.formURL);
    const responder=(url.hostname==="docs.google.com" && /^\/forms\/d\/e\/[A-Za-z0-9_-]+\/viewform$/.test(url.pathname)) ||
      (url.hostname==="forms.gle" && /^\/[A-Za-z0-9_-]+$/.test(url.pathname));
    if (url.protocol==="https:" && !url.username && !url.password && !url.port && responder) form=url.href;
  } catch { /* An uncreated form is not a working contact channel. */ }
  if (config?.publicReady===true && form) {
    document.querySelectorAll("[data-form-link]").forEach(link => {link.href=form;link.hidden=false;});
    document.querySelectorAll("[data-form-pending]").forEach(item => {item.hidden=true;});
  }
})();
