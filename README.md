# One Click Movie Recap

A small Flask web app for creating a recap video from a **direct video-file URL**.

## Features

- Movie/video URL input in a mobile-friendly web page
- Speech-to-text using Groq Whisper
- Recap script generation using **Gemini 3.8 Flash only** (`gemini-3.8-flash`)
- Burmese or English narration using Microsoft Edge TTS
- Approximate SRT subtitles burned into an MP4
- No app login/password and no Streamlit

## Important limits

- Use only videos you own or are authorized to process.
- Accepts direct public HTTP(S) video-file URLs, not YouTube/Netflix pages or login-protected streaming links.
- Test with a short clip under 10 minutes and under 80 MB.
- The final video uses the source video's picture with recap narration replacing the original audio; it is a first working version, not an automatic scene-selection editor.
- Subtitles are timed approximately across the generated narration, not word-level captions.
- Render Free storage is temporary and long processing jobs can be interrupted by service restarts or resource limits.
- Groq/Gemini quotas and charges depend on your API accounts. Render Free does not make AI APIs free.
- The app fixes the Gemini model to `gemini-3.8-flash`; it does not switch to another Gemini model automatically.

## Deploy to Render

1. Create a Render Web Service from this GitHub repository.
2. Choose the Docker runtime and Free plan if available for your account.
3. Add environment variables:
   - `GEMINI_API_KEY`: your Google AI Studio API key
   - `GROQ_API_KEY`: your Groq API key
   - `GEMINI_MODEL` is already set to `gemini-3.8-flash` in `render.yaml`; the app intentionally uses this exact model.
4. Deploy and open the generated Render URL.
5. Start by testing a short, authorized direct MP4 URL.

Do not commit API keys to GitHub. Keep them in Render Environment settings.
