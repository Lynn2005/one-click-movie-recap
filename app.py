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
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1020;color:#f3f4f6;font-family:Arial,"Noto Sans Myanmar",sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 48px}.panel{background:#151d32;border:1px solid #293652;border-radius:18px;padding:22px;margin:16px 0}
h1{font-size:28px;margin:0 0 8px}p{line-height:1.65;color:#c7d0e0}.muted{font-size:14px;color:#9caac3}
label{display:block;margin:16px 0 8px;font-weight:700}input,select{width:100%;padding:13px;border-radius:10px;border:1px solid #455574;background:#0c1428;color:white;font-size:16px}
button,.download{display:inline-block;border:0;border-radius:10px;background:#6d8cff;color:#071024;font-weight:800;padding:14px 18px;font-size:16px;text-decoration:none;cursor:pointer;width:100%;margin-top:18px}
button:disabled{opacity:.55;cursor:wait}.status{padding:12px;border-radius:10px;background:#0c1428;white-space:pre-wrap;overflow-wrap:anywhere}
.note{border-left:3px solid #e8b85b;padding-left:12px}.small{font-size:13px}.hidden{display:none}a{color:#9fb4ff}
</style>
</head>
<body><main>
<h1>🎬 One Click Movie Recap</h1>
<p>Movie video URL ထည့်ပြီး Recap Script၊ AI Voice၊ Subtitle နဲ့ MP4 ထုတ်ပါ။</p>
<div class="panel">
<h2>⚙️ API Key Settings</h2>
<p class="muted">API Key များကို ဒီ Browser ထဲမှာ သိမ်းထားမည်။ Recap စတင်ချိန်တွင် HTTPS ဖြင့် Server သို့ ပို့ပြီး အလုပ်လုပ်ရန်သာ အသုံးပြုမည်။ GitHub သို့မဟုတ် Server ဖိုင်ထဲတွင် မသိမ်းပါ။ ကိုယ်ပိုင်ဖုန်း/Browser တွင်သာ Save လုပ်ပါ။</p>
<label for="geminiKey">Gemini API Key</label>
<input id="geminiKey" type="password" autocomplete="off" placeholder="Gemini API Key ထည့်ပါ">
<label for="groqKey">Groq API Key</label>
<input id="groqKey" type="password" autocomplete="off" placeholder="Groq API Key ထည့်ပါ">
<button id="saveKeys" type="button">🔐 Save Keys</button>
<button id="clearKeys" type="button" style="background:#35415a;color:#fff;margin-top:8px">Saved Keys ဖျက်မယ်</button>
<p id="keyStatus" class="muted small" role="status">Key မသိမ်းရသေးပါ။</p>
</div>
<div class="panel">
<form id="jobform">
<label for="url">Option 1 — Direct video URL</label>
<input id="url" name="url" type="url" placeholder="https://example.com/video.mp4">
<p class="muted small">သို့မဟုတ် ဖုန်းထဲက ဗီဒီယိုဖိုင်ကို တင်နိုင်ပါတယ်။ URL နဲ့ Upload နှစ်ခုလုံး မလိုပါ—တစ်ခုကိုပဲ ရွေးပါ။</p>
<label for="videoFile">Option 2 — ဖုန်းထဲက Video Upload (MP4 / MOV / WEBM / M4V)</label>
<input id="videoFile" name="video" type="file" accept=".mp4,.mov,.webm,.m4v,video/mp4,video/quicktime,video/webm">
<label for="language">Recap ဘာသာစကား</label>
<select id="language" name="language"><option value="my">မြန်မာ</option><option value="en">English</option></select>
<p class="muted">ဗီဒီယို 80 MB အောက်၊ ၁၀ မိနစ်အောက် ဖြစ်ရပါမယ်။ URL နည်းမှာ direct video URL ကိုသာ လက်ခံပါတယ်။</p>
<button id="submit" type="submit">Recap စတင်မယ်</button>
</form></div>
<div id="result" class="panel hidden"><h2>အခြေအနေ</h2><div id="status" class="status">စောင့်ဆိုင်းနေသည်…</div><div id="links"></div></div>
<div class="panel">
<p class="note small">မှတ်ချက် — သင်အသုံးပြုခွင့်ရှိသော ဗီဒီယိုကိုသာ ထည့်ပါ။ Render Free တွင် storage ယာယီဖြစ်ပြီး service အိပ်ပျော်ခြင်း သို့မဟုတ် restart ဖြစ်ခြင်းကြောင့် အလုပ်ရပ်သွားနိုင်သည်။ AI API များတွင် quota/ကုန်ကျစရိတ် ရှိနိုင်သည်။</p>
<p class="muted small">Gemini model: gemini-3.8-flash</p>
</div>
</main>
<script>
const form=document.getElementById('jobform'),btn=document.getElementById('submit'),result=document.getElementById('result'),statusBox=document.getElementById('status'),links=document.getElementById('links');
const geminiInput=document.getElementById('geminiKey'),groqInput=document.getElementById('groqKey'),keyStatus=document.getElementById('keyStatus');
function loadSavedKeys(){try{geminiInput.value=localStorage.getItem('oneclick_gemini_key')||'';groqInput.value=localStorage.getItem('oneclick_groq_key')||'';keyStatus.textContent=(geminiInput.value&&groqInput.value)?'ဒီ Browser ထဲမှာ Key နှစ်ခုလုံး သိမ်းထားသည်။':'Key မသိမ်းရသေးပါ။';}catch(e){keyStatus.textContent='ဒီ Browser မှာ local storage အသုံးပြုမရပါ။';}}
document.getElementById('saveKeys').addEventListener('click',()=>{const g=geminiInput.value.trim(),q=groqInput.value.trim();if(!g||!q){keyStatus.textContent='Gemini နဲ့ Groq Key နှစ်ခုလုံး ထည့်ပါ။';return;}try{localStorage.setItem('oneclick_gemini_key',g);localStorage.setItem('oneclick_groq_key',q);keyStatus.textContent='Key နှစ်ခုလုံးကို ဒီ Browser ထဲမှာ Save လုပ်ပြီးပါပြီ။';}catch(e){keyStatus.textContent='Save မလုပ်နိုင်ပါ။ Browser storage setting ကို စစ်ပါ။';}});
document.getElementById('clearKeys').addEventListener('click',()=>{try{localStorage.removeItem('oneclick_gemini_key');localStorage.removeItem('oneclick_groq_key');}catch(e){}geminiInput.value='';groqInput.value='';keyStatus.textContent='Browser ထဲက Saved Keys ကို ဖျက်ပြီးပါပြီ။';});
loadSavedKeys();
form.addEventListener('submit',async(e)=>{e.preventDefault();const geminiKey=geminiInput.value.trim(),groqKey=groqInput.value.trim(),videoFile=document.getElementById('videoFile').files[0],videoUrl=document.getElementById('url').value.trim();if(!geminiKey||!groqKey){result.classList.remove('hidden');statusBox.textContent='အရင်ဆုံး API Key Settings မှာ Gemini နဲ့ Groq Key နှစ်ခုလုံးထည့်ပြီး Save Keys ကိုနှိပ်ပါ။';return;}if((!videoUrl&&!videoFile)||(videoUrl&&videoFile)){result.classList.remove('hidden');statusBox.textContent='Video URL သို့မဟုတ် Upload Video တစ်ခုကိုပဲ ရွေးပါ။';return;}btn.disabled=true;result.classList.remove('hidden');links.innerHTML='';statusBox.textContent='အလုပ်စတင်နေသည်…';
try{let body,headers;if(videoFile){body=new FormData();body.append('url','');body.append('language',document.getElementById('language').value);body.append('gemini_api_key',geminiKey);body.append('groq_api_key',groqKey);body.append('video',videoFile);headers={};}else{body=JSON.stringify({url:videoUrl,language:document.getElementById('language').value,gemini_api_key:geminiKey,groq_api_key:groqKey});headers={'Content-Type':'application/json'};}const res=await fetch('/api/jobs',{method:'POST',headers,body});const data=await res.json();if(!res.ok)throw new Error(data.error||'စတင်၍မရပါ');poll(data.id);}catch(err){statusBox.textContent='အမှား: '+err.message;btn.disabled=false;}});
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

def synthesize_voice(script, language, audio_path):
    # Microsoft Edge neural voices. Burmese voice is selected for Burmese narration.
    voice = "my-MM-NilarNeural" if language == "my" else "en-US-AriaNeural"
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

def render_video(video_path, voice_path, srt_path, output_path):
    # Burn subtitles, replace source audio with recap narration, and stop at the narration end.
    subtitle_filter = "subtitles=filename='" + str(srt_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'") + "':force_style='FontName=Noto Sans Myanmar,FontSize=20,Outline=2,Shadow=1,Alignment=2,MarginV=32'"
    run_command(["ffmpeg", "-y", "-i", str(video_path), "-i", str(voice_path),
                 "-vf", subtitle_filter, "-map", "0:v:0", "-map", "1:a:0",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                 "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart",
                 str(output_path)], timeout=420)
    if not output_path.exists() or output_path.stat().st_size == 0:
        fail("MP4 ဖိုင် မထွက်လာပါ။")

def update_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)

def process_job(job_id, url, language, workdir, gemini_api_key, groq_api_key, uploaded=False):
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
        synthesize_voice(script, language, voice)

        update_job(job_id, message="Subtitle အချိန်ညှိနေသည်…")
        make_subtitles(script, voice, subtitles)

        update_job(job_id, message="နောက်ဆုံး MP4 ကို တည်ဆောက်နေသည်။ ခဏစောင့်ပါ…")
        render_video(video, voice, subtitles, output)
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
    else:
        url = (request.form.get("url") or "").strip()
        language = request.form.get("language", "my")
        gemini_api_key = (request.form.get("gemini_api_key") or "").strip()
        groq_api_key = (request.form.get("groq_api_key") or "").strip()
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
    thread = threading.Thread(target=process_job, args=(job_id, url, language, workdir, gemini_api_key, groq_api_key, uploaded), daemon=True)
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
