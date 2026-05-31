# AIMI Advisor — Simple Install (Dockge)

Three steps. No file editing.

## 1. Put this folder in your Dockge stacks directory

Copy the whole `aimi-advisor` folder into your Dockge stacks path, e.g.:

```
/mnt/Data/Dockge/stacks/aimi-advisor
```

(Upload the zip there and unzip it. The folder must contain `compose.yaml`,
`Dockerfile`, and the `app` / `templates` / `static` folders.)

## 2. Open Dockge

The `aimi-advisor` stack appears in the list automatically.
Click it, then click **Deploy**.

(First deploy builds the image — takes 1–3 minutes. That's normal.)

## 3. Open the app

```
http://YOUR-TRUENAS-IP:8080
```

Done. Add a profile, paste your Nightscout URL + token, click Analyze.

---

### If port 8080 is already used

Open `compose.yaml` in Dockge's editor, change the first `8080` only:

```yaml
    ports:
      - "8095:8080"
```

Redeploy, then use `http://YOUR-TRUENAS-IP:8095`.

### Your data

Stored in a Docker volume named `aimi-data` — it survives restarts and updates.
Nothing to configure.
