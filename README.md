# AIMI Settings Advisor

A self-hostable web app that connects to your **Nightscout** site, pulls **15+ days**
of CGM data, and produces **rule-based recommendations** to bring glucose into range.
The recommendation engine has the **OpenAPS AIMI code logic baked in** (LGS thresholds,
PK/PD presets, ISF fusion bounds, insulin-stacking guards, learning pace).

Supports **multiple user profiles** — useful for a household or a clinic.

> ⚠️ **Informational only.** This tool does not control your pump and does not replace
> medical advice. Review every suggested change with your diabetes care team.

---

## Features

- Connects to Nightscout via **access token** (recommended) or **API secret**
- Pulls CGM entries, treatments, and the active profile
- Computes TIR, GMI, CV, hourly patterns, low-episode counts
- Rule-based engine encoding AIMI source logic, each recommendation citing the code reference
- Multi-user profiles with **encrypted credentials at rest** (Fernet)
- Single-container deploy via Docker / docker-compose
- Dark dashboard UI with charts

---

## Quick start (docker-compose)

```bash
git clone <your-repo> aimi-advisor
cd aimi-advisor
docker compose up -d --build
```

Open http://localhost:8080

The SQLite database and the encryption key live in the `aimi-data` volume, so your
profiles persist across restarts. **Back up that volume** — losing `secret.key` means
stored credentials can't be decrypted.

### Run with plain Docker

```bash
docker build -t aimi-advisor .
docker run -d --name aimi-advisor -p 8080:8080 -v aimi-data:/data aimi-advisor
```

### Run locally without Docker

```bash
pip install -r requirements.txt
AIMI_DB=./aimi.db AIMI_KEY=./secret.key python wsgi.py
```

---

## Getting a Nightscout token (recommended over API secret)

1. In Nightscout, open **Admin Tools** (the hamburger menu → Admin Tools).
2. Under **Subjects - People, Devices, etc.**, click **Add new Subject**.
3. Name it e.g. `advisor`, give it the **`readable`** role.
4. Copy the generated **Access Token** (looks like `advisor-1a2b3c4d5e6f7890`).
5. Paste it into the "Access Token" field when creating a profile.

A read-only token can't modify your data — safest option. If your site uses
`AUTH_DEFAULT_ROLES=readable` (public read) you may not need any credential at all.

---

## How the engine works

`app/engine.py` encodes thresholds and logic mirrored from the AIMI source:

| Recommendation | Code reference |
|---|---|
| LGS threshold is a hard floor | `HypoThresholdMath.computeHypoThreshold()` |
| Max IOB + priority ceiling (1.20× + 2U) | `InsulinStackingStance` + `OApsAIMIPriorityMaxIobFactor` |
| PK/PD Ultra-fast preset (DIA 5–8h, peak 35–95, init 6h) | `PkpdPresetProfiles.applyPkpdInsulinPreset()` |
| ISF fusion neutral 0.75/1.25 | `PkpdCorrectionPrudence` |
| SMB tail damping cautious/neutral/permissive | `PkpdSmbTailDamping` |
| Learning pace slow/normal/fast | `PkpdSettingsSupport.PkpdLearningPace` |
| Sensitivity/resistance target asymmetry | `DetermineBasalAIMI2` line ~2571 |
| TDD7 → ci = 450/TDD7 | `DetermineBasalAIMI2` line ~2794 |

Data-driven rules (overnight lows, evening highs) come straight from the hourly pattern.

Enter your current AIMI settings in the dashboard's **⚙ Edit AIMI settings** panel so the
engine can compare them against your data. Settings are saved per user.

---

## Multiple users

Each profile stores its own Nightscout URL, credentials, timezone offset, and AIMI
settings. Create as many as you like from the home page. Click **Analyze →** on any
profile to run a fresh report (always pulls at least 15 days).

---

## Security notes

- Credentials are encrypted at rest with a Fernet key stored at `/data/secret.key`.
- The app never returns raw tokens/secrets to the browser.
- Put it behind a reverse proxy (Caddy, nginx, Traefik) with HTTPS and basic auth if
  exposing beyond your LAN. It has **no built-in login** — treat it as a trusted-network tool.

---

## Project layout

```
aimi-advisor/
├── app/
│   ├── __init__.py      # Flask routes
│   ├── nightscout.py    # NS API client (token / api-secret)
│   ├── analytics.py     # TIR, hourly, variability, profile extraction
│   ├── engine.py        # rule-based recommendations (AIMI logic)
│   └── store.py         # SQLite + encrypted credentials
├── templates/
│   ├── index.html       # profile management
│   └── dashboard.html   # analysis + recommendations
├── static/style.css
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── wsgi.py
```
