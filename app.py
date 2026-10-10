import ipaddress
import os
import re
import socket
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
OUTPUT_ROOT = Path(tempfile.gettempdir()) / "movie-recap-jobs"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

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
label{display:block;margin:7px 0 4px;font-weight:700;font-size:13px}input,select{width:100%;padding:8px;border-radius:7px;border:1px solid var(--field-border);background:var(--field);color:var(--text);font-size:14px}input[type=range]{padding:0;height:18px}.option-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}.edit-tools{border:1px solid var(--border);border-radius:9px;padding:9px;margin-top:8px}.edit-tools h3{font-size:15px;margin:2px 0 7px}.edit-tools label{margin:5px 0 3px;font-size:12px}.edit-tools input:not([type=range]),.edit-tools select{padding:7px;font-size:13px}.edit-tools .preview{margin-top:7px}.preview-title{font-size:13px;font-weight:700;margin:4px 0}.preview-hint{font-size:11px;margin:5px 0}.preview{position:relative;overflow:hidden;border-radius:10px;background:#05070d;aspect-ratio:16/9;margin-top:10px}.preview video{width:100%;height:100%;object-fit:contain;transition:filter .15s,transform .15s}.playback-controls{display:flex;align-items:center;gap:8px;margin-top:6px}.playback-controls button{width:auto;min-width:72px;margin:0;padding:6px 10px;font-size:12px}.playback-controls input{flex:1;min-width:0;padding:0}.playback-time{font-size:11px;color:var(--muted2);white-space:nowrap}.preview-overlay{position:absolute;inset:0;pointer-events:none;display:flex;align-items:flex-end;justify-content:center;padding:5%;text-align:center}.preview-subtitle{color:#fff;text-shadow:0 2px 4px #000,1px 1px 2px #000;font-weight:700}.preview-logo{position:absolute;top:8px;right:8px;color:#fff;background:#0008;padding:3px 6px;border-radius:4px;font-size:12px}.preview-text{position:absolute;top:8px;left:8px;color:white;text-shadow:1px 1px 3px #000;font-size:12px}
button,.download{display:inline-block;border:0;border-radius:8px;background:var(--button);color:var(--button-text);font-weight:800;padding:10px 12px;font-size:14px;text-decoration:none;cursor:pointer;width:100%;margin-top:10px}
button:disabled{opacity:.55;cursor:wait}.key-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.status{padding:10px;border-radius:8px;background:var(--status);white-space:pre-wrap;overflow-wrap:anywhere}.topbar{display:flex;align-items:center;justify-content:space-between;gap:10px}.topbar h1{font-size:20px}.settings-btn{width:auto;margin:0;padding:8px 10px;white-space:nowrap}.settings-wrap{position:fixed;z-index:20;inset:0;background:#0009;display:flex;justify-content:flex-end;align-items:flex-start;padding:12px}.settings-card{width:min(340px,100%);max-height:90vh;overflow:auto;background:var(--panel);border:1px solid var(--field-border);border-radius:12px;padding:14px;box-shadow:0 12px 40px #0008}.settings-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.settings-head h2{margin:0}.close-btn{width:auto;margin:0;padding:5px 10px;background:#35415a;color:white}.settings-card label{margin-top:12px}.settings-card .muted{margin-top:8px}
.note{border-left:3px solid #e8b85b;padding-left:12px}.small{font-size:13px}.hidden{display:none}a{color:#9fb4ff}
</style>
</head>
<body><main>
<div class="topbar"><h1>🎬 One Click Movie Recap</h1><div style="display:flex;gap:7px;align-items:center"><button class="settings-btn" id="themeToggle" type="button" aria-label="Toggle light and dark theme">☀️ Light</button><button class="settings-btn" id="openSettings" type="button">⚙️ Settings</button></div></div>
<p class="muted">Video ထည့်ပါ → Recap MP4 ထုတ်ပါ</p>
<div class="settings-wrap hidden" id="settingsWrap"><div class="settings-card"><div class="settings-head"><h2>⚙️ API Keys</h2><button class="close-btn" id="closeSettings" type="button">✕</button></div>
<label for="geminiKey">Gemini API Key</label>
<input id="geminiKey" type="password" autocomplete="off" placeholder="Gemini API Key ထည့်ပါ">
<label for="groqKey">Groq API Key</label>
<input id="groqKey" type="password" autocomplete="off" placeholder="Groq API Key ထည့်ပါ">
<div class="key-actions"><button id="saveKeys" type="button">Save Keys</button><button id="clearKeys" type="button" style="background:#35415a;color:#fff">Clear</button></div>
<p id="keyStatus" class="muted small" role="status">Key မသိမ်းရသေးပါ။</p>
<p class="muted small"><a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">Gemini Key ↗</a> · <a href="https://console.groq.com/keys" target="_blank" rel="noopener">Groq Key ↗</a></p>
</div></div>
<div class="panel">
<form id="jobform">
<label for="url">Option 1 — Direct video URL</label>
<input id="url" name="url" type="url" placeholder="https://example.com/video.mp4">
<p class="muted small">URL သို့မဟုတ် ဖုန်းထဲက Video တစ်ခုကို ရွေးပါ။</p>
<label for="videoFile">Option 2 — ဖုန်းထဲက Video Upload (MP4 / MOV / WEBM / M4V)</label>
<input id="videoFile" name="video" type="file" accept=".mp4,.mov,.webm,.m4v,video/mp4,video/quicktime,video/webm">
<div class="option-grid"><div><label for="language">Recap ဘာသာ</label><select id="language" name="language"><option value="my">မြန်မာ</option><option value="en">English</option></select></div><div><label for="voice">Voice</label><select id="voice" name="voice"><option value="my-MM-NilarNeural">မြန်မာ Nilar</option><option value="en-US-AriaNeural">English Aria</option><option value="en-US-GuyNeural">English Guy</option><option value="en-GB-SoniaNeural">English Sonia</option></select></div></div>
<div class="preview-title">👁️ Live Edit Preview</div><div class="preview" id="preview"><video id="previewVideo" playsinline class="hidden"></video><div class="preview-logo" id="previewLogo">My Recap</div><div class="preview-text" id="previewText"></div><div class="preview-overlay"><div class="preview-subtitle" id="previewSubtitle">Subtitle Preview</div></div></div><div class="playback-controls"><button id="previewPlay" type="button">▶ Play</button><input id="previewSeek" type="range" min="0" max="1000" value="0" aria-label="Video seek"><span class="playback-time" id="previewTime">0:00 / 0:00</span></div><p class="muted preview-hint" id="previewHint">Upload Video ရွေးပါ။ အောက်က Edit Tools ပြောင်းတာနဲ့ ဒီမှာပြောင်းလဲမှုကို ကြည့်နိုင်ပါတယ်။</p>
<div class="edit-tools"><h3>🎨 Edit Tools</h3><div class="option-grid"><div><label for="brightness">Brightness <span id="brightnessValue">100%</span></label><input id="brightness" type="range" min="50" max="150" value="100"></div><div><label for="contrast">Contrast <span id="contrastValue">100%</span></label><input id="contrast" type="range" min="50" max="150" value="100"></div><div><label for="saturation">Saturation <span id="saturationValue">100%</span></label><input id="saturation" type="range" min="0" max="200" value="100"></div><div><label for="blur">Blur <span id="blurValue">0</span></label><input id="blur" type="range" min="0" max="10" value="0"></div><div><label for="subSize">Subtitle Size</label><input id="subSize" type="range" min="14" max="36" value="20"></div><div><label for="subColor">Subtitle Color</label><select id="subColor"><option value="&H00FFFFFF">White</option><option value="&H0000FFFF">Yellow</option><option value="&H0000FF00">Green</option><option value="&H00FF8080">Light Blue</option></select></div><div><label for="subPosition">Subtitle Position</label><select id="subPosition"><option value="bottom">အောက်</option><option value="middle">အလယ်</option><option value="top">အပေါ်</option></select></div></div>
<div class="option-grid"><div><label><input id="mirror" type="checkbox" style="width:auto;margin-right:5px"> Mirror လှန်</label></div><div><label for="logoText">Logo / Watermark</label><input id="logoText" maxlength="40" placeholder="My Recap"></div></div><label for="overlayText">Video ပေါ်တင်မည့်စာသား</label><input id="overlayText" maxlength="100" placeholder="ကိုယ်ထည့်ချင်တဲ့စာသား"></div><p class="muted small">အများဆုံး 80 MB · 10 မိနစ် · Direct video URL</p>
<button id="submit" type="submit">Recap စတင်မယ်</button>
</form></div>
<div id="result" class="panel hidden"><h2>အခြေအနေ</h2><div id="status" class="status">စောင့်ဆိုင်းနေသည်…</div><div id="links"></div></div>
<p class="muted small">Gemini 3.8 Flash · ကိုယ်အသုံးပြုခွင့်ရှိသော ဗီဒီယိုကိုသာ အသုံးပြုပါ။</p>
</main>
<script>
const form=document.getElementById('jobform'),btn=document.getElementById('submit'),result=document.getElementById('result'),statusBox=document.getElementById('status'),links=document.getElementById('links');
const geminiInput=document.getElementById('geminiKey'),groqInput=document.getElementById('groqKey'),keyStatus=document.getElementById('keyStatus');
const settingsWrap=document.getElementById('settingsWrap');const themeToggle=document.getElementById('themeToggle');function applyTheme(theme){document.body.classList.toggle('light',theme==='light');themeToggle.textContent=theme==='light'?'🌙 Dark':'☀️ Light';try{localStorage.setItem('oneclick_theme',theme);}catch(e){}}let savedTheme='dark';try{savedTheme=localStorage.getItem('oneclick_theme')||'dark';}catch(e){}applyTheme(savedTheme);themeToggle.addEventListener('click',()=>applyTheme(document.body.classList.contains('light')?'dark':'light'));document.getElementById('openSettings').addEventListener('click',()=>settingsWrap.classList.remove('hidden'));document.getElementById('closeSettings').addEventListener('click',()=>settingsWrap.classList.add('hidden'));settingsWrap.addEventListener('click',e=>{if(e.target===settingsWrap)settingsWrap.classList.add('hidden');});
const previewVideo=document.getElementById('previewVideo'),previewLogo=document.getElementById('previewLogo'),previewText=document.getElementById('previewText'),previewSubtitle=document.getElementById('previewSubtitle');const previewPlay=document.getElementById('previewPlay'),previewSeek=document.getElementById('previewSeek'),previewTime=document.getElementById('previewTime');function timeLabel(s){if(!Number.isFinite(s))s=0;return Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0')}function syncPlayback(){const d=previewVideo.duration||0;previewSeek.value=d?Math.round(previewVideo.currentTime/d*1000):0;previewTime.textContent=timeLabel(previewVideo.currentTime)+' / '+timeLabel(d);previewPlay.textContent=previewVideo.paused?'▶ Play':'❚❚ Pause'}previewPlay.addEventListener('click',()=>{if(previewVideo.paused)previewVideo.play().catch(()=>{});else previewVideo.pause();syncPlayback()});previewSeek.addEventListener('input',()=>{if(previewVideo.duration)previewVideo.currentTime=(+previewSeek.value/1000)*previewVideo.duration});['timeupdate','durationchange','play','pause','ended'].forEach(ev=>previewVideo.addEventListener(ev,syncPlayback));
function updatePreview(){const blur=+document.getElementById('blur').value,size=+document.getElementById('subSize').value,brightness=+document.getElementById('brightness').value,contrast=+document.getElementById('contrast').value,saturation=+document.getElementById('saturation').value;document.getElementById('blurValue').textContent=blur;document.getElementById('brightnessValue').textContent=brightness+'%';document.getElementById('contrastValue').textContent=contrast+'%';document.getElementById('saturationValue').textContent=saturation+'%';previewVideo.style.filter='brightness('+brightness+'%) contrast('+contrast+'%) saturate('+saturation+'%)'+(blur?' blur('+Math.min(blur,4)+'px)':'');previewVideo.style.transform=document.getElementById('mirror').checked?'scaleX(-1)':'';previewLogo.textContent=document.getElementById('logoText').value;previewLogo.style.display=previewLogo.textContent?'block':'none';previewText.textContent=document.getElementById('overlayText').value;previewSubtitle.style.fontSize=size+'px';previewSubtitle.style.color=({'&H00FFFFFF':'#fff','&H0000FFFF':'#ffff00','&H0000FF00':'#00ff00','&H00FF8080':'#8080ff'})[document.getElementById('subColor').value]||'#fff';const p=document.getElementById('subPosition').value;document.querySelector('.preview-overlay').style.alignItems=p==='top'?'flex-start':p==='middle'?'center':'flex-end';}
['brightness','contrast','saturation','blur','subSize','subColor','subPosition','mirror','logoText','overlayText'].forEach(id=>document.getElementById(id).addEventListener('input',updatePreview));
document.getElementById('videoFile').addEventListener('change',e=>{const file=e.target.files[0];if(previewVideo.src)URL.revokeObjectURL(previewVideo.src);if(file){previewVideo.src=URL.createObjectURL(file);previewVideo.classList.remove('hidden');previewPlay.classList.remove('hidden');previewSeek.classList.remove('hidden');previewVideo.load();}else{previewVideo.pause();previewVideo.removeAttribute('src');previewVideo.classList.add('hidden');previewPlay.classList.add('hidden');previewSeek.classList.add('hidden');}syncPlayback();updatePreview();});
document.getElementById('language').addEventListener('change',()=>{const v=document.getElementById('voice');if(document.getElementById('language').value==='my')v.value='my-MM-NilarNeural';else if(v.value==='my-MM-NilarNeural')v.value='en-US-AriaNeural';});updatePreview();
function loadSavedKeys(){try{geminiInput.value=localStorage.getItem('oneclick_gemini_key')||'';groqInput.value=localStorage.getItem('oneclick_groq_key')||'';keyStatus.textContent=(geminiInput.value&&groqInput.value)?'ဒီ Browser ထဲမှာ Key နှစ်ခုလုံး သိမ်းထားသည်။':'Key မသိမ်းရသေးပါ။';}catch(e){keyStatus.textContent='ဒီ Browser မှာ local storage အသုံးပြုမရပါ။';}}
document.getElementById('saveKeys').addEventListener('click',()=>{const g=geminiInput.value.trim(),q=groqInput.value.trim();if(!g||!q){keyStatus.textContent='Gemini နဲ့ Groq Key နှစ်ခုလုံး ထည့်ပါ။';return;}try{localStorage.setItem('oneclick_gemini_key',g);localStorage.setItem('oneclick_groq_key',q);keyStatus.textContent='Key နှစ်ခုလုံးကို ဒီ Browser ထဲမှာ Save လုပ်ပြီးပါပြီ။';}catch(e){keyStatus.textContent='Save မလုပ်နိုင်ပါ။ Browser storage setting ကို စစ်ပါ။';}});
document.getElementById('clearKeys').addEventListener('click',()=>{try{localStorage.removeItem('oneclick_gemini_key');localStorage.removeItem('oneclick_groq_key');}catch(e){}geminiInput.value='';groqInput.value='';keyStatus.textContent='Browser ထဲက Saved Keys ကို ဖျက်ပြီးပါပြီ။';});
loadSavedKeys();
form.addEventListener('submit',async(e)=>{e.preventDefault();const geminiKey=geminiInput.value.trim(),groqKey=groqInput.value.trim(),videoFile=document.getElementById('videoFile').files[0],videoUrl=document.getElementById('url').value.trim();if(!geminiKey||!groqKey){result.classList.remove('hidden');statusBox.textContent='အရင်ဆုံး API Key Settings မှာ Gemini နဲ့ Groq Key နှစ်ခုလုံးထည့်ပြီး Save Keys ကိုနှိပ်ပါ။';return;}if((!videoUrl&&!videoFile)||(videoUrl&&videoFile)){result.classList.remove('hidden');statusBox.textContent='Video URL သို့မဟုတ် Upload Video တစ်ခုကိုပဲ ရွေးပါ။';return;}btn.disabled=true;result.classList.remove('hidden');links.innerHTML='';statusBox.textContent='အလုပ်စတင်နေသည်…';
try{let body,headers;const options={voice:document.getElementById('voice').value,brightness:document.getElementById('brightness').value,contrast:document.getElementById('contrast').value,saturation:document.getElementById('saturation').value,blur:document.getElementById('blur').value,mirror:document.getElementById('mirror').checked,sub_size:document.getElementById('subSize').value,sub_color:document.getElementById('subColor').value,sub_position:document.getElementById('subPosition').value,logo_text:document.getElementById('logoText').value,overlay_text:document.getElementById('overlayText').value};if(videoFile){body=new FormData();body.append('url','');body.append('language',document.getElementById('language').value);body.append('gemini_api_key',geminiKey);body.append('groq_api_key',groqKey);Object.entries(options).forEach(([k,v])=>body.append(k,String(v)));body.append('video',videoFile);headers={};}else{body=JSON.stringify({url:videoUrl,language:document.getElementById('language').value,gemini_api_key:geminiKey,groq_api_key:groqKey,options});headers={'Content-Type':'application/json'};}const res=await fetch('/api/jobs',{method:'POST',headers,body});const data=await res.json();if(!res.ok)throw new Error(data.error||'စတင်၍မရပါ');poll(data.id);}catch(err){statusBox.textContent='အမှား: '+err.message;btn.disabled=false;}});
async function poll(id){try{const res=await fetch('/api/jobs/'+id);const data=await res.json();if(!res.ok)throw new Error(data.error||'အခြေအနေမရပါ');statusBox.textContent=data.message||data.status;
if(data.status==='done'){links.innerHTML='<a class="download" href="/api/jobs/'+id+'/download">MP4 ဒေါင်းလုဒ်လုပ်ရန်</a><p><a href="/api/jobs/'+id+'/script">Recap Script ကြည့်ရန်</a></p>';btn.disabled=false;return;}
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
    prompt = (
        "Create an original, concise movie recap narration based only on this transcript. "
        "Write in " + language_name + ". Use clear spoken language, no headings, no timestamps, "
        "no invented facts, no copied dialogue, and no claims that are unsupported by the transcript. "
        "Aim for 2 to 3 minutes of narration at most. Output only the narration text.\n\n"
        "TRANSCRIPT:\n" + transcript
    )
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_MODEL + ":generateContent"
    response = requests.post(endpoint, params={"key": api_key},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"maxOutputTokens": 3500, "thinkingConfig": {"thinkingLevel": "LOW"}}},
        timeout=120)
    if response.status_code >= 400:
        fail("Gemini 3.8 Flash API error (" + str(response.status_code) + "): " + response.text[:600])
    data = response.json()
    try:
        text = "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"]).strip()
    except (KeyError, IndexError, TypeError):
        fail("Gemini 3.8 Flash မှ recap စာသား မရပါ။ API quota/model access ကို စစ်ပါ။")
    if not text:
        fail("Gemini 3.8 Flash က စာသားအလွတ် ပြန်ပေးထားသည်။")
    return text

def synthesize_voice(script, language, audio_path, voice):
    # Voice is selected in the edit panel.
    try:
        communicate = edge_tts.Communicate(script, voice)
        import asyncio
        asyncio.run(communicate.save(str(audio_path)))
    except Exception as exc:
        fail("AI Voice ဖန်တီးမရပါ။ Network/voice availability ကို စစ်ပါ။ " + str(exc)[:300])
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        fail("AI Voice ဖိုင် မထွက်လာပါ။")

def split_sentences(text):
    pieces = re.split(r"(?<=[.!?။])\s+|\n+", text.strip())
    pieces = [p.strip() for p in pieces if p.strip()]
    # Avoid subtitles that are too long on a mobile screen.
    lines = []
    for piece in pieces:
        while len(piece) > 90:
            cut = piece.rfind(" ", 0, 85)
            if cut < 25:
                cut = 85
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

def make_subtitles(script, voice_path, srt_path):
    duration_text = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "default=noprint_wrappers=1:nokey=1", str(voice_path)], timeout=30)
    duration = float(duration_text)
    if duration <= 0:
        fail("Voice audio duration မမှန်ပါ။")
    chunks = split_sentences(script)
    weights = [max(1, len(chunk)) for chunk in chunks]
    total_weight = sum(weights)
    current = 0.0
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (chunk, weight) in enumerate(zip(chunks, weights), start=1):
            start = current
            current += duration * weight / total_weight
            end = min(duration, current)
            f.write(f"{i}\n{seconds_to_srt(start)} --> {seconds_to_srt(end)}\n{chunk}\n\n")

def render_video(video_path, voice_path, srt_path, output_path, options):
    # Apply the edit settings to the exported MP4.
    srt_name = str(srt_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    alignment = {"bottom": 2, "middle": 5, "top": 8}.get(options.get("sub_position"), 2)
    font_size = max(14, min(36, int(options.get("sub_size", 20))))
    primary = options.get("sub_color", "&H00FFFFFF")
    subtitle_filter = "subtitles=filename='" + srt_name + "':force_style='FontName=Noto Sans Myanmar,FontSize=" + str(font_size) + ",PrimaryColour=" + primary + ",Outline=2,Shadow=1,Alignment=" + str(alignment) + ",MarginV=28'"
    filters = []
    if options.get("mirror"): filters.append("hflip")
    blur = max(0, min(10, int(options.get("blur", 0))))
    if blur: filters.append("boxblur=" + str(blur) + ":1")
    brightness = max(50, min(150, int(options.get("brightness", 100)))) / 100
    contrast = max(50, min(150, int(options.get("contrast", 100)))) / 100
    saturation = max(0, min(200, int(options.get("saturation", 100)))) / 100
    if brightness != 1 or contrast != 1 or saturation != 1:
        filters.append("eq=brightness=" + str(brightness - 1) + ":contrast=" + str(contrast) + ":saturation=" + str(saturation))
    filters.append(subtitle_filter)
    def esc_text(value):
        return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%").replace(",", "\\,")
    logo = (options.get("logo_text") or "").strip()
    overlay = (options.get("overlay_text") or "").strip()
    if logo: filters.append("drawtext=text='" + esc_text(logo) + "':fontcolor=white:fontsize=20:box=1:boxcolor=black@0.45:boxborderw=5:x=w-tw-12:y=12")
    if overlay: filters.append("drawtext=text='" + esc_text(overlay) + "':fontcolor=white:fontsize=22:borderw=2:bordercolor=black:x=(w-text_w)/2:y=12")
    video_filter = ",".join(filters)
    run_command(["ffmpeg", "-y", "-i", str(video_path), "-i", str(voice_path),
                 "-vf", video_filter, "-map", "0:v:0", "-map", "1:a:0",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                 "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart",
                 str(output_path)], timeout=420)
    if not output_path.exists() or output_path.stat().st_size == 0:
        fail("MP4 ဖိုင် မထွက်လာပါ။")

def update_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)

def process_job(job_id, url, language, workdir, gemini_api_key, groq_api_key, options, uploaded=False):
    try:
        video = workdir / "source.mp4"
        original_audio = workdir / "source.mp3"
        voice = workdir / "recap_voice.mp3"
        subtitles = workdir / "recap.srt"
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
        (workdir / "recap.txt").write_text(script, encoding="utf-8")

        update_job(job_id, message="AI Voice ဖန်တီးနေသည်…")
        synthesize_voice(script, language, voice, options.get("voice", "my-MM-NilarNeural" if language == "my" else "en-US-AriaNeural"))

        update_job(job_id, message="Subtitle အချိန်ညှိနေသည်…")
        make_subtitles(script, voice, subtitles)

        update_job(job_id, message="နောက်ဆုံး MP4 ကို တည်ဆောက်နေသည်။ ခဏစောင့်ပါ…")
        render_video(video, voice, subtitles, output, options)
        update_job(job_id, status="done", message="ပြီးပါပြီ။ MP4 ကို ဒေါင်းလုဒ်လုပ်နိုင်ပါပြီ။",
                   output=str(output), script=str(workdir / "recap.txt"), finished=time.time())
    except Exception as exc:
        update_job(job_id, status="error", message=str(exc)[:1500], finished=time.time())
    finally:
        # Keep only completed outputs temporarily for download; remove source and intermediates.
        try:
            for name in ("source.mp4", "source.mp3", "recap_voice.mp3", "recap.srt"):
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
        options = {"voice": request.form.get("voice", ""), "brightness": request.form.get("brightness", "100"), "contrast": request.form.get("contrast", "100"), "saturation": request.form.get("saturation", "100"), "blur": request.form.get("blur", "0"), "mirror": request.form.get("mirror") == "true", "sub_size": request.form.get("sub_size", "20"), "sub_color": request.form.get("sub_color", "&H00FFFFFF"), "sub_position": request.form.get("sub_position", "bottom"), "logo_text": request.form.get("logo_text", "")[:40], "overlay_text": request.form.get("overlay_text", "")[:100]}
        uploaded_file = request.files.get("video")
        if uploaded_file and not uploaded_file.filename:
            uploaded_file = None
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
    allowed_voices = ("my-MM-NilarNeural", "en-US-AriaNeural", "en-US-GuyNeural", "en-GB-SoniaNeural")
    if options.get("voice") not in allowed_voices: options["voice"] = "my-MM-NilarNeural" if language == "my" else "en-US-AriaNeural"
    try:
        options["blur"] = max(0, min(10, int(options.get("blur", 0))))
        options["sub_size"] = max(14, min(36, int(options.get("sub_size", 20))))
    except (TypeError, ValueError):
        return jsonify({"error": "Video edit setting မမှန်ပါ။"}), 400
    if options.get("sub_color") not in ("&H00FFFFFF", "&H0000FFFF", "&H0000FF00", "&H00FF8080"): options["sub_color"] = "&H00FFFFFF"
    if options.get("sub_position") not in ("bottom", "middle", "top"): options["sub_position"] = "bottom"
    options["mirror"] = bool(options.get("mirror"))
    options["logo_text"] = str(options.get("logo_text", ""))[:40]
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
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "message": "အလုပ်တန်းစီနေသည်…", "created": time.time()}
    thread = threading.Thread(target=process_job, args=(job_id, url, language, workdir, gemini_api_key, groq_api_key, options, uploaded), daemon=True)
    thread.start()
    return jsonify({"id": job_id, "status": "queued"}), 202

@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job မတွေ့ပါ။ Render restart ဖြစ်ခဲ့ပါက ပြန်စတင်ပါ။"}), 404
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
