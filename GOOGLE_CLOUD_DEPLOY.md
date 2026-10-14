# Deploy One Click Movie Recap to Google Cloud

This keeps the existing GitHub project and uses **Cloud Run** for the Flask/FFmpeg app plus a **private Cloud Storage bucket** for every completed final MP4. The bucket is the durable video library; Cloud Run's local filesystem is temporary.

## 1. Create a Google Cloud project

1. Open https://console.cloud.google.com/
2. Create/select a project and enable billing.
3. Choose one region and use it for both Cloud Run and the bucket (for example `asia-southeast1`).
4. Enable **Cloud Run**, **Cloud Build**, **Artifact Registry**, and **Cloud Storage** APIs.

Google Cloud may require a billing account even when eligible free-tier allowances are available. Set a budget alert before processing large videos.

## 2. Create the private video bucket

In **Cloud Storage → Buckets → Create**:
- Name: globally unique, e.g. `YOUR_PROJECT_ID-lynn-recap-videos`
- Location type: Region; choose the same region as Cloud Run.
- Access control: **Uniform**.
- Keep **Public access prevention** enabled.
- Do not add an automatic deletion lifecycle rule if you want to retain every final video.

Copy the bucket name exactly. Do not make the bucket public; the app streams videos through its API.

## 3. Deploy from the existing GitHub repository

1. Open **Cloud Run → Services → Deploy container → Continuously deploy from a repository** (the wording may vary).
2. Connect GitHub and select `Lynn2005/one-click-movie-recap`, branch `main`.
3. Choose the Dockerfile build method; the repository already contains a Dockerfile with FFmpeg and Myanmar fonts.
4. Service name: `one-click-movie-recap`; region: the same region chosen above.
5. Configure:
   - CPU: 2 vCPU to start
   - Memory: 4 GiB to start
   - Request timeout: 3600 seconds
   - Maximum instances: 1 initially
   - Concurrency: 1 (the app processes long video jobs in a background thread)
   - Minimum instances: 1 and CPU always allocated / no CPU throttling for the current background-thread design
   - Ingress: as needed; public access is convenient for a personal app, but add authentication before sharing widely.
6. Create the service. Cloud Run supplies the `PORT` environment variable; the existing Dockerfile binds Gunicorn to it.

**Important:** this app currently starts video processing in a background thread after returning HTTP 202. Always-allocated CPU and a minimum instance reduce the chance of the work being paused, but a restart can still interrupt an in-progress job. Completed MP4s are durable after the upload to Cloud Storage. For stronger production reliability, the next step is moving processing to Cloud Tasks or Cloud Run Jobs.

## 4. Configure the service account permissions

In Cloud Run service details, open **Security** and note the service account used by the service. In **IAM**, grant that service account **Storage Object Admin** on only the recap bucket (bucket-level permission is preferred).

Do not upload a service-account JSON key to GitHub or put it in the app's API Key Settings. Cloud Run's attached service account uses Application Default Credentials automatically.

## 5. Add the environment variable

Cloud Run → service → **Edit and deploy new revision** → Variables & Secrets:

| Name | Value |
|---|---|
| `GCS_BUCKET` | Exact private bucket name, e.g. `YOUR_PROJECT_ID-lynn-recap-videos` |

Do not set `MOVIE_RECAP_DATA_DIR` to a normal Cloud Run path for long-term storage; local disk is temporary. The app uploads each completed MP4 to `gs://BUCKET/final-videos/JOB_ID.mp4`, and the Saved Video Library reads the bucket after restarts.

Deploy the new revision after adding the variable. The latest GitHub commit also adds `google-cloud-storage` to `requirements.txt`.

## 6. Verify

1. Open the Cloud Run service URL.
2. Enter your Gemini and Groq API keys in the app's API Key Settings. They are stored in that browser's local storage, not in the Cloud Run bucket.
3. Generate a short video first.
4. Confirm it appears in **Saved Video Library**, preview it, and download it.
5. Restart/redeploy the Cloud Run service, reopen the app, and confirm the same video is still listed and playable.
6. In Cloud Storage, verify an object exists under `final-videos/`.

If the library is empty, check the `GCS_BUCKET` spelling, Cloud Run service account permissions, and Cloud Run logs. A bucket upload failure should cause the job to report an error rather than falsely claiming the final video was retained.

## Retention and cost

- Final MP4 files stay in the private bucket until you delete them or configure a deletion lifecycle rule.
- Cloud Storage charges for stored GB, operations, and network egress; Cloud Run/Cloud Build can also incur charges.
- Set a billing budget alert, monitor bucket growth, and keep a separate backup for irreplaceable videos.
- Existing videos that were only stored on Render's ephemeral filesystem cannot be migrated automatically by this code. Download any old videos while the old service still has them, then upload them to the new bucket if needed.
