# One Click Movie Recap

A small Flask web app for creating a recap video from a **direct video-file URL**.

## Main workflow

**MP4 Upload → Original Transcript → Burmese Recap Script → Burmese AI Voice → Subtitle Timing → Final MP4**

- Three-step mobile UI: MP4 Upload → Burmese AI Voice settings → Final MP4
- Direct video URL input or upload from a phone
- Speech-to-text using Groq Whisper
- Burmese recap script generation using Gemini
- Burmese narration using Microsoft Edge TTS; default voice is Thiha (male), with Nilar (female) available
- Subtitle SRT is generated and timed against the generated voice; it is provided separately and is not burned into the MP4 by default
- The source video picture is retained, and the generated Burmese narration replaces the original audio
- No app login/password and no Streamlit
- Website API Key Settings with Save/Clear buttons
- API keys are saved in the current browser's localStorage and sent over HTTPS only when a recap job starts; the server does not write them to repository files or persistent storage

## Important limits

- Use only videos you own or are authorized to process.
- Accepts direct public HTTP(S) video-file URLs, not YouTube/Netflix pages or login-protected streaming links.
- Test with a short clip under 10 minutes and under 80 MB. Upload accepts MP4, MOV, WEBM, and M4V.
- The final video uses the source video's picture with recap narration replacing the original audio; it is a first working version, not an automatic scene-selection editor.
- SRT subtitle timing is estimated across the generated narration, not word-level captions. The SRT is separate from the MP4.
- Render Free storage is temporary and long processing jobs can be interrupted by service restarts or resource limits.
- Groq/Gemini quotas and charges depend on your API accounts. Render Free does not make AI APIs free.
- The app fixes the Gemini model to `gemini-3.8-flash`; it does not switch to another Gemini model automatically.

## Deploy to Render

1. Create a Render Web Service from this GitHub repository.
2. Choose the Docker runtime and Free plan if available for your account.
3. Deploy and open the generated Render URL.
4. In the website's **API Key Settings**, enter both the Google AI Studio Gemini API key and Groq API key, then tap **Save Keys**.
5. Start by testing a short, authorized direct MP4 URL or upload a video file from your phone.

Keys are stored in the current browser on that device, not in Render Environment or GitHub. When a job starts, the browser sends them over HTTPS and the running job uses them in memory. Clearing browser site data or tapping **Saved Keys ဖျက်မယ်** removes the saved browser copies. Only save keys on a private device; browser storage is not a password vault. Never share keys or commit them to GitHub.
