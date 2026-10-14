import ipaddress
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import edge_tts
import requests
from flask import Flask, jsonify, render_template_string, send_file, abort, redirect

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 82 * 1024 * 1024  # Each video is submitted separately; keep the existing per-request limit.

# Keep the requested Gemini model fixed. No automatic model fallback.
GEMINI_MODEL = "gemini-3.8-flash"
MAX_VIDEO_BYTES = 80 * 1024 * 1024
MAX_VIDEO_SECONDS = 600
MAX_TRANSCRIPT_CHARS = 45000
JOBS = {}
JOBS_LOCK = threading.Lock()
# Set MOVIE_RECAP_DATA_DIR to a mounted persistent disk (for example /var/data)
# on hosts that support it. Without that setting, the default temporary directory
# is convenient but is not durable across host restarts/redeploys.
# Render persistent disks are commonly mounted at /var/data. If the disk is attached,
# prefer it by default; otherwise use the temporary fallback and warn in the UI/API.
_default_data_root = Path("/var/data") if Path("/var/data").is_dir() and os.getenv("RENDER") else (Path(tempfile.gettempdir()) / "movie-recap-data")
DATA_ROOT = Path(os.getenv("MOVIE_RECAP_DATA_DIR") or _default_data_root)
DATA_ROOT.mkdir(parents=True, exist_ok=True)
OUTPUT_ROOT = DATA_ROOT / "movie-recap-jobs"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
LATEST_ROOT = DATA_ROOT / "latest-artifacts"
LATEST_ROOT.mkdir(parents=True, exist_ok=True)
JOB_DB = DATA_ROOT / "jobs.sqlite3"

def _r2_configured():
    return bool(os.getenv("R2_ACCOUNT_ID") and os.getenv("R2_ACCESS_KEY_ID") and os.getenv("R2_SECRET_ACCESS_KEY") and os.getenv("R2_BUCKET_NAME"))

def _r2_client():
    if not _r2_configured():
        return None
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url="https://" + os.environ["R2_ACCOUNT_ID"].strip() + ".r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
    )

def _r2_video_key(job_id):
    return "jobs/" + str(job_id) + "/movie_recap.mp4"

def _r2_metadata_key(job_id):
    return "jobs/" + str(job_id) + "/job.json"

def _r2_save_job(job_id, job):
    client = _r2_client()
    if client is None:
        return False
    client.put_object(
        Bucket=os.environ["R2_BUCKET_NAME"],
        Key=_r2_metadata_key(job_id),
        Body=json.dumps(job, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    return True

def _r2_get_job(job_id):
    client = _r2_client()
    if client is None:
        return None
    try:
        obj = client.get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=_r2_metadata_key(job_id))
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return None

def _r2_saved_jobs():
    client = _r2_client()
    if client is None:
        return []
    found = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=os.environ["R2_BUCKET_NAME"], Prefix="jobs/"):
            for item in page.get("Contents", []):
                key = item.get("Key", "")
                if key.endswith("/job.json"):
                    job_id = key[len("jobs/"):-len("/job.json")]
                    job = _r2_get_job(job_id)
                    if job:
                        found.append((job_id, job))
    except Exception:
        return found
    return found

def _r2_upload_video(job_id, path):
    client = _r2_client()
    if client is None:
        return False
    client.upload_file(
        str(path),
        os.environ["R2_BUCKET_NAME"],
        _r2_video_key(job_id),
        ExtraArgs={"ContentType": "video/mp4"},
    )
    return True

def _r2_video_url(job_id):
    client = _r2_client()
    if client is None:
        return None
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": os.environ["R2_BUCKET_NAME"], "Key": _r2_video_key(job_id)},
        ExpiresIn=3600,
    )

def _get_final_video_path(job_id, job=None):
    """Return a local MP4 if present; cloud-only jobs are served through signed R2 URLs."""
    path = OUTPUT_ROOT / str(job_id) / "movie_recap.mp4"
    return path if path.is_file() else None

def _db_connect():
    connection = sqlite3.connect(str(JOB_DB, ), timeout=10)
    connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    return connection

def _save_job_locked(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return
    try:
        with _db_connect() as connection:
            connection.execute(
                "INSERT INTO jobs(id, payload) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (job_id, json.dumps(job, ensure_ascii=False)),
            )
    except (OSError, sqlite3.Error):
        # Keep the app usable even if the configured storage is temporarily unavailable.
        pass
    # R2 is the durable source of truth when Cloudflare credentials are configured.
    try:
        _r2_save_job(job_id, job)
    except Exception:
        pass

def _load_jobs():
    try:
        with _db_connect() as connection:
            rows = connection.execute("SELECT id, payload FROM jobs").fetchall()
        for job_id, payload in rows:
            try:
                job = json.loads(payload)
                if job.get("status") in ("queued", "running"):
                    job.update(status="error", message="Server restart ဖြစ်သွားသောကြောင့် အရင်အလုပ် ဆက်မလုပ်နိုင်ပါ။ Video ကို ပြန်တင်ပြီး Generate Final Video ကို ထပ်နှိပ်ပါ။", finished=time.time())
                    with _db_connect() as connection:
                        connection.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job_id))
                JOBS[job_id] = job
            except (TypeError, ValueError):
                continue
    except (OSError, sqlite3.Error):
        pass
    # Restore job metadata from R2 after a Render restart/redeploy.
    for job_id, job in _r2_saved_jobs():
        try:
            if job.get("status") in ("queued", "running"):
                job.update(status="error", message="Server restart ဖြစ်သွားသောကြောင့် အရင်အလုပ် ဆက်မလုပ်နိုင်ပါ။ Video ကို ပြန်တင်ပြီး Generate Final Video ကို ထပ်နှိပ်ပါ။", finished=time.time())
                _r2_save_job(job_id, job)
            JOBS[job_id] = job
            with _db_connect() as connection:
                connection.execute("INSERT INTO jobs(id, payload) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (job_id, json.dumps(job, ensure_ascii=False)))
        except Exception:
            continue

_load_jobs()

HTML = r"""
<!doctype html>
<html lang="my">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>One Click Movie Recap</title>
<style>
:root{color-scheme:dark;--page:#080d18;--page2:#0c1424;--text:#f4f7ff;--panel:#101a2c;--panel2:#142139;--border:#263650;--muted:#b8c5d9;--muted2:#8192ad;--field:#0a1323;--field-border:#344865;--button:#5d8cff;--button-text:#071226;--status:#0a1424;--accent:#70a5ff;--accent2:#83e2d0;--shadow:0 18px 50px #0003}
*{box-sizing:border-box}
html{min-height:100%;background:var(--page)}
body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#1a3154 0,transparent 48%),var(--page);color:var(--text);font-family:"Noto Sans Myanmar","Padauk",sans-serif;transition:background .2s,color .2s}
body.light{color-scheme:light;--page:#f2f5fb;--page2:#eaf0fa;--text:#17233a;--panel:#fff;--panel2:#f7f9fe;--border:#dce4f0;--muted:#465772;--muted2:#70809a;--field:#fbfcff;--field-border:#cbd6e7;--button:#315fe6;--button-text:#fff;--status:#f0f4fb;--accent:#315fe6;--accent2:#168a80;--shadow:0 18px 50px #243c6412;background:radial-gradient(ellipse at 50% -20%,#dbe7ff 0,transparent 48%),var(--page)}
main{width:min(100% - 36px,1000px);margin:0 auto;padding:24px 0 42px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:0 0 20px;border-bottom:1px solid var(--border)}
.topbar>div:first-child{gap:12px!important}.topbar h1{font-size:clamp(18px,2.4vw,25px);letter-spacing:-.6px;line-height:1.25;margin:0;font-weight:850}.topbar h1::first-letter{color:var(--accent)}
.topbar>div:last-child{flex-shrink:0}.topbar>div:last-child>#themeToggle,.topbar>div:last-child>#openSettings{padding:5px 8px;font-size:10px;min-height:28px;border-radius:8px;line-height:1.1}.settings-btn{width:auto;margin:0;padding:9px 13px;white-space:nowrap;background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:11px;font-size:12px}
main>.muted:not(.small){font-size:14px;margin:15px 0 20px;color:var(--muted)}
.panel{background:linear-gradient(155deg,var(--panel),color-mix(in srgb,var(--panel) 91%,var(--accent) 9%));border:1px solid var(--border);border-radius:22px;padding:clamp(16px,3vw,30px);margin:16px 0;box-shadow:var(--shadow)}
h1{font-size:24px;margin:0 0 6px}h2{font-size:19px;letter-spacing:-.25px;margin:0 0 12px}p{line-height:1.6;color:var(--muted);margin:9px 0}.muted{font-size:12px;color:var(--muted2)}
label{display:block;margin:12px 0 6px;font-weight:700;font-size:13px;letter-spacing:.05px}
input,select{width:100%;min-height:42px;padding:10px 12px;border-radius:11px;border:1px solid var(--field-border);background:var(--field);color:var(--text);font:inherit;font-size:14px;outline:none;transition:border-color .18s,box-shadow .18s}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent)}
input[type=range]{min-height:20px;padding:0;accent-color:var(--accent);background:transparent;border:0;box-shadow:none}
input[type=checkbox]{width:17px;height:17px;min-height:0;vertical-align:middle;accent-color:var(--accent)}
input[type=file]{padding:8px;min-height:48px;border-style:dashed;background:color-mix(in srgb,var(--field) 86%,var(--accent) 14%)}
.option-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.edit-tools{border:1px solid var(--border);border-radius:15px;padding:12px 15px;margin-top:18px;background:color-mix(in srgb,var(--panel2) 72%,transparent)}
.edit-tools summary{cursor:pointer;font-size:14px;font-weight:800;padding:7px 2px;list-style-position:inside}.edit-tools summary::marker{color:var(--accent)}.edit-adjustments{padding-top:12px}.edit-tools h3{font-size:15px;margin:2px 0 8px}.edit-tools label{margin:8px 0 5px;font-size:12px}.edit-tools input:not([type=range]),.edit-tools select{padding:9px 10px;font-size:13px;min-height:39px}.edit-tools .preview{margin-top:8px}
.preview-title{font-size:14px;font-weight:800;margin:8px 0;color:var(--text)}.preview-hint{font-size:12px;margin:9px 0}
.preview{position:relative;overflow:hidden;touch-action:pan-y;border-radius:16px;background:#05070d;aspect-ratio:16/9;margin-top:12px;max-height:70vh;width:100%;border:1px solid #31435e;box-shadow:0 10px 30px #0003}
.preview video{width:100%;height:100%;object-fit:contain;transition:filter .15s,transform .15s}
.playback-controls{display:flex;align-items:center;gap:10px;margin-top:10px;padding:9px 11px;background:var(--panel2);border:1px solid var(--border);border-radius:12px}
.playback-controls button{width:auto;min-width:84px;margin:0;padding:8px 12px;font-size:12px}.playback-controls input{flex:1;min-width:0;padding:0;min-height:18px}.playback-time{font-size:11px;color:var(--muted2);white-space:nowrap;font-variant-numeric:tabular-nums}
.preview-overlay{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;display:block;padding:0;text-align:center;overflow:visible;z-index:4}
.preview-subtitle{position:absolute;left:50%;top:88%;transform:translate(-50%,-50%);color:#fff;text-shadow:0 2px 4px #000,1px 1px 2px #000;font-weight:700;white-space:pre-wrap;max-width:94%;width:max-content;pointer-events:auto;touch-action:none;cursor:move;user-select:none;font-size:clamp(14px,3.2cqw,36px);line-height:1.15;overflow:visible}
#autoBlurBand{position:absolute;left:0;right:0;bottom:0;height:22%;background:#ffffff22;backdrop-filter:blur(9px);-webkit-backdrop-filter:blur(9px);z-index:3;pointer-events:none;transition:opacity .15s}
.preview-logo{position:absolute;z-index:5;left:90%;top:8%;transform:translate(-50%,-50%);color:#fff;background:#0008;padding:3px 6px;border-radius:5px;font-size:12px;display:flex;align-items:center;justify-content:center;overflow:visible;pointer-events:auto;touch-action:none;cursor:move;user-select:none;min-width:12px;min-height:12px}
.preview-logo img{display:block;width:auto;height:auto;max-width:100%;max-height:100%;object-fit:contain;flex:0 1 auto}
.preview-logo .resize-handle,.preview-subtitle .resize-handle{position:absolute;right:-8px;bottom:-8px;width:17px;height:17px;border:2px solid white;border-radius:50%;background:#718cff;box-shadow:0 1px 5px #000;touch-action:none;cursor:nwse-resize}
.preview-subtitle{overflow:visible}.preview-text{position:absolute;top:8px;left:8px;color:white;text-shadow:1px 1px 3px #000;font-size:12px}
button,.download{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid transparent;border-radius:11px;background:var(--button);color:var(--button-text);font-weight:800;padding:12px 16px;font-size:14px;text-decoration:none;cursor:pointer;width:100%;margin-top:12px;min-height:44px;transition:transform .15s,filter .15s,box-shadow .15s}
button:hover,.download:hover{filter:brightness(1.07);box-shadow:0 5px 18px color-mix(in srgb,var(--button) 20%,transparent)}button:active{transform:translateY(1px)}button:disabled{opacity:.55;cursor:wait;box-shadow:none}
.key-actions{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.status{padding:14px;border-radius:12px;background:var(--status);border:1px solid var(--border);white-space:pre-wrap;overflow-wrap:anywhere}

.settings-wrap{position:fixed;z-index:20;inset:0;background:#020611b8;backdrop-filter:blur(8px);display:flex;justify-content:flex-end;align-items:stretch;padding:14px}
.settings-card{width:min(420px,100%);max-height:100%;overflow:auto;background:var(--panel);border:1px solid var(--border);border-radius:20px;padding:22px;box-shadow:0 24px 80px #0007}
.settings-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.settings-head h2{margin:0}.close-btn{width:auto;margin:0;padding:7px 11px;background:var(--panel2);border-color:var(--border);color:var(--text);min-height:36px}
.settings-card label{margin-top:16px}.key-input-row{display:flex;gap:7px;align-items:stretch}.key-input-row input{flex:1;min-width:0}.key-input-row .toggle-key{width:82px;flex:0 0 82px;margin:0;padding:6px;font-size:12px;background:var(--panel2);border-color:var(--border);color:var(--text)}
.settings-card .muted{margin-top:10px}.settings-card a{color:var(--accent)}
.note{border-left:3px solid #e8b85b;padding-left:12px}.small{font-size:13px}.hidden{display:none}.wizard-step{display:none}.wizard-step.active{display:block}.wizard-step[data-step="2"]{display:none!important}
.step-head{display:flex;align-items:center;gap:13px;margin:2px 0 22px}.step-number{display:inline-flex;align-items:center;justify-content:center;min-width:38px;height:38px;border-radius:13px;background:linear-gradient(145deg,var(--button),#78a8ff);color:var(--button-text);font-weight:900;box-shadow:0 6px 16px color-mix(in srgb,var(--button) 24%,transparent)}
.step-title{font-size:19px;font-weight:850;letter-spacing:-.3px}.step-desc{font-size:12px;color:var(--muted2);margin-top:4px}
.wizard-nav{display:flex;gap:10px;margin-top:24px;padding-top:16px;border-top:1px solid var(--border)}.wizard-nav button{width:100%;margin-top:0}.wizard-nav .back{background:var(--panel2);border-color:var(--border);color:var(--text)}
.stepper-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;margin:0 0 12px}.stepper-item{display:flex;flex-direction:row;align-items:center;justify-content:center;gap:5px;min-width:0;padding:6px 3px;background:var(--panel2);border:1px solid var(--border);border-radius:9px;color:var(--muted2);cursor:pointer;margin:0;font-size:10px;line-height:1.15}.stepper-item .stepper-circle{display:flex;flex:0 0 22px;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;font-size:11px;font-weight:900;border:1px solid var(--border);background:var(--panel);color:var(--text)}.stepper-label{min-width:0;overflow-wrap:anywhere;text-align:center}.stepper-item.active{border-color:var(--button);background:color-mix(in srgb,var(--button) 10%,var(--panel2));color:var(--text)}.stepper-item.active .stepper-circle,.stepper-item.done .stepper-circle{background:var(--button);color:var(--button-text);border-color:var(--button)}.stepper-item.done{color:var(--text)}.step-progress{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:0 0 26px}.step-progress span{height:5px;border-radius:8px;background:var(--border);transition:background .2s}.step-progress span.on{background:linear-gradient(90deg,var(--button),var(--accent2))}.loading-percent{font-size:32px;font-weight:900;text-align:center;font-variant-numeric:tabular-nums;margin:8px 0 12px}.loading-track{height:8px;background:var(--border);border-radius:99px;overflow:hidden;margin-bottom:12px}.loading-bar{height:100%;border-radius:99px;background:linear-gradient(90deg,var(--button),var(--accent2));transition:width .35s ease}.status{display:block;font-size:13px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0;color:var(--text)}.step-desc,main>p.muted,#jobform .muted,#previewHint,#previewDeviceHint,.preview-title{display:none!important}
a{color:#9fbaff}
@media(min-width:760px){main{padding-top:30px}.topbar{padding-bottom:22px}.panel{margin-top:20px}.wizard-step[data-step="0"]{max-width:760px}.wizard-step[data-step="1"]{max-width:760px}.wizard-step[data-step="3"]{max-width:760px}.preview{max-height:560px}}
@media(max-width:600px){main{width:100%;padding:12px 12px 28px}.topbar{gap:8px;padding-bottom:15px}.topbar>div:first-child{gap:8px!important}.topbar h1{font-size:16px;letter-spacing:-.35px}.settings-btn{padding:8px 9px;font-size:11px;border-radius:9px}.panel{padding:15px 13px;border-radius:17px;margin:12px 0}.stepper-cards{gap:4px;margin-bottom:10px}.stepper-item{flex-direction:column;gap:3px;font-size:8px;padding:5px 2px;border-radius:8px}.stepper-item .stepper-circle{flex-basis:21px;width:21px;height:21px;font-size:10px}.step-progress{margin-bottom:20px}.step-head{gap:10px;margin-bottom:18px}.step-number{min-width:34px;height:34px;border-radius:11px}.step-title{font-size:17px}.step-desc{font-size:11px}.option-grid{gap:10px}.wizard-nav{gap:8px}.wizard-nav button{padding:11px 8px;font-size:12px}.preview{border-radius:12px}.playback-controls{gap:7px;padding:7px}.playback-controls button{min-width:65px;padding:7px 8px}.playback-time{font-size:10px}.settings-wrap{padding:8px}.settings-card{padding:17px;border-radius:16px}.edit-tools{padding:10px}.edit-tools summary{font-size:13px}}
@media(max-width:380px){.topbar h1{font-size:14px}.settings-btn{padding:7px;font-size:10px}.option-grid{grid-template-columns:1fr}.wizard-nav{flex-direction:column-reverse}.wizard-nav button{width:100%}.playback-time{display:none}}

/* Live Edit responsive layouts: compact phones, standard phones, large phones, tablets and landscape. */
.preview-device-hint{font-size:11px;color:var(--muted2);margin:7px 2px 0}
main{padding-left:max(0px,env(safe-area-inset-left));padding-right:max(0px,env(safe-area-inset-right))}
.preview{width:100%;max-width:100%;max-height:70vh;max-height:70dvh;overflow:hidden}
.preview video{display:block;max-width:100%;max-height:100%;object-fit:contain}
@media(max-width:360px){
 main{padding:10px 9px calc(22px + env(safe-area-inset-bottom))}
 .panel{padding:12px 10px;border-radius:14px}
 .preview{border-radius:9px;max-height:58vh;max-height:58dvh}
 .preview-subtitle{max-width:96%;font-size:clamp(12px,4cqw,24px)}
 .preview-logo{max-width:34%;font-size:10px}
 .playback-controls{gap:5px;padding:6px}
 .playback-controls button{min-width:56px;padding:6px;font-size:11px}
}
@media(min-width:361px) and (max-width:430px){
 .preview{max-height:64vh;max-height:64dvh}
 .preview-subtitle{max-width:95%;font-size:clamp(13px,3.7cqw,29px)}
 .preview-logo{max-width:38%}
}
@media(min-width:431px) and (max-width:600px){
 main{padding-left:14px;padding-right:14px}
 .preview{max-height:68vh;max-height:68dvh}
 .preview-subtitle{font-size:clamp(14px,3.3cqw,32px)}
}
@media(orientation:landscape) and (max-height:500px){
 main{padding-top:8px;padding-bottom:calc(12px + env(safe-area-inset-bottom))}
 .preview{max-height:52vh;max-height:52dvh}
 .preview-subtitle{font-size:clamp(12px,2.8cqw,24px)}
 .settings-wrap{padding:5px}
}
@media(min-width:760px){
 .preview{max-height:68vh;max-height:68dvh}
 .preview-device-hint{font-size:12px}
}

/* Hide subtitle and logo UI; keep compatibility inputs hidden for the editor script. */
#previewLogo,#previewSubtitle{display:none!important}
.edit-adjustments .option-grid:has(#subtitlesOn),.edit-adjustments .option-grid:has(#logoText),.edit-adjustments:has(#logoImage) > label[for="logoImage"],#logoImage{display:none!important}
</style>
</head>
<body><main>
<div class="topbar"><div style="display:flex;align-items:center;gap:8px;min-width:0"><h1>🎬 One Click Movie Recap</h1></div><div style="display:flex;flex-direction:column;gap:6px;align-items:stretch"><button class="settings-btn" id="themeToggle" type="button" aria-label="Toggle light and dark theme">☀️ Light</button><button class="settings-btn" id="openSettings" type="button">⚙️ Settings</button><div style="display:grid;grid-template-columns:1fr 1fr;gap:6px"><button class="settings-btn" id="newJobBtn" type="button">＋ New</button><button class="settings-btn" id="openDashboard" type="button" aria-label="Open Dashboard">▦ Dashboard</button></div></div></div>
<p class="muted">Video ထည့်ပါ → Recap MP4 ထုတ်ပါ</p>

</section>



<div class="settings-wrap hidden" id="settingsWrap"><div class="settings-card"><div class="settings-head"><h2>⚙️ API Keys</h2><button class="close-btn" id="closeSettings" type="button">✕</button></div>
<label for="geminiKey">Gemini API Key</label>
<div class="key-input-row"><input id="geminiKey" type="password" autocomplete="off" placeholder="Gemini API Key ထည့်ပါ"><button class="toggle-key" id="toggleGeminiKey" type="button" aria-label="Show Gemini API key">ကြည့်ရန်</button></div>
<label for="groqKey">Groq API Key</label>
<div class="key-input-row"><input id="groqKey" type="password" autocomplete="off" placeholder="Groq API Key ထည့်ပါ"><button class="toggle-key" id="toggleGroqKey" type="button" aria-label="Show Groq API key">ကြည့်ရန်</button></div>
<div class="key-actions"><button id="saveKeys" type="button">Save Keys</button><button id="clearKeys" type="button" style="background:#35415a;color:#fff">Clear</button></div>
<p id="keyStatus" class="muted small" role="status">Key မသိမ်းရသေးပါ။</p>
<p class="muted small"><a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">Gemini Key ↗</a> · <a href="https://console.groq.com/keys" target="_blank" rel="noopener">Groq Key ↗</a></p>
</div></div>
<div class="panel">
<form id="jobform">
<nav class="stepper-cards" aria-label="Recap steps"><button type="button" class="stepper-item active" data-goto-step="0"><span class="stepper-circle">1</span><span class="stepper-label">Video Upload</span></button><button type="button" class="stepper-item" data-goto-step="1"><span class="stepper-circle">2</span><span class="stepper-label">Voice &amp; Language</span></button><button type="button" class="stepper-item" data-goto-step="3"><span class="stepper-circle">3</span><span class="stepper-label">Final Video</span></button></nav>
<div class="step-progress" aria-label="Progress"><span class="on"></span><span></span><span></span></div>
<section class="wizard-step active" data-step="0"><div class="step-head"><span class="step-number">1</span><div><div class="step-title">Video Upload</div><div class="step-desc">ဖုန်းထဲက Video သို့မဟုတ် Direct URL ထည့်ပါ</div></div></div>
<label for="url">Option 1 — Direct video URL</label>
<input id="url" name="url" type="url" placeholder="https://example.com/video.mp4">
<p class="muted small">URL သို့မဟုတ် ဖုန်းထဲက Video တစ်ခုကို ရွေးပါ။</p>
<label for="videoFile">Option 2 — ဖုန်းထဲက Video Upload (MP4 / MOV / WEBM / M4V)</label>
<input id="videoFile" name="video" type="file" multiple accept=".mp4,.mov,.webm,.m4v,video/mp4,video/quicktime,video/webm">\n<p class="muted small" id="multiUploadHint">Video အများအပြားကို တစ်ခါတည်းရွေးနိုင်သည်။ အများဆုံး ၅ ဖိုင်၊ တစ်ဖိုင်လျှင် 80 MB အထိ။ ဖိုင်တစ်ခုစီကို သီးခြား Job အဖြစ် တစ်ပြိုင်နက် Processing လုပ်မည်။</p>
<div class="wizard-nav"><button type="button" class="next-step" data-next="1">Next → Voice</button></div></section>
<section class="wizard-step" data-step="1"><div class="step-head"><span class="step-number">2</span><div><div class="step-title">Voice & Language</div><div class="step-desc">Recap ဘာသာနဲ့ AI အသံ ရွေးပါ</div></div></div>
<div class="option-grid"><div><label for="language">Recap ဘာသာ</label><select id="language" name="language"><option value="my">မြန်မာ</option><option value="en">English</option></select></div><div><label for="voice">Voice</label><select id="voice" name="voice"><option value="my-MM-NilarNeural">မြန်မာ Nilar (မိန်းမသံ)</option><option value="my-MM-ThihaNeural">မြန်မာ Thiha (ယောက်ျားသံ)</option></select></div><div><label for="voiceSpeed">AI Voice ပြောနှုန်း</label><select id="voiceSpeed" name="voice_speed"><option value="100">ပုံမှန် (100%)</option><option value="110">နည်းနည်းမြန် (110%)</option><option value="120" selected>မြန်မြန် (120%)</option><option value="130">အမြန်ဆုံး (130%)</option></select><p class="muted small">Default ကို ၂၀% မြန်အောင်ထားပြီး Voice ကို Video အရှည်အတိုင်း အတင်းမနှေးစေပါ။</p></div><div><label for="subtitlePace">SRT စာတန်းပြောနှုန်း (၃ စက္ကန့်ခန့်)</label><select id="subtitlePace" name="subtitle_pace"><option value="slow">ဖြည်းဖြည်း၊ ရှင်းရှင်း — ၁၀–၁၅ လုံး</option><option value="normal" selected>ပုံမှန် Recap — ၁၅–၂၅ လုံး</option><option value="fast">မြန်မြန်ပြော — ၂၅–၃၅ လုံး</option></select></div></div>

<div style="margin-top:14px;padding:14px;border:1px solid var(--border);border-radius:13px;background:var(--panel2)">



</div>
<div class="wizard-nav"><button type="button" class="back back-step" data-back="0">← Back</button><button type="button" class="next-step" data-next="3">Next → Final Video</button></div></section>
<section class="wizard-step" data-step="2"><div class="step-head"><span class="step-number">3</span><div><div class="step-title">Edit Tools</div><div class="step-desc">Original Subtitle Auto Blur နဲ့ Video Effect ချိန်ပါ</div></div></div>
<div class="preview-title">👁️ Live Edit Preview</div><div class="preview" id="preview"><video id="previewVideo" playsinline class="hidden"></video><div class="preview-logo" id="previewLogo"><img id="previewLogoImage" alt="Logo preview"><span id="previewLogoText">My Recap</span></div><div class="preview-text" id="previewText"></div><div id="autoBlurBand" aria-hidden="true"></div><div class="preview-overlay"><div class="preview-subtitle" id="previewSubtitle">မြန်မာစာတန်းထိုး အစမ်းမြင်ကွင်း<span class="resize-handle" id="subtitleResize" aria-label="Resize subtitle"></span></div></div></div><div class="preview-device-hint" id="previewDeviceHint">📱 ဖုန်းမျက်နှာပြင်အကျယ်အလိုက် Live Edit ကို အလိုအလျောက်ချိန်ညှိထားသည်။</div><div class="playback-controls"><button id="previewPlay" type="button">▶ Play</button><input id="previewSeek" type="range" min="0" max="1000" value="0" aria-label="Video seek"><span class="playback-time" id="previewTime">0:00 / 0:00</span></div>
<p class="muted preview-hint" id="previewHint">Upload Video ရွေးပါ။ အောက်က Edit Tools ပြောင်းတာနဲ့ ဒီမှာပြောင်းလဲမှုကို ကြည့်နိုင်ပါတယ်။</p>
<details class="edit-tools"><summary class="edit-summary">🎨 Edit Tools — ချိန်ညှိရန် နှိပ်ပါ</summary><div class="edit-adjustments"><div><p class="muted small">Fast Rendering ကို အလိုအလျောက် အသုံးပြုမည်။ Video နဲ့ AI Voice ကို sync လုပ်ပေးပါမည်။</p></div><div><label><input id="autoBlurSubtitles" type="checkbox" checked style="width:auto;margin-right:5px"> 🌫️ Auto Blur Original Subtitles</label></div><div><label><input id="autoEffect" type="checkbox" style="width:auto;margin-right:5px"> ✨ Auto Effect</label></div><div class="option-grid"><div><label><input id="subtitlesOn" type="checkbox" checked style="width:auto;margin-right:5px"> 📝 Subtitles On / Off</label><label for="subSize">စာတန်းအရွယ်အစား</label><input id="subSize" type="range" min="20" max="64" value="40"></div><div><label for="subColor">စာတန်းအရောင်</label><select id="subColor"><option value="&H00FFFFFF">White</option><option value="&H0000FFFF">Yellow</option><option value="&H0000FF00">Green</option><option value="&H00FF8080">Light Blue</option></select></div><div><label for="subPosition">စာတန်းနေရာ</label><select id="subPosition"><option value="bottom">အောက်</option><option value="middle">အလယ်</option><option value="top">အပေါ်</option></select></div><div><label for="subX">စာတန်း X <span id="subXValue">50%</span></label><input id="subX" type="range" min="5" max="95" value="50"></div><div><label for="subY">စာတန်း Y <span id="subYValue">88%</span></label><input id="subY" type="range" min="5" max="95" value="88"></div><div><label for="subWeight">စာတန်းအထူ</label><select id="subWeight"><option value="bold">Bold</option><option value="normal">Normal</option></select></div><div><label for="subOutline">စာတန်းအနားသတ် <span id="subOutlineValue">2</span></label><input id="subOutline" type="range" min="0" max="6" value="2"></div></div>
<div class="option-grid"><div><label><input id="mirror" type="checkbox" style="width:auto;margin-right:5px"> Mirror လှန်</label></div><div><label for="logoText">Logo / Watermark စာသား</label><input id="logoText" maxlength="40" value="My Recap" placeholder="My Recap"></div></div><label for="logoImage">🖼️ Logo ပုံ Upload (PNG/JPG/WEBP, 5 MB အထိ)</label><input id="logoImage" type="file" accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"><div class="option-grid"><div><label for="logoX">Logo X <span id="logoXValue">90%</span></label><input id="logoX" type="range" min="0" max="100" value="90"></div><div><label for="logoY">Logo Y <span id="logoYValue">8%</span></label><input id="logoY" type="range" min="0" max="100" value="8"></div><div><label for="logoW">Logo အရွယ်အစား <span id="logoWValue">4%</span></label><input id="logoW" type="range" min="1" max="12" value="4"></div><div><label for="logoOpacity">Logo Opacity <span id="logoOpacityValue">100%</span></label><input id="logoOpacity" type="range" min="10" max="100" value="100"></div></div></details><p class="muted small">အများဆုံး 80 MB · 10 မိနစ် · Direct video URL</p>
<div class="wizard-nav"><button type="button" class="back back-step" data-back="1">← Back</button><button type="button" class="next-step" data-next="3">Next → Final Video</button></div></section>
<section class="wizard-step" data-step="3"><div class="step-head"><span class="step-number">3</span><div><div class="step-title">Final Video</div><div class="step-desc">သတ်မှတ်ချက်များကို စစ်ပြီး Recap MP4 ထုတ်ပါ</div></div></div><p class="muted small">✓ Video / Voice / Edit Tools အားလုံးပြီးပါပြီ။ Recap ထုတ်ရန် အောက်ကခလုတ်ကို နှိပ်ပါ။</p>
<button id="submit" type="submit">🎬 Generate Final Video</button><div class="wizard-nav"><button type="button" class="back back-step" data-back="1">← Back to Voice</button></div></section>
</form></div>
<div id="result" class="panel hidden"><div id="loadingPercent" class="loading-percent">0%</div><div class="loading-track"><div id="loadingBar" class="loading-bar" style="width:0%"></div></div><div id="status" class="status" aria-live="polite"></div><div id="links"></div></div>
<section id="dashboardPanel" class="panel hidden"><div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap"><div><h2 style="margin:0">▦ Dashboard</h2><p class="muted small">ထုတ်ပြီးသိမ်းထားသော Final Video များကို ပြန်ဖွင့်၊ ဒေါင်းလုဒ်လုပ်နိုင်သည်။</p></div><button type="button" id="dashboardNewBtn" class="settings-btn">＋ New Recap</button></div><div id="savedVideos"><h3>🎬 Final Videos</h3><div id="savedVideosList">သိမ်းထားသော Video များကို စစ်ဆေးနေသည်…</div><p id="storageNote" class="muted small"></p></div></section>
<p class="muted small">Gemini 3.8 Flash · ကိုယ်အသုံးပြုခွင့်ရှိသော ဗီဒီယိုကိုသာ အသုံးပြုပါ။</p>
</main>
<script>
const form=document.getElementById('jobform'),btn=document.getElementById('submit'),result=document.getElementById('result');
const dashboardPanel=document.getElementById('dashboardPanel');
function showDashboard(){
 document.getElementById('jobform').parentElement.classList.add('hidden');
 document.getElementById('result').classList.add('hidden');
 dashboardPanel.classList.remove('hidden');
 loadSavedVideos();
 dashboardPanel.scrollIntoView({behavior:'smooth',block:'start'});
}
function showRecapForm(){
 dashboardPanel.classList.add('hidden');
 const formPanel=document.getElementById('jobform').parentElement;
 formPanel.style.display='';
 formPanel.classList.remove('hidden');
 document.getElementById('result').classList.add('hidden');
 document.getElementById('jobform').parentElement.scrollIntoView({behavior:'smooth',block:'start'});
}
document.getElementById('openDashboard').addEventListener('click',showDashboard);
document.getElementById('dashboardNewBtn').addEventListener('click',showRecapForm);
let statusBox=document.getElementById('status');if(!statusBox){statusBox=document.createElement('div');statusBox.id='status';statusBox.className='status';statusBox.setAttribute('aria-live','polite');(result||document.body).appendChild(statusBox);}const links=document.getElementById('links')||(()=>{const el=document.createElement('div');el.id='links';(result||document.body).appendChild(el);return el;})();const loadingPercent=document.getElementById('loadingPercent')||(()=>{const el=document.createElement('div');el.id='loadingPercent';el.className='loading-percent';el.textContent='0%';(result||document.body).prepend(el);return el;})();const loadingBar=document.getElementById('loadingBar')||(()=>{const track=document.createElement('div');track.className='loading-track';const el=document.createElement('div');el.id='loadingBar';el.className='loading-bar';el.style.width='0%';track.appendChild(el);(result||document.body).insertBefore(track,statusBox);return el;})();function setProgress(value){const p=Math.max(0,Math.min(100,Number(value)||0));loadingPercent.textContent=p+'%';loadingBar.style.width=p+'%';}
document.getElementById('newJobBtn').addEventListener('click',()=>{if(btn.disabled&&!confirm('လက်ရှိလုပ်ဆောင်နေသော Job ကို စောင့်မနေဘဲ စာမျက်နှာအသစ်ဖွင့်မလား။'))return;window.location.reload();});
const geminiInput=document.getElementById('geminiKey'),groqInput=document.getElementById('groqKey'),keyStatus=document.getElementById('keyStatus');
const settingsWrap=document.getElementById('settingsWrap');const themeToggle=document.getElementById('themeToggle');function applyTheme(theme){document.body.classList.toggle('light',theme==='light');themeToggle.textContent=theme==='light'?'🌙 Dark':'☀️ Light';try{localStorage.setItem('oneclick_theme',theme);}catch(e){}}let savedTheme='dark';try{savedTheme=localStorage.getItem('oneclick_theme')||'dark';}catch(e){}applyTheme(savedTheme);themeToggle.addEventListener('click',()=>applyTheme(document.body.classList.contains('light')?'dark':'light'));document.getElementById('openSettings').addEventListener('click',()=>settingsWrap.classList.remove('hidden'));function bindKeyToggle(buttonId,input){const toggle=document.getElementById(buttonId);toggle.addEventListener('click',()=>{const showing=input.type==='text';input.type=showing?'password':'text';toggle.textContent=showing?'ကြည့်ရန်':'ဖျောက်ရန်';toggle.setAttribute('aria-label',(showing?'Show':'Hide')+' API key');});}bindKeyToggle('toggleGeminiKey',geminiInput);bindKeyToggle('toggleGroqKey',groqInput);document.getElementById('closeSettings').addEventListener('click',()=>settingsWrap.classList.add('hidden'));settingsWrap.addEventListener('click',e=>{if(e.target===settingsWrap)settingsWrap.classList.add('hidden');});
const previewVideo=document.getElementById('previewVideo'),previewLogo=document.getElementById('previewLogo'),previewText=document.getElementById('previewText'),previewSubtitle=document.getElementById('previewSubtitle');const previewPlay=document.getElementById('previewPlay'),previewSeek=document.getElementById('previewSeek'),previewTime=document.getElementById('previewTime');function timeLabel(s){if(!Number.isFinite(s)||s<0)return '--:--';return Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0')}function syncPlayback(){const d=previewVideo.duration;const validDuration=Number.isFinite(d)&&d>0;previewSeek.value=validDuration?Math.round(previewVideo.currentTime/d*1000):0;previewTime.textContent=timeLabel(Number.isFinite(previewVideo.currentTime)?previewVideo.currentTime:0)+' / '+timeLabel(validDuration?d:NaN);previewPlay.textContent=previewVideo.paused?'▶ Play':'❚❚ Pause';}function resolvePreviewDuration(){if(previewVideo.duration===Infinity){const finish=()=>{if(Number.isFinite(previewVideo.duration)&&previewVideo.duration>0){previewVideo.removeEventListener('timeupdate',finish);try{previewVideo.currentTime=0}catch(_){}syncPlayback();}};previewVideo.addEventListener('timeupdate',finish);try{previewVideo.currentTime=1e101}catch(_){previewVideo.removeEventListener('timeupdate',finish);}}else syncPlayback();}previewPlay.addEventListener('click',()=>{if(previewVideo.paused)previewVideo.play().catch(()=>{});else previewVideo.pause();syncPlayback()});previewSeek.addEventListener('input',()=>{if(previewVideo.duration)previewVideo.currentTime=(+previewSeek.value/1000)*previewVideo.duration});['timeupdate','durationchange','loadedmetadata','loadeddata','canplay','play','pause','ended'].forEach(ev=>previewVideo.addEventListener(ev,()=>{if(ev==='loadedmetadata'||ev==='durationchange')resolvePreviewDuration();else syncPlayback();}));
function updatePreview(){const size=+document.getElementById('subSize').value,frameHeight=previewVideo.videoHeight||720;['subX','subY','subOutline','logoX','logoY','logoOpacity','logoW'].forEach(id=>{const el=document.getElementById(id),out=document.getElementById(id+'Value');if(out)out.textContent=el.value+(id==='subOutline'?'':'%')});const autoEffect=document.getElementById('autoEffect').checked;previewVideo.style.filter=autoEffect?'contrast(1.04) saturate(1.06)':'';const autoBlur=document.getElementById('autoBlurSubtitles').checked,autoBlurBand=document.getElementById('autoBlurBand');autoBlurBand.style.display=autoBlur?'block':'none';previewVideo.style.transform=document.getElementById('mirror').checked?'scaleX(-1)':'';document.getElementById('previewLogoText').textContent=document.getElementById('logoText').value;previewLogo.style.left=document.getElementById('logoX').value+'%';previewLogo.style.top=document.getElementById('logoY').value+'%';const frameWidth=previewVideo.videoWidth||1280,boxWidth=previewVideo.clientWidth||800,boxHeight=previewVideo.clientHeight||450;const frameScale=Math.min(boxWidth/frameWidth,boxHeight/(previewVideo.videoHeight||720));const logoImg=document.getElementById('previewLogoImage'),hasLogoImage=!!logoImg.getAttribute('src');if(hasLogoImage){const naturalW=logoImg.naturalWidth||300,naturalH=logoImg.naturalHeight||150;const autoW=Math.max(24,(+document.getElementById('logoW').value/100)*frameWidth*frameScale);const autoH=autoW*naturalH/naturalW;previewLogo.style.width=autoW+'px';previewLogo.style.height=autoH+'px';previewLogo.style.minWidth='0';previewLogo.style.minHeight='0';previewLogo.style.maxWidth='none';previewLogo.style.maxHeight='none';previewLogo.style.aspectRatio='auto';previewLogo.style.padding='0';previewLogo.style.background='transparent';logoImg.style.width='100%';logoImg.style.height='100%';logoImg.style.maxWidth='none';logoImg.style.maxHeight='none';logoImg.style.objectFit='contain';previewLogo.style.fontSize='12px';}else{previewLogo.style.aspectRatio='auto';previewLogo.style.padding='3px 6px';previewLogo.style.background='#0008';logoImg.style.width='auto';logoImg.style.height='auto';previewLogo.style.width='max-content';previewLogo.style.height='max-content';previewLogo.style.fontSize=(Math.max(12,Math.round(frameHeight*0.18*0.12))*frameScale)+'px';}previewLogo.style.opacity=+document.getElementById('logoOpacity').value/100;previewLogo.style.display=(document.getElementById('logoText').value||hasLogoImage)?'flex':'none';previewText.textContent='';previewText.style.left='50%';previewText.style.top=(12/(previewVideo.videoHeight||720)*100)+'%';previewText.style.transform='translateX(-50%)';previewText.style.fontSize=(22*frameScale)+'px';previewText.style.color='#fff';previewText.style.textShadow='-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000';const previewHeight=previewVideo.clientHeight||450;previewSubtitle.style.fontSize=Math.max(12,Math.min(36,size*(previewVideo.videoHeight?Math.min((previewVideo.clientWidth||800)/(previewVideo.videoWidth||1280),(previewVideo.clientHeight||450)/frameHeight):1)))+'px';previewSubtitle.style.fontWeight=document.getElementById('subWeight').value;previewSubtitle.style.left=document.getElementById('subX').value+'%';previewSubtitle.style.top=document.getElementById('subY').value+'%';previewSubtitle.style.textShadow='0 0 '+(+document.getElementById('subOutline').value*2)+'px #000,1px 1px '+document.getElementById('subOutline').value+'px #000';previewSubtitle.style.color=({'&H00FFFFFF':'#fff','&H0000FFFF':'#ffff00','&H0000FF00':'#00ff00','&H00FF8080':'#8080ff'})[document.getElementById('subColor').value]||'#fff';previewSubtitle.style.top=document.getElementById('subY').value+'%';}
document.getElementById('subPosition').addEventListener('change',()=>{const p=document.getElementById('subPosition').value;document.getElementById('subY').value=p==='top'?20:(p==='middle'?50:88);updatePreview();});
['autoBlurSubtitles','subSize','subColor','subPosition','subX','subY','subWeight','subOutline','mirror','logoText','logoX','logoY','logoOpacity','autoEffect'].forEach(id=>{const el=document.getElementById(id);if(el)el.addEventListener('input',updatePreview);});
document.getElementById('logoImage').addEventListener('change',e=>{const f=e.target.files[0],img=document.getElementById('previewLogoImage');if(img.dataset.url)URL.revokeObjectURL(img.dataset.url);img.dataset.url='';img.removeAttribute('src');if(f){img.dataset.url=URL.createObjectURL(f);img.src=img.dataset.url;img.style.display='block';document.getElementById('previewLogoText').style.display='none';}else{img.style.display='none';document.getElementById('previewLogoText').style.display='';}updatePreview();});
function bindDrag(element,xControl,yControl,resizeHandle,sizeControl,min,max){
 let drag=null;
 const preview=document.getElementById('preview');
 const stop=()=>{drag=null;};
 element.addEventListener('pointerdown',e=>{
  if(e.target===resizeHandle)return;
  if(e.button!==undefined&&e.button!==0)return;
  const box=preview.getBoundingClientRect();
  if(!box.width||!box.height)return;
  drag={mode:'move',pointerId:e.pointerId,px:e.clientX,py:e.clientY,x:+document.getElementById(xControl).value,y:+document.getElementById(yControl).value,box};
  try{element.setPointerCapture(e.pointerId);}catch(_){}
  e.preventDefault();
 });
 document.addEventListener('pointermove',e=>{
  if(!drag||drag.pointerId!==e.pointerId||drag.mode!=='move')return;
  const dx=(e.clientX-drag.px)/drag.box.width*100;
  const dy=(e.clientY-drag.py)/drag.box.height*100;
  const x=document.getElementById(xControl),y=document.getElementById(yControl);
  x.value=String(Math.max(Number(x.min||0),Math.min(Number(x.max||100),drag.x+dx)));
  y.value=String(Math.max(Number(y.min||0),Math.min(Number(y.max||100),drag.y+dy)));
  updatePreview();e.preventDefault();
 },{passive:false});
 document.addEventListener('pointerup',e=>{if(drag&&drag.pointerId===e.pointerId)stop();});
 document.addEventListener('pointercancel',e=>{if(drag&&drag.pointerId===e.pointerId)stop();});
 if(resizeHandle&&sizeControl){
  resizeHandle.addEventListener('pointerdown',e=>{
   e.preventDefault();e.stopPropagation();
   const box=preview.getBoundingClientRect();if(!box.width||!box.height)return;
   drag={mode:'resize',pointerId:e.pointerId,px:e.clientX,py:e.clientY,w:+document.getElementById(sizeControl).value,box};
   try{resizeHandle.setPointerCapture(e.pointerId);}catch(_){}
  });
  document.addEventListener('pointermove',e=>{
   if(!drag||drag.pointerId!==e.pointerId||drag.mode!=='resize')return;
   const delta=(e.clientX-drag.px)/drag.box.width*100;
   document.getElementById(sizeControl).value=String(Math.max(min,Math.min(max,drag.w+delta)));
   updatePreview();e.preventDefault();
  },{passive:false});
 }
}
bindDrag(previewLogo,'logoX','logoY',null,null,0,0);bindDrag(previewSubtitle,'subX','subY',document.getElementById('subtitleResize'),'subSize',14,36);

// Removed music controls; no listener needed.
let currentStep=0;function showStep(n){currentStep=Math.max(0,Math.min(3,n));document.querySelectorAll('.wizard-step').forEach(el=>el.classList.toggle('active',Number(el.dataset.step)===currentStep));document.querySelectorAll('.step-progress span').forEach((el,i)=>el.classList.toggle('on',i<=(currentStep===3?2:currentStep)));document.querySelectorAll('.stepper-item').forEach(el=>{const step=Number(el.dataset.gotoStep);el.classList.toggle('active',step===currentStep);el.classList.toggle('done',step<currentStep);el.setAttribute('aria-current',step===currentStep?'step':'false');});window.scrollTo(0,0);}document.querySelectorAll('.stepper-item').forEach(el=>el.addEventListener('click',()=>{const target=Number(el.dataset.gotoStep);showStep(target);}));document.querySelectorAll('.next-step').forEach(el=>el.addEventListener('click',()=>{showStep(Number(el.dataset.next));}));document.querySelectorAll('.back-step').forEach(el=>el.addEventListener('click',()=>showStep(Number(el.dataset.back))));
previewVideo.addEventListener('loadedmetadata',()=>{resolvePreviewDuration();if(previewVideo.videoWidth>0&&previewVideo.videoHeight>0){const preview=document.getElementById('preview'),ratio=previewVideo.videoWidth/previewVideo.videoHeight;preview.style.aspectRatio=previewVideo.videoWidth+'/'+previewVideo.videoHeight;preview.style.width='min(100%, '+Math.round(window.innerHeight*0.64*ratio)+'px)';preview.style.maxHeight=Math.round(window.innerHeight*0.64)+'px';updatePreview();}});function loadPreviewSource(){const preview=document.getElementById('preview');preview.style.aspectRatio='16/9';preview.style.width='100%';preview.style.maxHeight='64dvh';const file=document.getElementById('videoFile').files[0],url=document.getElementById('url').value.trim();if(previewVideo.dataset.objectUrl){URL.revokeObjectURL(previewVideo.dataset.objectUrl);previewVideo.dataset.objectUrl='';}previewVideo.pause();previewVideo.removeAttribute('src');if(file){previewVideo.dataset.objectUrl=URL.createObjectURL(file);previewVideo.src=previewVideo.dataset.objectUrl;previewVideo.classList.remove('hidden');previewPlay.classList.remove('hidden');previewSeek.classList.remove('hidden');previewVideo.load();document.getElementById('previewHint').textContent='Upload Video ကို တိုက်ရိုက်ပြထားပါတယ်။ မြန်မာစာတန်းထိုး အစမ်းမြင်ကွင်း ကို ဖုန်းမျက်နှာပြင်နဲ့ကိုက်ညီအောင် ချိန်ထားပါတယ်။ Subtitle/Logo ကို ဖိဆွဲရွှေ့နိုင်ပါတယ်။';}else if(url&&/\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(url)){previewVideo.src=url;previewVideo.classList.remove('hidden');previewPlay.classList.remove('hidden');previewSeek.classList.remove('hidden');previewVideo.load();document.getElementById('previewHint').textContent='URL Preview — direct video link ဖြစ်ရပါမယ်။ YouTube/TikTok webpage link များကို browser preview မှာ တိုက်ရိုက်မဖွင့်နိုင်နိုင်ပါ။';}else{previewVideo.classList.add('hidden');previewPlay.classList.add('hidden');previewSeek.classList.add('hidden');document.getElementById('previewHint').textContent='Upload Video ရွေးပါ သို့မဟုတ် တိုက်ရိုက် MP4 URL ထည့်ပါ။';}syncPlayback();updatePreview();}document.getElementById('videoFile').addEventListener('change',loadPreviewSource);document.getElementById('url').addEventListener('change',loadPreviewSource);document.getElementById('url').addEventListener('input',()=>{if(!document.getElementById('videoFile').files[0]){clearTimeout(window.previewUrlTimer);window.previewUrlTimer=setTimeout(loadPreviewSource,700);}});
document.getElementById('subtitlesOn').addEventListener('change',()=>{document.getElementById('previewSubtitle').style.display=document.getElementById('subtitlesOn').checked?'':'none';});document.getElementById('language').addEventListener('change',()=>{const v=document.getElementById('voice');if(!v.value.startsWith('my-MM-'))v.value='my-MM-NilarNeural';});updatePreview();
function loadSavedKeys(){try{geminiInput.value=localStorage.getItem('oneclick_gemini_key')||'';groqInput.value=localStorage.getItem('oneclick_groq_key')||'';keyStatus.textContent=(geminiInput.value&&groqInput.value)?'ဒီ Browser ထဲမှာ Key နှစ်ခုလုံး သိမ်းထားသည်။':'Key မသိမ်းရသေးပါ။';}catch(e){keyStatus.textContent='ဒီ Browser မှာ local storage အသုံးပြုမရပါ။';}}
document.getElementById('saveKeys').addEventListener('click',()=>{const g=geminiInput.value.trim(),q=groqInput.value.trim();if(!g||!q){keyStatus.textContent='Gemini နဲ့ Groq Key နှစ်ခုလုံး ထည့်ပါ။';return;}try{localStorage.setItem('oneclick_gemini_key',g);localStorage.setItem('oneclick_groq_key',q);keyStatus.textContent='Key နှစ်ခုလုံးကို ဒီ Browser ထဲမှာ Save လုပ်ပြီးပါပြီ။';}catch(e){keyStatus.textContent='Save မလုပ်နိုင်ပါ။ Browser storage setting ကို စစ်ပါ။';}});
document.getElementById('clearKeys').addEventListener('click',()=>{try{localStorage.removeItem('oneclick_gemini_key');localStorage.removeItem('oneclick_groq_key');}catch(e){}geminiInput.value='';groqInput.value='';keyStatus.textContent='Browser ထဲက Saved Keys ကို ဖျက်ပြီးပါပြီ။';});
loadSavedKeys();
async function submitMultipleVideos(files,geminiKey,groqKey){
 const result=document.getElementById('result'),statusBox=document.getElementById('status'),links=document.getElementById('links'),btn=document.getElementById('submit');
 const recapScript=document.getElementById('recapScript')?.value.trim()||'';
 const options={voice:document.getElementById('voice').value,voice_speed:document.getElementById('voiceSpeed').value,subtitle_pace:document.getElementById('subtitlePace').value,auto_effect:document.getElementById('autoEffect').checked,auto_blur_subtitles:document.getElementById('autoBlurSubtitles').checked,mirror:document.getElementById('mirror').checked,overlay_text:''};
 const jobs=[];
 result.classList.remove('hidden');links.innerHTML='';setProgress(0);btn.disabled=true;
 statusBox.textContent='Video '+files.length+' ခုကို Upload လုပ်ပြီး Processing စတင်နေသည်…';
 try{
  for(let i=0;i<files.length;i++){
   const file=files[i],body=new FormData();
   body.append('url','');body.append('language',document.getElementById('language').value);
   body.append('gemini_api_key',geminiKey);body.append('groq_api_key',groqKey);
   Object.entries(options).forEach(([k,v])=>body.append(k,String(v)));
   body.append('video',file);body.append('recap_script',recapScript);
   statusBox.textContent='Upload လုပ်နေသည် ('+(i+1)+'/'+files.length+'): '+file.name;
   const res=await fetch('/api/jobs',{method:'POST',body});
   const data=await res.json();
   if(!res.ok)throw new Error(file.name+': '+(data.error||'Job စတင်၍မရပါ'));
   jobs.push({id:data.id,title:file.name,status:'queued',progress:0,message:''});
  }
  await pollMultipleJobs(jobs);
 }catch(err){
  statusBox.textContent='Multiple Upload အမှား: '+err.message+(jobs.length?'\nစတင်ပြီးသော Job များ ဆက်လက်လုပ်ဆောင်နေပါသည်။':'');
  if(jobs.length)pollMultipleJobs(jobs);else btn.disabled=false;
 }
}
async function pollMultipleJobs(jobs){
 const result=document.getElementById('result'),statusBox=document.getElementById('status'),links=document.getElementById('links'),btn=document.getElementById('submit');
 const escapeHtml=value=>String(value||'').replace(/[&<>"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
 async function refresh(){
  let total=0,finished=0;
  for(const job of jobs){
   if(job.status==='done'||job.status==='error') {total+=job.progress||0;finished++;continue;}
   try{
    const res=await fetch('/api/jobs/'+encodeURIComponent(job.id),{cache:'no-store'}),data=await res.json();
    if(res.ok){job.status=data.status||job.status;job.progress=Number(data.progress)||0;job.message=data.message||'';}
    else {job.status='error';job.message=data.error||'Job status မရပါ';}
   }catch(err){job.message='Status စစ်နေသည်…';}
   total+=job.progress||0;if(job.status==='done'||job.status==='error')finished++;
  }
  setProgress(Math.round(total/jobs.length));
  statusBox.textContent='Multiple Processing: '+finished+'/'+jobs.length+' ခု ပြီးဆုံး/ရပ်တန့်ပြီး\\n'+jobs.map((j,i)=>(i+1)+'. '+j.title+' — '+(j.status==='done'?'ပြီးပါပြီ':j.status==='error'?'အမှား':(j.progress||0)+'% '+(j.message||'လုပ်ဆောင်နေသည်'))).join('\n');
  links.innerHTML=jobs.map((j,i)=>'<div style="border:1px solid var(--border);border-radius:12px;padding:12px;margin:10px 0;background:var(--panel2)"><strong>'+escapeHtml((i+1)+'. '+j.title)+'</strong><p class="muted small">'+(j.status==='done'?'ပြီးပါပြီ':j.status==='error'?'မအောင်မြင်ပါ':'Processing '+(j.progress||0)+'%')+'</p>'+(j.status==='done'?'<video controls playsinline preload="metadata" style="display:block;width:100%;max-height:55vh;aspect-ratio:16/9;object-fit:contain;background:#05070d;border-radius:12px;margin:10px 0" src="/api/jobs/'+encodeURIComponent(j.id)+'/preview"></video><a class="download" href="/api/jobs/'+encodeURIComponent(j.id)+'/download">⬇️ Final MP4 ဒေါင်းလုဒ်လုပ်ရန်</a>':j.status==='error'?'<p class="small">'+escapeHtml(j.message)+'</p>':'<p class="muted small">လုပ်ဆောင်နေသည်…</p>')+'</div>').join('');
  if(finished===jobs.length){
   btn.disabled=false;loadSavedVideos();
   const back=document.createElement('button');back.type='button';back.className='back';back.textContent='← Video Upload သို့ ပြန်သွားရန်';back.style.background='var(--panel2)';back.style.color='var(--text)';back.style.borderColor='var(--border)';
   back.addEventListener('click',()=>{document.getElementById('jobform').parentElement.style.display='';showStep(0);document.querySelector('.wizard-step[data-step="0"]')?.scrollIntoView({behavior:'smooth',block:'start'});});
   links.prepend(back);statusBox.scrollIntoView({behavior:'smooth',block:'center'});return;
  }
  setTimeout(refresh,3000);
 }
 result.classList.remove('hidden');await refresh();
}
form.addEventListener('submit',async(e)=>{e.preventDefault();const geminiKey=geminiInput.value.trim(),groqKey=groqInput.value.trim(),videoFiles=Array.from(document.getElementById('videoFile').files||[]),videoFile=videoFiles[0],videoUrl=document.getElementById('url').value.trim();const recapScript=document.getElementById('recapScript')?.value.trim()||'';if(!recapScript&&(!geminiKey||!groqKey)){result.classList.remove('hidden');statusBox.textContent='Auto Recap အတွက် API Key Settings မှာ Gemini နဲ့ Groq Key နှစ်ခုလုံးထည့်ပြီး Save Keys ကိုနှိပ်ပါ။ ကိုယ်ပိုင် Script သုံးလျှင် API Key မလိုပါ။';return;}if((!videoUrl&&!videoFiles.length)||(videoUrl&&videoFiles.length)){result.classList.remove('hidden');statusBox.textContent='Video URL သို့မဟုတ် Upload Video ကိုသာ ရွေးပါ။';return;}if(videoFiles.length>5){result.classList.remove('hidden');statusBox.textContent='တစ်ကြိမ်လျှင် အများဆုံး Video ၅ ဖိုင်သာ ရွေးနိုင်သည်။';return;}if(videoFiles.some(file=>file.size>80*1024*1024)){result.classList.remove('hidden');statusBox.textContent='Video တစ်ဖိုင်ချင်းစီသည် 80 MB ထက် မကြီးရပါ။';return;}if(videoFiles.length>1){await submitMultipleVideos(videoFiles,geminiKey,groqKey);return;}btn.disabled=true;result.classList.remove('hidden');links.innerHTML='';setProgress(0);statusBox.textContent='';
try{const body=new FormData();const options={voice:document.getElementById('voice').value,voice_speed:document.getElementById('voiceSpeed').value,subtitle_pace:document.getElementById('subtitlePace').value,auto_effect:document.getElementById('autoEffect').checked,auto_blur_subtitles:document.getElementById('autoBlurSubtitles').checked,mirror:document.getElementById('mirror').checked,overlay_text:''};body.append('url',videoFile?'':videoUrl);body.append('language',document.getElementById('language').value);body.append('gemini_api_key',geminiKey);body.append('groq_api_key',groqKey);Object.entries(options).forEach(([k,v])=>body.append(k,String(v)));if(videoFile)body.append('video',videoFile);body.append('recap_script',document.getElementById('recapScript')?.value.trim()||'');const res=await fetch('/api/jobs',{method:'POST',body});const data=await res.json();if(!res.ok)throw new Error(data.error||'စတင်၍မရပါ');poll(data.id);}catch(err){statusBox.textContent='အမှား: '+err.message;btn.disabled=false;}});
async function poll(id){try{const res=await fetch('/api/jobs/'+id);const data=await res.json();if(!res.ok){if(res.status===404){statusBox.textContent='အရင် Job ကို server မှာ မတွေ့တော့ပါ။ Render restart/redeploy ဖြစ်ခဲ့နိုင်ပါတယ်။ Video ရွေးထားဆဲဆိုရင် Generate Final Video ကို ထပ်နှိပ်ပြီး Job အသစ်စတင်ပါ။';links.innerHTML='';btn.disabled=false;showStep(3);return;}throw new Error(data.error||'အခြေအနေမရပါ');}setProgress(data.progress??(data.status==='done'?100:0));statusBox.textContent=data.message|| (data.status==='queued'?'အလုပ်တန်းစီနေသည်…':data.status==='running'?'လုပ်ဆောင်နေသည်…':'');
if(data.status==='done'){setProgress(100);loadSavedVideos();links.innerHTML='<button type="button" id="finalBackBtn" class="back" style="width:100%;margin-bottom:12px">← နောက်သို့ — Voice ပြန်သွားရန်</button><h3>🎬 Final Video Preview</h3><video id="finalVideoPreview" controls playsinline preload="metadata" style="display:block;width:100%;max-height:72vh;aspect-ratio:16/9;object-fit:contain;background:#05070d;border:1px solid #31435e;border-radius:14px;margin:10px 0" src="/api/jobs/'+id+'/preview"></video><a class="download" href="/api/jobs/'+id+'/download">⬇️ Final MP4 ဒေါင်းလုဒ်လုပ်ရန်</a>';document.getElementById('finalBackBtn').addEventListener('click',()=>{document.getElementById('jobform').parentElement.style.display='';showStep(1);document.querySelector('.wizard-step[data-step="1"]')?.scrollIntoView({behavior:'smooth',block:'start'});});document.getElementById('jobform').parentElement.style.display='none';document.querySelectorAll('main > p.muted').forEach(el=>el.style.display='none');let resultHeading=document.querySelector('#result h2');if(!resultHeading&&result){resultHeading=document.createElement('h2');result.prepend(resultHeading);}if(resultHeading)resultHeading.textContent='🎬 Final Video Preview';btn.disabled=false;showStep(3);statusBox.scrollIntoView({behavior:'smooth',block:'center'});return;}
if(data.status==='error'){statusBox.textContent='Error: '+data.message;btn.disabled=false;return;}setTimeout(()=>poll(id),3000);
}catch(err){statusBox.textContent='အခြေအနေစစ်ရာတွင် ပြဿနာ: '+err.message;btn.disabled=false;}}

window.addEventListener('resize',()=>{if(previewVideo.videoWidth&&previewVideo.videoHeight){const ratio=previewVideo.videoWidth/previewVideo.videoHeight,livePreview=document.getElementById('preview');livePreview.style.width='min(100%, '+Math.round(window.innerHeight*0.64*ratio)+'px)';livePreview.style.maxHeight=Math.round(window.innerHeight*0.64)+'px';}});

async function loadSavedVideos(){
 const list=document.getElementById('savedVideosList'),note=document.getElementById('storageNote');
 try{
  const res=await fetch('/api/jobs',{cache:'no-store'}),data=await res.json();
  if(!res.ok)throw new Error(data.error||'စာရင်းမရပါ');
  const jobs=data.jobs||[];
  if(!jobs.length){list.textContent='သိမ်းထားသော Final Video မရှိသေးပါ။';}
  else{
   list.innerHTML=jobs.map(job=>'<div style="border:1px solid var(--border);border-radius:12px;padding:12px;margin:10px 0;background:var(--panel2)"><strong style="overflow-wrap:anywhere">'+escapeHistoryText(job.title)+'</strong><p class="muted small">ပြီးစီးချိန်: '+(job.finished?new Date(job.finished*1000).toLocaleString():'မသိရသေး')+'</p><div style="display:flex;gap:8px;flex-wrap:wrap"><button type="button" data-history-preview="'+job.id+'" style="width:auto;flex:1;margin:0">▶ ပြန်ဖွင့်ရန်</button><a class="download" style="width:auto;flex:1;margin:0" href="'+job.download_url+'">⬇️ MP4 သိမ်းရန်</a></div><div id="history-player-'+job.id+'"></div></div>').join('');
   list.querySelectorAll('[data-history-preview]').forEach(button=>button.addEventListener('click',()=>{
    const id=button.getAttribute('data-history-preview'),slot=document.getElementById('history-player-'+id);
    slot.innerHTML='<video controls playsinline preload="metadata" style="display:block;width:100%;max-height:65vh;aspect-ratio:16/9;object-fit:contain;background:#05070d;border-radius:12px;margin-top:12px" src="/api/jobs/'+id+'/preview"></video>';
    slot.scrollIntoView({behavior:'smooth',block:'nearest'});
   }));
  }
  note.textContent=data.persistent_storage_configured?'Server storage: persistent disk path detected.':'သတိပြုရန် — Reload သာမက Render restart/redeploy ပြီးနောက်လည်း မပျောက်စေရန် Render တွင် Persistent Disk ကို /var/data တွင် Mount လုပ်ပါ။';
 }catch(err){list.textContent='သိမ်းထားသော Video စာရင်းကို မဖတ်နိုင်ပါ: '+err.message;}
}
function escapeHistoryText(value){return String(value||'Movie Recap').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));}
window.addEventListener('DOMContentLoaded',loadSavedVideos);
</script></body></html>
"""

def fail(message):
    raise RuntimeError(message)

def validate_public_http_url(url):
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            fail("http:// သို့မဟုတ် https:// ဖြင့်စသော တိုက်ရိုက်ဗီဒီယို URL ထည့်ပါ။")
        if parsed.username or parsed.password:
            fail("URL ထဲမှာ username/password မထည့်ပါနှင့်။")
        host = parsed.hostname.lower()
        if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
            fail("Local/private URL များကို ခွင့်မပြုပါ။")
        try:
            addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            for item in addresses:
                ip = ipaddress.ip_address(item[4][0])
                if not ip.is_global:
                    fail("Public internet video URL ကိုသာ အသုံးပြုပါ။")
        except socket.gaierror:
            fail("URL host ကို မတွေ့ပါ။ URL ကို ပြန်စစ်ပါ။")
        return url.strip()
    except ValueError:
        fail("URL မမှန်ပါ။")

def run_command(args, timeout=300):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        fail("Video processing အချိန်ကျော်သွားသည်။ ပိုတိုသော clip ဖြင့် စမ်းပါ။")
    if result.returncode != 0:
        details = (result.stderr or "Unknown FFmpeg error")[-1200:]
        fail("Video processing မအောင်မြင်ပါ။ FFmpeg: " + details)
    return result.stdout.strip()

def _is_supported_platform_url(url):
    """Recognize common public video-page URLs that are not direct media files."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    supported_hosts = (
        "youtube.com", "youtu.be", "youtube-nocookie.com",
        "facebook.com", "fb.watch",
        "tiktok.com",
        "instagram.com",
        "vimeo.com",
        "dailymotion.com",
    )
    return any(host == domain or host.endswith("." + domain) for domain in supported_hosts)


def _download_platform_video(url, destination):
    """Download one public video page using yt-dlp, with strict output limits."""
    try:
        import yt_dlp
    except ImportError:
        fail("Video URL support ကို မတပ်ဆင်ရသေးပါ။ Server ကို requirements.txt အသစ်ဖြင့် redeploy လုပ်ပါ။")
    temp_dir = Path(tempfile.mkdtemp(prefix="url-video-", dir=str(destination.parent)))
    try:
        output_template = str(temp_dir / "source.%(ext)s")
        options = {
            "outtmpl": output_template,
            "format": "best[filesize<83886080]/best",
            "max_filesize": MAX_VIDEO_BYTES,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 25,
            "retries": 2,
            "fragment_retries": 2,
            "merge_output_format": "mp4",
            "match_filter": yt_dlp.utils.match_filter_func("duration <= 600"),
            "restrictfilenames": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
        except Exception as exc:
            message = str(exc)
            if "duration" in message.lower() and ("match filter" in message.lower() or "filtered" in message.lower()):
                fail("ဗီဒီယိုသည် ၁၀ မိနစ်ထက်ရှည်နေသည်။ ၁၀ မိနစ်အောက် clip ကိုသုံးပါ။")
            if "filesize" in message.lower() or "file size" in message.lower():
                fail("ဗီဒီယို 80 MB ထက်ကြီးနေသည်။ ပိုသေးသော clip ကို အသုံးပြုပါ။")
            fail("ဒီ Video URL ကို download မလုပ်နိုင်ပါ။ Public video link ဖြစ်ကြောင်း၊ login မလိုကြောင်းနှင့် platform က ခွင့်ပြုကြောင်း စစ်ပါ။ အသေးစိတ်: " + message[:240])
        if isinstance(info, dict) and info.get("duration") and info["duration"] > MAX_VIDEO_SECONDS:
            fail("ဗီဒီယိုသည် ၁၀ မိနစ်ထက်ရှည်နေသည်။ ၁၀ မိနစ်အောက် clip ကိုသုံးပါ။")
        candidates = [p for p in temp_dir.iterdir() if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv")]
        if not candidates:
            fail("URL မှ video ဖိုင် မရပါ။ Public video link ကို ပြန်စစ်ပါ။")
        downloaded = max(candidates, key=lambda p: p.stat().st_size)
        size = downloaded.stat().st_size
        if size <= 0:
            fail("URL မှ video ဖိုင် အလွတ်ရရှိသည်။")
        if size > MAX_VIDEO_BYTES:
            fail("ဗီဒီယို 80 MB ထက်ကြီးနေသည်။ ပိုသေးသော clip ကို အသုံးပြုပါ။")
        shutil.move(str(downloaded), str(destination))
        return size, "video/" + destination.suffix.lstrip(".")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def download_video(url, destination):
    validate_public_http_url(url)
    if _is_supported_platform_url(url):
        return _download_platform_video(url, destination)
    try:
        response = requests.get(url, stream=True, timeout=(15, 30), allow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0 OneClickMovieRecap/1.0"})
        response.raise_for_status()
    except requests.RequestException as exc:
        fail("Video URL ကို ဖွင့်မရပါ။ Direct MP4/MOV/WEBM ဖိုင်လင့်ခ်ဖြစ်ကြောင်းနှင့် public access ရှိကြောင်း စစ်ပါ။ အသေးစိတ်: " + str(exc)[:180])
    validate_public_http_url(response.url)
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type or "application/json" in content_type:
        response.close()
        fail("ဒီ URL က video ဖိုင်လင့်ခ်မဟုတ်ဘဲ webpage ဖြစ်နေသည်။ YouTube/Facebook/TikTok/Vimeo link ဆိုရင် platform video link အဖြစ်ထည့်ပါ၊ အခြားဆိုဒ်ဆိုရင် direct .mp4/.mov/.webm URL ကိုသုံးပါ။")
    length = response.headers.get("Content-Length")
    try:
        if length and int(length) > MAX_VIDEO_BYTES:
            response.close()
            fail("ဗီဒီယို 80 MB ထက်ကြီးနေသည်။ ပိုသေးသော clip ကို အသုံးပြုပါ။")
    except ValueError:
        pass
    total = 0
    try:
        with open(destination, "wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    fail("ဗီဒီယို 80 MB ထက်ကြီးနေသည်။ ပိုသေးသော clip ကို အသုံးပြုပါ။")
                output.write(chunk)
    finally:
        response.close()
    if total < 1024:
        fail("URL မှ ဗီဒီယိုဖိုင် မရပါ။ Direct MP4/MOV/WEBM URL ဟုတ်မဟုတ် စစ်ပါ။")
    return total, content_type

def extract_audio(video_path, audio_path):
    run_command(["ffmpeg", "-y", "-i", str(video_path), "-vn", "-t", str(MAX_VIDEO_SECONDS),
                 "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio_path)], timeout=180)
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        fail("ဗီဒီယိုထဲမှ အသံမထုတ်နိုင်ပါ။ အသံပါသော video clip ဖြင့် စမ်းပါ။")

def groq_transcribe(audio_path, api_key):
    api_key = (api_key or "").strip()
    if not api_key:
        fail("Groq API Key မထည့်ရသေးပါ။ Website ရဲ့ API Key Settings ကို စစ်ပါ။")
    with open(audio_path, "rb") as audio:
        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": "Bearer " + api_key},
            data={"model": "whisper-large-v3-turbo", "response_format": "verbose_json"},
            files={"file": (audio_path.name, audio, "audio/mpeg")},
            timeout=180,
        )
    if response.status_code >= 400:
        fail("Groq transcription API error (" + str(response.status_code) + "): " + response.text[:500])
    data = response.json()
    transcript = (data.get("text") or "").strip()
    if not transcript:
        fail("ဗီဒီယိုထဲက စကားသံကို မဖော်ထုတ်နိုင်ပါ။")
    return transcript[:MAX_TRANSCRIPT_CHARS]

def make_recap(transcript, language, api_key):
    api_key = (api_key or "").strip()
    if not api_key:
        fail("Gemini API Key မထည့်ရသေးပါ။ Website ရဲ့ API Key Settings ကို စစ်ပါ။")
    language_name = "natural Burmese (Myanmar)" if language == "my" else "natural English"

    # Generate sequential sections instead of asking for the whole transcript in one
    # response. This avoids silently losing the beginning/end when the model hits its
    # output-token limit. Keep sentence boundaries when splitting.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?။])\s*|\n+", transcript) if s.strip()]
    # Also split unusually long unpunctuated Whisper output, so no API request is oversized.
    pieces = []
    for sentence in sentences or [transcript.strip()]:
        while len(sentence) > 8500:
            cut = sentence.rfind(" ", 0, 8500)
            if cut < 1000:
                cut = 8500
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    chunks, current = [], ""
    for sentence in pieces:
        if current and len(current) + len(sentence) + 1 > 8500:
            chunks.append(current)
            current = ""
        current = (current + " " + sentence).strip()
    if current:
        chunks.append(current)
    if not chunks:
        fail("Transcript စာသား မရပါ။")

    # Retry temporary overloads, then try alternate Gemini models automatically.
    model_candidates = list(dict.fromkeys([GEMINI_MODEL, "gemini-3.7-flash", "gemini-3.5-flash-lite"]))
    sections = []
    for index, chunk in enumerate(chunks):
        position = "first" if index == 0 else ("last" if index == len(chunks)-1 else "middle")
        prompt = (
            "Write section " + str(index+1) + " of " + str(len(chunks)) +
            " of one complete, chronological movie-recap narration, based ONLY on this transcript segment. "
            "This is the " + position + " segment of the source. Preserve the important events in their given order; "
            "do not skip the start or ending events contained in this segment, do not invent facts, and do not repeat events from other segments. "
            "Write in " + language_name + ". If Burmese, use natural Myanmar script only, never romanized Burmese. "
            "Use clear spoken language and natural punctuation (။ and ၊ for Burmese). No headings, labels, timestamps, bullet points, or copied dialogue. "
            "Make this section concise but complete, around 100-180 words when the source segment supports it. "
            "For the first segment, start the story naturally; for the last segment, finish the events shown in this segment without inventing an ending. "
            "Output only narration text.\n\nTRANSCRIPT SEGMENT:\n" + chunk
        )
        response = None
        used_model = None
        last_error = ""
        for model in model_candidates:
            endpoint = "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent"
            for attempt, delay in enumerate((0, 1.5, 3, 6)):
                if delay:
                    time.sleep(delay)
                try:
                    response = requests.post(endpoint, params={"key": api_key},
                        json={"contents": [{"parts": [{"text": prompt}]}],
                              "generationConfig": {"maxOutputTokens": 3500, "thinkingConfig": {"thinkingLevel": "LOW"}}},
                        timeout=120)
                except requests.RequestException as exc:
                    last_error = str(exc)
                    continue
                if response.status_code == 429:
                    # A quota may be model-specific. Do not retry the same model
                    # repeatedly; move directly to the next supported fallback.
                    last_error = "HTTP 429: " + response.text[:1000]
                    break
                if response.status_code == 200:
                    used_model = model
                    break
                last_error = "HTTP " + str(response.status_code) + ": " + response.text[:400]
                # Retry temporary overloads; if still overloaded, move to the next model.
                if response.status_code in (500, 502, 503, 504):
                    continue
                # A model that is unavailable/not enabled should not block fallback models.
                if response.status_code == 404:
                    break
                fail("Gemini API error (" + str(response.status_code) + "): " + response.text[:600])
            if used_model:
                break
        if not used_model or response is None or response.status_code != 200:
            fail("Gemini models များမှ တုံ့ပြန်မှု မရပါ။ အဓိက model အလုပ်များနေပြီး fallback မော်ဒယ်များလည်း မရနိုင်ပါ။ နောက်မှ ပြန်စမ်းပါ။ အသေးစိတ်: " + last_error[:300])
        data = response.json()
        try:
            section = "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"]).strip()
        except (KeyError, IndexError, TypeError):
            fail("Gemini " + used_model + " မှ recap အပိုင်း " + str(index+1) + " ကို မရပါ။ API quota/model access ကို စစ်ပါ။")
        if not section:
            fail("Gemini " + used_model + " က recap အပိုင်း " + str(index+1) + " ကို အလွတ်ပြန်ပေးထားသည်။")
        sections.append(section)
    return "\n\n".join(sections)

def synthesize_voice(script, language, audio_path, voice, voice_speed=100):
    # Edge TTS can occasionally reject WebSocket handshakes; retry with a fresh connection.
    import asyncio
    last_error = None
    for attempt in range(3):
        try:
            if audio_path.exists():
                audio_path.unlink()
            speed = max(70, min(130, int(voice_speed)))
            rate = f"{speed - 100:+d}%"
            communicate = edge_tts.Communicate(script, voice, rate=rate)
            asyncio.run(communicate.save(str(audio_path)))
            if audio_path.exists() and audio_path.stat().st_size > 0:
                return
            last_error = RuntimeError("TTS က အသံဖိုင်အလွတ် ပြန်ပေးခဲ့သည်။")
        except Exception as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    fail("AI Voice ဖန်တီးမရပါ။ Microsoft Edge TTS connection ကို ၃ ကြိမ် စမ်းပြီး မအောင်မြင်ပါ။ ခဏနောက်ပြန်စမ်းပါ။ Error: " + str(last_error)[:250])

def clean_myanmar_text(text):
    """Only remove invisible formatting characters; do not guess-convert Myanmar text."""
    import unicodedata
    value = str(text or "")
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Cf")
    value = value.replace("\ufeff", "")
    return unicodedata.normalize("NFC", value)


def split_sentences(text):
    # Burmese sentences often have no whitespace after ။ or ၊.
    pieces = re.split(r"(?<=[.!?။])\s*|\n+", text.strip())
    pieces = [p.strip() for p in pieces if p.strip()]
    lines = []
    for piece in pieces:
        while len(piece) > 54:
            cut = piece.rfind(" ", 0, 48)
            if cut < 15:
                cut = min(len(piece), 48)
            lines.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            lines.append(piece)
    return lines or [text.strip()]

def seconds_to_srt(seconds):
    ms = int(max(0, seconds) * 1000)
    hours, rem = divmod(ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

def fit_voice_to_video(video_path, voice_path):
    """Match narration length to the uploaded video using pitch-preserving tempo adjustment."""
    def probe(path):
        value = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=30)
        return float(value)
    try:
        video_duration = probe(video_path)
        voice_duration = probe(voice_path)
    except (TypeError, ValueError):
        fail("Video/AI Voice အချိန်ကို မဖတ်နိုင်ပါ။")
    if video_duration <= 0 or voice_duration <= 0:
        fail("Video သို့မဟုတ် AI Voice duration မမှန်ပါ။")
    tempo = voice_duration / video_duration
    factors = []
    while tempo > 2.0:
        factors.append(2.0)
        tempo /= 2.0
    while tempo < 0.5:
        factors.append(0.5)
        tempo /= 0.5
    factors.append(tempo)
    atempo = ",".join(f"atempo={factor:.8f}" for factor in factors)
    fitted = voice_path.with_name("recap_voice_fitted.mp3")
    run_command(["ffmpeg", "-y", "-i", str(voice_path), "-vn", "-af", atempo,
                 "-codec:a", "libmp3lame", "-q:a", "3", str(fitted)], timeout=180)
    if not fitted.exists() or fitted.stat().st_size == 0:
        fail("AI Voice ကို Video အချိန်နဲ့ ညှိမရပါ။")
    fitted.replace(voice_path)


def subtitle_units(text):
    """Approximate reading units: Burmese syllables, or whitespace-delimited words for Latin text."""
    import unicodedata
    text = unicodedata.normalize("NFC", clean_myanmar_text(text or ""))
    myanmar_units = list(re.finditer(r"[\u1000-\u1021\u1023-\u102A\u103F](?:[\u102B-\u103E\u105A-\u105D\u1060-\u1097])*", text))
    if myanmar_units:
        return [(m.start(), m.end()) for m in myanmar_units]
    return [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]

def split_subtitle_cues(text, pace="normal"):
    """Split recap into readable subtitle cues using the selected approximate 3-second pace."""
    targets = {"slow": 12, "normal": 20, "fast": 30}
    target = targets.get(str(pace or "normal").lower(), 20)
    text = clean_myanmar_text(text or "").strip()
    if not text:
        return []
    # Prefer natural sentence boundaries before subdividing long sentences.
    sentences = [part.strip() for part in re.split(r"(?<=[.!?။])\s*|\n+", text) if part.strip()]
    cues = []
    for sentence in sentences:
        units = subtitle_units(sentence)
        if len(units) <= target:
            cues.append(sentence)
            continue
        start = 0
        while start < len(units):
            end_index = min(start + target, len(units))
            if end_index >= len(units):
                end_pos = len(sentence)
            else:
                # Aim for a nearby natural pause after the target, without creating a tiny cue.
                target_pos = units[end_index - 1][1]
                search_end = min(len(sentence), units[min(len(units)-1, end_index + 5)][1])
                pause_candidates = [m.end() for m in re.finditer(r"[\s၊,;:။.!?]", sentence[target_pos:search_end])]
                end_pos = target_pos + pause_candidates[0] if pause_candidates else target_pos
                if end_pos <= 0:
                    end_pos = target_pos
            chunk = sentence[units[start][0]:end_pos].strip()
            if not chunk:
                end_pos = units[end_index - 1][1]
                chunk = sentence[units[start][0]:end_pos].strip()
            if chunk:
                cues.append(chunk)
            while end_index < len(units) and units[end_index][0] < end_pos:
                end_index += 1
            start = max(start + 1, end_index)
    return cues or [text]

def render_video(video_path, voice_path, output_path, options, music_path=None, progress_callback=None):
    def esc_text(value):
        return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%").replace(",", "\\,")

    overlay = (options.get("overlay_text") or "").strip()

    # Use the full source video and fit its playback timeline to the final AI voice.
    # Faster narration speeds the video up; longer narration slows the video down.
    # This keeps video, voice, and subtitles ending together instead of cutting the
    # source video off at the narration endpoint.
    video_duration_text = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)], timeout=30)
    voice_duration_text = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "default=noprint_wrappers=1:nokey=1", str(voice_path)], timeout=30)
    try:
        source_video_duration = float(video_duration_text)
        target_voice_duration = float(voice_duration_text)
    except (TypeError, ValueError):
        fail("Video/AI Voice duration ကို မဖတ်နိုင်ပါ။")
    if source_video_duration <= 0 or target_voice_duration <= 0:
        fail("Video သို့မဟုတ် AI Voice duration မမှန်ပါ။")
    video_tempo_factor = target_voice_duration / source_video_duration

    base_filters = [f"setpts=(PTS-STARTPTS)*{video_tempo_factor:.10f}"]
    if options.get("mirror"):
        base_filters.append("hflip")
    if options.get("auto_effect", True):
        base_filters.append("eq=contrast=1.04:saturation=1.06")

    # Prefer Noto Sans Myanmar, a Unicode Myanmar font, for overlays and subtitles.
    font_path = "/usr/share/fonts/truetype/padauk/Padauk-Regular.ttf"
    if not Path(font_path).is_file():
        font_path = "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf"
    if overlay:
        base_filters.append("drawtext=fontfile='" + font_path + "':text='" + esc_text(overlay) + "':fontcolor=white:fontsize=22:borderw=2:bordercolor=black:x=(w-text_w)/2:y=12")

    command = ["ffmpeg", "-y", "-i", str(video_path), "-i", str(voice_path)]
    music_index = None
    if music_path and music_path.exists() and music_path.stat().st_size:
        command += ["-stream_loop", "-1", "-i", str(music_path)]
        music_index = 2
    # Build the final video with enabled visual effects; SRT remains a separate download.
    graph = []
    pre = ",".join(base_filters) if base_filters else "null"
    if options.get("auto_blur_subtitles", True):
        # Blur the standard lower subtitle band across the full frame to hide source subtitles.
        graph.append("[0:v:0]" + pre + ",split=2[vclean][vblur_src]")
        graph.append("[vblur_src]crop=w=iw:h=ih*0.22:x=0:y=ih*0.78,gblur=sigma=18:steps=3,format=yuv420p[vblur]")
        graph.append("[vclean][vblur]overlay=x=0:y=H-h:eof_action=repeat[vblurred]")
        graph.append("[vblurred]null[vbase]")
    else:
        graph.append("[0:v:0]" + pre + "[vbase]")

    graph.append("[vbase]null[vout]")

    if music_index is not None:
        volume=max(0,min(100,int(options.get("music_volume",20))))/100
        graph += ["[1:a:0]volume=1[voice]",f"[{music_index}:a:0]volume={volume}[music]","[voice][music]amix=inputs=2:duration=first:dropout_transition=0,alimiter=limit=0.95[aout]"]
    else:
        graph.append("[1:a:0]volume=1,alimiter=limit=0.95[aout]")

    # Use the AI voice duration as the master timeline. The video is trimmed to
    # the same endpoint so the exported MP4, narration, and generated subtitles end together.
    voice_duration_text = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "default=noprint_wrappers=1:nokey=1", str(voice_path)], timeout=30)
    try:
        voice_duration = max(0.1, float(voice_duration_text))
    except (TypeError, ValueError):
        fail("AI Voice timeline duration ကို မဖတ်နိုင်ပါ။")
    # Use the fixed fast encoding profile for consistent render speed.
    profile = {"preset": "ultrafast", "crf": "28", "threads": "0", "audio_bitrate": "128k"}
    command += ["-filter_complex",";".join(graph),"-map","[vout]","-map","[aout]",
                "-t", f"{voice_duration:.3f}", "-c:v","libx264","-preset",profile["preset"],"-crf",profile["crf"],
                "-threads",profile["threads"],"-filter_complex_threads","2","-c:a","aac","-b:a",profile["audio_bitrate"],"-ar","44100","-map_metadata","-1",
                "-movflags","+faststart",str(output_path)]
    # Stream FFmpeg's real encoding progress so the UI percentage advances during rendering.
    command[-1:-1] = ["-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        last_fraction = 0.0
        for line in proc.stdout:
            if "=" not in line:
                continue
            key, value = line.strip().split("=", 1)
            if key in ("out_time_us", "out_time_ms"):
                try:
                    # FFmpeg's out_time_ms value is in microseconds despite its name.
                    elapsed = max(0.0, float(value) / 1_000_000)
                    fraction = min(1.0, elapsed / voice_duration)
                    if progress_callback and fraction > last_fraction:
                        last_fraction = fraction
                        progress_callback(80 + int(fraction * 19))
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
        stderr_text = proc.stderr.read() if proc.stderr else ""
        return_code = proc.wait(timeout=900)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        fail("Video processing အချိန်ကျော်သွားသည်။ ပိုတိုသော clip ဖြင့် စမ်းပါ။")
    if return_code != 0:
        fail("Video processing မအောင်မြင်ပါ။ FFmpeg: " + (stderr_text or "Unknown FFmpeg error")[-1200:])
    if progress_callback:
        progress_callback(99)
    if not output_path.exists() or output_path.stat().st_size == 0:
        fail("MP4 ဖိုင် မထွက်လာပါ။")
    audio_check = run_command(["ffprobe","-v","error","-select_streams","a:0","-show_entries","stream=codec_name","-of","default=noprint_wrappers=1:nokey=1",str(output_path)], timeout=30).strip()
    if not audio_check:
        fail("Final MP4 ထဲမှာ Audio Stream မပါပါ။")


def make_srt(script, voice_path, srt_path, options=None):
    """Create readable SRT cues paced around three seconds and timed to the voice duration."""
    options = options or {}
    duration_text = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "default=noprint_wrappers=1:nokey=1", str(voice_path)], timeout=30)
    duration = float(duration_text)
    chunks = [chunk.strip() for chunk in split_subtitle_cues(clean_myanmar_text(script), options.get("subtitle_pace", "normal")) if chunk.strip()]
    if duration <= 0 or not chunks:
        fail("SRT ဖိုင်ဖန်တီးရန် Voice duration သို့မဟုတ် Script မမှန်ပါ။")
    weights = [max(1, len(subtitle_units(chunk))) for chunk in chunks]
    total = sum(weights)
    current = 0.0
    with open(srt_path, "w", encoding="utf-8") as handle:
        for index, pair in enumerate(zip(chunks, weights), start=1):
            chunk, weight = pair
            start = current
            current += duration * weight / total
            end = min(duration, current)
            safe = chunk.replace("-->", "→").replace("\n", " ")
            handle.write(str(index) + "\n" + seconds_to_srt(start) + " --> " + seconds_to_srt(end) + "\n" + safe + "\n\n")

def save_latest_artifacts(workdir):
    """Replace the saved script/SRT/voice only when a new set is ready."""
    mapping = {"recap.txt": "script.txt", "recap.srt": "recap.srt", "recap_voice.mp3": "ai_voice.mp3"}
    staged = []
    try:
        for source_name, target_name in mapping.items():
            source = workdir / source_name
            if not source.is_file() or source.stat().st_size == 0:
                fail("သိမ်းဆည်းရန်လိုအပ်သော ဖိုင် မရှိပါ: " + source_name)
            temp_target = LATEST_ROOT / (target_name + ".new")
            shutil.copy2(source, temp_target)
            staged.append((temp_target, LATEST_ROOT / target_name))
        for temp_target, final_target in staged:
            os.replace(temp_target, final_target)
    except Exception:
        for temp_target, final_target in staged:
            try:
                temp_target.unlink(missing_ok=True)
            except OSError:
                pass
        raise

def update_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)
            _save_job_locked(job_id)

def process_job(job_id, url, language, workdir, gemini_api_key, groq_api_key, options, uploaded=False, recap_script=""):
    try:
        video = workdir / "source.mp4"
        original_audio = workdir / "source.mp3"
        voice = workdir / "recap_voice.mp3"
        output = workdir / "movie_recap.mp4"

        if uploaded:
            size = video.stat().st_size
            update_job(job_id, status="running", progress=5)
        else:
            update_job(job_id, status="running", progress=2)
            size, _ = download_video(url, video)
            update_job(job_id, progress=5)
        if recap_script:
            update_job(job_id, progress=15)
            script = clean_myanmar_text(recap_script)
        else:
            extract_audio(video, original_audio)
            update_job(job_id, progress=15)
            transcript = groq_transcribe(original_audio, groq_api_key)
            update_job(job_id, progress=35)
            script = make_recap(transcript, language, gemini_api_key)
        # Gemini/transcription may insert invisible ZWNJ/zero-width characters
        # inside Burmese syllables (e.g. "မ‌ောင်"); remove them before TTS and subtitles.
        script = clean_myanmar_text(script)
        (workdir / "recap.txt").write_text(script, encoding="utf-8")

        update_job(job_id, progress=55)
        synthesize_voice(script, language, voice, options.get("voice", "my-MM-NilarNeural"), options.get("voice_speed", 120))

        # Keep the selected narration pace. Do not stretch narration to the source-video duration.
        update_job(job_id, progress=70)

        update_job(job_id, progress=75)
        music_path = workdir / "background_music"
        if not music_path.exists():
            music_path = None
        srt_file = workdir / "recap.srt"
        make_srt(script, voice, srt_file, options)

        update_job(job_id, progress=80)
        render_video(video, voice, output, options, music_path, progress_callback=lambda percent: update_job(job_id, progress=percent))
        save_latest_artifacts(workdir)
        title = JOBS.get(job_id, {}).get("title") or "Movie Recap"
        cloud_video_saved = False
        if _r2_configured():
            update_job(job_id, progress=95, message="Cloudflare R2 သို့ Final Video သိမ်းနေသည်...")
            _r2_upload_video(job_id, output)
            cloud_video_saved = True
        update_job(job_id, status="done", progress=100, message="",
                   output=str(output), script=str(workdir / "recap.txt"), srt=str(srt_file), voice=str(voice), finished=time.time(), title=title,
                   r2_video_key=_r2_video_key(job_id) if cloud_video_saved else None,
                   video_size=output.stat().st_size)
    except Exception as exc:
        update_job(job_id, status="error", progress=0, message=str(exc)[:1500], finished=time.time())
    finally:
        # Keep only completed outputs temporarily for download; remove source and intermediates.
        try:
            for name in ("source.mp4", "source.mp3", "background_music"):
                path = workdir / name
                if path.exists():
                    path.unlink()
        except OSError:
            pass

@app.get("/")
def index():
    return render_template_string(HTML)

@app.post("/api/voice-preview")
def create_voice_preview():
    from flask import request
    data = request.get_json(silent=True) or {}
    language = data.get("language", "my")
    voice = data.get("voice", "my-MM-NilarNeural")
    allowed = {"my-MM-NilarNeural", "my-MM-ThihaNeural"}
    if voice not in allowed:
        return jsonify({"error": "Voice ရွေးချယ်မှု မမှန်ပါ။"}), 400
    try:
        speed = max(70, min(130, int(data.get("speed", 100))))
    except (TypeError, ValueError):
        speed = 100
    rate = f"{speed - 100:+d}%"
    sample = "မင်္ဂလာပါ။ ဒီအသံက သီဟနဲ့ နီလာ AI Voice ကို အစမ်းနားထောင်ဖို့ ဖြစ်ပါတယ်။" if language == "my" else "Hello! This is a short voice preview."
    async def render_sample():
        chunks = []
        communicate = edge_tts.Communicate(sample, voice, rate=rate)
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                chunks.append(chunk.get("data", b""))
        return b"".join(chunks)
    try:
        audio_bytes = __import__("asyncio").run(render_sample())
    except Exception as exc:
        return jsonify({"error": "အသံအစမ်းထုတ်မရပါ။ ခဏနောက် ပြန်စမ်းပါ။ " + str(exc)[:180]}), 502
    if not audio_bytes:
        return jsonify({"error": "AI Voice မှ အသံအချက်အလက် မရပါ။"}), 502
    return send_file(io.BytesIO(audio_bytes), mimetype="audio/mpeg", download_name="voice-preview.mp3", as_attachment=False)

@app.get("/health")
def health():
    return jsonify({"ok": True, "gemini_model": GEMINI_MODEL})

@app.get("/api/jobs")
def list_jobs():
    """List completed videos stored locally or in Cloudflare R2."""
    jobs = []
    seen = set()
    rows = []
    try:
        with _db_connect() as connection:
            rows = connection.execute("SELECT id, payload FROM jobs ORDER BY rowid DESC").fetchall()
    except (OSError, sqlite3.Error):
        pass
    for job_id, payload in rows:
        try:
            job = json.loads(payload)
        except (TypeError, ValueError):
            continue
        path = _get_final_video_path(job_id, job)
        cloud_saved = bool(job.get("r2_video_key"))
        if job.get("status") == "done" and ((path and path.is_file()) or cloud_saved):
            jobs.append({"id": job_id, "title": str(job.get("title") or "Movie Recap")[:180],
                         "created": job.get("created"), "finished": job.get("finished"),
                         "size": path.stat().st_size if path and path.is_file() else job.get("video_size", 0),
                         "preview_url": "/api/jobs/" + job_id + "/preview",
                         "download_url": "/api/jobs/" + job_id + "/download"})
            seen.add(job_id)
    for job_id, job in _r2_saved_jobs():
        if job_id in seen:
            continue
        if job.get("status") == "done" and job.get("r2_video_key"):
            jobs.append({"id": job_id, "title": str(job.get("title") or "Movie Recap")[:180],
                         "created": job.get("created"), "finished": job.get("finished"),
                         "size": job.get("video_size", 0),
                         "preview_url": "/api/jobs/" + job_id + "/preview",
                         "download_url": "/api/jobs/" + job_id + "/download"})
    jobs.sort(key=lambda item: item.get("finished") or item.get("created") or 0, reverse=True)
    return jsonify({"jobs": jobs[:100], "persistent_storage_configured": _r2_configured() or bool(os.getenv("MOVIE_RECAP_DATA_DIR") or (os.getenv("RENDER") and Path("/var/data").is_dir())), "cloudflare_r2_configured": _r2_configured()})

@app.post("/api/jobs")
def create_job():
    from flask import request
    uploaded_file = None
    music_file = None
    recap_script = ""
    if request.is_json:
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        language = data.get("language", "my")
        gemini_api_key = (data.get("gemini_api_key") or "").strip()
        groq_api_key = (data.get("groq_api_key") or "").strip()
        options = data.get("options") or {}
    else:
        url = (request.form.get("url") or "").strip()
        language = request.form.get("language", "my")
        gemini_api_key = (request.form.get("gemini_api_key") or "").strip()
        groq_api_key = (request.form.get("groq_api_key") or "").strip()
        recap_script = (request.form.get("recap_script") or "").strip()
        options = {"voice": request.form.get("voice", ""), "voice_speed": request.form.get("voice_speed", "100"), "mirror": request.form.get("mirror") == "true", "overlay_text": request.form.get("overlay_text", "")[:100], "music_volume": request.form.get("music_volume", "20"), "auto_effect": request.form.get("auto_effect", "true") == "true", "auto_blur_subtitles": request.form.get("auto_blur_subtitles", "true") == "true", "subtitle_pace": request.form.get("subtitle_pace", "normal")}
        uploaded_file = request.files.get("video")
        if uploaded_file and not uploaded_file.filename:
            uploaded_file = None
        music_file = request.files.get("music")
        if music_file and not music_file.filename:
            music_file = None
    if not recap_script and (not gemini_api_key or not groq_api_key):
        return jsonify({"error": "Auto Recap သုံးရန် Website API Key Settings မှာ Gemini နဲ့ Groq Key နှစ်ခုလုံး ထည့်ပါ။ Script ကို ကိုယ်တိုင်ထည့်လျှင် Key မလိုပါ။"}), 400
    if len(gemini_api_key) > 512 or len(groq_api_key) > 512:
        return jsonify({"error": "API Key ပုံစံမမှန်ပါ။ Key ကို ပြန်စစ်ပါ။"}), 400
    if not url and not uploaded_file:
        return jsonify({"error": "Video URL ထည့်ပါ သို့မဟုတ် ဖုန်းထဲက Video ကို Upload လုပ်ပါ။"}), 400
    if url and uploaded_file:
        return jsonify({"error": "URL နဲ့ Upload ကို တစ်ခုတည်းသာ ရွေးပါ။"}), 400
    if language not in ("my", "en"):
        return jsonify({"error": "ဘာသာစကားရွေးချယ်မှု မမှန်ပါ။"}), 400
    if len(recap_script) > 30000:
        return jsonify({"error": "Recap Script သည် စာလုံး 30,000 ထက် မကျော်ရပါ။"}), 400
    if request.is_json: options = data.get("options") or {}
    allowed_voices = ("my-MM-NilarNeural", "my-MM-ThihaNeural")
    if options.get("voice") not in allowed_voices: options["voice"] = "my-MM-NilarNeural"
    try: options["voice_speed"] = max(70, min(130, int(options.get("voice_speed", 100))))
    except (TypeError, ValueError): options["voice_speed"] = 100
    options["mirror"] = str(options.get("mirror", "")).lower() in ("true", "1", "yes") if not isinstance(options.get("mirror"), bool) else options.get("mirror")
    options["auto_lighting"] = str(options.get("auto_lighting", True)).lower() in ("true", "1", "yes") if not isinstance(options.get("auto_lighting", True), bool) else options.get("auto_lighting", True)
    try:
        options["music_volume"] = max(0, min(100, int(options.get("music_volume", 20))))
    except (TypeError, ValueError):
        options["music_volume"] = 20
    options["overlay_text"] = str(options.get("overlay_text", ""))[:100]
    if uploaded_file:
        filename = (uploaded_file.filename or "").lower()
        allowed_extensions = (".mp4", ".mov", ".webm", ".m4v")
        if not filename.endswith(allowed_extensions):
            return jsonify({"error": "MP4, MOV, WEBM သို့မဟုတ် M4V ဖိုင်ကိုသာ Upload လုပ်ပါ။"}), 400
    else:
        try:
            validate_public_http_url(url)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
    if music_file:
        music_name = (music_file.filename or "").lower()
        if not music_name.endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg")):
            return jsonify({"error": "Background Music ကို MP3, WAV, M4A, AAC သို့မဟုတ် OGG ဖိုင်သာ သုံးပါ။"}), 400
    job_id = uuid.uuid4().hex
    workdir = OUTPUT_ROOT / job_id
    workdir.mkdir(parents=True, exist_ok=False)
    uploaded = bool(uploaded_file)
    if uploaded:
        source_path = workdir / "source.mp4"
        uploaded_file.save(source_path)
        if source_path.stat().st_size == 0:
            source_path.unlink(missing_ok=True)
            workdir.rmdir()
            return jsonify({"error": "Upload ဖိုင် အလွတ်ဖြစ်နေသည်။"}), 400
        if source_path.stat().st_size > MAX_VIDEO_BYTES:
            source_path.unlink(missing_ok=True)
            workdir.rmdir()
            return jsonify({"error": "ဗီဒီယို 80 MB ထက်ကြီးနေသည်။ ပိုသေးသောဖိုင်ကို သုံးပါ။"}), 413
    if music_file:
        music_path = workdir / "background_music"
        music_file.save(music_path)
        if music_path.stat().st_size > 15 * 1024 * 1024:
            music_path.unlink(missing_ok=True)
            if uploaded:
                (workdir / "source.mp4").unlink(missing_ok=True)
            workdir.rmdir()
            return jsonify({"error": "Background Music ဖိုင် 15 MB ထက်မကြီးရပါ။"}), 413
        if music_path.stat().st_size == 0:
            music_path.unlink(missing_ok=True)
            if uploaded:
                (workdir / "source.mp4").unlink(missing_ok=True)
            workdir.rmdir()
            return jsonify({"error": "Background Music ဖိုင် အလွတ်ဖြစ်နေသည်။"}), 400
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "message": "အလုပ်တန်းစီနေသည်…", "created": time.time(), "title": (Path(uploaded_file.filename).name if uploaded_file and uploaded_file.filename else (Path(urlparse(url).path).name or "Movie Recap"))[:180]}
        _save_job_locked(job_id)
    thread = threading.Thread(target=process_job, args=(job_id, url, language, workdir, gemini_api_key, groq_api_key, options, uploaded, recap_script), daemon=True)
    thread.start()
    return jsonify({"id": job_id, "status": "queued"}), 202

@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    # Read local SQLite first, then recover the durable job record from R2.
    recovered = None
    try:
        with _db_connect() as connection:
            row = connection.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            recovered = json.loads(row[0])
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        recovered = None

    if not recovered:
        recovered = _r2_get_job(job_id)
    if recovered:
        final_path = _get_final_video_path(job_id, recovered)
        if final_path and final_path.is_file():
            recovered.update(status="done", progress=100, message="ပြီးပါပြီ။ Final Video ကို ကြည့်ရှု/ဒေါင်းလုဒ်လုပ်နိုင်ပါပြီ။", finished=recovered.get("finished") or time.time())
        # For active jobs, trust the shared SQLite payload. Startup recovery
        # already marks interrupted jobs as errors; do not mark a live job failed
        # merely because this HTTP request reached a different worker.
        with JOBS_LOCK:
            JOBS[job_id] = recovered
            _save_job_locked(job_id)
        safe = {k: v for k, v in recovered.items() if k in ("status", "message", "progress", "created", "finished")}
        return jsonify(safe)

    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            safe = {k: v for k, v in job.items() if k in ("status", "message", "progress", "created", "finished")}
            return jsonify(safe)

    return jsonify({
        "error": "ဒီ Job ID ကို server မှာ မတွေ့တော့ပါ။ Render restart ဖြစ်ပြီး Job data မကျန်တော့နိုင်ပါ။ Video ကို ပြန်ရွေးပြီး Generate Final Video ကို ထပ်နှိပ်ပါ။",
        "recovery": "job_not_found"
    }), 404

@app.get("/api/jobs/<job_id>/download")
def download_job(job_id):
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id) or {})
    if not job:
        job = _r2_get_job(job_id) or {}
    if job and job.get("status") != "done":
        abort(404)
    path = _get_final_video_path(job_id, job)
    if path is None:
        if job.get("r2_video_key"):
            signed = _r2_video_url(job_id)
            if signed:
                return redirect(signed, code=302)
        abort(404)
    return send_file(path, as_attachment=True, download_name="movie_recap.mp4", mimetype="video/mp4", conditional=True)

@app.get("/api/jobs/<job_id>/preview")
def preview_job(job_id):
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id) or {})
    if not job:
        job = _r2_get_job(job_id) or {}
    if job and job.get("status") != "done":
        abort(404)
    path = _get_final_video_path(job_id, job)
    if path is None:
        if job.get("r2_video_key"):
            signed = _r2_video_url(job_id)
            if signed:
                return redirect(signed, code=302)
        abort(404)
    response = send_file(path, as_attachment=False, download_name="movie_recap.mp4", mimetype="video/mp4", conditional=True)
    response.headers["Accept-Ranges"] = "bytes"
    return response

@app.get("/api/jobs/<job_id>/srt")
def srt_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    path = Path(job.get("srt", ""))
    if not path.is_file() or path.parent.parent != OUTPUT_ROOT:
        abort(404)
    return send_file(path, as_attachment=True, download_name="recap.srt", mimetype="application/x-subrip; charset=utf-8")

@app.get("/api/jobs/<job_id>/voice-preview")
def voice_preview_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    path = Path(job.get("voice", ""))
    if not path.is_file() or path.parent.parent != OUTPUT_ROOT:
        abort(404)
    return send_file(path, as_attachment=False, download_name="ai_voice.mp3", mimetype="audio/mpeg")

@app.get("/api/jobs/<job_id>/voice")
def voice_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    path = Path(job.get("voice", ""))
    if not path.is_file() or path.parent.parent != OUTPUT_ROOT:
        abort(404)
    return send_file(path, as_attachment=True, download_name="ai_voice.mp3", mimetype="audio/mpeg")

@app.get("/api/latest/<kind>")
def latest_artifact(kind):
    files = {"script": ("script.txt", "text/plain; charset=utf-8"), "srt": ("recap.srt", "application/x-subrip; charset=utf-8"), "voice": ("ai_voice.mp3", "audio/mpeg")}
    if kind not in files:
        abort(404)
    filename, mimetype = files[kind]
    path = LATEST_ROOT / filename
    if not path.is_file() or path.stat().st_size == 0:
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename, mimetype=mimetype)

@app.get("/api/jobs/<job_id>/script")
def script_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    path = Path(job.get("script", ""))
    if not path.is_file() or path.parent.parent != OUTPUT_ROOT:
        abort(404)
    return send_file(path, mimetype="text/plain; charset=utf-8", as_attachment=False)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
