# QUICKSTART GUIDE
### AFRC (Ubuntu / Debian) — Fresh VPS Installation

> This quickstart guide is designed for a clean, fast installation on a brand new VPS (Ubuntu 22.04/24.04 or Debian 12/13).

---

## Pre-flight Checklist

### 1. SSH into Your Server
```bash
ssh root@<YOUR_VPS_IP>
```

### 2. Check Firewall in Your Cloud Provider's Console
Ensure the following ports are open in your provider's security group / firewall settings:
- **Port 443** (HTTPS for secure mobile camera access)
- **Port 8000** (Default AFRC application port)
- **Port 22** (SSH)
- *(Optional)* **Port 80** (HTTP for SSL verification)

### 3. Have Your Credentials Ready
Have the following service credentials on hand before starting:
- **AI API Key** (Choose at least one):
  - **Google Gemini** (Recommended: Gemini 2.5 Flash)
  - **OpenRouter**
  - **xAI (Grok)**
  - **OpenAI**
- **Cloudflare Envs** *(Optional)*:
  - Turnstile Sitekey & Secret Key
  - R2 API Token & Account ID (for cloud plate image storage)
- **Curlec Envs** *(Optional)*:
  - Key ID, Key Secret, and Webhook Secret

---

## 5-Step Automated Setup

### Step 1: Update & Install Git
```bash
apt update && apt install -y git
```

---

### Step 2: Generate SSH Deploy Key
Generate a fresh ed25519 deploy key and display the public key:
```bash
ssh-keygen -t ed25519 -C "vps-afrc-deploy" -f ~/.ssh/id_ed25519 -N "" && echo "" && cat ~/.ssh/id_ed25519.pub
```
Copy the printed output (starts with `ssh-ed25519 ...`).

---

### Step 3: Add Deploy Key in GitHub
1. Open your repository on GitHub:
   👉 **`https://github.com/areppp06/AI-Food-Recognition-Cashier-AFRC-Prototype/settings/keys`**
2. Click **"Add deploy key"**.
3. **Title**: `Production VPS`
4. **Key**: Paste the copied key from Step 2.
5. **Allow write access**: **Leave unchecked** (Write Disabled).
6. Click **"Add key"**.

---

### Step 4: Git Clone & Run Installer
Clone directly to `/root/AFRC` and launch the installer:
```bash
cd /root && git clone git@github.com:areppp06/AI-Food-Recognition-Cashier-AFRC-Prototype.git AFRC && cd AFRC && bash install.sh
```
*(If prompted with `Are you sure you want to continue connecting (yes/no/[fingerprint])?`, type `yes` and press Enter).*

---

### Step 5: Follow Interactive Installation & Key In Envs
The installer handles Python, virtualenv, dependencies, database, and systemd service automatically. Follow the on-screen prompts:

1. **Host & Port**: Press **Enter** to keep `0.0.0.0` and `8000`.
2. **AI Provider**:
   - Choose `[1] OpenRouter`, `[2] xAI (Grok)`, `[3] Google Gemini`, or `[4] OpenAI`.
   - Paste the API key for your selected provider.
3. **Other Envs**: Paste your Cloudflare / Curlec keys (or press **Enter** to keep defaults).
4. **Caddy / SSL**:
   - If using a domain for HTTPS on port 443, type `y` and enter your domain.
   - If using direct IP access (`http://<IP>:8000`), press **Enter** (`n`).
5. **First-Time Admin Setup**:
   - Scan the terminal **QR Code** or open the printed setup URL:
     ```text
     http://<YOUR_VPS_IP>:8000/login?setup_token=...
     ```
   - Enter your initial **Admin Username** and **Password**. Done!

---

## Quick Reference Commands

```bash
# Check service status
systemctl status afrc

# Restart application
systemctl restart afrc

# View live application logs
journalctl -u afrc -f

# Re-run environment setup
cd /root/AFRC && bash install.sh --configure

# Pull latest code update
cd /root/AFRC && git pull && systemctl restart afrc
```
