# Security

* **Secrets** (DB password, SMTP password, session key, recipient address) live only in the
  private env file referenced by systemd `EnvironmentFile=` (`root:xvm`, mode 0640). They are
  never stored in the repository, the database or ordinary logs, and they are redacted from the
  admin UI and diagnostics.
* **Admin UI**: PBKDF2-SHA256 password hashes (390k iterations), session cookies (`SameSite=Strict`,
  optional `Secure`), CSRF tokens on every form, account lockout after 5 failed logins (15 min), CSP
  and anti-framing headers, no API docs endpoints. Bind to localhost or the LAN; use a TLS reverse
  proxy if it is reachable from other machines.
* **Least privilege**: the web UI runs as the unprivileged `xvm` user and never needs X access.
  Only the capture worker needs the desktop's X authority (see `docs/DEPLOYMENT.md` § 6 for a
  non-root option).
* **Private data**: diagnostic frames are private screenshots. Exports are pseudonymized and
  masked by default. Never publish frames, rendered HTML, logs with customer text or database dumps.
* **No platform circumvention**: the monitor does not bypass CAPTCHAs, use private APIs, read
  cookies or alter the browser. Verification prompts pause automation for the owner.
* Report security issues privately to the repository owner instead of opening a public issue
  with sensitive details.
