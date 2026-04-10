# GitHub OAuth App — One-Time Setup

Ad Sync uses GitHub's **Device Flow** so clients can authorize the app from any browser without copy-pasting tokens. Device Flow requires one OAuth App registered under your GitHub account.

This is a **one-time, 5-minute setup**. Once done, every client install uses the same `client_id` — no per-client setup needed.

## Step 1: Create the OAuth App

1. Go to <https://github.com/settings/developers> (or Settings → Developer settings → OAuth Apps)
2. Click **New OAuth App**
3. Fill in:
   - **Application name:** `Ad Sync by AI Simple`
   - **Homepage URL:** `https://aisimple.co` (or wherever your product page lives)
   - **Application description:** `Syncs Meta ad performance data into ClickUp`
   - **Authorization callback URL:** `https://aisimple.co/callback` *(Device Flow doesn't use this, but GitHub requires a value — put anything valid)*
4. Click **Register application**

## Step 2: Enable Device Flow

On the OAuth App's settings page (where you just landed):

1. Scroll down to the **Device Flow** section
2. Check **Enable Device Flow**
3. Click **Update application**

## Step 3: Copy the Client ID

On the same page, the **Client ID** is displayed at the top. It looks like `Iv1.abc123def456`.

**Do NOT generate a client secret.** Device Flow doesn't need one, and not having a secret is what makes this auth method safe to embed in a distributed desktop app.

## Step 4: Plug it into the app

Open `app/github_client.py` and replace the placeholder:

```python
GITHUB_CLIENT_ID = "REPLACE_WITH_REAL_CLIENT_ID"
```

with the real value:

```python
GITHUB_CLIENT_ID = "Iv1.abc123def456"
```

Rebuild the app:

```bash
python app/build.py --clean
```

## What this OAuth App gets access to

When a client authorizes via the Device Flow, they grant it these scopes:

- `repo` — read/write to their repos (so the app can create the private `adsync-config` repo and push the sync code + workflow)
- `workflow` — edit GitHub Actions workflow files (so the app can write `.github/workflows/sync.yml`)

Clients see exactly which scopes they're approving on the authorization screen. They can revoke at any time from <https://github.com/settings/applications>.

## What clients will see

1. Client clicks **Connect GitHub** in the desktop app
2. Desktop app shows an 8-character user code (e.g., `WDJB-MJHT`) and opens <https://github.com/login/device> in their browser
3. Client enters the code, clicks **Continue**, reviews the scopes (`repo`, `workflow`), clicks **Authorize Ad Sync by AI Simple**
4. Desktop app polls GitHub, receives the access token, and now has API access scoped to the authenticated user

Done. No copy-pasting tokens, no Personal Access Token creation, no GitHub developer knowledge required from the client.

## Rate limits

GitHub Device Flow and OAuth token exchange are subject to standard rate limits:

- Device Flow polling: GitHub sends `slow_down` responses if we poll too fast. The app handles this automatically.
- Post-auth API calls: authenticated users get 5,000 req/hour. Way more than enough.

## When to re-create the OAuth App

Only if:

- You're changing the display name shown to clients on the auth screen
- You want to use a different GitHub organization to own the OAuth App
- The current client_id is somehow compromised (unlikely — it's a public identifier by design)

Otherwise, leave it alone forever.
