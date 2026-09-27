const passwordInput = document.getElementById("admin-password");
const passwordToggle = document.getElementById("password-toggle");

if (passwordInput && passwordToggle) {
    passwordToggle.addEventListener("click", function () {
        const willShow = passwordInput.type === "password";
        passwordInput.type = willShow ? "text" : "password";
        passwordToggle.setAttribute("aria-pressed", String(willShow));
        passwordToggle.setAttribute("aria-label", willShow ? "Hide password" : "Show password");
        passwordToggle.title = willShow ? "Hide password" : "Show password";
        passwordInput.focus();
    });
}
