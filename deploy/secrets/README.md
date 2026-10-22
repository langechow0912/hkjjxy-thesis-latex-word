# Runtime secrets (never commit secret files)

This directory is bind-mounted read-only into the app container. Place only the
credentials you intend the server to use here:

- `thesis_auth_pass` — generated HTTP Basic Auth password (required; deployment
  refuses to start without it)
- `dayipaper_pat` — optional Dayipaper PAT, raw token or `PAT=...`
- `tabword_api_key` — optional separate Tabword API key
- `alipay_app_private_key.pem` — optional Alipay app private key
- `alipay_platform_public_key.pem` — optional Alipay platform public key for
  callback signature verification (not the app public key)

On EC2, keep this directory and its files readable only by the deployment
administrator and the container's unprivileged UID/GID `10001`. Do not include
credentials in `.env`, source control, image layers, or support messages.
