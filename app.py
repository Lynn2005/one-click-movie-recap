import ipaddress
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
from flask import Flask, jsonify, render_template_string, send_file, abort

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 82 * 1024 * 1024

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
DATA_ROOT = Path(os.getenv("MOVIE_RECAP_DATA_DIR") or (Path(tempfile.gettempdir()) / "movie-recap-data"))
DATA_ROOT.mkdir(parents=True, exist_ok=True)
OUTPUT_ROOT = DATA_ROOT / "movie-recap-jobs"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
LATEST_ROOT = DATA_ROOT / "latest-artifacts"
LATEST_ROOT.mkdir(parents=True, exist_ok=True)
JOB_DB = DATA_ROOT / "jobs.sqlite3"

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

_load_jobs()

HTML = r"""
<!doctype html>
<html lang="my">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>One Click Movie Recap</title>
<style>
:root{color-scheme:dark;--page:#0b1020;--text:#f3f4f6;--panel:#151d32;--border:#293652;--muted:#c7d0e0;--muted2:#9caac3;--field:#0c1428;--field-border:#455574;--button:#6d8cff;--button-text:#071024;--status:#0c1428}*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--text);font-family:Arial,"Noto Sans Myanmar",sans-serif;transition:background .2s,color .2s}body.light{color-scheme:light;--page:#f3f5fb;--text:#182033;--panel:#ffffff;--border:#d7deea;--muted:#46536b;--muted2:#66748b;--field:#f8faff;--field-border:#bcc7d9;--button:#315ce8;--button-text:#ffffff;--status:#eef2fa}
main{max-width:600px;margin:0 auto;padding:12px 12px 24px}.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:13px;margin:10px 0}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0 0 8px}p{line-height:1.4;color:var(--muted);margin:7px 0}.muted{font-size:12px;color:var(--muted2)}
label{display:block;margin:7px 0 4px;font-weight:700;font-size:13px}input,select{width:100%;padding:8px;border-radius:7px;border:1px solid var(--field-border);background:var(--field);color:var(--text);font-size:14px}input[type=range]{padding:0;height:18px}.option-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}.edit-tools{border:1px solid var(--border);border-radius:9px;padding:9px;margin-top:8px}.edit-tools summary{cursor:pointer;font-size:15px;font-weight:800;padding:5px 2px;list-style-position:inside}.edit-tools summary::marker{color:var(--button)}.edit-adjustments{padding-top:8px}.edit-tools h3{font-size:15px;margin:2px 0 7px}.edit-tools label{margin:5px 0 3px;font-size:12px}.edit-tools input:not([type=range]),.edit-tools select{padding:7px;font-size:13px}.edit-tools .preview{margin-top:7px}.preview-title{font-size:13px;font-weight:700;margin:4px 0}.preview-hint{font-size:11px;margin:5px 0}.preview{position:relative;overflow:hidden;border-radius:10px;background:#05070d;aspect-ratio:16/9;margin-top:10px;max-height:70vh;width:100%}.preview video{width:100%;height:100%;object-fit:contain;transition:filter .15s,transform .15s}.playback-controls{display:flex;align-items:center;gap:8px;margin-top:6px}.playback-controls button{width:auto;min-width:72px;margin:0;padding:6px 10px;font-size:12px}.playback-controls input{flex:1;min-width:0;padding:0}.playback-time{font-size:11px;color:var(--muted2);white-space:nowrap}.preview-overlay{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;display:block;padding:0;text-align:center;overflow:visible;z-index:4}.preview-subtitle{position:absolute;left:50%;top:88%;transform:translate(-50%,-50%);color:#fff;text-shadow:0 2px 4px #000,1px 1px 2px #000;font-weight:700;white-space:pre-wrap;max-width:94%;width:max-content;pointer-events:auto;touch-action:none;cursor:move;user-select:none;font-size:clamp(14px,3.2cqw,36px);line-height:1.15;overflow:visible}.blur-box{position:absolute;left:50%;top:84%;width:55%;height:12%;transform:translate(-50%,-50%);background:#ffffff35;backdrop-filter:blur(7px);border:2px dashed #7e9bff;touch-action:none;cursor:move;z-index:3}.blur-box .resize-handle{position:absolute;right:-7px;bottom:-7px;width:18px;height:18px;border:2px solid white;border-radius:50%;background:#718cff;touch-action:none;cursor:nwse-resize}.blur-box .blur-height-handle{right:auto;left:-7px;bottom:-7px;cursor:ns-resize;background:#16a085}.preview-logo{position:absolute;z-index:5;left:90%;top:8%;transform:translate(-50%,-50%);color:#fff;background:#0008;padding:3px 6px;border-radius:4px;font-size:12px;display:flex;align-items:center;justify-content:center;overflow:visible;pointer-events:auto;touch-action:none;cursor:move;user-select:none;min-width:12px;min-height:12px}.preview-logo img{display:block;width:auto;height:auto;max-width:100%;max-height:100%;object-fit:contain;flex:0 1 auto}.preview-logo .resize-handle{position:absolute;right:-7px;bottom:-7px;width:18px;height:18px;border:2px solid white;border-radius:50%;background:#718cff;box-shadow:0 1px 5px #000;touch-action:none;cursor:nwse-resize}.preview-subtitle .resize-handle{position:absolute;right:-10px;bottom:-10px;width:18px;height:18px;border:2px solid white;border-radius:50%;background:#718cff;box-shadow:0 1px 5px #000;touch-action:none;cursor:nwse-resize}.preview-subtitle{overflow:visible}.preview-text{position:absolute;top:8px;left:8px;color:white;text-shadow:1px 1px 3px #000;font-size:12px}
button,.download{display:inline-block;border:0;border-radius:8px;background:var(--button);color:var(--button-text);font-weight:800;padding:10px 12px;font-size:14px;text-decoration:none;cursor:pointer;width:100%;margin-top:10px}
button:disabled{opacity:.55;cursor:wait}.key-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.status{padding:10px;border-radius:8px;background:var(--status);white-space:pre-wrap;overflow-wrap:anywhere}.topbar{display:flex;align-items:center;justify-content:space-between;gap:10px}.topbar h1{font-size:20px}.settings-btn{width:auto;margin:0;padding:8px 10px;white-space:nowrap}.settings-wrap{position:fixed;z-index:20;inset:0;background:#0009;display:flex;justify-content:flex-end;align-items:flex-start;padding:12px}.settings-card{width:min(340px,100%);max-height:90vh;overflow:auto;background:var(--panel);border:1px solid var(--field-border);border-radius:12px;padding:14px;box-shadow:0 12px 40px #0008}.settings-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.settings-head h2{margin:0}.close-btn{width:auto;margin:0;padding:5px 10px;background:#35415a;color:white}.settings-card label{margin-top:12px}.key-input-row{display:flex;gap:6px;align-items:stretch}.key-input-row input{flex:1;min-width:0}.key-input-row .toggle-key{width:76px;flex:0 0 76px;margin:0;padding:6px;font-size:12px;background:#35415a;color:#fff}.settings-card .muted{margin-top:8px}
.note{border-left:3px solid #e8b85b;padding-left:12px}.small{font-size:13px}.hidden{display:none}.wizard-step{display:none}.wizard-step.active{display:block}.step-head{display:flex;align-items:center;gap:10px;margin:2px 0 14px}.step-number{display:inline-flex;align-items:center;justify-content:center;min-width:32px;height:32px;border-radius:50%;background:var(--button);color:var(--button-text);font-weight:800}.step-title{font-size:18px;font-weight:800}.step-desc{font-size:12px;color:var(--muted2);margin-top:3px}.wizard-nav{display:flex;gap:8px;margin-top:16px}.wizard-nav button{width:100%;margin-top:0}.wizard-nav .back{background:#35415a;color:#fff}.step-progress{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin:14px 0}.step-progress span{height:5px;border-radius:8px;background:var(--border)}.step-progress span.on{background:var(--button)}a{color:#9fb4ff}

</style>
</head>
<body><main>
<div class="topbar"><div style="display:flex;align-items:center;gap:8px;min-width:0"><button class="settings-btn" id="newJobBtn" type="button">＋ New</button><h1>🎬 One Click Movie Recap</h1></div><div style="display:flex;gap:7px;align-items:center"><button class="settings-btn" id="themeToggle" type="button" aria-label="Toggle light and dark theme">☀️ Light</button><button class="settings-btn" id="openSettings" type="button">⚙️ Settings</button></div></div>
<p class="muted">Video ထည့်ပါ → Recap MP4 ထုတ်ပါ</p>

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
<div class="step-progress" aria-label="Progress"><span class="on"></span><span></span><span></span><span></span></div>
<section class="wizard-step active" data-step="0"><div class="step-head"><span class="step-number">1</span><div><div class="step-title">Video Upload</div><div class="step-desc">ဖုန်းထဲက Video သို့မဟုတ် Direct URL ထည့်ပါ</div></div></div>
<label for="url">Option 1 — Direct video URL</label>
<input id="url" name="url" type="url" placeholder="https://example.com/video.mp4">
<p class="muted small">URL သို့မဟုတ် ဖုန်းထဲက Video တစ်ခုကို ရွေးပါ။</p>
<label for="videoFile">Option 2 — ဖုန်းထဲက Video Upload (MP4 / MOV / WEBM / M4V)</label>
<input id="videoFile" name="video" type="file" accept=".mp4,.mov,.webm,.m4v,video/mp4,video/quicktime,video/webm">
<div class="wizard-nav"><button type="button" class="next-step" data-next="1">Next → Voice</button></div></section>
<section class="wizard-step" data-step="1"><div class="step-head"><span class="step-number">2</span><div><div class="step-title">Voice & Language</div><div class="step-desc">Recap ဘာသာနဲ့ AI အသံ ရွေးပါ</div></div></div>
<div class="option-grid"><div><label for="language">Recap ဘာသာ</label><select id="language" name="language"><option value="my">မြန်မာ</option><option value="en">English</option></select></div><div><label for="voice">Voice</label><select id="voice" name="voice"><option value="my-MM-NilarNeural">မြန်မာ Nilar (မိန်းမသံ)</option><option value="my-MM-ThihaNeural">မြန်မာ Thiha (ယောက်ျားသံ)</option><option value="en-US-AriaNeural">English Aria</option><option value="en-US-GuyNeural">English Guy</option><option value="en-GB-SoniaNeural">English Sonia</option></select></div></div>
<label for="voiceSpeed">AI Voice Speed <span id="voiceSpeedValue">100%</span></label><input id="voiceSpeed" name="voice_speed" type="range" min="70" max="130" step="5" value="100"><p class="muted small">70% = ဖြည်းဖြည်း · 100% = ပုံမှန် · 130% = မြန်မြန်။ ဒီနှုန်းနဲ့ AI Voice MP3 ထုတ်ပါမယ်။</p>
<div class="wizard-nav"><button type="button" class="back back-step" data-back="0">← Back</button><button type="button" class="next-step" data-next="2">Next → Edit Tools</button></div></section>
<section class="wizard-step" data-step="2"><div class="step-head"><span class="step-number">3</span><div><div class="step-title">Edit Tools</div><div class="step-desc">Subtitle, logo, lighting နဲ့ music ချိန်ပါ</div></div></div>
<div class="preview-title">👁️ Live Edit Preview</div><div class="preview" id="preview"><video id="previewVideo" playsinline class="hidden"></video><div class="preview-logo" id="previewLogo"><img id="previewLogoImage" alt="Logo preview"><span id="previewLogoText">My Recap</span></div><div class="preview-text" id="previewText"></div><div class="blur-box" id="blurBox"><span class="resize-handle" id="blurResize" aria-label="Blur width"></span><span class="resize-handle blur-height-handle" id="blurHeightResize" aria-label="Blur height"></span></div><div class="preview-overlay"><div class="preview-subtitle" id="previewSubtitle">Subtitle Preview<span class="resize-handle" id="subtitleResize" aria-label="Resize subtitle"></span></div></div></div><div class="playback-controls"><button id="previewPlay" type="button">▶ Play</button><input id="previewSeek" type="range" min="0" max="1000" value="0" aria-label="Video seek"><span class="playback-time" id="previewTime">0:00 / 0:00</span></div><p class="muted preview-hint" id="previewHint">Upload Video ရွေးပါ။ အောက်က Edit Tools ပြောင်းတာနဲ့ ဒီမှာပြောင်းလဲမှုကို ကြည့်နိုင်ပါတယ်။</p>
<details class="edit-tools"><summary class="edit-summary">🎨 Edit Tools — ချိန်ညှိရန် နှိပ်ပါ</summary><div class="edit-adjustments"><div><label><input id="autoEffect" type="checkbox" checked style="width:auto;margin-right:5px"> ✨ Auto Effect</label></div><div class="option-grid"><div><label for="blur">Blur Box Strength <span id="blurValue">0</span></label><input id="blur" type="range" min="0" max="10" value="0"><input id="blurX" type="hidden" value="50"><input id="blurY" type="hidden" value="84"><input id="blurW" type="hidden" value="55"><input id="blurH" type="hidden" value="12"><p class="muted small">Blur Box ကို video ပေါ်မှာ လက်နဲ့ရွှေ့/အရွယ်ပြောင်းနိုင်သည်။</p></div><div><label for="subSize">Subtitle Size</label><input id="subSize" type="range" min="20" max="64" value="40"></div><div><label for="subColor">Subtitle Color</label><select id="subColor"><option value="&H00FFFFFF">White</option><option value="&H0000FFFF">Yellow</option><option value="&H0000FF00">Green</option><option value="&H00FF8080">Light Blue</option></select></div><div><label for="subPosition">Subtitle Position</label><select id="subPosition"><option value="bottom">အောက်</option><option value="middle">အလယ်</option><option value="top">အပေါ်</option></select></div><div><label for="subX">Subtitle X <span id="subXValue">50%</span></label><input id="subX" type="range" min="5" max="95" value="50"></div><div><label for="subY">Subtitle Y <span id="subYValue">88%</span></label><input id="subY" type="range" min="5" max="95" value="88"></div><div><label for="subWeight">Subtitle Weight</label><select id="subWeight"><option value="bold">Bold</option><option value="normal">Normal</option></select></div><div><label for="subOutline">Subtitle Outline <span id="subOutlineValue">2</span></label><input id="subOutline" type="range" min="0" max="6" value="2"></div></div>
<div class="option-grid"><div><label><input id="mirror" type="checkbox" style="width:auto;margin-right:5px"> Mirror လှန်</label></div><div><label for="logoText">Logo / Watermark စာသား</label><input id="logoText" maxlength="40" value="My Recap" placeholder="My Recap"></div></div><label for="logoImage">🖼️ Logo ပုံ Upload (PNG/JPG/WEBP, 5 MB အထိ)</label><input id="logoImage" type="file" accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"><div class="option-grid"><div><label for="logoX">Logo X <span id="logoXValue">90%</span></label><input id="logoX" type="range" min="0" max="100" value="90"></div><div><label for="logoY">Logo Y <span id="logoYValue">8%</span></label><input id="logoY" type="range" min="0" max="100" value="8"></div><div class="muted small" style="grid-column:1/-1">Logo Width / Height: Auto (မူရင်းအချိုးအစားအတိုင်း အလိုအလျောက်ညှိမည်)</div><div><label for="logoOpacity">Logo Opacity <span id="logoOpacityValue">100%</span></label><input id="logoOpacity" type="range" min="10" max="100" value="100"></div></div><label for="overlayText">Video ပေါ်တင်မည့်စာသား</label><input id="overlayText" maxlength="100" placeholder="ကိုယ်ထည့်ချင်တဲ့စာသား"></details><p class="muted small">အများဆုံး 80 MB · 10 မိနစ် · Direct video URL</p>
<div class="wizard-nav"><button type="button" class="back back-step" data-back="1">← Back</button><button type="button" class="next-step" data-next="3">Next → Final Video</button></div></section>
<section class="wizard-step" data-step="3"><div class="step-head"><span class="step-number">4</span><div><div class="step-title">Final Video</div><div class="step-desc">သတ်မှတ်ချက်များကို စစ်ပြီး Recap MP4 ထုတ်ပါ</div></div></div><p class="muted small">✓ Video / Voice / Edit Tools အားလုံးပြီးပါပြီ။ Recap ထုတ်ရန် အောက်ကခလုတ်ကို နှိပ်ပါ။</p>
<button id="submit" type="submit">🎬 Generate Final Video</button><div class="wizard-nav"><button type="button" class="back back-step" data-back="2">← Back to Edit Tools</button></div></section>
</form></div>
<div id="result" class="panel hidden"><h2>အခြေအနေ</h2><div id="status" class="status">စောင့်ဆိုင်းနေသည်…</div><div id="links"></div></div>
<p class="muted small">Gemini 3.8 Flash · ကိုယ်အသုံးပြုခွင့်ရှိသော ဗီဒီယိုကိုသာ အသုံးပြုပါ။</p>
</main>
<script>
const form=document.getElementById('jobform'),btn=document.getElementById('submit'),result=document.getElementById('result'),statusBox=document.getElementById('status'),links=document.getElementById('links');
document.getElementById('newJobBtn').addEventListener('click',()=>{if(btn.disabled&&!confirm('လက်ရှိလုပ်ဆောင်နေသော Job ကို စောင့်မနေဘဲ စာမျက်နှာအသစ်ဖွင့်မလား။'))return;window.location.reload();});
const geminiInput=document.getElementById('geminiKey'),groqInput=document.getElementById('groqKey'),keyStatus=document.getElementById('keyStatus');
const settingsWrap=document.getElementById('settingsWrap');const themeToggle=document.getElementById('themeToggle');function applyTheme(theme){document.body.classList.toggle('light',theme==='light');themeToggle.textContent=theme==='light'?'🌙 Dark':'☀️ Light';try{localStorage.setItem('oneclick_theme',theme);}catch(e){}}let savedTheme='dark';try{savedTheme=localStorage.getItem('oneclick_theme')||'dark';}catch(e){}applyTheme(savedTheme);themeToggle.addEventListener('click',()=>applyTheme(document.body.classList.contains('light')?'dark':'light'));document.getElementById('openSettings').addEventListener('click',()=>settingsWrap.classList.remove('hidden'));function bindKeyToggle(buttonId,input){const toggle=document.getElementById(buttonId);toggle.addEventListener('click',()=>{const showing=input.type==='text';input.type=showing?'password':'text';toggle.textContent=showing?'ကြည့်ရန်':'ဖျောက်ရန်';toggle.setAttribute('aria-label',(showing?'Show':'Hide')+' API key');});}bindKeyToggle('toggleGeminiKey',geminiInput);bindKeyToggle('toggleGroqKey',groqInput);document.getElementById('closeSettings').addEventListener('click',()=>settingsWrap.classList.add('hidden'));settingsWrap.addEventListener('click',e=>{if(e.target===settingsWrap)settingsWrap.classList.add('hidden');});
const previewVideo=document.getElementById('previewVideo'),previewLogo=document.getElementById('previewLogo'),previewText=document.getElementById('previewText'),previewSubtitle=document.getElementById('previewSubtitle');const previewPlay=document.getElementById('previewPlay'),previewSeek=document.getElementById('previewSeek'),previewTime=document.getElementById('previewTime');function timeLabel(s){if(!Number.isFinite(s))s=0;return Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0')}function syncPlayback(){const d=previewVideo.duration||0;previewSeek.value=d?Math.round(previewVideo.currentTime/d*1000):0;previewTime.textContent=timeLabel(previewVideo.currentTime)+' / '+timeLabel(d);previewPlay.textContent=previewVideo.paused?'▶ Play':'❚❚ Pause'}previewPlay.addEventListener('click',()=>{if(previewVideo.paused)previewVideo.play().catch(()=>{});else previewVideo.pause();syncPlayback()});previewSeek.addEventListener('input',()=>{if(previewVideo.duration)previewVideo.currentTime=(+previewSeek.value/1000)*previewVideo.duration});['timeupdate','durationchange','play','pause','ended'].forEach(ev=>previewVideo.addEventListener(ev,syncPlayback));
function updatePreview(){const blur=+document.getElementById('blur').value,size=+document.getElementById('subSize').value,frameHeight=previewVideo.videoHeight||720;['blur','brightness','contrast','saturation','subX','subY','subOutline','logoX','logoY','logoOpacity'].forEach(id=>{const el=document.getElementById(id),out=document.getElementById(id+'Value');if(out)out.textContent=el.value+(id==='subOutline'?'':'%')});const autoEffect=document.getElementById('autoEffect').checked;previewVideo.style.filter=autoEffect?'contrast(1.04) saturate(1.06)':'';const bb=document.getElementById('blurBox');bb.style.display=blur?'block':'none';const frameScaleForBlur=Math.min((previewVideo.clientWidth||800)/(previewVideo.videoWidth||1280),(previewVideo.clientHeight||450)/(previewVideo.videoHeight||720));bb.style.left=document.getElementById('blurX').value+'%';bb.style.top=document.getElementById('blurY').value+'%';bb.style.width=document.getElementById('blurW').value+'%';bb.style.height=document.getElementById('blurH').value+'%';bb.style.backdropFilter='blur('+(blur*2*frameScaleForBlur)+'px)';previewVideo.style.transform=document.getElementById('mirror').checked?'scaleX(-1)':'';document.getElementById('previewLogoText').textContent=document.getElementById('logoText').value;previewLogo.style.left=document.getElementById('logoX').value+'%';previewLogo.style.top=document.getElementById('logoY').value+'%';const frameWidth=previewVideo.videoWidth||1280,boxWidth=previewVideo.clientWidth||800,boxHeight=previewVideo.clientHeight||450;const frameScale=Math.min(boxWidth/frameWidth,boxHeight/(previewVideo.videoHeight||720));const logoImg=document.getElementById('previewLogoImage'),hasLogoImage=!!logoImg.getAttribute('src');if(hasLogoImage){const naturalW=logoImg.naturalWidth||300,naturalH=logoImg.naturalHeight||150;const autoW=Math.max(36,0.18*frameWidth*frameScale);const autoH=autoW*naturalH/naturalW;previewLogo.style.width=autoW+'px';previewLogo.style.height=autoH+'px';previewLogo.style.minWidth='0';previewLogo.style.minHeight='0';previewLogo.style.maxWidth='none';previewLogo.style.maxHeight='none';previewLogo.style.aspectRatio='auto';previewLogo.style.padding='0';previewLogo.style.background='transparent';logoImg.style.width='100%';logoImg.style.height='100%';logoImg.style.maxWidth='none';logoImg.style.maxHeight='none';logoImg.style.objectFit='contain';previewLogo.style.fontSize='12px';}else{previewLogo.style.aspectRatio='auto';previewLogo.style.padding='3px 6px';previewLogo.style.background='#0008';logoImg.style.width='auto';logoImg.style.height='auto';previewLogo.style.width='max-content';previewLogo.style.height='max-content';previewLogo.style.fontSize=(Math.max(12,Math.round(frameHeight*0.18*0.12))*frameScale)+'px';}previewLogo.style.opacity=+document.getElementById('logoOpacity').value/100;previewLogo.style.display=(document.getElementById('logoText').value||hasLogoImage)?'flex':'none';previewText.textContent=document.getElementById('overlayText').value;previewText.style.left='50%';previewText.style.top=(12/(previewVideo.videoHeight||720)*100)+'%';previewText.style.transform='translateX(-50%)';previewText.style.fontSize=(22*frameScale)+'px';previewText.style.color='#fff';previewText.style.textShadow='-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000';const previewHeight=previewVideo.clientHeight||450;previewSubtitle.style.fontSize=Math.max(12,Math.min(36,size*(previewVideo.videoHeight?Math.min((previewVideo.clientWidth||800)/(previewVideo.videoWidth||1280),(previewVideo.clientHeight||450)/frameHeight):1)))+'px';previewSubtitle.style.fontWeight=document.getElementById('subWeight').value;previewSubtitle.style.left=document.getElementById('subX').value+'%';previewSubtitle.style.top=document.getElementById('subY').value+'%';previewSubtitle.style.textShadow='0 0 '+(+document.getElementById('subOutline').value*2)+'px #000,1px 1px '+document.getElementById('subOutline').value+'px #000';previewSubtitle.style.color=({'&H00FFFFFF':'#fff','&H0000FFFF':'#ffff00','&H0000FF00':'#00ff00','&H00FF8080':'#8080ff'})[document.getElementById('subColor').value]||'#fff';previewSubtitle.style.top=document.getElementById('subY').value+'%';}
document.getElementById('subPosition').addEventListener('change',()=>{const p=document.getElementById('subPosition').value;document.getElementById('subY').value=p==='top'?20:(p==='middle'?50:88);updatePreview();});
['blur','subSize','subColor','subPosition','subX','subY','subWeight','subOutline','mirror','logoText','logoX','logoY','logoOpacity','overlayText','autoEffect','blurX','blurY','blurW','blurH'].forEach(id=>{const el=document.getElementById(id);if(el)el.addEventListener('input',updatePreview);});
document.getElementById('logoImage').addEventListener('change',e=>{const f=e.target.files[0],img=document.getElementById('previewLogoImage');if(img.dataset.url)URL.revokeObjectURL(img.dataset.url);img.dataset.url='';img.removeAttribute('src');if(f){img.dataset.url=URL.createObjectURL(f);img.src=img.dataset.url;img.style.display='block';document.getElementById('previewLogoText').style.display='none';}else{img.style.display='none';document.getElementById('previewLogoText').style.display='';}updatePreview();});
function bindDrag(element,xControl,yControl,resizeHandle,sizeControl,min,max){let drag=null;element.addEventListener('pointerdown',e=>{if(e.target===resizeHandle||e.target===document.getElementById('blurHeightResize'))return;const box=document.getElementById('preview').getBoundingClientRect();drag={mode:'move',px:e.clientX,py:e.clientY,x:+document.getElementById(xControl).value,y:+document.getElementById(yControl).value,w:sizeControl?+document.getElementById(sizeControl).value:0,box};element.setPointerCapture(e.pointerId);e.preventDefault();});element.addEventListener('pointermove',e=>{if(!drag||drag.mode!=='move')return;const dx=(e.clientX-drag.px)/drag.box.width*100,dy=(e.clientY-drag.py)/drag.box.height*100;document.getElementById(xControl).value=Math.max(0,Math.min(100,drag.x+dx));document.getElementById(yControl).value=Math.max(0,Math.min(100,drag.y+dy));updatePreview();});element.addEventListener('pointerup',()=>drag=null);element.addEventListener('pointercancel',()=>drag=null);if(resizeHandle&&sizeControl){resizeHandle.addEventListener('pointerdown',e=>{e.preventDefault();e.stopPropagation();const box=document.getElementById('preview').getBoundingClientRect();drag={mode:'resize',px:e.clientX,py:e.clientY,w:+document.getElementById(sizeControl).value,box};resizeHandle.setPointerCapture(e.pointerId);});resizeHandle.addEventListener('pointermove',e=>{if(!drag||drag.mode!=='resize')return;const delta=(resizeHandle===document.getElementById('blurHeightResize'))?((e.clientY-drag.py)/drag.box.height*100):((e.clientX-drag.px)/drag.box.width*100);document.getElementById(sizeControl).value=Math.max(min,Math.min(max,drag.w+delta));updatePreview();});resizeHandle.addEventListener('pointerup',()=>drag=null);resizeHandle.addEventListener('pointercancel',()=>drag=null);}}
bindDrag(previewLogo,'logoX','logoY',null,null,0,0);bindDrag(previewSubtitle,'subX','subY',document.getElementById('subtitleResize'),'subSize',14,36);bindDrag(document.getElementById('blurBox'),'blurX','blurY',document.getElementById('blurResize'),'blurW',10,100);bindDrag(document.getElementById('blurBox'),'blurX','blurY',document.getElementById('blurHeightResize'),'blurH',5,50);

// Removed music controls; no listener needed.
let currentStep=0;function showStep(n){currentStep=Math.max(0,Math.min(3,n));document.querySelectorAll('.wizard-step').forEach(el=>el.classList.toggle('active',Number(el.dataset.step)===currentStep));document.querySelectorAll('.step-progress span').forEach((el,i)=>el.classList.toggle('on',i<=currentStep));window.scrollTo(0,0);}document.querySelectorAll('.next-step').forEach(el=>el.addEventListener('click',()=>{if(currentStep===0){const url=document.getElementById('url').value.trim(),file=document.getElementById('videoFile').files[0];if((!url&&!file)||(url&&file)){alert('Video URL သို့မဟုတ် Upload Video တစ်ခုကို ရွေးပါ။');return;}}showStep(Number(el.dataset.next));}));document.querySelectorAll('.back-step').forEach(el=>el.addEventListener('click',()=>showStep(Number(el.dataset.back))));
previewVideo.addEventListener('loadedmetadata',()=>{if(previewVideo.videoWidth>0&&previewVideo.videoHeight>0){const preview=document.getElementById('preview'),ratio=previewVideo.videoWidth/previewVideo.videoHeight;preview.style.aspectRatio=previewVideo.videoWidth+'/'+previewVideo.videoHeight;preview.style.width='min(100%, calc(70vh * '+ratio+'))';preview.style.maxHeight='none';updatePreview();}});function loadPreviewSource(){const preview=document.getElementById('preview');preview.style.aspectRatio='16/9';preview.style.width='100%';preview.style.maxHeight='70vh';const file=document.getElementById('videoFile').files[0],url=document.getElementById('url').value.trim();if(previewVideo.dataset.objectUrl){URL.revokeObjectURL(previewVideo.dataset.objectUrl);previewVideo.dataset.objectUrl='';}previewVideo.pause();previewVideo.removeAttribute('src');if(file){previewVideo.dataset.objectUrl=URL.createObjectURL(file);previewVideo.src=previewVideo.dataset.objectUrl;previewVideo.classList.remove('hidden');previewPlay.classList.remove('hidden');previewSeek.classList.remove('hidden');previewVideo.load();document.getElementById('previewHint').textContent='Upload Video ကို တိုက်ရိုက်ပြထားပါတယ်။ Subtitle Preview ကို ဖုန်းမျက်နှာပြင်နဲ့ကိုက်ညီအောင် ချိန်ထားပါတယ်။ Subtitle/Logo ကို ဖိဆွဲရွှေ့နိုင်ပါတယ်။';}else if(url&&/\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(url)){previewVideo.src=url;previewVideo.classList.remove('hidden');previewPlay.classList.remove('hidden');previewSeek.classList.remove('hidden');previewVideo.load();document.getElementById('previewHint').textContent='URL Preview — direct video link ဖြစ်ရပါမယ်။ YouTube/TikTok webpage link များကို browser preview မှာ တိုက်ရိုက်မဖွင့်နိုင်နိုင်ပါ။';}else{previewVideo.classList.add('hidden');previewPlay.classList.add('hidden');previewSeek.classList.add('hidden');document.getElementById('previewHint').textContent='Upload Video ရွေးပါ သို့မဟုတ် တိုက်ရိုက် MP4 URL ထည့်ပါ။';}syncPlayback();updatePreview();}document.getElementById('videoFile').addEventListener('change',loadPreviewSource);document.getElementById('url').addEventListener('change',loadPreviewSource);document.getElementById('url').addEventListener('input',()=>{if(!document.getElementById('videoFile').files[0]){clearTimeout(window.previewUrlTimer);window.previewUrlTimer=setTimeout(loadPreviewSource,700);}});
document.getElementById('language').addEventListener('change',()=>{const v=document.getElementById('voice');if(document.getElementById('language').value==='my'){if(!v.value.startsWith('my-MM-'))v.value='my-MM-NilarNeural';}else if(v.value.startsWith('my-MM-'))v.value='en-US-AriaNeural';});const voiceSpeed=document.getElementById('voiceSpeed');voiceSpeed.addEventListener('input',()=>{document.getElementById('voiceSpeedValue').textContent=voiceSpeed.value+'%';});updatePreview();
function loadSavedKeys(){try{geminiInput.value=localStorage.getItem('oneclick_gemini_key')||'';groqInput.value=localStorage.getItem('oneclick_groq_key')||'';keyStatus.textContent=(geminiInput.value&&groqInput.value)?'ဒီ Browser ထဲမှာ Key နှစ်ခုလုံး သိမ်းထားသည်။':'Key မသိမ်းရသေးပါ။';}catch(e){keyStatus.textContent='ဒီ Browser မှာ local storage အသုံးပြုမရပါ။';}}
document.getElementById('saveKeys').addEventListener('click',()=>{const g=geminiInput.value.trim(),q=groqInput.value.trim();if(!g||!q){keyStatus.textContent='Gemini နဲ့ Groq Key နှစ်ခုလုံး ထည့်ပါ။';return;}try{localStorage.setItem('oneclick_gemini_key',g);localStorage.setItem('oneclick_groq_key',q);keyStatus.textContent='Key နှစ်ခုလုံးကို ဒီ Browser ထဲမှာ Save လုပ်ပြီးပါပြီ။';}catch(e){keyStatus.textContent='Save မလုပ်နိုင်ပါ။ Browser storage setting ကို စစ်ပါ။';}});
document.getElementById('clearKeys').addEventListener('click',()=>{try{localStorage.removeItem('oneclick_gemini_key');localStorage.removeItem('oneclick_groq_key');}catch(e){}geminiInput.value='';groqInput.value='';keyStatus.textContent='Browser ထဲက Saved Keys ကို ဖျက်ပြီးပါပြီ။';});
loadSavedKeys();
form.addEventListener('submit',async(e)=>{e.preventDefault();const geminiKey=geminiInput.value.trim(),groqKey=groqInput.value.trim(),videoFile=document.getElementById('videoFile').files[0],videoUrl=document.getElementById('url').value.trim();if(!geminiKey||!groqKey){result.classList.remove('hidden');statusBox.textContent='အရင်ဆုံး API Key Settings မှာ Gemini နဲ့ Groq Key နှစ်ခုလုံးထည့်ပြီး Save Keys ကိုနှိပ်ပါ။';return;}if((!videoUrl&&!videoFile)||(videoUrl&&videoFile)){result.classList.remove('hidden');statusBox.textContent='Video URL သို့မဟုတ် Upload Video တစ်ခုကိုပဲ ရွေးပါ။';return;}btn.disabled=true;result.classList.remove('hidden');links.innerHTML='';statusBox.textContent='အလုပ်စတင်နေသည်…';
try{const body=new FormData();const options={voice:document.getElementById('voice').value,voice_speed:document.getElementById('voiceSpeed').value,auto_effect:document.getElementById('autoEffect').checked,blur:document.getElementById('blur').value,blur_x:document.getElementById('blurX').value,blur_y:document.getElementById('blurY').value,blur_w:document.getElementById('blurW').value,blur_h:document.getElementById('blurH').value,mirror:document.getElementById('mirror').checked,sub_size:document.getElementById('subSize').value,sub_color:document.getElementById('subColor').value,sub_position:document.getElementById('subPosition').value,sub_x:document.getElementById('subX').value,sub_y:document.getElementById('subY').value,sub_weight:document.getElementById('subWeight').value,sub_outline:document.getElementById('subOutline').value,logo_text:document.getElementById('logoText').value,logo_x:document.getElementById('logoX').value,logo_y:document.getElementById('logoY').value,logo_opacity:document.getElementById('logoOpacity').value,overlay_text:document.getElementById('overlayText').value,};body.append('url',videoFile?'':videoUrl);body.append('language',document.getElementById('language').value);body.append('gemini_api_key',geminiKey);body.append('groq_api_key',groqKey);Object.entries(options).forEach(([k,v])=>body.append(k,String(v)));if(videoFile)body.append('video',videoFile);const logoFile=document.getElementById('logoImage').files[0];if(logoFile)body.append('logo_image',logoFile);const res=await fetch('/api/jobs',{method:'POST',body});const data=await res.json();if(!res.ok)throw new Error(data.error||'စတင်၍မရပါ');poll(data.id);}catch(err){statusBox.textContent='အမှား: '+err.message;btn.disabled=false;}});
async function poll(id){try{const res=await fetch('/api/jobs/'+id);const data=await res.json();if(!res.ok){if(res.status===404){statusBox.textContent='အရင် Job ကို server မှာ မတွေ့တော့ပါ။ Render restart/redeploy ဖြစ်ခဲ့နိုင်ပါတယ်။ Video ရွေးထားဆဲဆိုရင် Generate Final Video ကို ထပ်နှိပ်ပြီး Job အသစ်စတင်ပါ။';links.innerHTML='';btn.disabled=false;showStep(3);return;}throw new Error(data.error||'အခြေအနေမရပါ');}statusBox.textContent=data.message||data.status;
if(data.status==='done'){links.innerHTML='<h3>🎧 AI Voice အစမ်းနားထောင်ရန်</h3><audio id="voicePreviewAudio" controls preload="metadata" style="width:100%;margin:6px 0" src="/api/jobs/'+id+'/voice-preview"></audio><label for="voicePreviewSpeed">Preview Speed <span id="voicePreviewSpeedValue">100%</span></label><input id="voicePreviewSpeed" type="range" min="70" max="130" step="5" value="100" style="width:100%"><p class="muted small">နားထောင်နေစဉ် အမြန်နှုန်းကို စမ်းချိန်နိုင်သည်။ Final MP3 speed သည် Generate မလုပ်မီ ရွေးထားသည့် AI Voice Speed အတိုင်း ဖြစ်သည်။</p><a class="download" href="/api/jobs/'+id+'/download">MP4 ဒေါင်းလုဒ်လုပ်ရန်</a><a class="download" style="background:#35415a;color:#fff" href="/api/jobs/'+id+'/script">Recap Script ကိုဖတ်ရန်</a><a class="download" style="background:#237a57;color:#fff" href="/api/jobs/'+id+'/srt">SRT Subtitle ဒေါင်းလုဒ်လုပ်ရန်</a><a class="download" style="background:#7652b8;color:#fff" href="/api/jobs/'+id+'/voice">AI Voice MP3 ဒေါင်းလုဒ်လုပ်ရန်</a>';const previewAudio=document.getElementById('voicePreviewAudio'),previewSpeed=document.getElementById('voicePreviewSpeed');previewSpeed.addEventListener('input',()=>{previewAudio.playbackRate=Number(previewSpeed.value)/100;document.getElementById('voicePreviewSpeedValue').textContent=previewSpeed.value+'%';});btn.disabled=false;showStep(3);statusBox.scrollIntoView({behavior:'smooth',block:'center'});return;}
if(data.status==='error'){statusBox.textContent='မအောင်မြင်ပါ: '+data.message;btn.disabled=false;return;}setTimeout(()=>poll(id),3000);
}catch(err){statusBox.textContent='အခြေအနေစစ်ရာတွင် ပြဿနာ: '+err.message;btn.disabled=false;}}
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

def download_video(url, destination):
    response = requests.get(url, stream=True, timeout=(15, 30), allow_redirects=True,
                            headers={"User-Agent": "OneClickMovieRecap/1.0"})
    response.raise_for_status()
    final_url = validate_public_http_url(response.url)
    content_type = response.headers.get("Content-Type", "").lower()
    length = response.headers.get("Content-Length")
    if length and int(length) > MAX_VIDEO_BYTES:
        fail("ဗီဒီယို 80 MB ထက်ကြီးနေသည်။ ပိုသေးသော clip ကို အသုံးပြုပါ။")
    total = 0
    with open(destination, "wb") as output:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_VIDEO_BYTES:
                fail("ဗီဒီယို 80 MB ထက်ကြီးနေသည်။ ပိုသေးသော clip ကို အသုံးပြုပါ။")
            output.write(chunk)
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
                    error_text = response.text[:3000]
                    retry_match = re.search(r"retry in ([0-9hms. ]+)", error_text, re.IGNORECASE)
                    retry_hint = (" Google က " + retry_match.group(1).strip() + " နောက်မှ ပြန်စမ်းရန် ပြထားသည်။") if retry_match else " Google AI Studio မှာ usage/quota ကို စစ်ပါ။"
                    fail("Gemini API quota/rate limit (429) ဖြစ်နေသည်။" + retry_hint + " ခဏစောင့်ပြီး ပြန်စမ်းပါ။")
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
    """Remove invisible format characters that can split Burmese syllable shaping."""
    return str(text or "").replace("\u200c", "").replace("\u200b", "").replace("\ufeff", "")


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

def make_subtitles(script, voice_path, srt_path, video_path=None, options=None):
    script = clean_myanmar_text(script)
    options = options or {}
    duration_text = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "default=noprint_wrappers=1:nokey=1", str(voice_path)], timeout=30)
    duration = float(duration_text)
    width, height = 1280, 720
    if video_path:
        dimensions = run_command(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(video_path)], timeout=30)
        try: width, height = [int(v) for v in dimensions.strip().split("x")[:2]]
        except Exception: pass
    if duration <= 0: fail("Voice audio duration မမှန်ပါ။")
    chunks = split_sentences(script)
    weights = [max(1, len(chunk)) for chunk in chunks]
    total_weight = sum(weights)
    x = round(width * max(5, min(95, int(options.get("sub_x", 50)))) / 100)
    default_y = {"bottom":88,"middle":50,"top":20}.get(options.get("sub_position"),88)
    y = round(height * max(5, min(95, int(options.get("sub_y", default_y))) ) / 100)
    size = max(14, min(36, int(options.get("sub_size",20))))
    # Scale subtitle font to the source video height (720p reference).
    render_size = max(14, round(size * height / 720))
    color = options.get("sub_color","&H00FFFFFF")
    bold = -1 if options.get("sub_weight","bold") == "bold" else 0
    outline = max(0,min(6,int(options.get("sub_outline",2))))
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\nScaledBorderAndShadow: yes\n")
        f.write(f"PlayResX: {width}\nPlayResY: {height}\n\n[V4+ Styles]\n")
        f.write("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
        # Use the user's uploaded A ka 08 font when bundled in fonts/; keep a safe fallback.
        custom_font = Path(__file__).resolve().parent / "fonts" / "eka-08-regular.ttf"
        font_family = "A ka 08" if custom_font.is_file() else "Noto Sans Myanmar"
        f.write(f"Style: Default,{font_family},{render_size},{color},&H000000FF,&H00000000,&H80000000,{bold},0,1,{outline},1,5,10,10,10,1\n")
        logo_text = (options.get("logo_text") or "").strip().replace("{","(").replace("}",")").replace("\n"," ")
        if logo_text:
            logo_width = max(5, min(60, int(options.get("logo_w",18))))
            logo_size = max(12, round(height * logo_width / 100 * 0.12))
            logo_x = round(width * max(0, min(100, int(options.get("logo_x",90)))) / 100)
            logo_y = round(height * max(0, min(100, int(options.get("logo_y",8)))) / 100)
            logo_opacity = max(10, min(100, int(options.get("logo_opacity",100))))
            logo_alpha = round(255 * (100-logo_opacity) / 100)
            f.write(f"Style: Logo,Noto Sans Myanmar,{logo_size},&H{logo_alpha:02X}FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,1,1,0,5,10,10,10,1\n")
        f.write("\n[Events]\n")
        f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        current = 0.0
        def ass_time(seconds):
            cs = int(max(0,seconds)*100)
            hh,rem=divmod(cs,360000); mm,rem=divmod(rem,6000); ss,cc=divmod(rem,100)
            return f"{hh}:{mm:02}:{ss:02}.{cc:02}"
        if logo_text:
            f.write(f"Dialogue: 1,0:00:00.00,{ass_time(duration)},Logo,,0,0,0,,{{\\pos({logo_x},{logo_y})}}{logo_text}\n")
        for chunk,weight in zip(chunks,weights):
            start=current; current += duration*weight/total_weight; end=min(duration,current)
            safe=chunk.replace("{","(").replace("}",")").replace("\\","\\\\").replace("\n"," ")
            f.write(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{{\\pos({x},{y})}}{safe}\n")

def render_video(video_path, voice_path, srt_path, output_path, options, music_path=None, logo_path=None):
    srt_name = str(srt_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    blur = max(0, min(10, int(options.get("blur", 0))))
    bx = max(0, min(100, int(options.get("blur_x", 50))))
    by = max(0, min(100, int(options.get("blur_y", 84))))
    bw = max(10, min(100, int(options.get("blur_w", 55))))
    bh = max(5, min(50, int(options.get("blur_h", 12))))

    def esc_text(value):
        return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%").replace(",", "\\,")

    logo = ""
    overlay = (options.get("overlay_text") or "").strip()
    base_filters = []
    if options.get("mirror"):
        base_filters.append("hflip")
    if options.get("auto_effect", True):
        base_filters.append("eq=contrast=1.04:saturation=1.06")

    # Text layers use the uploaded Myanmar font bundled in /app/fonts.
    custom_font = Path(__file__).resolve().parent / "fonts" / "eka-08-regular.ttf"
    font_path = str(custom_font) if custom_font.is_file() else "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf"
    # Overlay text uses Padauk too, so Burmese shaping matches the subtitles.
    if overlay:
        base_filters.append("drawtext=fontfile='" + font_path + "':text='" + esc_text(overlay) + "':fontcolor=white:fontsize=22:borderw=2:bordercolor=black:x=(w-text_w)/2:y=12")

    command = ["ffmpeg", "-y", "-i", str(video_path), "-i", str(voice_path)]
    music_index = None
    logo_index = None
    if music_path and music_path.exists() and music_path.stat().st_size:
        command += ["-stream_loop", "-1", "-i", str(music_path)]
        music_index = 2
    if logo_path and logo_path.exists() and logo_path.stat().st_size:
        command += ["-i", str(logo_path)]
        logo_index = 3 if music_index is not None else 2

    # Build one graph so mirror, regional blur, subtitles, and image watermark work together.
    graph = []
    pre = ",".join(base_filters) if base_filters else "null"
    if blur > 0:
        graph.append("[0:v:0]" + pre + ",split=2[vclean][vblur_src]")
        crop_x = f"max(0,min(iw-iw*{bw/100},iw*{bx/100}-iw*{bw/100}/2))"
        crop_y = f"max(0,min(ih-ih*{bh/100},ih*{by/100}-ih*{bh/100}/2))"
        graph.append(f"[vblur_src]crop=w=iw*{bw/100}:h=ih*{bh/100}:x='{crop_x}':y='{crop_y}',boxblur={max(1,blur*2)}:1[vblur]")
        # Use the same clamped top-left coordinates for crop and overlay.
        # Without this, dragging the box near an edge makes the final blur drift
        # away from the Live Edit preview because crop() clamps but overlay() did not.
        overlay_x = f"W*{max(0, min(1-bw/100, bx/100-bw/200))}"
        overlay_y = f"H*{max(0, min(1-bh/100, by/100-bh/200))}"
        graph.append(f"[vclean][vblur]overlay=x='{overlay_x}':y='{overlay_y}':eof_action=repeat[vblurred]")
        graph.append("[vblurred]subtitles=filename='" + srt_name + "'[vbase]")
    else:
        graph.append("[0:v:0]" + pre + ",subtitles=filename='" + srt_name + "'[vbase]")

    if logo_index is not None:
        lx, ly = int(options.get("logo_x",90)), int(options.get("logo_y",8))
        op = max(10,min(100,int(options.get("logo_opacity",100))))/100
        graph.append(f"[{logo_index}:v:0][vbase]scale2ref=w=main_w*0.18:h=main_h*0.18:force_original_aspect_ratio=decrease[logo][vref]")
        graph.append("[logo]format=rgba,colorchannelmixer=aa=" + str(op) + "[logoalpha]")
        graph.append("[vref][logoalpha]overlay=x='W*" + str(lx/100) + "-w/2':y='H*" + str(ly/100) + "-h/2':eof_action=repeat[vout]")
    else:
        graph.append("[vbase]null[vout]")

    if music_index is not None:
        volume=max(0,min(100,int(options.get("music_volume",20))))/100
        graph += ["[1:a:0]loudnorm=I=-16:TP=-1.5:LRA=11,volume=1[voice]",f"[{music_index}:a:0]volume={volume}[music]","[voice][music]amix=inputs=2:duration=first:dropout_transition=2,loudnorm=I=-16:TP=-1.5:LRA=11[aout]"]
    else:
        graph.append("[1:a:0]loudnorm=I=-16:TP=-1.5:LRA=11[aout]")

    command += ["-filter_complex",";".join(graph),"-map","[vout]","-map","[aout]",
                "-shortest","-c:v","libx264","-preset","ultrafast","-crf","28",
                "-c:a","aac","-b:a","192k","-ar","44100","-map_metadata","-1",
                "-movflags","+faststart",str(output_path)]
    run_command(command, timeout=420)
    if not output_path.exists() or output_path.stat().st_size == 0:
        fail("MP4 ဖိုင် မထွက်လာပါ။")
    audio_check = run_command(["ffprobe","-v","error","-select_streams","a:0","-show_entries","stream=codec_name","-of","default=noprint_wrappers=1:nokey=1",str(output_path)], timeout=30).strip()
    if not audio_check:
        fail("Final MP4 ထဲမှာ Audio Stream မပါပါ။")


def make_srt(script, voice_path, srt_path):
    """Create SRT timings proportionally to the generated voice duration."""
    duration_text = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "default=noprint_wrappers=1:nokey=1", str(voice_path)], timeout=30)
    duration = float(duration_text)
    chunks = [chunk.strip() for chunk in split_sentences(clean_myanmar_text(script)) if chunk.strip()]
    if duration <= 0 or not chunks:
        fail("SRT ဖိုင်ဖန်တီးရန် Voice duration သို့မဟုတ် Script မမှန်ပါ။")
    weights = [max(1, len(chunk)) for chunk in chunks]
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

def process_job(job_id, url, language, workdir, gemini_api_key, groq_api_key, options, uploaded=False):
    try:
        video = workdir / "source.mp4"
        original_audio = workdir / "source.mp3"
        voice = workdir / "recap_voice.mp3"
        subtitles = workdir / "recap.ass"
        output = workdir / "movie_recap.mp4"

        if uploaded:
            size = video.stat().st_size
            update_job(job_id, status="running", message=f"တင်ထားသော ဗီဒီယိုကို လုပ်ဆောင်နေသည် ({size // (1024*1024)} MB)…")
        else:
            update_job(job_id, status="running", message="ဗီဒီယိုကို URL မှ ဒေါင်းလုဒ်လုပ်နေသည်…")
            size, _ = download_video(url, video)
            update_job(job_id, message=f"ဗီဒီယိုရရှိပါပြီ ({size // (1024*1024)} MB)။ အသံထုတ်နေသည်…")
        extract_audio(video, original_audio)

        update_job(job_id, message="Groq Whisper ဖြင့် အသံကို စာသားပြောင်းနေသည်…")
        transcript = groq_transcribe(original_audio, groq_api_key)

        update_job(job_id, message="Gemini 3.8 Flash ဖြင့် recap စာသားရေးနေသည်…")
        script = make_recap(transcript, language, gemini_api_key)
        # Gemini/transcription may insert invisible ZWNJ/zero-width characters
        # inside Burmese syllables (e.g. "မ‌ောင်"); remove them before TTS and subtitles.
        script = clean_myanmar_text(script)
        (workdir / "recap.txt").write_text(script, encoding="utf-8")

        update_job(job_id, message="AI Voice ဖန်တီးနေသည်…")
        synthesize_voice(script, language, voice, options.get("voice", "my-MM-NilarNeural" if language == "my" else "en-US-AriaNeural"), options.get("voice_speed", 100))

        update_job(job_id, message="Subtitle အချိန်ညှိနေသည်…")
        music_path = workdir / "background_music"
        if not music_path.exists():
            music_path = None
        logo_path = workdir / "logo_image"
        if not logo_path.exists():
            logo_path = None
        # Show one consistent logo in preview and final output: uploaded image takes
        # precedence; otherwise use the default/custom text watermark.
        if logo_path:
            options["logo_text"] = ""
        make_subtitles(script, voice, subtitles, video, options)
        srt_file = workdir / "recap.srt"
        make_srt(script, voice, srt_file)

        update_job(job_id, message="နောက်ဆုံး MP4 ကို တည်ဆောက်နေသည်။ ခဏစောင့်ပါ…")
        render_video(video, voice, subtitles, output, options, music_path, logo_path)
        save_latest_artifacts(workdir)
        update_job(job_id, status="done", message="ပြီးပါပြီ။ MP4, Script, SRT နဲ့ AI Voice ကို ဒေါင်းလုဒ်လုပ်နိုင်ပါပြီ။",
                   output=str(output), script=str(workdir / "recap.txt"), srt=str(srt_file), voice=str(voice), finished=time.time())
    except Exception as exc:
        update_job(job_id, status="error", message=str(exc)[:1500], finished=time.time())
    finally:
        # Keep only completed outputs temporarily for download; remove source and intermediates.
        try:
            for name in ("source.mp4", "source.mp3", "background_music", "logo_image"):
                path = workdir / name
                if path.exists():
                    path.unlink()
        except OSError:
            pass

@app.get("/")
def index():
    return render_template_string(HTML)

@app.get("/health")
def health():
    return jsonify({"ok": True, "gemini_model": GEMINI_MODEL})

@app.post("/api/jobs")
def create_job():
    from flask import request
    uploaded_file = None
    music_file = None
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
        options = {"voice": request.form.get("voice", ""), "voice_speed": request.form.get("voice_speed", "100"), "brightness": request.form.get("brightness", "100"), "contrast": request.form.get("contrast", "100"), "saturation": request.form.get("saturation", "100"), "blur": request.form.get("blur", "0"), "mirror": request.form.get("mirror") == "true", "sub_size": request.form.get("sub_size", "20"), "sub_color": request.form.get("sub_color", "&H00FFFFFF"), "sub_position": request.form.get("sub_position", "bottom"), "logo_text": request.form.get("logo_text", "")[:40], "overlay_text": request.form.get("overlay_text", "")[:100], "music_volume": request.form.get("music_volume", "20"), "auto_effect": request.form.get("auto_effect", "true") == "true", "blur_x": request.form.get("blur_x","50"), "blur_y": request.form.get("blur_y","84"), "blur_w": request.form.get("blur_w","55"), "blur_h": request.form.get("blur_h","12"), "sub_x": request.form.get("sub_x","50"), "sub_y": request.form.get("sub_y","88"), "sub_weight": request.form.get("sub_weight","bold"), "sub_outline": request.form.get("sub_outline","2"), "logo_x": request.form.get("logo_x","90"), "logo_y": request.form.get("logo_y","8"), "logo_opacity": request.form.get("logo_opacity","100")}
        uploaded_file = request.files.get("video")
        if uploaded_file and not uploaded_file.filename:
            uploaded_file = None
        music_file = request.files.get("music")
        logo_file = None
        if logo_file and not logo_file.filename: logo_file = None
        if music_file and not music_file.filename:
            music_file = None
    if not gemini_api_key or not groq_api_key:
        return jsonify({"error": "Website API Key Settings မှာ Gemini နဲ့ Groq Key နှစ်ခုလုံး ထည့်ပါ။"}), 400
    if len(gemini_api_key) > 512 or len(groq_api_key) > 512:
        return jsonify({"error": "API Key ပုံစံမမှန်ပါ။ Key ကို ပြန်စစ်ပါ။"}), 400
    if not url and not uploaded_file:
        return jsonify({"error": "Video URL ထည့်ပါ သို့မဟုတ် ဖုန်းထဲက Video ကို Upload လုပ်ပါ။"}), 400
    if url and uploaded_file:
        return jsonify({"error": "URL နဲ့ Upload ကို တစ်ခုတည်းသာ ရွေးပါ။"}), 400
    if language not in ("my", "en"):
        return jsonify({"error": "ဘာသာစကားရွေးချယ်မှု မမှန်ပါ။"}), 400
    if request.is_json: options = data.get("options") or {}
    if request.is_json: logo_file = None
    allowed_voices = ("my-MM-NilarNeural", "my-MM-ThihaNeural", "en-US-AriaNeural", "en-US-GuyNeural", "en-GB-SoniaNeural")
    if options.get("voice") not in allowed_voices: options["voice"] = "my-MM-NilarNeural" if language == "my" else "en-US-AriaNeural"
    try: options["voice_speed"] = max(70, min(130, int(options.get("voice_speed", 100))))
    except (TypeError, ValueError): options["voice_speed"] = 100
    try:
        options["blur"] = max(0, min(10, int(options.get("blur", 0))))
        options["sub_size"] = max(20, min(64, int(options.get("sub_size", 40))))
    except (TypeError, ValueError):
        return jsonify({"error": "Video edit setting မမှန်ပါ။"}), 400
    if options.get("sub_color") not in ("&H00FFFFFF", "&H0000FFFF", "&H0000FF00", "&H00FF8080"): options["sub_color"] = "&H00FFFFFF"
    if options.get("sub_position") not in ("bottom", "middle", "top"): options["sub_position"] = "bottom"
    options["mirror"] = str(options.get("mirror", "")).lower() in ("true", "1", "yes") if not isinstance(options.get("mirror"), bool) else options.get("mirror")
    options["auto_lighting"] = str(options.get("auto_lighting", True)).lower() in ("true", "1", "yes") if not isinstance(options.get("auto_lighting", True), bool) else options.get("auto_lighting", True)
    try:
        options["music_volume"] = max(0, min(100, int(options.get("music_volume", 20))))
    except (TypeError, ValueError):
        options["music_volume"] = 20
    options["logo_text"] = ""
    for key, default, low, high in (("blur_x",50,0,100),("blur_y",84,0,100),("blur_w",55,10,100),("blur_h",12,5,50),("sub_x",50,5,95),("sub_y",88,5,95),("sub_outline",2,0,6),("logo_x",90,0,100),("logo_y",8,0,100),("logo_opacity",100,10,100)):
        try: options[key] = max(low, min(high, int(options.get(key, default))))
        except (TypeError, ValueError): options[key] = default
    if options.get("sub_weight") not in ("bold","normal"): options["sub_weight"] = "bold"
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
    if logo_file and not (logo_file.filename or "").lower().endswith((".png",".jpg",".jpeg",".webp")):
        return jsonify({"error":"Logo ပုံကို PNG, JPG သို့မဟုတ် WEBP သာ သုံးပါ။"}), 400
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
    if logo_file:
        logo_path = workdir / "logo_image"
        logo_file.save(logo_path)
        if logo_path.stat().st_size == 0 or logo_path.stat().st_size > 5*1024*1024:
            logo_path.unlink(missing_ok=True)
            if uploaded: (workdir / "source.mp4").unlink(missing_ok=True)
            workdir.rmdir()
            return jsonify({"error":"Logo ပုံသည် 5 MB အောက် ဖြစ်ရပါမည်။"}), 413
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
        JOBS[job_id] = {"status": "queued", "message": "အလုပ်တန်းစီနေသည်…", "created": time.time()}
        _save_job_locked(job_id)
    thread = threading.Thread(target=process_job, args=(job_id, url, language, workdir, gemini_api_key, groq_api_key, options, uploaded), daemon=True)
    thread.start()
    return jsonify({"id": job_id, "status": "queued"}), 202

@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({
                "error": "ဒီ Job ID ကို လက်ရှိ server မှာ မတွေ့တော့ပါ။ Render restart/redeploy ဖြစ်ခဲ့နိုင်ပါတယ်။ Video ဖိုင်ရှိနေသေးလျှင် Generate Final Video ကို ပြန်နှိပ်ပါ။",
                "recovery": "job_not_found"
            }), 404
        safe = {k: v for k, v in job.items() if k in ("status", "message", "created", "finished")}
        return jsonify(safe)

@app.get("/api/jobs/<job_id>/download")
def download_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    path = Path(job.get("output", ""))
    if not path.is_file() or path.parent.parent != OUTPUT_ROOT:
        abort(404)
    return send_file(path, as_attachment=True, download_name="movie_recap.mp4", mimetype="video/mp4")

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
