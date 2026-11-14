# Runtime secrets (never commit secret files)

This directory is bind-mounted read-only into the app container. Place only the
credentials you intend the server to use here:

- `affiliate_admin_pass` — independent management dashboard password. Set
  `AFFILIATE_ADMIN_USER=admin` and
  `AFFILIATE_ADMIN_PASS_FILE=/run/secrets/affiliate_admin_pass` in `.env`.
  The host file is `deploy/secrets/affiliate_admin_pass`; Docker mounts it at
  `/run/secrets/affiliate_admin_pass`. Use these exact lowercase directory and
  file names: Linux paths are case-sensitive. Environment variable names stay
  uppercase. `thesis_auth_pass` below protects the public site and is a separate
  password. Local preview credentials under `.secrets/local-preview/` are
  machine-specific and are not the server's Docker secret path.
- `thesis_auth_pass` — optional HTTP Basic Auth password. Authentication is
  disabled by default; set `THESIS_REQUIRE_AUTH=true`, a username, and the
  `THESIS_AUTH_PASS_FILE` path in `.env` to enable it.
- `dayipaper_pat` — optional Dayipaper PAT, raw token or `PAT=...`
- `tabword_api_key` — optional separate Tabword API key
- `smtp_password` — SMTP authorization code (not the mailbox login password) used to send
  email login codes; set `SMTP_HOST`, `SMTP_USER` and `SMTP_FROM` in `.env`
- `smtp_accounts.json` — optional, several sending mailboxes in priority order (first
  entry is tried first; on auth failure, connection error or rate limit the next one is
  used, and a failed account is skipped for 1-10 minutes). It is re-read on every send,
  so adding or reordering accounts needs no restart. When it holds at least one usable
  account it takes precedence over `SMTP_*` / `smtp_password`. To add a new mailbox with
  top priority, put its entry at the top of the list. Authorization codes, not login
  passwords. Make it readable by UID/GID `10001` only, like the other files here:

  ```json
  [
    {"host": "smtp.exmail.qq.com", "port": 465, "security": "ssl", "user": "new@your-corp.com", "password": "..."},
    {"host": "smtp.163.com", "port": 465, "security": "ssl", "user": "paperstudio_layout@163.com", "password_file": "/run/secrets/smtp_password"},
    {"host": "smtp.exmail.qq.com", "port": 465, "security": "ssl", "user": "zhouliang@syd965.wecom.work", "password": "..."},
    {"host": "smtp.qq.com", "port": 465, "security": "ssl", "user": "529813342@qq.com", "password": "..."}
  ]
  ```

  Optional per entry: `"from"` (default = `user`), `"enabled": false` to switch one off.
  The display name is always `Paper Layout`.
- `alipay_app_private_key.pem` — optional Alipay app private key
- `alipay_platform_public_key.pem` — optional Alipay platform public key for
  callback signature verification (not the app public key)

On EC2, keep this directory and its files readable only by the deployment
administrator and the container's unprivileged UID/GID `10001`. Do not include
credentials in `.env`, source control, image layers, or support messages.
