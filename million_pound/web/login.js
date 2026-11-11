"use strict";
document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.target.querySelector("button");
  const message = document.querySelector("#message");
  button.disabled = true;
  message.textContent = "";
  try {
    const response = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({password: document.querySelector("#password").value})});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "登录失败，请重试");
    window.location.assign("/");
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
});
