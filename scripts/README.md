# GitHub Activity Scripts

Automation for `nirmal@dell-rack-13` to poll GitHub, auto-trigger Claude reviews,
and send a daily email digest.

---

## What it does

- Every 15 min: polls GitHub for new review requests, assigned PRs, assigned issues
- New review requests: automatically triggers `claude -p "/review-pr <URL>"` and
  saves the output to `~/Project/Claude-Workspace/reviews/<date>/pr_<repo>_<number>.md`
- 6pm daily (Mon–Fri): sends an email digest to `nirmal.unnikrishnan@amd.com`
  with all pending items; skips if nothing changed since last send

---

## One-time setup

### 1. Install msmtp

```bash
sudo apt install msmtp msmtp-mta
```

### 2. Configure msmtp for Gmail

Generate a Gmail app password:
- Go to https://myaccount.google.com/apppasswords
- App name: `dell-rack-13`
- Copy the 16-character password

Create `~/.msmtprc`:

```
defaults
auth           on
tls            on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
logfile        ~/Project/Claude-Workspace/scripts/msmtp.log

account gmail
host           smtp.gmail.com
port           587
from           amdnirmal26@gmail.com
user           amdnirmal26@gmail.com
password       <your-16-char-app-password>

account default : gmail
```

Lock it down:
```bash
chmod 600 ~/.msmtprc
```

Test it:
```bash
echo -e "Subject: test\n\nhello" | msmtp --account=gmail nirmal.unnikrishnan@amd.com
```

### 3. Install Claude CLI

```bash
npm install -g @anthropic-ai/claude-code
```

Set your Anthropic API key (stored only in your personal login):
```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.bashrc
source ~/.bashrc
```

Test it:
```bash
claude -p "say hello"
```

### 4. Authenticate GitHub CLI

```bash
gh auth login
# Choose: GitHub.com → HTTPS → Login with browser (or paste token)
```

Test it:
```bash
gh api user --jq .login
# Should print: nunnikri
```

### 5. Create required directories

```bash
mkdir -p ~/Project/Claude-Workspace/reviews
mkdir -p ~/Project/Claude-Workspace/scripts
```

### 6. Set up cron jobs

```bash
crontab -e
```

Add these lines:

```
# Poll GitHub every 15 minutes and trigger Claude reviews for new requests
*/15 * * * * /usr/bin/python3 /home/nirmal/Project/Claude-Workspace/scripts/check_github_activity.py >> /home/nirmal/Project/Claude-Workspace/scripts/poller.log 2>&1

# Send 6pm digest Mon-Fri (skip if nothing new)
0 18 * * 1-5 /usr/bin/python3 /home/nirmal/Project/Claude-Workspace/scripts/send_digest.py >> /home/nirmal/Project/Claude-Workspace/scripts/mailer.log 2>&1

# Daily sync of TheRock with upstream at 6am
0 6 * * * cd /home/nirmal/Project/Claude-Workspace/TheRock && git pull --ff-only >> /home/nirmal/Project/Claude-Workspace/TheRock/sync.log 2>&1 && git submodule update --recursive >> /home/nirmal/Project/Claude-Workspace/TheRock/sync.log 2>&1 && echo "$(date): sync complete" >> /home/nirmal/Project/Claude-Workspace/TheRock/sync.log
```

---

## Manual usage

**Check what's pending right now (no review trigger):**
```bash
python3 ~/Project/Claude-Workspace/scripts/check_github_activity.py --no-review
```

**Dry run (see what would happen without doing it):**
```bash
python3 ~/Project/Claude-Workspace/scripts/check_github_activity.py --dry-run
```

**Force send a digest email now:**
```bash
python3 ~/Project/Claude-Workspace/scripts/send_digest.py --force
```

**Preview digest email without sending:**
```bash
python3 ~/Project/Claude-Workspace/scripts/send_digest.py --dry-run
```

---

## Log files

| File | Contents |
|------|----------|
| `scripts/poller.log` | Output from each 15-min poll run |
| `scripts/mailer.log` | Output from each digest send attempt |
| `scripts/msmtp.log` | msmtp send/error log |
| `scripts/last_digest_state.json` | Tracks what was in the last sent email |
| `~/Project/Claude-Workspace/activity_state.json` | Persistent GitHub activity state |

**Check recent activity:**
```bash
tail -50 ~/Project/Claude-Workspace/scripts/poller.log
tail -20 ~/Project/Claude-Workspace/scripts/mailer.log
```

---

## Review output location

Auto-generated reviews are saved to:
```
~/Project/Claude-Workspace/reviews/
└── 2026-06-05/
    ├── pr_TheRock_4910.md
    └── pr_rocm-systems_5400.md
```

The 6pm email includes a one-line summary from each review file.

---

## Resetting state

If you want the poller to re-trigger reviews for all currently open items:
```bash
rm ~/Project/Claude-Workspace/activity_state.json
```

If you want the next digest to send regardless of whether things changed:
```bash
python3 ~/Project/Claude-Workspace/scripts/send_digest.py --force
```
