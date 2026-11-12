// Klein script voor Test-knoppen, modellijst en bevestigingen (CSP: geen inline scripts).
(function () {
  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";

  async function post(url, body) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body || {}),
      credentials: "same-origin",
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return resp.json();
  }

  function show(box, ok, lines, invite) {
    box.hidden = false;
    box.className = "test-result " + (ok ? "ok" : "fout");
    box.replaceChildren();
    for (const line of lines) {
      const p = document.createElement("p");
      p.textContent = line;
      box.appendChild(p);
    }
    if (invite) {
      const p = document.createElement("p");
      const a = document.createElement("a");
      a.href = invite; a.target = "_blank"; a.rel = "noopener";
      a.textContent = "➜ Bot uitnodigen op je server";
      p.appendChild(a);
      box.appendChild(p);
    }
  }

  document.querySelectorAll(".js-test").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const form = btn.closest("form");
      const box = form.querySelector(".test-result");
      const input = form.querySelector('input[name="secret"]');
      const value = btn.dataset.stored ? "" : (input?.value || "");
      const body = { value };
      for (const k of ["help_channel_id", "memory_channel_id"]) {
        const el = document.querySelector(`input[name="${k}"]`);
        if (el && el.value.trim()) body[k] = el.value.trim();
      }
      btn.disabled = true;
      const old = btn.textContent;
      btn.textContent = "Bezig met testen…";
      try {
        const r = await post("/api/test/" + btn.dataset.key, body);
        show(box, r.ok, r.lines || [], r.invite);
      } catch (e) {
        show(box, false, ["Testen lukte niet (" + e.message + "). Ververs de pagina en probeer opnieuw."]);
      } finally {
        btn.disabled = false;
        btn.textContent = old;
      }
    });
  });

  const modelsBtn = document.getElementById("js-models");
  if (modelsBtn) {
    modelsBtn.addEventListener("click", async () => {
      const box = document.getElementById("models-result");
      const primary = document.querySelector('input[name="model_primary"]');
      const fallbacks = document.querySelector('textarea[name="model_fallbacks"]');
      const datalist = document.getElementById("model-list");
      modelsBtn.disabled = true;
      box.hidden = false;
      box.textContent = "Bezig met ophalen…";
      try {
        const r = await post("/api/modellen");
        box.replaceChildren();
        if (!r.ok || !r.models.length) {
          box.textContent = r.error || "Geen gratis vision-modellen gevonden.";
          return;
        }
        datalist.replaceChildren();
        for (const m of r.models) {
          const opt = document.createElement("option");
          opt.value = m.id; datalist.appendChild(opt);
          const item = document.createElement("div");
          item.className = "model-item";
          const label = document.createElement("span");
          const code = document.createElement("code");
          code.textContent = m.id;
          label.appendChild(code);
          label.append(" · " + Math.round((m.context || 0) / 1000) + "k context");
          const actions = document.createElement("span");
          actions.className = "row";
          const b1 = document.createElement("button");
          b1.type = "button"; b1.textContent = "Als hoofdmodel";
          b1.onclick = () => { primary.value = m.id; };
          const b2 = document.createElement("button");
          b2.type = "button"; b2.textContent = "+ fallback";
          b2.onclick = () => {
            const list = fallbacks.value.split("\n").map((s) => s.trim()).filter(Boolean);
            if (!list.includes(m.id)) list.push(m.id);
            fallbacks.value = list.join("\n");
          };
          actions.append(b1, b2);
          item.append(label, actions);
          box.appendChild(item);
        }
        const note = document.createElement("p");
        note.className = "muted";
        note.textContent = "Vergeet niet onderaan op 'Instellingen opslaan' te klikken.";
        box.appendChild(note);
      } catch (e) {
        box.textContent = "Ophalen lukte niet (" + e.message + ").";
      } finally {
        modelsBtn.disabled = false;
      }
    });
  }

  // Themaknop: automatisch → donker → licht → automatisch
  const THEMES = { auto: "🌓 Automatisch", dark: "🌙 Donker", light: "☀️ Licht" };
  const NEXT = { auto: "dark", dark: "light", light: "auto" };
  function currentTheme() {
    const t = document.documentElement.getAttribute("data-theme");
    return t === "dark" || t === "light" ? t : "auto";
  }
  function applyTheme(t) {
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    try {
      if (t === "auto") localStorage.removeItem("thema"); else localStorage.setItem("thema", t);
    } catch (e) { /* negeren */ }
    document.querySelectorAll(".js-theme").forEach((b) => { b.textContent = THEMES[t]; });
  }
  document.querySelectorAll(".js-theme").forEach((btn) => {
    btn.textContent = THEMES[currentTheme()];
    btn.addEventListener("click", () => applyTheme(NEXT[currentTheme()]));
  });

  document.querySelectorAll(".js-confirm").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      if (!confirm(btn.dataset.confirm || "Weet je het zeker?")) ev.preventDefault();
    });
  });
})();
