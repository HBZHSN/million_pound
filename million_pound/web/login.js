"use strict";
document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.target.querySelector("button");
  const message = document.querySelector("#message");
  const password = document.querySelector("#password").value;
  const remember = document.querySelector("#remember")?.checked ?? true;
  button.disabled = true;
  message.textContent = "";
  try {
    const response = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({password, remember})});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "登录失败，请重试");
    if (window.PasswordCredential && navigator.credentials?.store) {
      try { await navigator.credentials.store(new window.PasswordCredential(event.target)); }
      catch {}
    }
    window.location.assign("/");
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
});
