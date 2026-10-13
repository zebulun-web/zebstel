(() => {
  "use strict";

  const cfg = JSON.parse(document.getElementById("site-config").textContent);
  const $ = (sel, root = document) => root.querySelector(sel);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const SYMBOLS = { NGN: "₦", GHS: "GH₵", ZAR: "R", KES: "KSh ", USD: "$" };
  const money = (n) => {
    const whole = Number.isInteger(n);
    const num = n.toLocaleString("en", {
      minimumFractionDigits: whole ? 0 : 2,
      maximumFractionDigits: whole ? 0 : 2,
    });
    return (SYMBOLS[cfg.currency] ?? cfg.currency + " ") + num;
  };

  function animateNumber(el, to) {
    if (reduceMotion || to === 0) { el.textContent = money(to); return; }
    const start = performance.now(), dur = 1400;
    const tick = (t) => {
      const p = Math.min(1, (t - start) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = money(Math.round(to * eased));
      if (p < 1) requestAnimationFrame(tick);
      else el.textContent = money(to);
    };
    requestAnimationFrame(tick);
  }

  async function loadStats() {
    try {
      const s = await (await fetch("/api/stats")).json();
      $("#goal").textContent = money(s.goal);
      animateNumber($("#raised"), s.raised);
      requestAnimationFrame(() => { $("#bar").style.width = s.percent + "%"; });
      $("#bar-track").setAttribute("aria-valuenow", Math.round(s.percent));
      $("#supporter-count").textContent =
        s.count === 0
          ? "Be the first to give."
          : `${s.count} ${s.count === 1 ? "person has" : "people have"} given so far.`;
    } catch {
      $("#raised").textContent = money(0);
      $("#goal").textContent = money(cfg.goal);
    }
  }

  async function loadWall() {
    const list = $("#wall-list");
    try {
      const items = await (await fetch("/api/supporters")).json();
      list.replaceChildren();
      items.forEach((it) => {
        const li = document.createElement("li");
        const msg = document.createElement("p");
        msg.className = "wall__msg";
        msg.textContent = it.message ? it.message : "Sent their love with a gift.";
        const who = document.createElement("p");
        who.className = "wall__name";
        who.textContent = it.name;
        li.append(msg, who);
        list.append(li);
      });
      $("#wall-empty").hidden = items.length > 0;
    } catch {
      $("#wall-empty").hidden = false;
    }
  }

  const form = $("#give-form");
  const giveBtn = $("#give-btn");
  const errorBox = $("#form-error");
  const customWrap = $("#custom-wrap");
  const customInput = $("#custom-amount");

  document.querySelectorAll("[data-amount-label]").forEach((el) => {
    el.textContent = money(Number(el.parentElement.querySelector("input").value));
  });

  const chosenAmount = () => {
    const picked = form.querySelector('input[name="preset"]:checked');
    if (!picked) return 0;
    return picked.value === "custom" ? Number(customInput.value) || 0 : Number(picked.value);
  };

  function refreshButton() {
    const amt = chosenAmount();
    giveBtn.textContent = amt >= cfg.min_amount ? `Give ${money(amt)}` : "Give";
  }

  form.addEventListener("change", (e) => {
    if (e.target.name === "preset") {
      const custom = e.target.value === "custom";
      customWrap.hidden = !custom;
      if (custom) customInput.focus();
    }
    refreshButton();
  });
  customInput.addEventListener("input", refreshButton);
  refreshButton();
    const showError = (msg, field) => {
    errorBox.textContent = msg;
    errorBox.hidden = false;
    form.querySelectorAll("[aria-invalid]").forEach((n) => n.removeAttribute("aria-invalid"));
    if (field) { field.setAttribute("aria-invalid", "true"); field.focus(); }
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorBox.hidden = true;

    const amount = chosenAmount();
    const name = form.name.value.trim();
    const email = form.email.value.trim();

    if (amount < cfg.min_amount) {
      return showError(`The smallest gift is ${money(cfg.min_amount)}.`, customWrap.hidden ? null : customInput);
    }
    if (!name) return showError("Please tell us your name.", form.name);
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      return showError("Please enter a valid email so we can send your receipt.", form.email);
    }

    giveBtn.disabled = true;
    giveBtn.textContent = "Taking you to secure payment…";
    try {
      const res = await fetch("/api/pay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          amount, name, email,
          message: form.message.value.trim(),
          hide_name: form.hide_name.checked,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.url) throw new Error(data.error || "Something went wrong. Please try again.");
      window.location.assign(data.url);
    } catch (err) {
      showError(err.message);
      giveBtn.disabled = false;
      refreshButton();
    }
  });

  (function countdown() {
    if (!cfg.wedding_date) return;
    const target = new Date(cfg.wedding_date + "T00:00:00");
    if (isNaN(target)) return;
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const days = Math.round((target - today) / 86400000);
    const el = $("#countdown");
    if (days < 0) return;
    el.textContent =
      days === 0 ? "Our wedding day is today!" :
      days === 1 ? "Tomorrow is our wedding day!" :
      `${days} days until our wedding day`;
    el.hidden = false;
  })();

  const shareData = {
    title: `${cfg.wedding_name}: ${cfg.couple}'s wedding`,
    text: `Join us in celebrating ${cfg.couple}'s wedding.`,
    url: location.origin + location.pathname,
  };
  const shareBtn = $("#share-btn");
  shareBtn.addEventListener("click", async () => {
    if (navigator.share) {
      try { await navigator.share(shareData); } catch { /* cancelled */ }
      return;
    }
    try {
      await navigator.clipboard.writeText(shareData.url);
      shareBtn.textContent = "Link copied";
      setTimeout(() => (shareBtn.textContent = "Share this page"), 2200);
    } catch {
      prompt("Copy this link:", shareData.url);
    }
  });
  $("#whatsapp-share").href = "https://wa.me/?text=" + encodeURIComponent(`${shareData.text} ${shareData.url}`);

  (async function returnFromPayment() {
    const params = new URLSearchParams(location.search);
    const gift = params.get("gift");
    if (!gift) return;
    const dlg = $("#thanks");
    const title = $("#thanks-title"), body = $("#thanks-body");
    let first = "";
    try {
      const info = await (await fetch(`/api/gift/${encodeURIComponent(params.get("ref") || "")}`)).json();
      first = info.first_name || "";
    } catch { /* ignore */ }

    if (gift === "thanks") {
      title.textContent = first ? `Thank you, ${first}!` : "Thank you!";
      body.textContent = "Your gift has arrived and your note is on the wall. We are so grateful to have you with us.";
    } else if (gift === "pending") {
      title.textContent = "We're confirming your gift";
      body.textContent = "Your payment is still being confirmed. It will appear here shortly. You don't need to pay again.";
    } else {
      title.textContent = "That payment didn't go through";
      body.textContent = "No money was taken. You're welcome to try again whenever you're ready.";
    }
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
    history.replaceState({}, "", location.pathname);
    loadStats(); loadWall();
  })();

  (function sparkles() {
    if (reduceMotion) return;
    const box = $("#sparkles");
    const count = window.innerWidth < 700 ? 16 : 30;
    for (let i = 0; i < count; i++) {
      const s = document.createElement("i");
      s.style.setProperty("--x", Math.random() * 100 + "%");
      s.style.setProperty("--y", Math.random() * 100 + "%");
      s.style.setProperty("--s", 2 + Math.random() * 4 + "px");
      s.style.setProperty("--d", 2.8 + Math.random() * 3.5 + "s");
      s.style.setProperty("--delay", Math.random() * 5 + "s");
      box.append(s);
    }
  })();

  loadStats();
  loadWall();
})();